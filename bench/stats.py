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


# the failure that most recently preceded a TURNOVER report, i.e. its cause
_TURNOVER_CAUSES = {
    OutcomeType.FAILED_DODGE: "failed_dodge", OutcomeType.FAILED_GFI: "failed_gfi",
    OutcomeType.FAILED_PICKUP: "failed_pickup", OutcomeType.FUMBLE: "fumble",
    OutcomeType.INACCURATE_PASS: "failed_pass", OutcomeType.FAILED_CATCH: "failed_catch",
    OutcomeType.INTERCEPTION: "interception", OutcomeType.KNOCKED_DOWN: "knocked_down",
    OutcomeType.PLAYER_EJECTED: "ejected", OutcomeType.FAILED_LEAP: "failed_leap",
    OutcomeType.FAILED_STAND_UP: "failed_stand_up",
}


def turnover_causes(game, team) -> dict:
    causes = {}
    reports = game.state.reports
    for i, r in enumerate(reports):
        if r.outcome_type != OutcomeType.TURNOVER or r.team != team:
            continue
        cause = "other"
        for prev in reversed(reports[max(0, i - 12):i]):
            if prev.outcome_type not in _TURNOVER_CAUSES:
                continue
            if prev.outcome_type == OutcomeType.KNOCKED_DOWN and prev.player is not None and prev.player.team != team:
                continue  # the opponent being knocked down doesn't cause *our* turnover
            cause = _TURNOVER_CAUSES[prev.outcome_type]
            break
        causes[cause] = causes.get(cause, 0) + 1
    return causes


def decision_quality(actions: list) -> dict:
    """Blood Bowl's core skill is 'safe actions first, risky ones last'. Measures it from the action log."""
    rolled = [a for a in actions if "success_est" in a]
    risky = [a for a in rolled if a["success_est"] < 0.99]
    out = {"actions_logged": len(rolled), "risky_actions": len(risky),
           "avg_risky_success": round(sum(a["success_est"] for a in risky) / len(risky), 3) if risky else None,
           "long_shots": sum(1 for a in risky if a["success_est"] < 0.5)}
    # ordering: within a team turn, how often is an action at least as safe as every later one?
    ordered = pairs = 0
    by_turn = {}
    for a in rolled:
        by_turn.setdefault((a["half"], a["turn"]), []).append(a["success_est"])
    for seq in by_turn.values():
        for i in range(len(seq)):
            for j in range(i + 1, len(seq)):
                pairs += 1
                ordered += seq[i] >= seq[j]
    out["safe_first_rate"] = round(ordered / pairs, 3) if pairs else None
    # how many players were left unactivated when a turnover ended the turn
    starts = {}
    for a in actions:
        if "unused_before" in a:
            starts[(a["half"], a["turn"], a["activation"])] = a["unused_before"]
    wasted = [starts.get((a["half"], a["turn"], a["activation"]), 1) - 1 for a in actions if a.get("turnover")]
    out["turnovers_logged"] = len(wasted)
    out["unactivated_at_turnover"] = round(sum(wasted) / len(wasted), 2) if wasted else None
    blocks = [a for a in rolled if a["action"] == "BLOCK" and a.get("block_dice") is not None]
    out["blocks_against_odds"] = sum(1 for a in blocks if a["block_dice"] < 0)
    out["blocks_logged"] = len(blocks)
    return out


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
    s["reflections"] = seat.counters.get("reflections", 0)
    s["reflections_after_turnover"] = seat.counters.get("reflections_after_turnover", 0)
    s["reflection_coverage"] = round(min(1.0, s["reflections"] / turns), 3)
    s["turnover_causes"] = turnover_causes(game, team)
    s.update(decision_quality(session.action_log.get(side, [])))
    # monitoring rate (cf. CivBench's PMR): share of non-infrastructure calls spent looking rather than acting
    info = sum(tool_use.get(t, 0) for t in ("get_state", "get_legal_actions", "get_player"))
    acting = sum(tool_use.get(t, 0) for t in ("move", "block", "blitz", "pass_ball", "handoff", "foul", "take_action"))
    s["monitoring_rate"] = round(info / (info + acting), 3) if info + acting else 0.0
    s["cache_hit_rate"] = round(s.get("cached_tokens", 0) / s["prompt_tokens"], 3) if s.get("prompt_tokens") else None
    s["model_unavailable"] = bool(getattr(driver, "unavailable", False))
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
