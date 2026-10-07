"""Runs one complete match between two configured models and returns the result + stats."""
import random
import threading
import time
from typing import Callable, Optional

from bench import version
from bench.transcript import Transcript
from bench.driver import DriverLimits, SeatDriver
from bench.llm import OpenRouterLLM, RandomPolicy, ScriptedPolicy
from bench.session import GameSession
from bench.stats import side_stats


def make_llm(model_cfg: dict, seat, api_key: Optional[str]):
    provider = model_cfg.get("provider", "openrouter")
    if provider == "random":
        return RandomPolicy(seat, seed=model_cfg.get("seed"), delay=float(model_cfg.get("delay", 0)))
    if provider == "scripted":
        return ScriptedPolicy(seat, seed=model_cfg.get("seed"), delay=float(model_cfg.get("delay", 0)))
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    return OpenRouterLLM(model_cfg["model"], api_key,
                         temperature=model_cfg.get("temperature"),
                         max_tokens=model_cfg.get("max_tokens", 2048),
                         extra=model_cfg.get("extra") or {},
                         prompt_cache=model_cfg.get("prompt_cache"))


CHECKPOINT_VERSION = 1


def describe_checkpoint(key: Optional[str], names: dict) -> str:
    """'away:turn-2-5' -> "from the start of <away name>'s turn 5 (half 2)"; None -> 'from kick-off'."""
    if not key:
        return "from kick-off (no checkpoint yet)"
    side, _, episode = key.partition(":")
    parts = episode.split("-")
    if len(parts) == 3 and parts[0] == "turn" and side in names:
        return f"from the start of {names[side]}'s turn {parts[2].rstrip('bq')} (half {parts[1]})"
    return f"from checkpoint {key}"


def limits_for(model_cfg: dict, settings: dict) -> DriverLimits:
    return DriverLimits(
        max_tool_calls_per_turn=int(model_cfg.get("max_tool_calls_per_turn", settings.get("max_tool_calls_per_turn", 40))),
        turn_time_limit=float(model_cfg.get("turn_time_limit", settings.get("turn_time_limit", 300))),
        max_illegal_streak=int(settings.get("max_illegal_streak", 3)),
        budget_usd=model_cfg.get("budget_usd_per_game", settings.get("budget_usd_per_game")),
    )


def _public_cfg(cfg: dict) -> dict:
    return {k: v for k, v in cfg.items() if k not in ("api_key",)}


class MatchRunner:
    def __init__(self, match_id: str, home_cfg: dict, away_cfg: dict, settings: dict,
                 api_key: Optional[str] = None, event_sink: Optional[Callable] = None,
                 seed: Optional[int] = None, transcript: bool = False,
                 resume: Optional[dict] = None, restarted: bool = False,
                 checkpoint_sink: Optional[Callable[[dict], None]] = None):
        """``resume``: a checkpoint (see :meth:`_checkpoint`) to continue from. ``restarted``: an earlier attempt
        at this match was interrupted (a fresh start without a checkpoint still keeps the seed and records the
        restart). ``checkpoint_sink`` receives a checkpoint at the start of every team turn."""
        self.match_id = match_id
        self.home_cfg = home_cfg
        self.away_cfg = away_cfg
        self.settings = settings
        if resume is not None:
            seed = resume["seed"]
        self.seed = seed if seed is not None else random.randint(0, 2 ** 31 - 1)
        self.session = GameSession(match_id, home_cfg["name"], away_cfg["name"],
                                   game_mode=str(settings.get("game_mode", "5")),
                                   team_file=settings.get("team", "human"),
                                   decision_timeout=float(settings.get("decision_timeout", 900)),
                                   event_sink=event_sink,
                                   max_messages_per_turn=int(settings.get("max_messages_per_turn", 2)),
                                   max_message_len=int(settings.get("max_message_len", 280)),
                                   seed=self.seed)
        if resume is not None:
            self.session.restore(resume["session"])   # before the drivers: their tools hold the game
        self.drivers = {}
        for i, (side, cfg) in enumerate((("home", home_cfg), ("away", away_cfg))):
            seat = self.session.seat(side)
            cfg = dict(cfg)
            cfg.setdefault("seed", self.seed + i)   # baselines are reproducible from the match seed
            llm = make_llm(cfg, seat, api_key)
            opp = away_cfg if side == "home" else home_cfg
            self.drivers[side] = SeatDriver(self.session, seat, llm, cfg["name"], limits_for(cfg, settings),
                                            opponent_name=opp["name"])
        self.max_game_seconds = float(settings.get("max_game_minutes", 120)) * 60
        self.elapsed_before = 0.0      # game time played before a restart
        self.resumes = []
        self._resume_note = None
        self._t0 = None
        if resume is not None:
            for side, st in resume["drivers"].items():
                self.drivers[side].restore(st)
            self.elapsed_before = resume["elapsed"]
            self.resumes = list(resume["resumes"])
        dropped, kept = [], 0
        if transcript:   # only once everything else is restored: reopening rewrites the partial transcript
            self.session.transcript, dropped, kept = Transcript.reopen(
                match_id, keep=resume["transcript_records"] if resume is not None else 0)
        if resume is not None or restarted or dropped:
            self._note_restart(resume, dropped, kept, transcript)
        if checkpoint_sink is not None:
            self._checkpoint_sink = checkpoint_sink
            self.session.checkpoint_hook = self._checkpoint

    # ---- restarts --------------------------------------------------------------------------------
    def _note_restart(self, resume, dropped, kept, transcript):
        # what the interrupted attempt spent after the checkpoint (its records are dropped, the money is gone);
        # dropped ``resume`` records carry the spend of earlier restarts that weren't checkpointed either
        lost = {"home": 0.0, "away": 0.0}
        for rec in dropped:
            if rec.get("type") == "llm_call" and rec.get("side") in lost:
                lost[rec["side"]] += float((rec.get("usage") or {}).get("cost") or 0.0)
            elif rec.get("type") == "resume":
                for side, c in (rec.get("restart_cost") or {}).items():
                    lost[side] = lost.get(side, 0.0) + float(c or 0.0)
        for side, d in self.drivers.items():
            d.usage["restart_cost"] = round(d.usage.get("restart_cost", 0.0) + lost[side], 6)
        expected = resume["transcript_records"] if resume is not None else 0
        info = {"ts": round(time.time(), 3), "checkpoint": resume["key"] if resume is not None else None,
                "git_sha": version.git_sha(), "dropped_records": len(dropped),
                "restart_cost": {k: round(v, 6) for k, v in lost.items()},
                "transcript_gap": bool(transcript) and kept < expected}
        self.resumes.append(info)
        self._resume_note = info

    def _checkpoint(self, key: str):
        elapsed = self.elapsed_before + (time.time() - self._t0 if self._t0 else 0.0)
        self._checkpoint_sink({
            "version": CHECKPOINT_VERSION, "match_id": self.match_id, "seed": self.seed, "key": key,
            "ts": time.time(), "elapsed": elapsed,
            "protocol_version": version.PROTOCOL_VERSION, "prompt_fingerprint": version.prompt_fingerprint(),
            "git_sha": version.git_sha(),
            "transcript_records": self.session.transcript.flush(),
            "session": self.session.state_dict(),
            "drivers": {side: d.state_dict() for side, d in self.drivers.items()},
            "resumes": self.resumes,
        })

    def meta(self) -> dict:
        return {
            "match_id": self.match_id,
            "seed": self.seed,
            "harness": {"git_sha": version.git_sha(), "protocol_version": version.PROTOCOL_VERSION,
                        "prompt_fingerprint": version.prompt_fingerprint()},
            "settings": self.settings,
            "models": {"home": _public_cfg(self.home_cfg), "away": _public_cfg(self.away_cfg)},
        }

    def _watchdog(self):
        """Caps total game time: past the cap both seats switch to the default policy, which ends turns
        immediately, so the game finishes at the current score (and is flagged as not admissible)."""
        if self.session.time_capped:   # already capped before a restart
            return
        while not self.session.finished:
            if time.time() - self._t0 + self.elapsed_before > self.max_game_seconds:
                self.session.time_capped = True
                self.session.log_event(None, "system", {"text": "Game time limit reached - remaining turns are "
                                                                "played by the default policy."})
                for seat in (self.session.home, self.session.away):
                    with seat.cond:
                        seat.autopilot = True
                        seat.cond.notify_all()
                return
            time.sleep(2)

    def run(self) -> dict:
        t0 = self._t0 = time.time()
        if not self.session.resumed:
            self.session.transcript.write("meta", None, **self.meta())
        if self._resume_note is not None:
            self.session.transcript.write("resume", None, **self._resume_note)
            where = describe_checkpoint(self._resume_note["checkpoint"],
                                        {"home": self.home_cfg["name"], "away": self.away_cfg["name"]})
            self.session.log_event(None, "system", {"text": f"The server restarted. Resuming {where}."})
        if not self.session.resumed:
            self.session.log_event(None, "system", {"text": f"Kick-off: {self.home_cfg['name']} (home) vs "
                                                            f"{self.away_cfg['name']} (away)"})
        self.session.start()
        for d in self.drivers.values():
            d.start()
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()
        self.session.thread.join()
        for d in self.drivers.values():
            d.thread.join(timeout=30)
        game = self.session.game
        home_score = game.state.home_team.state.score
        away_score = game.state.away_team.state.score
        result = {
            "home_score": home_score,
            "away_score": away_score,
            "error": self.session.error,
            "infra_error": self.session.infra_error,
            "duration": self.elapsed_before + time.time() - t0,
            "home_stats": side_stats(self.session, "home", self.drivers["home"]),
            "away_stats": side_stats(self.session, "away", self.drivers["away"]),
            "messages": list(self.session.messages),
            "reflections": list(self.session.reflections),
        }
        winner = "draw"
        if home_score > away_score:
            winner = "home"
        elif away_score > home_score:
            winner = "away"
        result["winner"] = winner
        # admissibility (cf. CivBench): natural conclusion under the pinned protocol with complete logs
        reasons = []
        if self.session.error:
            reasons.append("engine_error")
        if self.session.infra_error:
            reasons.append("infra_error")
        if self.session.time_capped:
            reasons.append("time_capped")
        if self.session.harness_errors:
            reasons.append("harness_error")
        if any(r.get("transcript_gap") for r in self.resumes):
            reasons.append("transcript_incomplete")
        for side, d in self.drivers.items():
            if d.crashed:
                reasons.append(f"{side}_agent_crashed")
            if d.unavailable:
                reasons.append(f"{side}_model_unavailable")
        meta = self.meta()
        meta.update({"time_capped": self.session.time_capped, "admissible": not reasons,
                     "harness_errors": self.session.harness_errors[:20],
                     "inadmissible_reasons": reasons,
                     "resumes": self.resumes,
                     "served": {side: d.usage.get("served", {}) for side, d in self.drivers.items()},
                     "transcript_records": self.session.transcript.records})
        result["meta"] = meta
        self.session.transcript.write("result", None, **{k: v for k, v in result.items()
                                                          if k not in ("messages", "reflections")})
        self.session.transcript.close(complete=not self.session.infra_error)
        self.session.log_event(None, "system", {"text": f"Final whistle: {home_score} - {away_score}"})
        return result
