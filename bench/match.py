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
                 seed: Optional[int] = None, transcript: bool = False):
        self.match_id = match_id
        self.home_cfg = home_cfg
        self.away_cfg = away_cfg
        self.settings = settings
        self.seed = seed if seed is not None else random.randint(0, 2 ** 31 - 1)
        self.session = GameSession(match_id, home_cfg["name"], away_cfg["name"],
                                   game_mode=str(settings.get("game_mode", "5")),
                                   team_file=settings.get("team", "human"),
                                   decision_timeout=float(settings.get("decision_timeout", 900)),
                                   event_sink=event_sink,
                                   max_messages_per_turn=int(settings.get("max_messages_per_turn", 2)),
                                   max_message_len=int(settings.get("max_message_len", 280)),
                                   seed=self.seed)
        if transcript:
            self.session.transcript = Transcript(match_id)
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
        t0 = time.time()
        while not self.session.finished:
            if time.time() - t0 > self.max_game_seconds:
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
        t0 = time.time()
        self.session.transcript.write("meta", None, **self.meta())
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
            "duration": time.time() - t0,
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
        for side, d in self.drivers.items():
            if d.crashed:
                reasons.append(f"{side}_agent_crashed")
            if d.unavailable:
                reasons.append(f"{side}_model_unavailable")
        meta = self.meta()
        meta.update({"time_capped": self.session.time_capped, "admissible": not reasons,
                     "harness_errors": self.session.harness_errors[:20],
                     "inadmissible_reasons": reasons,
                     "served": {side: d.usage.get("served", {}) for side, d in self.drivers.items()},
                     "transcript_records": self.session.transcript.records})
        result["meta"] = meta
        self.session.transcript.write("result", None, **{k: v for k, v in result.items()
                                                          if k not in ("messages", "reflections")})
        self.session.transcript.close(complete=not self.session.infra_error)
        self.session.log_event(None, "system", {"text": f"Final whistle: {home_score} - {away_score}"})
        return result
