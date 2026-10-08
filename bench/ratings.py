"""Ratings (Elo scale) and tournament standings computed from completed matches."""
import math
from collections import defaultdict
from typing import Dict, Iterable, List

START_ELO = 1000.0

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


# Ratings are a Bradley-Terry fit on the Elo scale: every completed game is used at once, so the result doesn't depend
# on the order games were played in, and a model with few games gets a wide interval instead of a confident number.
# A weak prior (centred on START_ELO) keeps unbeaten or winless models finite.
_C = math.log(10) / 400.0     # Elo points -> natural log-odds
PRIOR_SD = 400.0              # Elo points


def _fit(n: int, W, N, x0=None):
    """Newton's method on the penalised log-likelihood. W[i,j] = points i scored against j (draw = 0.5),
    N[i,j] = games between i and j. Returns ratings in log-odds units and their covariance."""
    import numpy as np
    x = np.zeros(n) if x0 is None else np.array(x0, dtype=float)
    inv_tau2 = 1.0 / (PRIOR_SD * _C) ** 2
    for _ in range(100):
        P = 1.0 / (1.0 + np.exp(x[None, :] - x[:, None]))   # P[i,j] = chance i beats j
        g = (W - N * P).sum(axis=1) - x * inv_tau2
        Hm = N * P * (1.0 - P)
        H = Hm - np.diag(Hm.sum(axis=1) + inv_tau2)          # negative definite thanks to the prior
        step = np.linalg.solve(H, g)
        x = x - step
        if np.abs(step).max() < 1e-7:
            break
    P = 1.0 / (1.0 + np.exp(x[None, :] - x[:, None]))
    Hm = N * P * (1.0 - P)
    cov = np.linalg.inv(np.diag(Hm.sum(axis=1) + inv_tau2) - Hm)
    return x, cov


def fit_ratings(matches: Iterable[dict]) -> Dict[str, dict]:
    """Rating, standard error and 95% interval (all in Elo points) for every model that has played."""
    import numpy as np
    matches = list(matches)
    ids = sorted({m["home_model"] for m in matches} | {m["away_model"] for m in matches})
    if not ids:
        return {}
    ix = {k: i for i, k in enumerate(ids)}
    W, N = np.zeros((len(ids), len(ids))), np.zeros((len(ids), len(ids)))
    for m in matches:
        h, a = ix[m["home_model"]], ix[m["away_model"]]
        s = _score(m)
        W[h, a] += s
        W[a, h] += 1.0 - s
        N[h, a] += 1
        N[a, h] += 1
    x, cov = _fit(len(ids), W, N)
    return {k: _rating(x[i], cov[i, i]) for k, i in ix.items()}


def _score(m: dict) -> float:
    return 1.0 if m["winner"] == "home" else (0.0 if m["winner"] == "away" else 0.5)


def _rating(x: float, var: float) -> dict:
    elo, se = START_ELO + x / _C, math.sqrt(max(var, 0.0)) / _C
    return {"elo": round(elo, 1), "se": round(se, 1), "lo": round(elo - 1.96 * se), "hi": round(elo + 1.96 * se)}


def compute_elo(matches: Iterable[dict], max_points: int = 300) -> Dict[str, dict]:
    """The current fit plus a history: the fit is redone as games accumulate (matches must be completed and in
    chronological order), and each model gets a point after each of its games. Long histories are sampled down to
    about max_points refits, each covering a run of games."""
    import numpy as np
    matches = list(matches)
    ids = sorted({m["home_model"] for m in matches} | {m["away_model"] for m in matches})
    if not ids:
        return {}
    ix = {k: i for i, k in enumerate(ids)}
    W, N = np.zeros((len(ids), len(ids))), np.zeros((len(ids), len(ids)))
    history = defaultdict(list)
    step = max(1, math.ceil(len(matches) / max_points))
    x, cov, touched = None, None, set()
    for n, m in enumerate(matches, 1):
        h, a = ix[m["home_model"]], ix[m["away_model"]]
        s = _score(m)
        W[h, a] += s
        W[a, h] += 1.0 - s
        N[h, a] += 1
        N[a, h] += 1
        touched.update((h, a))
        if n % step and n != len(matches):
            continue
        x, cov = _fit(len(ids), W, N, x)
        for i in sorted(touched):
            history[ids[i]].append({"t": m["finished_at"], "elo": round(START_ELO + x[i] / _C, 1)})
        touched = set()
    return {k: {**_rating(x[i], cov[i, i]), "history": history[k]} for k, i in ix.items()}


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
