"""
The agent loop: connects an LLM (or the random baseline) to a seat's MCP server.

For every "episode" (one of your team turns, a setup, a coin toss...) the driver starts a fresh
conversation containing the full situation, then lets the model call MCP tools until the
episode is over. Budgets (tool calls, wall time, illegal moves, money) are enforced here; when one
is exceeded the seat's fallback policy finishes the episode (usually by ending the turn).
"""
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Optional

import anyio
from mcp import Client

from bench import render
from bench.llm import LLMError
from bench.session import GameSession, Seat
from bench.tools import RULES_PRIMER, SeatTools, build_mcp_server, is_trivial


@dataclass
class DriverLimits:
    max_tool_calls_per_turn: int = 40
    turn_time_limit: float = 300.0
    max_illegal_streak: int = 3
    max_llm_errors: int = 4
    budget_usd: Optional[float] = None


def system_prompt(display_name: str, limits: DriverLimits, side: str, opponent_name: str = "another AI model") -> str:
    you = "H (home)" if side == "home" else "A (away)"
    return f"""You are {display_name}, the coach of a Blood Bowl team in an AI benchmark tournament. Your opponent is \
{opponent_name}, another AI model. You control the {you} players. You act ONLY by calling the provided tools (an MCP server).

{RULES_PRIMER}
HOW THIS WORKS
- Whenever you are needed (your team turn, setup, coin toss, kick-off, block dice, re-rolls, ...) you receive the full situation.
- Call action tools (move, block, blitz, pass_ball, handoff, foul, take_action, end_turn). Each returns what happened
  and your next pending decision. Resolve interrupting decisions (block dice, push squares, re-rolls) with take_action.
- Call end_turn when you are done with your turn. Unused players simply stay where they are.
- Budget: at most {limits.max_tool_calls_per_turn} tool calls and {int(limits.turn_time_limit)} seconds per turn. \
{limits.max_illegal_streak} illegal calls in a row, or running out of budget, ends your turn automatically.
- You may talk to your opponent with send_message (max 2 per turn). Spectators can read the chat.
- Always answer with a tool call. Keep any text you write very short."""


class SeatDriver:
    def __init__(self, session: GameSession, seat: Seat, llm, display_name: str, limits: DriverLimits,
                 opponent_name: str = "another AI model"):
        self.session = session
        self.opponent_name = opponent_name
        self.seat = seat
        self.llm = llm
        self.display_name = display_name
        self.limits = limits
        self.tools = SeatTools(session, seat, max_illegal=limits.max_illegal_streak)
        self.usage = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "latency": 0.0,
                      "tool_calls": 0, "llm_errors": 0, "no_tool_replies": 0, "budget_exhausted": 0,
                      "episodes": 0}
        self.consecutive_llm_errors = 0
        self.thread: Optional[threading.Thread] = None
        self.crashed: Optional[str] = None

    # ---- threading -----------------------------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self._thread_main, name=f"driver-{self.seat.side}", daemon=True)
        self.thread.start()

    def _thread_main(self):
        try:
            anyio.run(self.run)
        except BaseException as e:  # never let a driver stall the game
            self.crashed = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            self.session.log_event(self.seat.side, "error", {"text": f"Agent crashed: {self.crashed}. "
                                                                     "Default actions will be used."})
        finally:
            self._autopilot()

    def _autopilot(self):
        with self.seat.cond:
            self.seat.autopilot = True
            self.seat.cond.notify_all()

    # ---- main loop -------------------------------------------------------------------------------
    async def run(self):
        server = build_mcp_server(self.tools)
        try:
            async with Client(server) as client:
                listed = await client.list_tools()
                oa_tools = [{"type": "function", "function": {
                    "name": t.name, "description": t.description or "",
                    "parameters": t.input_schema}} for t in listed.tools]
                while True:
                    d = await anyio.to_thread.run_sync(self.seat.wait_for_decision)
                    if d is None or self.session.finished:
                        break
                    if self.seat.autopilot:
                        break
                    trivial = is_trivial(self.session.game)
                    if trivial is not None:
                        self.seat.counters["auto_resolved"] += 1
                        await anyio.to_thread.run_sync(lambda: self.seat.submit(trivial))
                        continue
                    await self.play_episode(client, oa_tools, d)
        finally:
            await self.llm.aclose()

    def _intro(self, d) -> str:
        game = self.session.game
        parts = []
        events = render.events_since(game, self.seat.report_cursor, limit=40)
        self.seat.report_cursor = len(game.state.reports)
        if events:
            parts.append("EVENTS SINCE YOUR LAST DECISION:\n  " + "\n  ".join(events))
        msgs = self.session.unread_messages(self.seat)
        if msgs:
            parts.append("MESSAGES FROM YOUR OPPONENT:\n  " + "\n  ".join(f'"{m}"' for m in msgs))
        parts.append("CURRENT SITUATION:\n" + render.full_state_text(game, self.seat.team))
        if d.our_turn:
            parts.append("It is YOUR TURN. Plan briefly, act with the tools, then call end_turn.")
        else:
            parts.append("A decision is required from you now (see the legal options above).")
        return "\n\n".join(parts)

    def _episode_over(self, key) -> bool:
        cur = self.seat.decision
        return self.session.finished or cur is None or cur.key != key

    def _over_budget(self) -> bool:
        return self.limits.budget_usd is not None and self.usage["cost"] >= self.limits.budget_usd

    async def play_episode(self, client, oa_tools, d):
        key = d.key
        t0 = time.time()
        self.usage["episodes"] += 1
        self.session.log_event(self.seat.side, "episode", {"key": key, "our_turn": d.our_turn, "proc": d.proc})
        messages = [{"role": "system", "content": system_prompt(self.display_name, self.limits, self.seat.side, self.opponent_name)},
                    {"role": "user", "content": self._intro(d)}]
        calls = 0
        illegal_streak = 0
        while not self._episode_over(key):
            reason = None
            if calls >= self.limits.max_tool_calls_per_turn:
                reason = f"tool-call budget ({self.limits.max_tool_calls_per_turn}) used up"
            elif time.time() - t0 > self.limits.turn_time_limit:
                reason = f"time limit ({int(self.limits.turn_time_limit)}s) exceeded"
            elif illegal_streak >= self.limits.max_illegal_streak:
                reason = f"{illegal_streak} illegal/invalid calls in a row"
            elif self._over_budget():
                reason = f"cost budget ${self.limits.budget_usd} for this game exhausted"
            if reason:
                self.seat.counters["budget_exhausted"] += 1
                self.usage["budget_exhausted"] += 1
                self.session.log_event(self.seat.side, "system", {"text": f"Auto-finishing: {reason}."})
                if self._over_budget():
                    self._autopilot()
                else:
                    self.seat.force_episode(key)
                return
            try:
                resp = await self.llm.chat(messages, oa_tools)
                self.consecutive_llm_errors = 0
            except LLMError as e:
                self.usage["llm_errors"] += 1
                self.consecutive_llm_errors += 1
                self.session.log_event(self.seat.side, "error", {"text": str(e)[:500]})
                if e.infra:
                    self.session.infra_error = str(e)[:300]
                    self.session.abort()
                    return
                if e.fatal or self.consecutive_llm_errors >= self.limits.max_llm_errors:
                    self.session.log_event(self.seat.side, "system",
                                           {"text": "Model unavailable - default actions for the rest of the game."})
                    self._autopilot()
                else:
                    self.seat.force_episode(key)
                return
            self.usage["llm_calls"] += 1
            self.usage["prompt_tokens"] += resp.prompt_tokens
            self.usage["completion_tokens"] += resp.completion_tokens
            self.usage["cost"] += resp.cost
            self.usage["latency"] += resp.latency
            thought = (resp.content or "").strip() or (resp.reasoning or "").strip()
            if thought:
                self.session.log_event(self.seat.side, "thought", {"text": thought[:1500]})
            messages.append(resp.raw_message)
            if not resp.tool_calls:
                self.usage["no_tool_replies"] += 1
                illegal_streak += 1
                messages.append({"role": "user", "content": "You must respond with a tool call. "
                                 "Use get_legal_actions if unsure, or end_turn to finish your turn."})
                continue
            for tc in resp.tool_calls:
                if self._episode_over(key):
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": "Skipped: phase is over."})
                    continue
                calls += 1
                self.usage["tool_calls"] += 1
                if tc.parse_error:
                    text, bad = f"Error: {tc.parse_error}", True
                else:
                    try:
                        r = await client.call_tool(tc.name, tc.arguments)
                        text = "\n".join(getattr(c, "text", "") for c in (r.content or []))
                        bad = bool(r.is_error) or text.startswith("ILLEGAL")
                    except Exception as e:
                        text, bad = f"Error calling {tc.name}: {e}", True
                if bad:
                    illegal_streak += 1
                    self.seat.counters["invalid_tool_calls"] += 1
                elif tc.name not in ("get_state", "get_legal_actions", "get_player", "send_message"):
                    illegal_streak = 0
                self.seat.counters["tool:" + tc.name] += 1
                self.session.log_event(self.seat.side, "tool", {
                    "name": tc.name, "args": tc.arguments, "ok": not bad, "result": text[:800]})
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
