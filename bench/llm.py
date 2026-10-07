"""
Chat-completion backends used by the driver.

* :class:`OpenRouterLLM` talks to https://openrouter.ai (OpenAI-compatible tool calling).
* :class:`RandomPolicy` is a free, offline baseline that "pretends" to be an LLM and picks random
  legal moves through the very same MCP tools. Useful as a benchmark floor and for testing.
"""
import asyncio
import json
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import httpx

OPENROUTER_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1") + "/chat/completions"


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]
    raw_arguments: str = ""
    parse_error: Optional[str] = None


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    latency: float = 0.0
    raw_message: Dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    def __init__(self, msg, fatal=False, infra=False):
        super().__init__(msg)
        self.fatal = fatal
        self.infra = infra  # our problem (bad key / no credits), not the model's


class OpenRouterLLM:
    def __init__(self, model: str, api_key: str, temperature: Optional[float] = None,
                 max_tokens: int = 2048, extra: Optional[dict] = None, timeout: float = 180.0,
                 max_retries: int = 4):
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra = extra or {}
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self):
        await self.client.aclose()

    async def chat(self, messages: List[dict], tools: List[dict]) -> LLMResponse:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": self.max_tokens,
            "usage": {"include": True},
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        body.update(self.extra)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": os.environ.get("PUBLIC_URL", "https://github.com/andreasthinks/botbowlbench"),
            "X-Title": "BotBowl Bench",
        }
        delay = 2.0
        last_err = None
        for attempt in range(self.max_retries + 1):
            t0 = time.time()
            try:
                r = await self.client.post(OPENROUTER_URL, json=body, headers=headers)
            except httpx.HTTPError as e:
                last_err = f"network error: {e}"
            else:
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except ValueError:
                        last_err = "invalid JSON from OpenRouter"
                    else:
                        if "error" in data and not data.get("choices"):
                            last_err = f"provider error: {data['error']}"
                        else:
                            return self._parse(data, time.time() - t0)
                elif r.status_code in (400, 401, 402, 403, 404):
                    raise LLMError(f"OpenRouter HTTP {r.status_code}: {r.text[:500]}",
                                   fatal=r.status_code in (401, 402, 403, 404),
                                   infra=r.status_code in (401, 402))
                else:
                    last_err = f"OpenRouter HTTP {r.status_code}: {r.text[:300]}"
            if attempt < self.max_retries:
                await asyncio.sleep(delay)
                delay *= 2
        raise LLMError(last_err or "unknown error")

    @staticmethod
    def _parse(data: dict, latency: float) -> LLMResponse:
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        resp = LLMResponse(content=msg.get("content") or "", latency=latency)
        resp.reasoning = msg.get("reasoning") or ""
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
                if not isinstance(args, dict):
                    raise ValueError("arguments must be a JSON object")
                err = None
            except ValueError as e:
                args, err = {}, f"could not parse arguments: {e}"
            resp.tool_calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""),
                                            arguments=args, raw_arguments=raw if isinstance(raw, str) else json.dumps(raw),
                                            parse_error=err))
        usage = data.get("usage") or {}
        resp.prompt_tokens = int(usage.get("prompt_tokens") or 0)
        resp.completion_tokens = int(usage.get("completion_tokens") or 0)
        resp.cost = float(usage.get("cost") or 0.0)
        raw_msg = {"role": "assistant", "content": resp.content or ""}
        if msg.get("tool_calls"):
            raw_msg["tool_calls"] = msg["tool_calls"]
        resp.raw_message = raw_msg
        return resp


TAUNTS = [
    "My dice are hot today.", "Is that your best formation?", "Nice try. Not nice enough.",
    "I calculated that. Probably.", "Gods of Nuffle, hear my plea!", "Good game so far!",
]


class RandomPolicy:
    """A pseudo-LLM that plays uniformly random legal moves via take_action / end_turn."""

    def __init__(self, seat, message_rate: float = 0.03, seed: Optional[int] = None, delay: float = 0.0):
        self.seat = seat
        self.delay = delay  # seconds per "call", so baseline games are watchable
        self.rng = random.Random(seed)
        self.message_rate = message_rate
        self._n = 0

    async def aclose(self):
        pass

    async def chat(self, messages: List[dict], tools: List[dict]) -> LLMResponse:
        from botbowl.core.table import ActionType
        from bench import render
        self._n += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        game = self.seat.session.game
        resp = LLMResponse(raw_message={"role": "assistant", "content": ""})
        if self.seat.session.finished or not self.seat.is_pending():
            resp.tool_calls.append(ToolCall(id=f"r{self._n}", name="get_legal_actions", arguments={}))
        elif self.rng.random() < self.message_rate:
            resp.tool_calls.append(ToolCall(id=f"r{self._n}", name="send_message",
                                            arguments={"text": self.rng.choice(TAUNTS)}))
        else:
            choices = [a for a in game.state.available_actions if a.action_type != ActionType.PLACE_PLAYER]
            # bias towards ending the turn sometimes so games don't drag
            a = self.rng.choice(choices)
            args = {"action_type": a.action_type.name}
            if a.action_type == ActionType.END_SETUP and not game.is_setup_legal(self.seat.team):
                for c in choices:
                    if c.action_type.name.startswith("SETUP_FORMATION_"):
                        args = {"action_type": c.action_type.name}
                        break
            elif a.players:
                args["player_id"] = render.pid(self.rng.choice(a.players))
            elif a.positions:
                pos = self.rng.choice(a.positions)
                args["x"], args["y"] = pos.x, pos.y
            resp.tool_calls.append(ToolCall(id=f"r{self._n}", name="take_action", arguments=args))
        tc = resp.tool_calls[0]
        resp.raw_message["tool_calls"] = [{"id": tc.id, "type": "function",
                                           "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}]
        return resp


class ScriptedPolicy(RandomPolicy):
    """A simple heuristic baseline that uses the high-level tools: safe blocks, advance the ball carrier,
    go for a loose ball, then end the turn. A useful "does the model beat a script?" reference point."""

    BLOCK_PREF = ["SELECT_DEFENDER_DOWN", "SELECT_DEFENDER_STUMBLES", "SELECT_PUSH", "SELECT_BOTH_DOWN",
                  "SELECT_ATTACKER_DOWN"]

    async def chat(self, messages: List[dict], tools: List[dict]) -> LLMResponse:
        from botbowl.core.table import ActionType
        from bench import render
        from bench.session import fallback_action
        self._n += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        game = self.seat.session.game
        team = self.seat.team
        call = ("get_legal_actions", {})
        if not self.seat.session.finished and self.seat.is_pending():
            types = {a.action_type: a for a in game.state.available_actions}
            if ActionType.START_MOVE in types or ActionType.START_BLOCK in types or ActionType.END_TURN in types \
                    and len(types) == 1:
                call = self._turn_call(game, team, types)
            else:
                for name in self.BLOCK_PREF:
                    if ActionType[name] in types:
                        call = ("take_action", {"action_type": name})
                        break
                else:
                    a = fallback_action(game, team)
                    args = {"action_type": a.action_type.name}
                    if a.player is not None:
                        args["player_id"] = render.pid(a.player)
                    if a.position is not None:
                        args["x"], args["y"] = a.position.x, a.position.y
                    call = ("take_action", args)
        name, args = call
        tc = ToolCall(id=f"s{self._n}", name=name, arguments=args)
        resp = LLMResponse(tool_calls=[tc], raw_message={"role": "assistant", "content": "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})
        return resp

    def _turn_call(self, game, team, types):
        from botbowl.core.table import ActionType
        from botbowl.core.pathfinding import get_all_paths
        from bench import render
        if self._n % 25 == 0:
            return ("send_message", {"text": self.rng.choice(TAUNTS)})
        # 1) blocks with the dice in our favour
        if ActionType.START_BLOCK in types:
            for p in types[ActionType.START_BLOCK].players:
                for opp in game.get_adjacent_players(p.position, team=game.get_opp_team(team), down=False):
                    dice = game.num_block_dice(p, opp)
                    if dice is not None and dice >= 2:
                        return ("block", {"player_id": render.pid(p), "target_id": render.pid(opp)})
        # 2) ball carrier heads for the endzone along the safest path
        carrier = game.get_ball_carrier()
        movers = types[ActionType.START_MOVE].players if ActionType.START_MOVE in types else []
        ex = render.target_endzone_x(game, team)
        if carrier is not None and carrier in movers:
            best = None
            for path in get_all_paths(game, carrier):
                end = path.steps[-1]
                score = -abs(end.x - ex) + path.prob * 3
                if path.prob >= 0.6 and (best is None or score > best[0]):
                    best = (score, end)
            if best is not None:
                return ("move", {"player_id": render.pid(carrier), "x": best[1].x, "y": best[1].y})
        # 3) someone goes for a loose ball
        ball = game.get_ball_position()
        if carrier is None and ball is not None and movers:
            p = min(movers, key=lambda m: m.position.distance(ball) if m.position else 99)
            return ("move", {"player_id": render.pid(p), "x": ball.x, "y": ball.y})
        # 4) a single step towards the endzone for one unused player
        if movers and self.rng.random() < 0.6:
            p = self.rng.choice(movers)
            if p.position is not None and p.state.up:
                step = 1 if ex > p.position.x else -1
                x = p.position.x + step
                if game.get_player_at(game.get_square(x, p.position.y)) is None and 1 <= x <= game.arena.width - 2:
                    return ("move", {"player_id": render.pid(p), "x": x, "y": p.position.y})
        return ("end_turn", {})
