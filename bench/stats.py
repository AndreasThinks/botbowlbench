"""Per-side match statistics: what happened on the pitch, how the model played, and what it cost."""
from botbowl.core.table import OutcomeType

# outcome -> (stat name, attributed to: "player" team of report.player, "opp" team of report.opp_player, "team")
_OUTCOME_STATS = {
    OutcomeType.TOUCHDOWN: ("touchdowns", "player"),
    OutcomeType.BLOCK_ACTION_STARTED: ("blocks", "player"),
    OutcomeType.BLITZ_ACTION_STARTED: ("blitzes", "player"),
    OutcomeType.FOUL_ACTION_STARTED: ("fouls", "player"),
    OutcomeType.PASS_ACTION_STARTED: ("passes", "player"),
    OutcomeType.HANDOFF_ACTION_STARTED: ("handoffs", "player"),
    OutcomeType.MOVE_ACTION_STARTED: ("moves", "player"),
    OutcomeType.SUCCESSFUL_DODGE: ("dodges", "player"),
    OutcomeType.FAILED_DODGE: ("failed_dodges", "player"),
    OutcomeType.SUCCESSFUL_GFI: ("gfis", "player"),
    OutcomeType.FAILED_GFI: ("failed_gfis", "player"),
    OutcomeType.SUCCESSFUL_PICKUP: ("pickups", "player"),
    OutcomeType.FAILED_PICKUP: ("failed_pickups", "player"),
    OutcomeType.ACCURATE_PASS: ("accurate_passes", "player"),
    OutcomeType.INACCURATE_PASS: ("inaccurate_passes", "player"),
    OutcomeType.FUMBLE: ("fumbles", "player"),
    OutcomeType.SUCCESSFUL_CATCH: ("catches", "player"),
    OutcomeType.FAILED_CATCH: ("failed_catches", "player"),
    OutcomeType.INTERCEPTION: ("interceptions", "player"),
    OutcomeType.PLAYER_EJECTED: ("ejections", "player"),
    OutcomeType.TURNOVER: ("turnovers", "team"),
    OutcomeType.REROLL_USED: ("rerolls_used", "team"),
    # damage *suffered* by report.player -> credited to the other side as "inflicted"
    OutcomeType.KNOCKED_DOWN: ("knockdowns_inflicted", "victim"),
    OutcomeType.KNOCKED_OUT: ("kos_inflicted", "victim"),
    OutcomeType.CASUALTY: ("casualties_inflicted", "victim"),
}


def _side_of_team(game, team):
    if team is None:
        return None
    return "home" if team == game.state.home_team else "away"


def outcome_counts(game):
    counts = {"home": {}, "away": {}}
    for r in game.state.reports:
        spec = _OUTCOME_STATS.get(r.outcome_type)
        if spec is None:
            continue
        name, who = spec
        side = None
        if who == "player" and r.player is not None:
            side = _side_of_team(game, r.player.team)
        elif who == "team":
            side = _side_of_team(game, r.team)
        elif who == "victim" and r.player is not None:
            victim = _side_of_team(game, r.player.team)
            side = None if victim is None else ("away" if victim == "home" else "home")
        if side is None:
            continue
        counts[side][name] = counts[side].get(name, 0) + 1
    return counts


def side_stats(session, side: str, driver) -> dict:
    game = session.game
    seat = session.seat(side)
    team = seat.team or (game.state.home_team if side == "home" else game.state.away_team)
    counts = outcome_counts(game)[side]
    turns = max(1, sum(1 for r in game.state.reports
                       if r.outcome_type == OutcomeType.TURN_START and r.team == team))
    s = dict(counts)
    s["turns"] = turns
    s["score"] = team.state.score
    for k in ("messages_sent", "message_chars", "messages_rejected", "illegal_actions", "invalid_tool_calls",
              "forced_actions", "timeouts", "auto_resolved", "budget_exhausted", "actions"):
        s[k] = seat.counters.get(k, 0)
    s.update({k: v for k, v in driver.usage.items()})
    s["cost"] = round(s.get("cost", 0.0), 6)
    s["crashed"] = driver.crashed
    tool_use = {k[5:]: v for k, v in seat.counters.items() if k.startswith("tool:")}
    s["tool_usage"] = tool_use
    # derived style metrics (per team turn)
    aggressive = s.get("blocks", 0) + s.get("blitzes", 0) + s.get("fouls", 0)
    risky = s.get("dodges", 0) + s.get("failed_dodges", 0) + s.get("gfis", 0) + s.get("failed_gfis", 0)
    s["aggression"] = round(aggressive / turns, 3)
    s["risk_taking"] = round(risky / turns, 3)
    s["passing_game"] = round((s.get("passes", 0) + s.get("handoffs", 0)) / turns, 3)
    s["chattiness"] = round(s["messages_sent"] / turns, 3)
    s["dirty_play"] = round(s.get("fouls", 0) / turns, 3)
    calls = max(1, s.get("tool_calls", 0))
    s["illegal_rate"] = round(s.get("invalid_tool_calls", 0) / calls, 3)
    return s
