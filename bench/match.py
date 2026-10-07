"""Runs one complete match between two configured models and returns the result + stats."""
import time
from typing import Callable, Optional

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
                         extra=model_cfg.get("extra") or {})


def limits_for(model_cfg: dict, settings: dict) -> DriverLimits:
    return DriverLimits(
        max_tool_calls_per_turn=int(model_cfg.get("max_tool_calls_per_turn", settings.get("max_tool_calls_per_turn", 40))),
        turn_time_limit=float(model_cfg.get("turn_time_limit", settings.get("turn_time_limit", 300))),
        max_illegal_streak=int(settings.get("max_illegal_streak", 3)),
        budget_usd=model_cfg.get("budget_usd_per_game", settings.get("budget_usd_per_game")),
    )


class MatchRunner:
    def __init__(self, match_id: str, home_cfg: dict, away_cfg: dict, settings: dict,
                 api_key: Optional[str] = None, event_sink: Optional[Callable] = None):
        self.match_id = match_id
        self.home_cfg = home_cfg
        self.away_cfg = away_cfg
        self.settings = settings
        self.session = GameSession(match_id, home_cfg["name"], away_cfg["name"],
                                   game_mode=str(settings.get("game_mode", "5")),
                                   team_file=settings.get("team", "human"),
                                   decision_timeout=float(settings.get("decision_timeout", 900)),
                                   event_sink=event_sink,
                                   max_messages_per_turn=int(settings.get("max_messages_per_turn", 2)),
                                   max_message_len=int(settings.get("max_message_len", 280)))
        self.drivers = {}
        for side, cfg in (("home", home_cfg), ("away", away_cfg)):
            seat = self.session.seat(side)
            llm = make_llm(cfg, seat, api_key)
            opp = away_cfg if side == "home" else home_cfg
            self.drivers[side] = SeatDriver(self.session, seat, llm, cfg["name"], limits_for(cfg, settings),
                                            opponent_name=opp["name"])

    def run(self) -> dict:
        t0 = time.time()
        self.session.log_event(None, "system", {"text": f"Kick-off: {self.home_cfg['name']} (home) vs "
                                                        f"{self.away_cfg['name']} (away)"})
        self.session.start()
        for d in self.drivers.values():
            d.start()
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
        }
        winner = "draw"
        if home_score > away_score:
            winner = "home"
        elif away_score > home_score:
            winner = "away"
        result["winner"] = winner
        self.session.log_event(None, "system", {"text": f"Final whistle: {home_score} - {away_score}"})
        return result
