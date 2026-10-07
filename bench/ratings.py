"""Elo ratings and tournament standings computed from completed matches."""
from collections import defaultdict
from typing import Dict, Iterable, List

START_ELO = 1000.0
K = 32.0

STYLE_KEYS = ["aggression", "risk_taking", "passing_game", "dirty_play", "chattiness", "illegal_rate",
              # decision quality / research metrics (averaged over the games where they are defined)
              "safe_first_rate", "avg_risky_success", "unactivated_at_turnover", "monitoring_rate",
              "reflection_coverage", "cache_hit_rate"]
SUM_KEYS = ["touchdowns", "blocks", "blitzes", "fouls", "passes", "handoffs", "casualties_inflicted",
            "knockdowns_inflicted", "turnovers", "messages_sent", "message_chars", "invalid_tool_calls",
            "tool_calls", "llm_calls", "prompt_tokens", "completion_tokens", "cost", "forced_actions",
            "budget_exhausted", "turns", "latency", "rerolls_used", "dodges", "failed_dodges", "gfis", "failed_gfis",
            "cached_tokens", "reasoning_tokens", "reflections", "risky_actions", "long_shots"]


def expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400.0))


def compute_elo(matches: Iterable[dict]) -> Dict[str, dict]:
    """matches must be completed and ordered chronologically."""
    elo = defaultdict(lambda: START_ELO)
    history = defaultdict(list)
    for m in matches:
        h, a = m["home_model"], m["away_model"]
        score_h = 1.0 if m["winner"] == "home" else (0.0 if m["winner"] == "away" else 0.5)
        eh = expected(elo[h], elo[a])
        delta = K * (score_h - eh)
        elo[h] += delta
        elo[a] -= delta
        history[h].append({"t": m["finished_at"], "elo": round(elo[h], 1)})
        history[a].append({"t": m["finished_at"], "elo": round(elo[a], 1)})
    return {k: {"elo": round(v, 1), "history": history[k]} for k, v in elo.items()}


def aggregate(matches: Iterable[dict]) -> Dict[str, dict]:
    """Win/draw/loss record plus summed and averaged per-side stats for every model."""
    agg = defaultdict(lambda: {"played": 0, "wins": 0, "draws": 0, "losses": 0, "td_for": 0, "td_against": 0,
                               "sums": defaultdict(float), "style": defaultdict(float),
                               "style_n": defaultdict(int)})
    for m in matches:
        for side, model, opp_side in (("home", m["home_model"], "away"), ("away", m["away_model"], "home")):
            a = agg[model]
            a["played"] += 1
            if m["winner"] == "draw":
                a["draws"] += 1
            elif m["winner"] == side:
                a["wins"] += 1
            else:
                a["losses"] += 1
            a["td_for"] += m[f"{side}_score"] or 0
            a["td_against"] += m[f"{opp_side}_score"] or 0
            stats = m.get(f"{side}_stats") or {}
            for k in SUM_KEYS:
                v = stats.get(k)
                if isinstance(v, (int, float)):
                    a["sums"][k] += v
            for k in STYLE_KEYS:
                v = stats.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    a["style"][k] += v
                    a["style_n"][k] += 1
    out = {}
    for model, a in agg.items():
        n = max(1, a["played"])
        sums = dict(a["sums"])
        style = {k: round(v / a["style_n"][k], 3) for k, v in a["style"].items()}
        msgs = sums.get("messages_sent", 0)
        style["avg_message_len"] = round(sums.get("message_chars", 0) / msgs, 1) if msgs else 0
        out[model] = {
            "played": a["played"], "wins": a["wins"], "draws": a["draws"], "losses": a["losses"],
            "td_for": a["td_for"], "td_against": a["td_against"], "td_diff": a["td_for"] - a["td_against"],
            "win_rate": round(a["wins"] / n, 3),
            "points": a["wins"] * 3 + a["draws"],
            "sums": sums, "style": style,
            "avg_cost": round(sums.get("cost", 0) / n, 4),
            "avg_tokens": int((sums.get("prompt_tokens", 0) + sums.get("completion_tokens", 0)) / n),
            "avg_latency": round(sums.get("latency", 0) / max(1, sums.get("llm_calls", 0)), 2),
            "cas_per_game": round(sums.get("casualties_inflicted", 0) / n, 2),
        }
    return out


def standings(matches: List[dict]) -> List[dict]:
    """Tournament table: 3 points for a win, 1 for a draw; ties broken by TD difference then TDs scored."""
    agg = aggregate(matches)
    table = []
    for model, a in agg.items():
        table.append({"model": model, **{k: a[k] for k in ("played", "wins", "draws", "losses", "td_for",
                                                           "td_against", "points")},
                      "td_diff": a["td_for"] - a["td_against"], "cas": a["sums"].get("casualties_inflicted", 0)})
    table.sort(key=lambda r: (-r["points"], -r["td_diff"], -r["td_for"], r["model"]))
    return table


def bootstrap_elo(matches: List[dict], n: int = 200, seed: int = 0) -> Dict[str, dict]:
    """95% intervals for each model's Elo: resample games with replacement and shuffle their order
    (Elo is order-dependent), recompute, take the 2.5/97.5 percentiles."""
    import random
    rng = random.Random(seed)
    samples = defaultdict(list)
    if not matches:
        return {}
    for _ in range(n):
        resample = [matches[rng.randrange(len(matches))] for _ in matches]
        rng.shuffle(resample)
        elo = compute_elo(resample)
        for model in {m["home_model"] for m in matches} | {m["away_model"] for m in matches}:
            samples[model].append(elo.get(model, {"elo": START_ELO})["elo"])
    out = {}
    for model, vals in samples.items():
        vals.sort()
        lo = vals[int(0.025 * (len(vals) - 1))]
        hi = vals[int(round(0.975 * (len(vals) - 1)))]
        out[model] = {"lo": round(lo), "hi": round(hi)}
    return out
