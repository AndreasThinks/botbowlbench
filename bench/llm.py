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
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    served_model: str = ""        # the model OpenRouter actually routed to (may differ in version)
    provider: str = ""            # the upstream provider that served the call
    generation_id: str = ""
    finish_reason: str = ""
    raw_message: Dict[str, Any] = field(default_factory=dict)


class LLMError(Exception):
    def __init__(self, msg, fatal=False, infra=False, timeout=False, uncertain_spend=False, deadline=False):
        super().__init__(msg)
        self.fatal = fatal
        self.infra = infra  # our problem (bad key / no credits), not the model's
        self.timeout = timeout  # HTTP timeout: the request was sent and never answered
        self.uncertain_spend = uncertain_spend  # the provider may have generated (and billed) a reply we never saw
        self.deadline = deadline  # cut off at the turn deadline: the turn's time ran out, not a model fault


def with_cache_breakpoints(messages: List[dict]) -> List[dict]:
    """Mark the system prompt and the episode's opening situation as cacheable (Anthropic-style
    ``cache_control``). Both are re-sent on every call within a turn, so caching them cuts input cost."""
    out = list(messages)
    for i in (0, 1):
        if i < len(out) and isinstance(out[i].get("content"), str) and out[i]["role"] in ("system", "user"):
            m = dict(out[i])
            m["content"] = [{"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}]
            out[i] = m
    return out


class OpenRouterLLM:
    def __init__(self, model: str, api_key: str, temperature: Optional[float] = None,
                 max_tokens: Optional[int] = None, extra: Optional[dict] = None, timeout: float = 180.0,
                 max_retries: int = 4, prompt_cache: Optional[bool] = None):
        self.model = model
        # OpenAI, DeepSeek, Gemini... cache prompt prefixes automatically; Anthropic needs explicit breakpoints
        self.prompt_cache = model.startswith("anthropic/") if prompt_cache is None else prompt_cache
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.extra = extra or {}
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self):
        await self.client.aclose()

    async def chat(self, messages: List[dict], tools: List[dict], deadline: Optional[float] = None) -> LLMResponse:
        body = {
            "model": self.model,
            "messages": with_cache_breakpoints(messages) if self.prompt_cache else messages,
            "tools": tools,
            "tool_choice": "auto",
            "usage": {"include": True},
        }
        if self.max_tokens is not None:
            body["max_tokens"] = self.max_tokens
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
            # httpx's timeout is per socket read, and OpenRouter keeps slow requests alive with whitespace, so it
            # never bounds a long generation. The turn deadline (or self.timeout without one) caps the whole request.
            budget = self.timeout if deadline is None else deadline - t0
            if budget <= 0:
                raise LLMError(f"{last_err or 'turn deadline reached'} (no time left for a model call)",
                               deadline=True)
            try:
                r = await asyncio.wait_for(self.client.post(OPENROUTER_URL, json=body, headers=headers), budget)
            except asyncio.TimeoutError:
                # like a read timeout, the provider may still be generating (and billing) after we hang up
                if deadline is not None:
                    raise LLMError(f"turn time limit reached after {budget:.0f}s without a reply (request cancelled)",
                                   uncertain_spend=True, deadline=True)
                raise LLMError(f"no reply within {budget:.0f}s, request cancelled", timeout=True,
                               uncertain_spend=True)
            except httpx.ReadTimeout as e:
                # The request went out and the reply never came: the provider may still be generating (and billing)
                # it. Retrying would stack up unseen spend, so fail this call instead.
                raise LLMError(f"HTTP read timeout after {self.timeout:g}s: {type(e).__name__}",
                               timeout=True, uncertain_spend=True)
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
                if deadline is not None and time.time() + delay >= deadline:
                    raise LLMError(f"{last_err or 'unknown error'} (no time left to retry)")
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
        resp.cached_tokens = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        resp.reasoning_tokens = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        resp.served_model = data.get("model") or ""
        resp.provider = data.get("provider") or ""
        resp.generation_id = data.get("id") or ""
        resp.finish_reason = choice.get("finish_reason") or ""
        raw_msg = {"role": "assistant", "content": resp.content or ""}
        if msg.get("tool_calls"):
            raw_msg["tool_calls"] = msg["tool_calls"]
        # Thinking models (Gemini, Claude, Kimi...) need their reasoning passed back unmodified on the next
        # request of a tool-calling loop, otherwise providers may reject the conversation.
        if msg.get("reasoning_details"):
            raw_msg["reasoning_details"] = msg["reasoning_details"]
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

    PLAN = "Keep activating players and look for chances to score."
    PREDICTION = "The opponent will try to block my players and advance the ball."

    def _single(self, name: str, args: dict) -> LLMResponse:
        tc = ToolCall(id=f"r{self._n}", name=name, arguments=args)
        return LLMResponse(tool_calls=[tc], raw_message={"role": "assistant", "content": "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]})

    def _forced_reflection(self, tools) -> Optional[LLMResponse]:
        if len(tools) == 1 and tools[0]["function"]["name"] == "reflect":
            return self._single("reflect", {"plan": self.PLAN, "prediction": self.PREDICTION})
        return None

    async def chat(self, messages: List[dict], tools: List[dict], deadline: Optional[float] = None) -> LLMResponse:
        from botbowl.core.table import ActionType
        from bench import render
        self._n += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        forced = self._forced_reflection(tools)
        if forced is not None:
            return forced
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
            if a.action_type == ActionType.END_TURN:
                return self._single("end_turn", {"plan": self.PLAN, "prediction": self.PREDICTION})
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

    async def chat(self, messages: List[dict], tools: List[dict], deadline: Optional[float] = None) -> LLMResponse:
        from botbowl.core.table import ActionType
        from bench import render
        from bench.session import fallback_action
        self._n += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        forced = self._forced_reflection(tools)
        if forced is not None:
            return forced
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
                    if a.action_type == ActionType.END_TURN:
                        return self._single("end_turn", {"plan": self.PLAN, "prediction": self.PREDICTION})
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
        return ("end_turn", {"plan": "Advance the ball carrier safely and make favourable blocks.",
                             "prediction": "The opponent will blitz my ball carrier if it can reach it."})


class BotbowlAgentPolicy(RandomPolicy):
    """Plays a native botbowl bot (any ``botbowl.Agent`` in the bot registry) through the same MCP tools.

    Each time the seat is waiting for a decision, the bot's ``act(game)`` picks the action and it is submitted
    with ``take_action`` (or ``end_turn``), so these games are recorded and scored exactly like LLM games.
    ``module`` is imported first so the bot registers itself, e.g. ``examples.scripted_bot_example``.
    """

    PLAN = "Follow the scripted priorities: safe blocks, protect the ball carrier, then advance."
    PREDICTION = "The opponent will try to reach my ball carrier."

    def __init__(self, seat, bot: str, module: Optional[str] = None, seed: Optional[int] = None,
                 delay: float = 0.0):
        super().__init__(seat, message_rate=0.0, seed=seed, delay=delay)
        import importlib
        import botbowl
        if module:
            importlib.import_module(module)
        self.agent = botbowl.make_bot(bot)
        self._started = False

    async def chat(self, messages: List[dict], tools: List[dict], deadline: Optional[float] = None) -> LLMResponse:
        from botbowl.core.table import ActionType
        from bench import render
        from bench.session import fallback_action
        self._n += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        forced = self._forced_reflection(tools)
        if forced is not None:
            return forced
        if self.seat.session.finished or not self.seat.is_pending():
            return self._single("get_legal_actions", {})
        game = self.seat.session.game
        team = self.seat.team
        if not self._started:
            self.agent.new_game(game, team)
            self._started = True
        # botbowl bots are seeded through Python's global RNG; keep them reproducible from the match seed
        state = random.getstate()
        random.seed(self.rng.random())
        try:
            action = self.agent.act(game)
        except Exception:
            action = None
        finally:
            random.setstate(state)
        if action is None or not game._is_action_allowed(action):
            action = fallback_action(game, team)
        if action.action_type == ActionType.END_TURN:
            return self._single("end_turn", {"plan": self.PLAN, "prediction": self.PREDICTION})
        args = {"action_type": action.action_type.name}
        if action.player is not None:
            args["player_id"] = render.pid(action.player)
        if action.position is not None:
            args["x"], args["y"] = action.position.x, action.position.y
        return self._single("take_action", args)
