"""
The agent loop: connects an LLM (or the random baseline) to a seat's MCP server.

For every "episode" (one of your team turns, a setup, a coin toss...) the driver starts a fresh
conversation containing the full situation, then lets the model call MCP tools until the
episode is over. Budgets (tool calls, wall time, illegal moves, money) are enforced here; when one
is exceeded the seat's fallback policy finishes the episode (usually by ending the turn).
"""
import copy
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
from bench.tools import INTERNAL_ERROR, RULES_PRIMER, SeatTools, build_mcp_server, is_trivial


INFO_TOOLS = ("get_state", "get_legal_actions", "get_player")


@dataclass
class DriverLimits:
    max_tool_calls_per_turn: int = 40
    turn_time_limit: float = 300.0
    max_illegal_streak: int = 3
    max_truncation_streak: int = 3  # finish_reason=length with no tools; separate from illegal
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
{limits.max_illegal_streak} illegal calls or {limits.max_truncation_streak} truncated replies in total since your \
last game action, or running out of budget, ends your turn automatically.
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
        self.usage = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
                      "reasoning_tokens": 0, "cost": 0.0, "latency": 0.0, "tool_calls": 0, "llm_errors": 0,
                      "no_tool_replies": 0, "output_truncations": 0, "http_timeouts": 0,
                      "uncertain_spend_calls": 0, "deadline_cutoffs": 0, "budget_exhausted": 0, "episodes": 0,
                      "served": {}}
        self.unavailable = False
        self._oa_tools = []
        self.consecutive_llm_errors = 0
        self.thread: Optional[threading.Thread] = None
        self.crashed: Optional[str] = None

    # ---- checkpoints -------------------------------------------------------------------------------
    def state_dict(self) -> dict:
        return {"usage": copy.deepcopy(self.usage), "unavailable": self.unavailable, "crashed": self.crashed,
                "consecutive_llm_errors": self.consecutive_llm_errors,
                "consecutive_illegal": self.tools.consecutive_illegal}

    def restore(self, state: dict):
        self.usage.update(copy.deepcopy(state["usage"]))
        self.unavailable = state["unavailable"]
        self.crashed = state["crashed"]
        self.consecutive_llm_errors = state["consecutive_llm_errors"]
        self.tools.consecutive_illegal = state["consecutive_illegal"]

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
                self._oa_tools = oa_tools
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

    def _record_usage(self, resp):
        u = self.usage
        u["llm_calls"] += 1
        u["prompt_tokens"] += resp.prompt_tokens
        u["completion_tokens"] += resp.completion_tokens
        u["cached_tokens"] += resp.cached_tokens
        u["reasoning_tokens"] += resp.reasoning_tokens
        u["cost"] += resp.cost
        u["latency"] += resp.latency
        if resp.served_model or resp.provider:
            k = f"{resp.served_model or '?'}@{resp.provider or '?'}"
            u["served"][k] = u["served"].get(k, 0) + 1

    async def _call_llm(self, messages, tools, key, sent, deadline=None):
        """One model call, fully recorded in the transcript. ``sent`` = messages already logged."""
        try:
            resp = await self.llm.chat(messages, tools, deadline=deadline)
        except LLMError as e:
            if e.timeout:
                self.usage["http_timeouts"] += 1
            if e.uncertain_spend:
                # no usage came back, so this call's cost is missing from usage["cost"] and the dollar cap
                self.usage["uncertain_spend_calls"] += 1
            raise
        self._record_usage(resp)
        self.session.transcript.write(
            "llm_call", self.seat.side, episode=key, new_messages=messages[sent:],
            response={"content": resp.content, "reasoning": resp.reasoning, "message": resp.raw_message,
                      "finish_reason": resp.finish_reason},
            usage={"prompt_tokens": resp.prompt_tokens, "completion_tokens": resp.completion_tokens,
                   "cached_tokens": resp.cached_tokens, "reasoning_tokens": resp.reasoning_tokens,
                   "cost": resp.cost},
            latency=round(resp.latency, 3), served_model=resp.served_model, provider=resp.provider,
            generation_id=resp.generation_id)
        return resp

    async def play_episode(self, client, oa_tools, d):
        key = d.key
        t0 = time.time()
        game = self.session.game
        turn_id = (game.state.half, self.seat.team.state.turn)
        self.usage["episodes"] += 1
        self.session.log_event(self.seat.side, "episode", {"key": key, "our_turn": d.our_turn, "proc": d.proc})
        messages = [{"role": "system", "content": system_prompt(self.display_name, self.limits, self.seat.side,
                                                                self.opponent_name)},
                    {"role": "user", "content": self._intro(d)}]
        self.session.transcript.write("episode", self.seat.side, episode=key, our_turn=d.our_turn, proc=d.proc,
                                      half=turn_id[0], turn=turn_id[1], messages=messages)
        sent = len(messages)
        calls = 0
        illegal_streak = 0
        truncation_streak = 0
        while not self._episode_over(key):
            reason = None
            if calls >= self.limits.max_tool_calls_per_turn:
                reason = f"tool-call budget ({self.limits.max_tool_calls_per_turn}) used up"
            elif time.time() - t0 > self.limits.turn_time_limit:
                reason = f"time limit ({int(self.limits.turn_time_limit)}s) exceeded"
            elif illegal_streak >= self.limits.max_illegal_streak:
                reason = f"{illegal_streak} illegal/invalid calls since last game action"
            elif truncation_streak >= self.limits.max_truncation_streak:
                reason = (f"{truncation_streak} output truncations since last game action "
                          f"(output length limit reached before a tool call)")
            elif self._over_budget():
                reason = f"cost budget ${self.limits.budget_usd} for this game exhausted"
            if reason:
                self.seat.counters["budget_exhausted"] += 1
                self.usage["budget_exhausted"] += 1
                self.session.log_event(self.seat.side, "system", {"text": f"Auto-finishing: {reason}."})
                self.session.transcript.write("system", self.seat.side, episode=key, text=f"auto-finish: {reason}")
                if self._over_budget():
                    self._autopilot()
                else:
                    self.seat.force_episode(key)
                return
            try:
                resp = await self._call_llm(messages, oa_tools, key, sent, deadline=t0 + self.limits.turn_time_limit)
                self.consecutive_llm_errors = 0
            except LLMError as e:
                if e.deadline:
                    # The turn's time ran out while the model was still replying. That is the time limit doing its
                    # job, not a provider fault: report it like the time-limit check above, not as an error, and
                    # don't count it towards marking the model unavailable.
                    self.usage["deadline_cutoffs"] += 1
                    self.seat.counters["budget_exhausted"] += 1
                    self.usage["budget_exhausted"] += 1
                    reason = (f"time limit ({int(self.limits.turn_time_limit)}s) reached while waiting for the "
                              f"model's reply")
                    self.session.log_event(self.seat.side, "system", {"text": f"Auto-finishing: {reason}."})
                    self.session.transcript.write("llm_cutoff", self.seat.side, episode=key, error=str(e)[:2000],
                                                  waited=round(time.time() - t0, 1))
                    self.session.transcript.write("system", self.seat.side, episode=key,
                                                  text=f"auto-finish: {reason}")
                    self.seat.force_episode(key)
                    return
                self.usage["llm_errors"] += 1
                self.consecutive_llm_errors += 1
                self.session.log_event(self.seat.side, "error", {"text": str(e)[:500]})
                self.session.transcript.write("llm_error", self.seat.side, episode=key, error=str(e)[:2000],
                                              infra=e.infra, fatal=e.fatal, timeout=e.timeout,
                                              uncertain_spend=e.uncertain_spend)
                if e.infra:
                    self.session.infra_error = str(e)[:300]
                    self.session.abort()
                    return
                if e.fatal or self.consecutive_llm_errors >= self.limits.max_llm_errors:
                    self.session.log_event(self.seat.side, "system",
                                           {"text": "Model unavailable - default actions for the rest of the game."})
                    self.unavailable = True
                    self._autopilot()
                else:
                    self.seat.force_episode(key)
                return
            thought = (resp.content or "").strip() or (resp.reasoning or "").strip()
            if thought:
                self.session.log_event(self.seat.side, "thought", {"text": thought[:1500]})
            messages.append(resp.raw_message)
            sent = len(messages)  # the assistant message is logged as the call's response; tool results go
            #                       out with the next call's new_messages
            if not resp.tool_calls:
                if (resp.finish_reason or "").lower() == "length":
                    # Reasoning / completion budget exhausted before a tool call — not a refusal.
                    self.usage["output_truncations"] += 1
                    truncation_streak += 1
                    messages.append({"role": "user", "content":
                                     "Your previous reply was truncated (finish_reason=length) before any tool call. "
                                     "Call a tool now. Prefer a short action (get_legal_actions, take_action, or "
                                     "end_turn) over long reasoning."})
                else:
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
                t_tool = time.time()
                if tc.parse_error:
                    text, bad = f"Error: {tc.parse_error}", True
                else:
                    try:
                        r = await client.call_tool(tc.name, tc.arguments)
                        text = "\n".join(getattr(c, "text", "") for c in (r.content or []))
                        bad = (bool(r.is_error) or text.startswith("ILLEGAL")) \
                            and not text.startswith(INTERNAL_ERROR)
                    except Exception as e:
                        text, bad = f"Error calling {tc.name}: {e}", True
                if bad:
                    illegal_streak += 1
                    self.seat.counters["invalid_tool_calls"] += 1
                elif tc.name not in INFO_TOOLS and tc.name not in ("send_message", "reflect"):
                    # A successful game action clears both counters (they are independent running totals since the last
                    # game action, not strictly consecutive).
                    # Info / chat / reflect alone must not reset them (avoids infinite length retries).
                    illegal_streak = 0
                    truncation_streak = 0
                self.seat.counters["tool:" + tc.name] += 1
                self.session.log_event(self.seat.side, "tool", {
                    "name": tc.name, "args": tc.arguments, "ok": not bad, "result": text[:800]})
                self.session.transcript.write("tool", self.seat.side, episode=key, call_id=tc.id, name=tc.name,
                                              args=tc.arguments, raw_args=tc.raw_arguments, ok=not bad, result=text,
                                              duration=round(time.time() - t_tool, 3))
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": text})
        # The turn ended without end_turn (turnover / touchdown): ask once for the reflection we missed.
        if d.our_turn and not self.session.finished and not self.seat.autopilot \
                and self.seat.autopilot_key != key \
                and not self.session.has_reflection(self.seat, *turn_id):
            await self._missed_reflection(messages, client, key, sent)

    async def _missed_reflection(self, messages, client, key, sent):
        reflect_tool = [t for t in self._oa_tools if t["function"]["name"] == "reflect"]
        if not reflect_tool:
            return
        messages.append({"role": "user", "content":
                         "Your turn is over (a turnover or touchdown ended it before you called end_turn). Call "
                         "reflect(plan, prediction) now: your plan for your next turn and what you expect the "
                         "opponent to do."})
        try:
            resp = await self._call_llm(messages, reflect_tool, key + "#reflect", sent)
        except LLMError as e:
            self.session.transcript.write("llm_error", self.seat.side, episode=key + "#reflect", error=str(e)[:500])
            return
        if not resp.tool_calls:
            if (resp.finish_reason or "").lower() == "length":
                self.usage["output_truncations"] += 1
                self.session.log_event(self.seat.side, "system",
                                       {"text": "Reflection reply truncated (finish_reason=length); skipping."})
                self.session.transcript.write("system", self.seat.side, episode=key + "#reflect",
                                              text="reflect truncated: finish_reason=length")
            else:
                self.usage["no_tool_replies"] += 1
            return
        for tc in resp.tool_calls[:1]:
            if tc.name == "reflect" and not tc.parse_error:
                r = await client.call_tool("reflect", tc.arguments)
                text = "\n".join(getattr(c, "text", "") for c in (r.content or []))
                self.session.transcript.write("tool", self.seat.side, episode=key + "#reflect", call_id=tc.id,
                                              name="reflect", args=tc.arguments, ok=not r.is_error, result=text)
                if not text.startswith("ILLEGAL"):
                    self.session.seat(self.seat.side).counters["reflections_after_turnover"] += 1
