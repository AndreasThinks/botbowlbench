"""
Tripwire: public health signals for every condition (a model plus its declared settings), read per served host.

Play first, flag automatically. A new condition plays its placement under the same settings as everyone else; every
game it plays feeds a few counters, and a fixed rule turns them into public flags:

* **R** reasoning not returned: reasoning was requested, yet on one host at least 80% of the condition's first 30
  ordinary team-turn replies showed no reasoning evidence (see :func:`reasoning_evidence`).
* **P** prose instead of tools: replies with no tool call above 5% over the latest 3 completed instrumented games.
* **L** output cap reached: replies cut off at the output length limit above 5% over the same games.
* **S** substituted play: the default policy played for the model because its provider failed (an exhausted model
  call, or the model marked unavailable after errors) in 2 of the latest 3 completed instrumented games.
* **H** host mix: the share of calls each host served, always shown.

Flags describe how a route behaved, never how well the model played. Nothing here changes settings, ratings,
placements or ``models.yaml``. R only suggests a successor condition for a maintainer to review; S may put a
scheduling hold on the condition (see ``Bench.check_health_holds``), which keeps its queued games and is released by
an admin.

Everything here is pure and deterministic: per-game records are plain, bounded JSON (stored with each side's match
stats) and :func:`condition_health` folds a condition's games into its current signals.
"""
import re
from typing import Dict, Iterable, List, Optional

# Bump when a counter's meaning changes: records with any other version read as "not measured".
HEALTH_VERSION = 1

# The published starting thresholds. The same for every condition; not tuned per model. The About page, the API and
# the scheduler all read them from here.
THRESHOLDS = {
    # first `window` ordinary team-turn replies on a host; `low_share` of them with no reasoning evidence. A reply has
    # evidence if the provider reports at least `min_reasoning_tokens` reasoning tokens, returns an encrypted
    # reasoning block with content, or returns at least `substantive_chars` non-space characters of readable
    # reasoning or of ordinary content.
    "R": {"window": 30, "low_share": 0.8, "min_reasoning_tokens": 16, "substantive_chars": 40},
    # over the latest `games` completed instrumented games: rate strictly above `rate`, for the condition as a whole
    # or for one host that served at least `min_host_responses` of those replies
    "P": {"rate": 0.05, "games": 3, "min_host_responses": 30},
    "L": {"rate": 0.05, "games": 3, "min_host_responses": 30},
    # provider-caused substituted play in at least `games_with` of the latest `games` completed instrumented games
    "S": {"games_with": 2, "games": 3},
}

_R, _P, _L, _S = (THRESHOLDS[k] for k in "RPLS")
SIGNALS = {
    "R": {"name": "Reasoning not returned",
          "trigger": f"Reasoning was requested, yet on one serving host at least {round(_R['low_share'] * 100)}% of "
                     f"the condition's first {_R['window']} ordinary team-turn replies came back with fewer than "
                     f"{_R['min_reasoning_tokens']} reasoning tokens, no readable or encrypted reasoning and nothing "
                     f"substantive in the reply text.",
          "means": "On that host, requiring a tool call may be stopping the provider from returning reasoning, as it "
                   "did on a few known routes. It does not prove that the model did no internal thinking.",
          "next": "Public flag and a review. A maintainer may open one successor condition whose only change is "
                  "automatic tool choice (reasoning stays requested), with its own ID and placement. Nothing is "
                  "changed automatically."},
    "P": {"name": "Prose instead of tools",
          "trigger": f"More than {round(_P['rate'] * 100)}% of replies had no tool call over the latest {_P['games']} "
                     f"completed instrumented games, for the condition or for one host with at least "
                     f"{_P['min_host_responses']} of those replies.",
          "means": "Model behaviour: the harness nudges each such reply and counts it towards the invalid-call limit.",
          "next": "Public flag only. Settings stay as they are."},
    "L": {"name": "Output cap reached",
          "trigger": f"More than {round(_L['rate'] * 100)}% of replies were cut off at the output length limit over "
                     f"the latest {_L['games']} completed instrumented games, for the condition or for one host with "
                     f"at least {_L['min_host_responses']} of those replies.",
          "means": "The model keeps generating past the shared output ceiling before it acts. Long output is not "
                   "evidence of better play.",
          "next": "Public flag only. The ceiling is the same for everyone and is not raised for one model."},
    "S": {"name": "Substituted play",
          "trigger": f"The default policy played for the model because its provider failed (model calls that still "
                     f"failed after retries, or the model marked unavailable) in {_S['games_with']} of the latest "
                     f"{_S['games']} completed instrumented games.",
          "means": "A provider outage, not the model's play. Fallback caused by the model's own illegal calls, "
                   "truncations or time budget does not count.",
          "next": "Public flag, and a scheduling hold on the condition: the game in progress finishes, its queued "
                  "games keep their place and wait, everything else carries on, until a maintainer releases the hold."},
    "H": {"name": "Host mix",
          "trigger": "Always shown.",
          "means": "The share of model calls each serving host answered, so an average cannot hide one route.",
          "next": "Information only."},
}

MAX_HOSTS = 12                 # named hosts per game record; any more share one bucket
MAX_HOST_CHARS = 48
OTHER_HOSTS = "other hosts"
UNKNOWN_HOST = "unknown"       # the host is not known: never guessed from an earlier reply

# why a model call failed for good (after retries); see LLMError.kind
ERROR_KINDS = ("rate_limited", "server_error", "network", "timeout", "provider_error", "invalid_response",
               "content_filter", "rejected", "not_found", "other")

# why the default policy made a decision for a seat (Seat counters "forced:<cause>")
FALLBACK_CAUSES = {
    "llm_error": "a model call failed after retries (provider)",
    "unavailable": "the model was marked unavailable after failed calls (provider)",
    "illegal_streak": "too many illegal or invalid calls since the last game action",
    "truncation_streak": "too many truncated replies since the last game action",
    "tool_budget": "the turn's tool-call budget was used up",
    "turn_time": "the turn's time limit ran out",
    "cost_budget": "the game's cost budget ran out",
    "time_cap": "the whole-game time cap was reached",
    "agent_crash": "the agent loop crashed (harness)",
    "decision_timeout": "a decision timed out",
    "aborted": "the game was aborted",
    "other": "other",
}
PROVIDER_CAUSES = ("llm_error", "unavailable")


# ---- per-game records ------------------------------------------------------------------------------------------
_HOST_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()/+-]*$")


def clean_host(name) -> Optional[str]:
    """A host's display name if it looks like one ("BaseTen", "Google Vertex"), else None. Error bodies are not
    trusted: anything with odd characters, long tokens or key-like text is dropped rather than stored."""
    if not isinstance(name, str):
        return None
    name = " ".join(name.split())
    if not name or len(name) > MAX_HOST_CHARS or not _HOST_OK.match(name):
        return None
    if re.search(r"(^|[^a-z])sk-", name.lower()) or any(len(t) >= 24 for t in re.split(r"[ ._()/+-]", name)):
        return None
    return name


def _chars(text) -> int:
    return sum(1 for ch in text if not ch.isspace()) if isinstance(text, str) else 0


def reasoning_evidence(*, content, reasoning, details, reasoning_tokens) -> Optional[str]:
    """Which signal shows that a reply carried reasoning ("tokens", "encrypted", "readable", "content"), or None for a
    low-evidence reply. Deliberately generous: anything that may be reasoning counts, so R only fires when a reply
    shows nothing at all. A newline-only one-token "reasoning" is low evidence; so is an empty reply."""
    t = THRESHOLDS["R"]
    if int(reasoning_tokens or 0) >= t["min_reasoning_tokens"]:
        return "tokens"
    readable = reasoning if isinstance(reasoning, str) else ""
    for d in details or []:
        if not isinstance(d, dict):
            continue
        if d.get("type") == "reasoning.encrypted":
            if isinstance(d.get("data"), str) and d["data"].strip():
                return "encrypted"
            continue
        for k in ("text", "summary"):
            if isinstance(d.get(k), str):
                readable += "\n" + d[k]
    if _chars(readable) >= t["substantive_chars"]:
        return "readable"
    if _chars(content) >= t["substantive_chars"]:
        return "content"
    return None


def new_record(*, reasoning_requested, reasoning_listed=None, tool_choice=None, max_tokens=None) -> dict:
    """An empty per-game, per-side record. ``coverage`` becomes "partial" when a game resumes from a checkpoint
    taken before instrumentation (its first part was never counted)."""
    return {"v": HEALTH_VERSION, "coverage": "full", "reasoning_requested": bool(reasoning_requested),
            "reasoning_listed": reasoning_listed, "tool_choice": {"requested": tool_choice, "effective": tool_choice},
            "max_tokens": max_tokens, "hosts": {}, "errors": {}, "fallback": {}}


def _new_host() -> dict:
    return {"responses": 0, "no_tool": 0, "truncated": 0, "tool_calls": 0, "bad_args": 0,
            "r_eligible": 0, "r_low": 0, "r_seq": "", "truncated_at": None,
            "reflection": {"responses": 0, "no_tool": 0, "truncated": 0}}


def _bucket(table: dict, host, make):
    h = clean_host(host) or UNKNOWN_HOST
    if h not in table and len(table) >= MAX_HOSTS:
        h = OTHER_HOSTS
    return table.setdefault(h, make())


def note_response(rec: dict, *, host, kind: str, hurried: bool, content, reasoning, details, reasoning_tokens,
                  finish_reason, tool_calls: int, bad_args: int = 0, completion_tokens: int = 0):
    """Count one model response (however many tool calls it holds). ``kind``: "turn" (the seat's own team turn),
    "decision" (setup, coin toss, kick-off and other decisions outside its turn) or "reflection" (the one-tool reply
    asked for after a turnover; counted apart so it never moves gameplay rates). Only unhurried "turn" replies count
    for R: a hurried reply (the last-chance call, or the short reply after a stopped one) asks for minimal thinking."""
    h = _bucket(rec["hosts"], host, _new_host)
    length = (finish_reason or "").lower() == "length"
    if kind == "reflection":
        r = h["reflection"]
        r["responses"] += 1
        if length:
            r["truncated"] += 1
        elif not tool_calls:
            r["no_tool"] += 1
        return
    h["responses"] += 1
    h["tool_calls"] += int(tool_calls)
    h["bad_args"] += int(bad_args)
    if length:
        h["truncated"] += 1
        n = int(completion_tokens or 0)
        if n:
            lo_hi = h["truncated_at"]
            h["truncated_at"] = [min(lo_hi[0], n), max(lo_hi[1], n)] if lo_hi else [n, n]
    elif not tool_calls:
        h["no_tool"] += 1
    if kind == "turn" and not hurried and rec.get("reasoning_requested"):
        low = reasoning_evidence(content=content, reasoning=reasoning, details=details,
                                 reasoning_tokens=reasoning_tokens) is None
        h["r_eligible"] += 1
        h["r_low"] += int(low)
        if len(h["r_seq"]) < THRESHOLDS["R"]["window"]:
            h["r_seq"] += "1" if low else "0"


def note_error(rec: dict, *, kind, host):
    """A gameplay model call that failed for good. ``host`` only when the error itself named it."""
    e = _bucket(rec["errors"], host, dict)
    k = kind if kind in ERROR_KINDS else "other"
    e[k] = e.get(k, 0) + 1


def provider_fallback(rec) -> int:
    """Decisions the default policy made because the provider failed."""
    fb = (rec or {}).get("fallback") or {}
    return sum(int(fb.get(c, 0) or 0) for c in PROVIDER_CAUSES)


def instrumented(rec) -> bool:
    return isinstance(rec, dict) and rec.get("v") == HEALTH_VERSION


# ---- a condition's games -------------------------------------------------------------------------------------
def games_for(model_id: str, matches: Iterable[dict]) -> List[dict]:
    """The completed (decoded) matches of one condition as aggregation input, oldest first."""
    out = []
    for m in matches:
        if model_id not in (m.get("home_model"), m.get("away_model")):
            continue
        side = "home" if m.get("home_model") == model_id else "away"
        stats = m.get(f"{side}_stats") or {}
        meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
        served = stats.get("served") or ((meta.get("served") or {}).get(side)) or {}
        out.append({"match_id": m.get("id"), "finished_at": m.get("finished_at"), "health": stats.get("health"),
                    "served": served})
    out.sort(key=lambda g: (g["finished_at"] or 0, g["match_id"] or ""))
    return out


def host_mix(games: Iterable[dict]) -> List[dict]:
    """Calls per serving host over every game, measured or not (``served`` predates the other counters)."""
    calls: Dict[str, int] = {}
    for g in games:
        for key, n in (g.get("served") or {}).items():
            host = clean_host(str(key).rsplit("@", 1)[-1] if "@" in str(key) else "") or UNKNOWN_HOST
            calls[host] = calls.get(host, 0) + int(n or 0)
    total = sum(calls.values())
    return [{"host": h, "calls": n, "share": round(n / total, 3) if total else None}
            for h, n in sorted(calls.items(), key=lambda kv: (-kv[1], kv[0]))]


def _r_signal(full: List[dict], legacy: int) -> dict:
    t = THRESHOLDS["R"]
    note = f"First {t['window']} instrumented team-turn replies on each host"
    if legacy:
        note += (f"; {legacy} earlier game{'s' if legacy != 1 else ''} predate instrumentation, so for an older "
                 f"condition this is not its very first window")
    games = [g for g in full if g["health"].get("reasoning_requested")]
    if any(g["health"].get("reasoning_listed") is False for g in full):
        return {"state": "not_applicable", "reason": "OpenRouter does not list reasoning support for this model",
                "hosts": [], "flagged_hosts": [], "window_note": note}
    if not games:
        return {"state": "not_applicable", "reason": "reasoning is not requested for this condition", "hosts": [],
                "flagged_hosts": [], "window_note": note}
    seqs: Dict[str, str] = {}
    first_seen: Dict[str, str] = {}
    for g in games:
        for host, h in (g["health"].get("hosts") or {}).items():
            first_seen.setdefault(host, g["match_id"])
            seqs[host] = (seqs.get(host, "") + str(h.get("r_seq") or ""))[:t["window"]]
    hosts = []
    for host, seq in seqs.items():
        low, n = seq.count("1"), len(seq)
        if n < t["window"]:
            state = "warming_up"
        else:
            state = "flagged" if low / n >= t["low_share"] - 1e-9 else "clear"
        hosts.append({"host": host, "eligible": n, "low": low, "share": round(low / n, 3) if n else None,
                      "state": state, "first_game": first_seen[host]})
    hosts.sort(key=lambda h: (-h["eligible"], h["host"]))
    flagged = sorted(h["host"] for h in hosts if h["state"] == "flagged")
    state = "flagged" if flagged else ("clear" if any(h["state"] == "clear" for h in hosts) else "warming_up")
    return {"state": state, "hosts": hosts, "flagged_hosts": flagged, "window_note": note}


def _rate_signal(key: str, field: str, full: List[dict]) -> dict:
    t = THRESHOLDS[key]
    window = full[-t["games"]:]
    tot = {"responses": 0, "count": 0, "tool_calls": 0, "bad_args": 0, "reflection_responses": 0,
           "reflection_count": 0}
    per: Dict[str, dict] = {}
    for g in window:
        for host, h in (g["health"].get("hosts") or {}).items():
            p = per.setdefault(host, {"host": host, "responses": 0, "count": 0, "truncated_at": None})
            p["responses"] += int(h.get("responses", 0))
            p["count"] += int(h.get(field, 0))
            tot["responses"] += int(h.get("responses", 0))
            tot["count"] += int(h.get(field, 0))
            tot["tool_calls"] += int(h.get("tool_calls", 0))
            tot["bad_args"] += int(h.get("bad_args", 0))
            refl = h.get("reflection") or {}
            tot["reflection_responses"] += int(refl.get("responses", 0))
            tot["reflection_count"] += int(refl.get(field, 0))
            if key == "L" and h.get("truncated_at"):
                lo, hi = h["truncated_at"]
                p["truncated_at"] = [min(lo, p["truncated_at"][0]), max(hi, p["truncated_at"][1])] \
                    if p["truncated_at"] else [lo, hi]
    hosts = []
    for p in sorted(per.values(), key=lambda p: (-p["responses"], p["host"])):
        p["rate"] = round(p["count"] / p["responses"], 4) if p["responses"] else None
        p["state"] = ("insufficient_data" if p["responses"] < t["min_host_responses"]
                      else "flagged" if p["count"] / p["responses"] > t["rate"] else "clear")
        if key != "L":
            p.pop("truncated_at")
        hosts.append(p)
    rate = tot["count"] / tot["responses"] if tot["responses"] else None
    flagged_hosts = sorted(p["host"] for p in hosts if p["state"] == "flagged")
    if len(window) < t["games"]:
        state = "warming_up"
    elif not tot["responses"]:
        state = "insufficient_data"
    else:
        state = "flagged" if (rate > t["rate"] or flagged_hosts) else "clear"
    out = {"state": state, "games": [g["match_id"] for g in window], "games_seen": len(window),
           "rate": round(rate, 4) if rate is not None else None, "hosts": hosts,
           "flagged_hosts": flagged_hosts if len(window) >= t["games"] else [], **tot}
    if key == "L":
        out["requested_max_tokens"] = window[-1]["health"].get("max_tokens") if window else None
    return out


def _s_signal(full: List[dict]) -> dict:
    t = THRESHOLDS["S"]
    window = full[-t["games"]:]
    games = [{"match_id": g["match_id"], "finished_at": g["finished_at"],
              "provider_fallback_actions": provider_fallback(g["health"]),
              "fallback": dict(g["health"].get("fallback") or {}),
              "errors": {h: dict(e) for h, e in (g["health"].get("errors") or {}).items()}} for g in window]
    hit = sum(1 for g in games if g["provider_fallback_actions"] > 0)
    state = "flagged" if hit >= t["games_with"] else ("warming_up" if len(window) < t["games"] else "clear")
    return {"state": state, "games_with": hit, "games_seen": len(window), "games": games}


def condition_health(games: List[dict], *, llm: bool = True, model_id: Optional[str] = None,
                     hold: Optional[dict] = None) -> dict:
    """Current signals of one condition from its completed games (see :func:`games_for`). Statuses: "flagged",
    "clear" (every applicable signal measured and quiet), "warming_up" (not enough measured replies or games yet),
    "unmeasured" (no instrumented game: legacy history reads as unknown, never as healthy) and "not_applicable"
    (baselines make no model calls)."""
    games = sorted(games, key=lambda g: (g.get("finished_at") or 0, g.get("match_id") or ""))
    full = [g for g in games if instrumented(g.get("health")) and g["health"].get("coverage") == "full"]
    partial = [g for g in games if instrumented(g.get("health")) and g["health"].get("coverage") != "full"]
    legacy = len(games) - len(full) - len(partial)
    out = {"version": HEALTH_VERSION, "thresholds": THRESHOLDS, "flags": [], "hold": hold, "successor": None,
           "coverage": {"games": len(games), "instrumented": len(full), "legacy": legacy, "partial": len(partial)},
           "hosts": host_mix(games), "since": full[0]["finished_at"] if full else None}
    if not llm:
        out.update(status="not_applicable", signals={k: {"state": "not_applicable"} for k in "RPLS"})
        return out
    if not full:
        out.update(status="unmeasured", signals={k: {"state": "unknown"} for k in "RPLS"})
        return out
    signals = {"R": _r_signal(full, legacy), "P": _rate_signal("P", "no_tool", full),
               "L": _rate_signal("L", "truncated", full), "S": _s_signal(full)}
    flags = [k for k in "RPLS" if signals[k]["state"] == "flagged"]
    if flags:
        status = "flagged"
    elif all(signals[k]["state"] in ("clear", "not_applicable") for k in "RPLS"):
        status = "clear"
    else:
        status = "warming_up"
    out.update(status=status, flags=flags, signals=signals)
    if "R" in flags:
        tc = (full[-1]["health"].get("tool_choice") or {})
        forced = (tc.get("effective") or tc.get("requested")) == "required"
        out["successor"] = {
            "from": model_id, "applied": False,
            "suggested_id": f"{model_id}-auto" if model_id else None,
            "change": {"tool_choice": "auto"} if forced else None,
            "note": ("Suggested only, not applied. A maintainer may add it to models.yaml as a separate condition "
                     "with its own ID and placement; its only change is tool_choice: auto. This condition keeps its "
                     "games, rating and settings." if forced else
                     "This condition already ran with automatic tool choice, so no change is pre-approved: "
                     "maintainer review only.")}
    return out


def compact(h: dict) -> dict:
    """What a leaderboard row needs: status, flags, the flagged hosts per signal and any hold."""
    sig = h.get("signals") or {}
    return {"status": h["status"], "flags": list(h["flags"]), "hold": h.get("hold"),
            "flagged_hosts": {k: sig[k].get("flagged_hosts", []) for k in h["flags"]},
            "coverage": h["coverage"]}


def s_hold_evidence(games: List[dict], since: Optional[float] = None) -> Optional[dict]:
    """Evidence for a scheduling hold when S fires on instrumented games finished after ``since`` (the last release,
    so a released hold is not put back on the same games), else None."""
    games = [g for g in sorted(games, key=lambda g: (g.get("finished_at") or 0, g.get("match_id") or ""))
             if instrumented(g.get("health")) and g["health"].get("coverage") == "full"
             and (since is None or (g.get("finished_at") or 0) > since)]
    s = _s_signal(games)
    if s["state"] != "flagged":
        return None
    hit = [g for g in s["games"] if g["provider_fallback_actions"] > 0]
    return {"signal": "S", "games": [g["match_id"] for g in hit],
            "provider_fallback_actions": {g["match_id"]: g["provider_fallback_actions"] for g in hit},
            "errors": {g["match_id"]: g["errors"] for g in hit},
            "window": [g["match_id"] for g in s["games"]]}
