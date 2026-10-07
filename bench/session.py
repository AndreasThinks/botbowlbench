"""
Runs a single botbowl game between two "seats" and bridges each seat to an asynchronous agent.

The botbowl engine is synchronous: it calls ``agent.act(game)`` whenever it needs a decision. A
:class:`Seat` is a botbowl Agent whose ``act`` blocks until the seat's driver (an LLM talking to the
MCP tools) submits an action. Tool calls from the driver therefore only ever touch the game while
the game thread is parked inside ``act`` for that seat, so no locking of the game itself is needed.
"""
import json
import random
import threading
import time
import traceback
import uuid
import zlib
from collections import Counter
from dataclasses import dataclass
from typing import Callable, List, Optional

from botbowl.core.game import Game
from botbowl.core.load import load_config, load_rule_set, load_team_by_filename
from botbowl.core.model import Action, Agent
from botbowl.core.procedure import Setup
from botbowl.core.table import ActionType


@dataclass
class Decision:
    seq: int
    key: str          # "episode" key: a new key means the driver starts a fresh conversation
    proc: str
    our_turn: bool


def episode_key(game, team) -> (str, bool):
    proc = game.state.stack.peek()
    turn = game.current_turn()
    if turn is not None and turn.team == team and not isinstance(proc, Setup):
        tag = "b" if turn.blitz else ("q" if turn.quick_snap else "")
        return f"turn-{game.state.half}-{team.state.turn}{tag}", True
    return f"{type(proc).__name__}-{game.state.half}-{game.state.round}", False


def fallback_action(game, team) -> Action:
    """A safe, legal default used on timeouts, budget exhaustion and repeated illegal moves."""
    available = game.state.available_actions
    types = {a.action_type: a for a in available}
    if ActionType.END_SETUP in types and game.is_setup_legal(team):
        return Action(ActionType.END_SETUP)
    for a in available:
        if a.action_type.name.startswith("SETUP_FORMATION_"):
            return Action(a.action_type)
    for t in [ActionType.END_TURN, ActionType.END_PLAYER_TURN, ActionType.SELECT_NONE, ActionType.HEADS,
              ActionType.RECEIVE, ActionType.SELECT_DEFENDER_DOWN, ActionType.SELECT_DEFENDER_STUMBLES,
              ActionType.SELECT_PUSH, ActionType.SELECT_BOTH_DOWN, ActionType.SELECT_ATTACKER_DOWN,
              ActionType.DONT_USE_REROLL, ActionType.DONT_USE_APOTHECARY, ActionType.DONT_USE_SKILL,
              ActionType.DONT_USE_BRIBE]:
        if t in types:
            return Action(t)
    choices = [a for a in available if a.action_type != ActionType.PLACE_PLAYER] or available
    choice = random.choice(choices)
    position = random.choice(choice.positions) if choice.positions else None
    player = random.choice(choice.players) if choice.players else None
    return Action(choice.action_type, position=position, player=player)


def timeline_point(data: dict, ts: Optional[float]) -> dict:
    """Compact per-frame index entry, computed from botbowl's game JSON (so it also works for old frames)."""
    st = data["state"]
    home, away = st["home_team"], st["away_team"]
    side = None
    if st.get("current_team_id") == home["team_id"]:
        side = "home"
    elif st.get("current_team_id") == away["team_id"]:
        side = "away"
    return {"ts": round(ts, 3) if ts else None, "half": st.get("half"), "ht": home["state"]["turn"],
            "at": away["state"]["turn"], "hs": home["state"]["score"], "as": away["state"]["score"], "side": side,
            "over": bool(st.get("game_over"))}


class Seat(Agent):
    """A botbowl Agent whose decisions are supplied by an external driver thread."""

    def __init__(self, session: "GameSession", side: str, name: str):
        super().__init__(name, human=False)
        self.session = session
        self.side = side
        self.team = None
        self.cond = threading.Condition()
        self.decision: Optional[Decision] = None   # set while a decision is pending
        self._seq = 0
        self._action: Optional[Action] = None
        self.autopilot = False                     # every decision uses the fallback
        self.autopilot_key: Optional[str] = None   # decisions in this episode use the fallback
        self.counters = Counter()
        self.report_cursor = 0                     # reports already shown to this seat
        self.msg_cursor = 0                        # opponent messages already shown to this seat

    # ---- game-thread side ------------------------------------------------------------------
    def new_game(self, game, team):
        self.team = team

    def end_game(self, game):
        with self.cond:
            self.cond.notify_all()

    def act(self, game):
        key, our_turn = episode_key(game, self.team)
        self.session.on_decision(self)
        if self.autopilot or self.autopilot_key == key or self.session.aborted:
            self.counters["forced_actions"] += 1
            return fallback_action(game, self.team)
        with self.cond:
            self._seq += 1
            self.decision = Decision(self._seq, key, type(game.state.stack.peek()).__name__, our_turn)
            self._action = None
            self.cond.notify_all()
            deadline = time.time() + self.session.decision_timeout
            while self._action is None and not self.autopilot and self.autopilot_key != key \
                    and not self.session.aborted and time.time() < deadline:
                self.cond.wait(min(1.0, max(0.01, deadline - time.time())))
            action = self._action
            self._action = None
            self.decision = None
            self.cond.notify_all()
        if action is None:
            self.counters["forced_actions"] += 1
            if time.time() >= deadline:
                self.counters["timeouts"] += 1
                self.session.log_event(self.side, "system", {"text": "Decision timed out - default action taken."})
            return fallback_action(game, self.team)
        self.counters["actions"] += 1
        self.counters["action:" + action.action_type.name] += 1
        return action

    # ---- driver-thread side ----------------------------------------------------------------
    def wait_for_decision(self, timeout: Optional[float] = None) -> Optional[Decision]:
        """Block until a decision is pending for this seat (or the game is over -> None)."""
        end = None if timeout is None else time.time() + timeout
        with self.cond:
            while self.decision is None and not self.session.finished:
                if end is not None and time.time() >= end:
                    return None
                self.cond.wait(1.0)
            return self.decision

    def submit(self, action: Action) -> Optional[Decision]:
        """Hand an action to the game and wait until this seat must decide again (None = game over)."""
        with self.cond:
            if self.decision is None:
                raise RuntimeError("No decision is pending for this seat.")
            seq = self.decision.seq
            self._action = action
            self.cond.notify_all()
            while not self.session.finished and (self.decision is None or self.decision.seq == seq):
                self.cond.wait(1.0)
            return self.decision

    def force_episode(self, key: str):
        """Let the fallback policy finish the current episode (e.g. end the turn)."""
        with self.cond:
            self.autopilot_key = key
            self.cond.notify_all()

    def is_pending(self) -> bool:
        return self.decision is not None


class GameSession:
    """One match: owns the botbowl Game, two seats, the live snapshot, chat and event feed."""

    def __init__(self, match_id: str, home_name: str, away_name: str, game_mode: str = "5",
                 team_file: str = "human", decision_timeout: float = 600.0,
                 event_sink: Optional[Callable] = None, record_frames: bool = True,
                 max_message_len: int = 280, max_messages_per_turn: int = 2, seed: Optional[int] = None):
        self.match_id = match_id
        self.decision_timeout = decision_timeout
        self.event_sink = event_sink
        self.record_frames = record_frames
        self.max_message_len = max_message_len
        self.max_messages_per_turn = max_messages_per_turn
        self.lock = threading.Lock()
        self.finished = False
        self.aborted = False
        self.error: Optional[str] = None
        self.infra_error: Optional[str] = None
        self.messages: List[dict] = []
        self.reflections: List[dict] = []
        self.action_log: dict = {"home": [], "away": []}
        self.time_capped = False
        self.harness_errors: List[str] = []
        self.seed = seed
        from bench.transcript import NullTranscript
        self.transcript = NullTranscript()
        self.frames: List[bytes] = []
        self.timeline: List[dict] = []   # one point per frame: time, half, turns, score, side to move
        self.latest_json: Optional[str] = None
        self.snapshot_seq = 0
        self.started_at = time.time()
        self.ended_at = None

        config = load_config(f"web-{game_mode}.json")
        config.competition_mode = False   # we enforce our own budgets instead of botbowl clocks
        config.fast_mode = True
        config.pathfinding_enabled = True
        config.pathfinding_directly_to_adjacent = True
        ruleset = load_rule_set(config.ruleset, all_rules=False)
        board_size = config.pitch_max
        home_team = load_team_by_filename(team_file, ruleset, board_size=board_size)
        away_team = load_team_by_filename(team_file, ruleset, board_size=board_size)
        home_team.name = home_name
        away_team.name = away_name
        home_team.bench_side = "home"
        away_team.bench_side = "away"
        self.home = Seat(self, "home", home_name)
        self.away = Seat(self, "away", away_name)
        self.game = Game(match_id, home_team, away_team, self.home, self.away, config, record=False)
        if seed is not None:
            self.game.set_seed(seed)
        self.thread: Optional[threading.Thread] = None

    def seat(self, side: str) -> Seat:
        return self.home if side == "home" else self.away

    def opponent(self, seat: Seat) -> Seat:
        return self.away if seat is self.home else self.home

    # ---- lifecycle ---------------------------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self._run, name=f"game-{self.match_id[:8]}", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            self.game.init()
            if not self.game.state.game_over:
                raise RuntimeError("Game loop exited before the game was over")
        except Exception as e:  # pragma: no cover - surfaced to the UI/DB
            self.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            self.ended_at = time.time()
            self._snapshot()
            self.finished = True
            for seat in (self.home, self.away):
                with seat.cond:
                    seat.cond.notify_all()

    def abort(self):
        self.aborted = True
        for seat in (self.home, self.away):
            with seat.cond:
                seat.cond.notify_all()

    # ---- snapshots / spectators --------------------------------------------------------------
    def on_decision(self, seat: Seat):
        self._snapshot()

    def _snapshot(self):
        try:
            data = self.game.to_json()
            reports = data["state"]["reports"]
            if len(reports) > 60:
                data["state"]["reports"] = reports[-60:]
            now = time.time()
            from bench.render import events_since
            reports = self.game.state.reports
            log = events_since(self.game, max(0, len(reports) - 40), limit=1000)[-6:]
            data["bench"] = {"match_id": self.match_id, "finished": self.finished, "ts": round(now, 3), "log": log}
            js = json.dumps(data)
            point = timeline_point(data, now)
        except Exception:
            traceback.print_exc()
            return
        with self.lock:
            self.latest_json = js
            self.snapshot_seq += 1
            if self.record_frames:
                self.frames.append(zlib.compress(js.encode("utf-8"), 6))
                self.timeline.append(point)

    # ---- chat ----------------------------------------------------------------------------------
    def post_message(self, seat: Seat, text: str) -> str:
        text = (text or "").strip()
        if not text:
            return "Message is empty."
        if len(text) > self.max_message_len:
            text = text[: self.max_message_len]
        key, _ = episode_key(self.game, seat.team) if seat.is_pending() else ("idle", False)
        sent = sum(1 for m in self.messages if m["side"] == seat.side and m["episode"] == key)
        if sent >= self.max_messages_per_turn:
            seat.counters["messages_rejected"] += 1
            return f"Message limit reached ({self.max_messages_per_turn} per turn). Not sent."
        msg = {"side": seat.side, "text": text, "ts": time.time(), "episode": key,
               "half": self.game.state.half, "turn": seat.team.state.turn if seat.team else 0}
        with self.lock:
            self.messages.append(msg)
        seat.counters["messages_sent"] += 1
        seat.counters["message_chars"] += len(text)
        self.log_event(seat.side, "message", {"text": text})
        self.transcript.write("message", seat.side, text=text, half=msg["half"], turn=msg["turn"])
        return "Message delivered to your opponent."

    # ---- research records ------------------------------------------------------------------------
    def current_turn_key(self, seat: Seat) -> str:
        return f"{self.game.state.half}-{seat.team.state.turn}" if seat.team else "-"

    def record_reflection(self, seat: Seat, plan: str, prediction: str, trigger: str):
        rec = {"side": seat.side, "half": self.game.state.half, "turn": seat.team.state.turn,
               "plan": plan.strip()[:1500], "prediction": prediction.strip()[:1500], "trigger": trigger,
               "ts": time.time()}
        with self.lock:
            self.reflections.append(rec)
        seat.counters["reflections"] += 1
        self.log_event(seat.side, "reflection", {"plan": rec["plan"], "prediction": rec["prediction"],
                                                 "turn": rec["turn"], "half": rec["half"], "trigger": trigger})
        self.transcript.write("reflection", seat.side, **{k: v for k, v in rec.items() if k not in ("side", "ts")})

    def has_reflection(self, seat: Seat, half: int, turn: int) -> bool:
        return any(r["side"] == seat.side and r["half"] == half and r["turn"] == turn for r in self.reflections)

    def record_action(self, seat: Seat, rec: dict):
        with self.lock:
            self.action_log[seat.side].append(rec)
        self.transcript.write("action", seat.side, **rec)

    def mark_action_outcome(self, seat: Seat, turn_id, turnover: bool, touchdown: bool):
        with self.lock:
            for rec in reversed(self.action_log[seat.side]):
                if (rec["half"], rec["turn"]) != tuple(turn_id):
                    break
                if "success_est" in rec:
                    rec["turnover"] = rec["turnover"] or turnover
                    rec["touchdown"] = rec["touchdown"] or touchdown
                    self.transcript.write("action_outcome", seat.side, half=rec["half"], turn=rec["turn"],
                                          activation=rec["activation"], turnover=turnover, touchdown=touchdown)
                    break

    def harness_error(self, text: str):
        """Bugs in the bench itself: logged, never blamed on the model, and they make the game inadmissible."""
        import traceback as tb
        self.harness_errors.append(text)
        self.log_event(None, "error", {"text": f"harness error: {text}"})
        self.transcript.write("harness_error", None, text=text, traceback=tb.format_exc()[-4000:])

    def unread_messages(self, seat: Seat) -> List[str]:
        opp_side = self.opponent(seat).side
        msgs = [m for m in self.messages[seat.msg_cursor:]]
        seat.msg_cursor = len(self.messages)
        return [m["text"] for m in msgs if m["side"] == opp_side]

    # ---- events ----------------------------------------------------------------------------------
    def log_event(self, side: Optional[str], kind: str, payload: dict):
        if self.event_sink is not None:
            try:
                self.event_sink(self.match_id, side, kind, payload)
            except Exception:
                traceback.print_exc()

    def new_id(self) -> str:
        return str(uuid.uuid4())
