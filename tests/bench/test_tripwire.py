"""Tripwire: per-condition, per-served-host health signals (R, P, L, S, H), provider-fallback attribution, the
persistent scheduling hold and how all of it reaches the API and the pages."""
import json
import os
import time

import pytest

from bench import db, health
from bench.llm import LLMError, LLMResponse, RandomPolicy, ToolCall
from bench.match import MatchRunner

RANDOM = {"name": "Random", "provider": "random", "seed": 1}
LLM_CFG = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model"}
R = health.THRESHOLDS["R"]


# ---- helpers ----------------------------------------------------------------------------------------------
def reply(host="HostA", kind="turn", hurried=False, content="", reasoning="", details=None, tokens=0,
          finish="tool_calls", tools=1, bad_args=0, completion=50):
    return dict(host=host, kind=kind, hurried=hurried, content=content, reasoning=reasoning, details=details or [],
                reasoning_tokens=tokens, finish_reason=finish, tool_calls=tools, bad_args=bad_args,
                completion_tokens=completion)


LOW = dict(reasoning="\n", tokens=1)                               # Nemotron's newline-only one-token "reasoning"
GOOD = dict(reasoning="I should block with the lineman first, it is the safest action available.", tokens=180)


def record(replies, requested=True, listed=None, fallback=None, errors=(), coverage="full", tool_choice="required"):
    rec = health.new_record(reasoning_requested=requested, reasoning_listed=listed, tool_choice=tool_choice,
                            max_tokens=16384)
    for r in replies:
        health.note_response(rec, **r)
    for kind, host in errors:
        health.note_error(rec, kind=kind, host=host)
    rec["fallback"] = dict(fallback or {})
    rec["coverage"] = coverage
    return rec


def game(i, rec, served=None):
    return {"match_id": f"g{i}", "finished_at": 1000.0 + i, "health": rec, "served": served or {}}


# ---- R: the reasoning-evidence rule ------------------------------------------------------------------------------
def test_newline_only_one_token_reasoning_is_low_evidence():
    assert health.reasoning_evidence(content="", reasoning="\n", details=[], reasoning_tokens=1) is None
    assert health.reasoning_evidence(content="", reasoning="", details=[], reasoning_tokens=0) is None
    assert health.reasoning_evidence(content="", reasoning=" \n\t", details=[], reasoning_tokens=R["min_reasoning_tokens"] - 1) is None


def test_reasoning_evidence_from_any_one_signal():
    ev = health.reasoning_evidence
    assert ev(content="", reasoning="", details=[], reasoning_tokens=R["min_reasoning_tokens"]) == "tokens"
    assert ev(content="", reasoning="", details=[{"type": "reasoning.encrypted", "data": "gAAAAB..."}],
              reasoning_tokens=0) == "encrypted"
    # an encrypted block with nothing in it is not evidence
    assert ev(content="", reasoning="", details=[{"type": "reasoning.encrypted", "data": ""}], reasoning_tokens=0) is None
    assert ev(content="", reasoning=GOOD["reasoning"], details=[], reasoning_tokens=0) == "readable"
    assert ev(content="", reasoning="", details=[{"type": "reasoning.text", "text": GOOD["reasoning"]}],
              reasoning_tokens=0) == "readable"
    assert ev(content="", reasoning="", details=[{"type": "reasoning.summary", "summary": GOOD["reasoning"]}],
              reasoning_tokens=0) == "readable"
    # thinking written into ordinary content counts too: opaque content is read conservatively, never as missing
    assert ev(content="<think>Blitz the carrier, then move the others up the left side.</think>", reasoning="",
              details=[], reasoning_tokens=0) == "content"
    assert ev(content="ok", reasoning="", details=[], reasoning_tokens=0) is None


def _r(rec, host):
    out = health.condition_health([game(1, rec)], llm=True)
    return out, next(h for h in out["signals"]["R"]["hosts"] if h["host"] == host)


def test_r_trips_at_exactly_80_percent_of_the_first_30_team_turn_replies():
    need = round(R["low_share"] * R["window"])          # 24 of 30
    replies = [reply(**LOW)] * need + [reply(**GOOD)] * (R["window"] - need)
    out, host = _r(record(replies), "HostA")
    assert host["state"] == "flagged" and host["eligible"] == 30 and host["low"] == need
    assert "R" in out["flags"] and out["status"] == "flagged"
    replies = [reply(**LOW)] * (need - 1) + [reply(**GOOD)] * (R["window"] - need + 1)
    out, host = _r(record(replies), "HostA")
    assert host["state"] == "clear" and "R" not in out["flags"]


def test_r_waits_for_the_full_first_window():
    out, host = _r(record([reply(**LOW)] * (R["window"] - 1)), "HostA")
    assert host["state"] == "warming_up" and host["eligible"] == R["window"] - 1
    assert out["signals"]["R"]["state"] == "warming_up" and "R" not in out["flags"]


def test_r_first_window_is_not_rolling():
    # 30 good replies decide the host; 100 later low ones don't change the verdict of the first window
    rec1 = record([reply(**GOOD)] * 30)
    rec2 = record([reply(**LOW)] * 100)
    out = health.condition_health([game(1, rec1), game(2, rec2)], llm=True)
    host = out["signals"]["R"]["hosts"][0]
    assert host["state"] == "clear" and host["low"] == 0 and host["eligible"] == 30


def test_r_ignores_setup_kickoff_reflection_and_hurried_replies():
    noise = ([reply(kind="decision", **LOW)] * 40 + [reply(kind="reflection", **LOW)] * 40
             + [reply(hurried=True, **LOW)] * 40)
    rec = record(noise + [reply(**GOOD)] * 30)
    out, host = _r(rec, "HostA")
    assert host["state"] == "clear" and host["low"] == 0 and host["eligible"] == 30
    # an easy zero-token setup reply never counts towards R
    out, host = _r(record([reply(kind="decision", tokens=0)] * 60), "HostA")
    assert host["eligible"] == 0 and host["state"] == "warming_up"


def test_r_encrypted_reasoning_and_content_evidence_do_not_trip():
    enc = reply(details=[{"type": "reasoning.encrypted", "data": "opaque-signature"}], tokens=0)
    out, host = _r(record([enc] * 30), "HostA")
    assert host["state"] == "clear"
    talk = reply(content="Moving the thrower first keeps the ball safe from their blitzer.", tokens=0)
    out, host = _r(record([talk] * 30), "HostA")
    assert host["state"] == "clear"


def test_r_is_not_applicable_without_requested_or_listed_reasoning():
    out = health.condition_health([game(1, record([reply(**LOW)] * 40, requested=False))], llm=True)
    assert out["signals"]["R"]["state"] == "not_applicable" and "R" not in out["flags"]
    out = health.condition_health([game(1, record([reply(**LOW)] * 40, listed=False))], llm=True)
    assert out["signals"]["R"]["state"] == "not_applicable"


def test_r_is_per_host_and_an_average_cannot_hide_one_route():
    rec = record([reply(host="HostA", **GOOD)] * 100 + [reply(host="HostB", **LOW)] * 30
                 + [reply(host="HostC", **GOOD)] * 5)
    out = health.condition_health([game(1, rec)], llm=True)
    hosts = {h["host"]: h for h in out["signals"]["R"]["hosts"]}
    assert hosts["HostA"]["state"] == "clear" and hosts["HostB"]["state"] == "flagged"
    assert hosts["HostC"]["state"] == "warming_up" and hosts["HostC"]["eligible"] == 5   # a newly seen host
    pooled = (hosts["HostB"]["low"]) / sum(h["eligible"] for h in hosts.values())
    assert pooled < R["low_share"]                     # the average would have stayed quiet
    assert "R" in out["flags"] and out["signals"]["R"]["flagged_hosts"] == ["HostB"]


def test_r_flag_suggests_one_successor_and_changes_nothing():
    out = health.condition_health([game(1, record([reply(**LOW)] * 30))], llm=True, model_id="nemo")
    s = out["successor"]
    assert s["change"] == {"tool_choice": "auto"} and s["suggested_id"] != "nemo" and s["applied"] is False
    # already on auto: no pre-approved change, only review
    out = health.condition_health([game(1, record([reply(**LOW)] * 30, tool_choice="auto"))], llm=True, model_id="x")
    assert out["successor"]["change"] is None and "review" in out["successor"]["note"].lower()


# ---- P and L: rates over the latest three completed instrumented games ------------------------------------------
def _pl_games(no_tool_last3, truncated_last3=0, oldest_no_tool=0, per_game=100):
    games = []
    for i, (nt, tr) in enumerate([(oldest_no_tool, 0)] + [(n, t) for n, t in zip(no_tool_last3, [truncated_last3, 0, 0])]):
        replies = ([reply(tools=0, finish="stop")] * nt + [reply(tools=0, finish="length")] * tr
                   + [reply(**GOOD)] * (per_game - nt - tr))
        games.append(game(i, record(replies)))
    return games


def test_p_threshold_is_strictly_above_5_percent_of_responses():
    out = health.condition_health(_pl_games([5, 5, 5]), llm=True)         # 15 / 300 = 5.0%
    p = out["signals"]["P"]
    assert p["responses"] == 300 and p["count"] == 15 and p["state"] == "clear"
    out = health.condition_health(_pl_games([5, 5, 6]), llm=True)         # 16 / 300 > 5%
    assert out["signals"]["P"]["state"] == "flagged" and "P" in out["flags"]
    assert out["signals"]["P"]["games"] == ["g1", "g2", "g3"]


def test_p_uses_only_the_latest_three_games():
    out = health.condition_health(_pl_games([0, 0, 0], oldest_no_tool=90), llm=True)
    assert out["signals"]["P"]["state"] == "clear" and out["signals"]["P"]["count"] == 0


def test_p_and_l_are_measured_apart_from_each_other_and_from_bad_arguments():
    replies = ([reply(tools=0, finish="length")] * 20 + [reply(bad_args=1)] * 20 + [reply(tools=3)] * 60)
    games = [game(i, record(replies)) for i in range(3)]
    out = health.condition_health(games, llm=True)
    assert out["signals"]["P"]["count"] == 0 and out["signals"]["P"]["state"] == "clear"
    assert out["signals"]["L"]["count"] == 60 and out["signals"]["L"]["state"] == "flagged"
    assert out["signals"]["P"]["responses"] == 300                # one response per reply, however many tools
    assert out["signals"]["P"]["tool_calls"] == 3 * (20 + 60 * 3)
    assert out["signals"]["P"]["bad_args"] == 60


def test_l_reports_requested_and_observed_ceilings():
    replies = [reply(tools=0, finish="length", completion=4096)] * 10 + [reply(**GOOD)] * 90
    out = health.condition_health([game(i, record(replies)) for i in range(3)], llm=True)
    L = out["signals"]["L"]
    assert L["state"] == "flagged" and L["requested_max_tokens"] == 16384
    assert L["hosts"][0]["truncated_at"] == [4096, 4096]


def test_reflection_replies_do_not_distort_gameplay_rates():
    replies = [reply(**GOOD)] * 100 + [reply(kind="reflection", tools=0, finish="stop")] * 50
    out = health.condition_health([game(i, record(replies)) for i in range(3)], llm=True)
    assert out["signals"]["P"]["responses"] == 300 and out["signals"]["P"]["count"] == 0
    assert out["signals"]["P"]["reflection_responses"] == 150


def test_p_and_l_warm_up_with_fewer_than_three_games_or_no_responses():
    out = health.condition_health([game(1, record([reply(tools=0, finish="stop")] * 50))], llm=True)
    assert out["signals"]["P"]["state"] == "warming_up" and out["signals"]["P"]["games_seen"] == 1
    out = health.condition_health([game(i, record([])) for i in range(3)], llm=True)
    assert out["signals"]["P"]["state"] == "insufficient_data" and out["signals"]["L"]["state"] == "insufficient_data"


def test_p_flags_one_host_hidden_by_the_pool():
    replies = [reply(host="Big", **GOOD)] * 300 + [reply(host="Small", tools=0, finish="stop")] * 10 \
        + [reply(host="Small", **GOOD)] * 30
    out = health.condition_health([game(i, record(replies)) for i in range(3)], llm=True)
    p = out["signals"]["P"]
    assert p["rate"] < health.THRESHOLDS["P"]["rate"]
    assert p["state"] == "flagged" and p["flagged_hosts"] == ["Small"]


# ---- coverage: legacy, partial, baselines -------------------------------------------------------------------------
def test_legacy_games_are_unmeasured_not_healthy_and_still_show_host_mix():
    served = {"fake/model@HostA": 30, "fake/model@HostB": 10}
    out = health.condition_health([game(1, None, served), game(2, {"v": 0}, served)], llm=True)
    assert out["status"] == "unmeasured" and out["flags"] == []
    assert out["coverage"] == {"games": 2, "instrumented": 0, "legacy": 2, "partial": 0}
    assert all(s["state"] == "unknown" for s in out["signals"].values())
    assert [(h["host"], h["calls"]) for h in out["hosts"]] == [("HostA", 60), ("HostB", 20)]
    assert out["hosts"][0]["share"] == 0.75


def test_zero_games_and_baselines():
    assert health.condition_health([], llm=True)["status"] == "unmeasured"
    out = health.condition_health([game(1, record([reply()] * 5))], llm=False)
    assert out["status"] == "not_applicable" and out["flags"] == []


def test_partial_coverage_games_are_left_out_of_the_windows():
    bad = [reply(tools=0, finish="stop")] * 50
    games = [game(i, record([reply(**GOOD)] * 100)) for i in range(3)] + [game(9, record(bad, coverage="partial"))]
    out = health.condition_health(games, llm=True)
    assert out["signals"]["P"]["state"] == "clear" and out["coverage"]["partial"] == 1


def test_mixed_legacy_and_instrumented_history_labels_the_window():
    games = [game(1, None, {"m@HostA": 5})] + [game(i, record([reply(**GOOD)] * 40)) for i in range(2, 5)]
    out = health.condition_health(games, llm=True)
    assert out["status"] == "clear" and out["coverage"]["legacy"] == 1
    assert "instrumented" in out["signals"]["R"]["window_note"]


# ---- S: provider-caused substituted play --------------------------------------------------------------------
def _s_games(*fallbacks):
    return [game(i, record([reply(**GOOD)] * 40, fallback=fb)) for i, fb in enumerate(fallbacks)]


def test_s_needs_provider_fallback_in_two_of_the_latest_three_games():
    out = health.condition_health(_s_games({"llm_error": 20}, {}, {"llm_error": 3}), llm=True)
    assert out["signals"]["S"]["state"] == "flagged" and out["signals"]["S"]["games_with"] == 2
    out = health.condition_health(_s_games({"llm_error": 20}, {"llm_error": 1}, {}, {}), llm=True)
    assert out["signals"]["S"]["state"] == "clear"     # only one of the latest three
    assert health.condition_health(_s_games({"unavailable": 40}, {"llm_error": 2}), llm=True)["signals"]["S"]["state"] == "flagged"
    assert health.condition_health(_s_games({"llm_error": 2}), llm=True)["signals"]["S"]["state"] == "warming_up"


def test_s_is_not_tripped_by_the_models_own_fallbacks():
    own = {"illegal_streak": 30, "truncation_streak": 9, "tool_budget": 4, "turn_time": 12, "reply_deadline": 3,
           "cost_budget": 1, "time_cap": 50}
    out = health.condition_health(_s_games(own, own, own), llm=True)
    assert out["signals"]["S"]["state"] == "clear" and out["signals"]["S"]["games_with"] == 0


def test_s_evidence_keeps_unknown_hosts_unknown():
    rec = record([reply(host="Together", **GOOD)] * 40, fallback={"llm_error": 20},
                 errors=[("rate_limited", None)] * 13 + [("server_error", "BaseTen")])
    out = health.condition_health([game(1, rec), game(2, rec)], llm=True)
    ev = out["signals"]["S"]["games"][-1]
    assert ev["provider_fallback_actions"] == 20
    assert ev["errors"] == {"unknown": {"rate_limited": 13}, "BaseTen": {"server_error": 1}}
    assert "Together" not in ev["errors"]


def test_records_are_bounded_and_json_safe():
    rec = record([reply(host=f"Host{i % 40}", **LOW) for i in range(400)] + [reply(host="H" * 300, **LOW)],
                 errors=[("rate_limited", "H" * 500)] * 3)
    blob = json.dumps(rec)
    assert len(rec["hosts"]) <= health.MAX_HOSTS + 1
    assert all(len(h) <= health.MAX_HOST_CHARS for h in rec["hosts"]) and all(len(h) <= health.MAX_HOST_CHARS for h in rec["errors"])
    assert all(len(h["r_seq"]) <= R["window"] for h in rec["hosts"].values())
    assert len(blob) < 12000


# ---- llm.py: structured, safe error attribution ------------------------------------------------------------
def _mock_llm(handler, **kw):
    import httpx
    from bench.llm import OpenRouterLLM
    llm = OpenRouterLLM("fake/model", "sk-or-v1-SECRETKEY", **kw)
    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


def _run_error(monkeypatch, status, body):
    import anyio
    import httpx
    import bench.llm
    seen = []

    async def no_sleep(_):
        pass
    monkeypatch.setattr(bench.llm.asyncio, "sleep", no_sleep)

    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        seen.append(request)
        return httpx.Response(status, json=body)

    async def go():
        llm = _mock_llm(handler)
        try:
            await llm.chat([{"role": "system", "content": "x"}], [{"type": "function", "function": {"name": "end_turn"}}])
        finally:
            await llm.aclose()
    with pytest.raises(LLMError) as ei:
        anyio.run(go)
    return ei.value, seen


def test_exhausted_429_names_its_host_only_when_the_error_does(monkeypatch):
    body = {"error": {"code": 429, "message": "Rate limit exceeded (Authorization: Bearer sk-or-v1-SECRETKEY)",
                      "metadata": {"provider_name": "BaseTen", "raw": "x" * 5000}}}
    e, seen = _run_error(monkeypatch, 429, body)
    assert e.kind == "rate_limited" and e.status == 429 and e.host == "BaseTen" and not e.fatal
    assert len(seen) == 5                                 # retried, then given up
    e, _ = _run_error(monkeypatch, 429, {"error": {"code": 429, "message": "slow down"}})
    assert e.kind == "rate_limited" and e.host is None
    e, _ = _run_error(monkeypatch, 502, {"error": {"message": "bad gateway"}})
    assert e.kind == "server_error" and e.host is None


def test_public_error_telemetry_has_no_secrets(monkeypatch):
    body = {"error": {"code": 429, "message": "Authorization: Bearer sk-or-v1-SECRETKEY",
                      "metadata": {"provider_name": "Base<script>Ten sk-or-v1-SECRETKEY", "headers": {"x": "y"}}}}
    e, _ = _run_error(monkeypatch, 429, body)
    rec = health.new_record(reasoning_requested=True)
    health.note_error(rec, kind=e.kind, host=e.host)
    blob = json.dumps(rec)
    assert "SECRET" not in blob and "<script>" not in blob and "Bearer" not in blob


# ---- driver integration -------------------------------------------------------------------------------------
class HostLLM:
    """A random player whose replies look like they came from a named host, with chosen reasoning evidence.
    ``fail_every``: every n-th call raises ``error`` instead."""
    reasoning_requested = True
    tool_choice = "required"
    max_tokens = 16384

    def __init__(self, seat, host="BaseTen", reasoning="\n", tokens=1, details=None, fail_every=0, error=None,
                 illegal=False):
        self.policy = RandomPolicy(seat, seed=5, message_rate=0)
        self.host, self.reasoning, self.tokens, self.details = host, reasoning, tokens, details or []
        self.fail_every, self.error, self.illegal, self.n = fail_every, error, illegal, 0

    async def chat(self, messages, tools, deadline=None, hurry=False):
        self.n += 1
        if self.fail_every and self.n % self.fail_every == 0:
            raise self.error()
        if self.illegal and len(tools) > 1:
            tc = ToolCall(id=f"x{self.n}", name="take_action", arguments={"action_type": "NOT_AN_ACTION"})
            resp = LLMResponse(tool_calls=[tc], raw_message={"role": "assistant", "content": "", "tool_calls": [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}]})
        else:
            resp = await self.policy.chat(messages, tools)
        resp.provider, resp.served_model = self.host, "fake/model-1"
        resp.reasoning, resp.reasoning_tokens = self.reasoning, self.tokens
        if self.details:
            resp.raw_message["reasoning_details"] = self.details
        return resp

    async def aclose(self):
        pass


def _play(monkeypatch, match_id="t-trip", settings=None, **kw):
    import bench.match
    real = bench.match.make_llm
    made = []

    def fake_make(cfg, seat, key):
        if cfg.get("provider") != "openrouter":
            return real(cfg, seat, key)
        made.append(HostLLM(seat, **kw))
        return made[-1]
    monkeypatch.setattr(bench.match, "make_llm", fake_make)
    return MatchRunner(match_id, LLM_CFG, RANDOM, {"budget_usd_per_game": 10, **(settings or {})}, api_key="k").run()


def test_a_full_game_records_versioned_per_host_health(monkeypatch):
    result = _play(monkeypatch)
    hs = result["home_stats"]
    rec = hs["health"]
    assert rec["v"] == health.HEALTH_VERSION and rec["coverage"] == "full" and rec["reasoning_requested"] is True
    host = rec["hosts"]["BaseTen"]
    # every model call is either a gameplay response or a reflection: nothing counted twice or lost
    assert host["responses"] + host["reflection"]["responses"] == hs["llm_calls"]
    assert 0 < host["r_eligible"] < host["responses"]     # setup, coin toss and kick-off replies are left out
    assert host["r_seq"] == "1" * min(30, host["r_eligible"])
    assert result["meta"]["health_version"] == health.HEALTH_VERSION
    assert result["meta"]["provider_fallback"] == {"home": 0, "away": 0}
    out = health.condition_health([game(1, rec)], llm=True)
    flagged = host["r_eligible"] >= 30
    assert (out["signals"]["R"]["state"] == "flagged") == flagged
    assert result["away_stats"]["health"]["hosts"] == {}   # the random baseline makes no model calls


def test_encrypted_reasoning_keeps_a_route_clear(monkeypatch):
    result = _play(monkeypatch, reasoning="", tokens=0, details=[{"type": "reasoning.encrypted", "data": "sig"}])
    host = result["home_stats"]["health"]["hosts"]["BaseTen"]
    assert host["r_low"] == 0 and set(host["r_seq"]) <= {"0"}


def test_provider_429s_are_attributed_as_substituted_play(monkeypatch):
    err = lambda: LLMError("OpenRouter HTTP 429: rate limited", kind="rate_limited", status=429, host=None)
    result = _play(monkeypatch, host="Together", fail_every=6, error=err)
    hs, meta = result["home_stats"], result["meta"]
    rec = hs["health"]
    assert hs["llm_errors"] > 0 and rec["errors"] == {"unknown": {"rate_limited": hs["llm_errors"]}}
    assert rec["fallback"].get("llm_error", 0) > 0
    assert meta["provider_fallback"]["home"] == rec["fallback"]["llm_error"] > 0
    assert meta["admissible"] is True                   # current admissibility is preserved, contamination shown apart
    assert hs["forced_actions"] >= rec["fallback"]["llm_error"]


def test_illegal_calls_lead_to_fallback_that_is_not_provider_caused(monkeypatch):
    result = _play(monkeypatch, illegal=True)
    rec = result["home_stats"]["health"]
    assert rec["fallback"].get("illegal_streak", 0) > 0
    assert result["meta"]["provider_fallback"]["home"] == 0 and rec["errors"] == {}


def test_deadline_cutoffs_are_time_budget_not_provider(monkeypatch):
    err = lambda: LLMError("turn time limit reached", deadline=True, uncertain_spend=True)
    result = _play(monkeypatch, fail_every=4, error=err, settings={"last_chance_seconds": 0, "reply_time_limit": 0})
    rec = result["home_stats"]["health"]
    assert rec["fallback"].get("turn_time", 0) > 0 and rec["fallback"].get("llm_error", 0) == 0
    assert result["meta"]["provider_fallback"]["home"] == 0 and rec["errors"] == {}


def test_malformed_arguments_are_counted_per_tool_call():
    import anyio
    from bench.driver import DriverLimits, SeatDriver
    from bench.session import GameSession

    class OneShot:
        reasoning_requested = True

        async def chat(self, messages, tools, deadline=None, hurry=False):
            return LLMResponse(provider="HostA", tool_calls=[
                ToolCall(id="a", name="get_state", arguments={}, parse_error="could not parse arguments"),
                ToolCall(id="b", name="get_state", arguments={})],
                raw_message={"role": "assistant", "content": ""})

        async def aclose(self):
            pass
    s = GameSession("t-args", "A", "B", seed=1)
    d = SeatDriver(s, s.home, OneShot(), "A", DriverLimits(), opponent_name="B")
    resp = anyio.run(d._call_llm, [{"role": "user", "content": "x"}], [], "turn-1-1", 0)
    d._note_health(resp, kind="turn", hurried=False)
    h = d.usage["health"]["hosts"]["HostA"]
    assert h["responses"] == 1 and h["tool_calls"] == 2 and h["bad_args"] == 1


def test_resume_restores_health_without_double_counting(monkeypatch, tmp_path):
    import pickle
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import bench.match
    real = bench.match.make_llm
    monkeypatch.setattr(bench.match, "make_llm", lambda cfg, seat, key: HostLLM(seat) if cfg.get("provider") == "openrouter"
                        else real(cfg, seat, key))
    checkpoints = []
    first = MatchRunner("t-trip-resume", LLM_CFG, RANDOM, {"budget_usd_per_game": 10}, api_key="k",
                        checkpoint_sink=lambda st: checkpoints.append(pickle.dumps(st))).run()
    cp = pickle.loads(checkpoints[len(checkpoints) // 2])
    saved = cp["drivers"]["home"]["usage"]["health"]
    assert saved["v"] == health.HEALTH_VERSION and saved["hosts"]["BaseTen"]["responses"] > 0
    resumed = MatchRunner("t-trip-resume", LLM_CFG, RANDOM, {"budget_usd_per_game": 10}, api_key="k", resume=cp)
    restored = resumed.drivers["home"].usage["health"]
    assert restored == saved and restored is not saved
    result = resumed.run()
    rec = result["home_stats"]["health"]
    assert rec["coverage"] == "full"
    h = rec["hosts"]["BaseTen"]
    assert h["responses"] + h["reflection"]["responses"] == result["home_stats"]["llm_calls"]
    # the first-window order survives the restart: the saved prefix is kept as is
    assert h["r_seq"].startswith(saved["hosts"]["BaseTen"]["r_seq"])
    assert first["home_stats"]["health"]["v"] == health.HEALTH_VERSION


def test_resume_from_a_checkpoint_without_health_marks_partial_coverage(monkeypatch, tmp_path):
    import pickle
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    checkpoints = []
    MatchRunner("t-trip-legacy", LLM_CFG | {"provider": "scripted"}, RANDOM, {},
                checkpoint_sink=lambda st: checkpoints.append(pickle.dumps(st))).run()
    cp = pickle.loads(checkpoints[len(checkpoints) // 2])
    for side in ("home", "away"):
        cp["drivers"][side]["usage"].pop("health")       # a checkpoint written before instrumentation
    result = MatchRunner("t-trip-legacy", LLM_CFG | {"provider": "scripted"}, RANDOM, {}, resume=cp).run()
    assert result["home_stats"]["health"]["coverage"] == "partial"


# ---- holds: a persistent scheduling hold, not enabled:false --------------------------------------------------
@pytest.fixture
def held_bench(bench_env):
    import bench.scheduler as sch
    b = sch.Bench()
    b.sync_models(force=True)
    return b


def _queue():
    return [(r["id"], r["status"], r["seq"], r["tournament_id"]) for r in
            db.rows("SELECT id, status, seq, tournament_id FROM matches ORDER BY tournament_id, seq")]


def test_hold_preserves_the_queue_and_skips_only_the_held_condition(held_bench, bench_env):
    b = held_bench
    before = _queue()
    assert b.place_hold("random-baseline", signal="S", placed_by="admin", evidence={"note": "test"})
    b.sync_models(force=True)                                       # a reload must not cancel anything
    assert _queue() == before
    assert db.row("SELECT enabled FROM models WHERE id='random-baseline'")["enabled"] == 1
    assert "random-baseline" in b.held_models()
    m = b.next_match()
    assert m is not None and "random-baseline" not in (m["home_model"], m["away_model"])
    first_held = db.row("SELECT * FROM matches WHERE status='queued' AND (home_model='random-baseline' OR "
                        "away_model='random-baseline') ORDER BY seq LIMIT 1")
    b.play(m)                                                       # unaffected games carry on
    assert db.row("SELECT status FROM matches WHERE id=?", (m["id"],))["status"] == "completed"
    assert db.row("SELECT status, seq FROM matches WHERE id=?", (first_held["id"],)) == \
        {"status": "queued", "seq": first_held["seq"]}
    # an explicit play() of a held fixture is refused, not run
    assert b.play(first_held).get("skipped")
    assert b.release_hold("random-baseline", released_by="admin")
    assert not b.held_models() and not b.release_hold("random-baseline")
    order = [r["id"] for r in db.rows("SELECT m.id FROM matches m JOIN tournaments t ON t.id=m.tournament_id "
                                      "WHERE m.status='queued' ORDER BY t.created_at, t.id, m.seq")]
    assert b.next_match()["id"] == order[0]


def test_all_held_is_reported_honestly_not_idle(held_bench):
    b = held_bench
    for mid in ("random-baseline", "random-two"):
        b.place_hold(mid, placed_by="admin")
    q = b.queue_state()
    assert q["queued"] > 0 and q["playable"] == 0 and q["held"] == q["queued"]
    assert b.next_match() is None
    status, detail = b.idle_status()
    assert status == "held" and "release" in detail.lower()
    assert "Random baseline" in detail and "Random two" in detail
    b.release_hold("random-two")
    assert b.next_match() is not None and b.idle_status()[0] == "ready"


def test_a_running_game_finishes_when_a_hold_lands_mid_game(held_bench, monkeypatch):
    b = held_bench
    import bench.scheduler as sch
    real_run = sch.MatchRunner.run

    def run_and_hold(self):
        b.place_hold(self.home_cfg["id"], placed_by="admin")
        return real_run(self)
    monkeypatch.setattr(sch.MatchRunner, "run", run_and_hold)
    m = b.next_match()
    b.play(m)
    assert db.row("SELECT status FROM matches WHERE id=?", (m["id"],))["status"] == "completed"
    assert m["home_model"] in b.held_models()


def test_holds_survive_a_restart_and_the_table_migrates(held_bench, monkeypatch, tmp_path):
    import bench.scheduler as sch
    held_bench.place_hold("random-two", placed_by="admin", evidence={"games": ["a"]})
    monkeypatch.setattr(sch, "_bench", None)
    b2 = sch.Bench()
    hold = b2.held_models()["random-two"]
    assert hold["placed_by"] == "admin" and hold["evidence"] == {"games": ["a"]} and hold["signal"] == "S"
    # an old database gains the table; running the migration twice is harmless
    import sqlite3
    path = str(tmp_path / "old.db")
    sqlite3.connect(path).executescript("CREATE TABLE matches (id TEXT PRIMARY KEY, tournament_id INTEGER, seq INTEGER, "
                                        "home_model TEXT, away_model TEXT, status TEXT, created_at REAL);").close()
    db.init(path)
    db.init(path)
    assert db.row("SELECT name FROM sqlite_master WHERE name='health_holds'")


def _insert_game(mid, home, away, finished, home_health=None, away_health=None, served=None):
    st = lambda rec: json.dumps({"tool_calls": 10, "turns": 16, "health": rec} if rec is not None else
                                {"tool_calls": 10, "turns": 16})
    meta = {"harness": {"protocol_version": "1.7"}, "served": served or {"home": {}, "away": {}}}
    tid = db.row("SELECT MIN(id) AS t FROM tournaments")["t"]
    db.execute("INSERT INTO matches (id, tournament_id, seq, home_model, away_model, status, home_score, away_score, "
               "winner, home_stats, away_stats, created_at, finished_at, meta, admissible) VALUES "
               "(?,?,?,?,?, 'completed', 1, 0, 'home', ?, ?, ?, ?, ?, 1)",
               (mid, tid, 900, home, away, st(home_health), st(away_health), finished, finished, json.dumps(meta)))


def _llm_env(bench_env, extra=""):
    bench_env.write_text(f"""
settings:
  pause_between_matches: 0
models:
  - name: Random baseline
    provider: random
  - name: Random two
    provider: random
  - name: Fake LLM
    model: fake/model
  - name: Claude Test
    model: anthropic/claude-test
  - name: Reasoner (reasoning)
    model: fake/reasoner
    tool_choice: auto
    extra:
      reasoning:
        effort: high
{extra}""")


@pytest.fixture
def llm_bench(bench_env):
    _llm_env(bench_env)
    import bench.scheduler as sch
    b = sch.Bench()
    b.sync_models(force=True)
    return b


def test_s_places_an_automatic_hold_only_from_new_instrumented_evidence(llm_bench, bench_env):
    b = llm_bench
    yaml_before = bench_env.read_text()
    cfg_before = db.row("SELECT config, enabled FROM models WHERE id='fake-llm'")
    queue_before = _queue()
    # plenty of legacy fallback, no instrumentation: never a retrospective hold
    for i in range(4):
        _insert_game(f"old{i}", "fake-llm", "random-baseline", 100 + i)
    b.check_health_holds(["fake-llm"])
    assert "fake-llm" not in b.held_models()
    bad = record([reply(**GOOD)] * 40, fallback={"llm_error": 20}, errors=[("rate_limited", None)] * 13)
    _insert_game("new1", "fake-llm", "random-baseline", 200, home_health=bad)
    b.check_health_holds(["fake-llm"])
    assert "fake-llm" not in b.held_models()                          # one game is not sustained
    _insert_game("new2", "random-baseline", "fake-llm", 201, away_health=bad)
    b.check_health_holds(["fake-llm", "random-baseline"])
    hold = b.held_models()["fake-llm"]
    assert hold["placed_by"] == "auto" and hold["signal"] == "S" and set(hold["evidence"]["games"]) == {"new1", "new2"}
    assert "random-baseline" not in b.held_models()                   # baselines are never held
    # nothing else moved: settings, enablement, queue
    assert bench_env.read_text() == yaml_before
    assert db.row("SELECT config, enabled FROM models WHERE id='fake-llm'") == cfg_before
    assert [q for q in _queue() if not q[0].startswith(("old", "new"))] == queue_before
    # a release is not undone by the same evidence: only games after the release count
    b.release_hold("fake-llm", released_by="admin")
    b.check_health_holds(["fake-llm"])
    assert "fake-llm" not in b.held_models()
    _insert_game("new3", "fake-llm", "random-two", time.time() + 10, home_health=bad)
    _insert_game("new4", "fake-llm", "random-two", time.time() + 11, home_health=bad)
    b.check_health_holds(["fake-llm"])
    assert "fake-llm" in b.held_models()


def test_r_p_l_flags_never_change_settings_placements_or_holds(llm_bench, bench_env):
    b = llm_bench
    yaml_before = bench_env.read_text()
    models_before = db.rows("SELECT * FROM models ORDER BY id")
    tournaments_before = db.rows("SELECT id, kind, status FROM tournaments ORDER BY id")
    awful = record([reply(**LOW)] * 30 + [reply(tools=0, finish="stop")] * 30 + [reply(tools=0, finish="length")] * 30)
    for i in range(3):
        _insert_game(f"rpl{i}", "fake-llm", "random-baseline", 300 + i, home_health=awful)
    out = b.condition_health("fake-llm")
    assert {"R", "P", "L"} <= set(out["flags"]) and "S" not in out["flags"]
    b.check_health_holds(["fake-llm"])
    b.sync_models(force=True)
    assert not b.held_models()
    assert bench_env.read_text() == yaml_before
    assert db.rows("SELECT * FROM models ORDER BY id") == models_before
    assert db.rows("SELECT id, kind, status FROM tournaments ORDER BY id") == tournaments_before


# ---- web: API, pages and admin release ----------------------------------------------------------------------
@pytest.fixture
def seeded_app(llm_bench, monkeypatch):
    from bench import model_info
    monkeypatch.setattr(model_info, "catalog", lambda: {})
    good = record([reply(host="HostA", **GOOD)] * 40)
    flagged = record([reply(host="HostA", **GOOD)] * 40 + [reply(host="HostB", **LOW)] * 30)
    for i in range(3):
        _insert_game(f"ok{i}", "fake-llm", "random-baseline", 400 + i, home_health=good,
                     served={"home": {"fake/model@HostA": 40}, "away": {}})
        _insert_game(f"fl{i}", "random-two", "claude-test", 500 + i, away_health=flagged,
                     served={"home": {}, "away": {"anthropic/claude-test@HostA": 40, "anthropic/claude-test@HostB": 30}})
    _insert_game("warm", "reasoner-reasoning", "random-two", 600, home_health=record([reply(**GOOD)] * 10))
    _insert_game("legacy", "random-baseline", "fake-llm", 50, served={"home": {}, "away": {"fake/model@Old": 9}})
    from bench.web import create_app
    return create_app(start_scheduler=False).test_client()


def test_health_reaches_the_api_for_each_case(seeded_app):
    c = seeded_app
    lb = {m["id"]: m for m in c.get("/api/leaderboard").get_json()["models"]}
    assert lb["fake-llm"]["health"]["status"] == "clear" and lb["fake-llm"]["health"]["flags"] == []
    assert lb["claude-test"]["health"]["status"] == "flagged" and lb["claude-test"]["health"]["flags"] == ["R"]
    assert lb["reasoner-reasoning"]["health"]["status"] == "warming_up"
    assert lb["random-baseline"]["health"]["status"] == "not_applicable"
    d = c.get("/api/models/claude-test").get_json()["health"]
    assert d["signals"]["R"]["flagged_hosts"] == ["HostB"]
    assert {h["host"] for h in d["hosts"]} == {"HostA", "HostB"}
    assert d["thresholds"] == health.THRESHOLDS and d["version"] == health.HEALTH_VERSION
    legacy = c.get("/api/models/fake-llm").get_json()["health"]
    assert legacy["coverage"]["legacy"] == 1 and legacy["coverage"]["instrumented"] == 3
    allh = c.get("/api/health").get_json()
    assert allh["thresholds"] == health.THRESHOLDS and allh["holds"] == []
    assert allh["conditions"]["claude-test"]["flags"] == ["R"]
    listing = {m["id"]: m for m in c.get("/api/models").get_json()["models"]}
    assert listing["claude-test"]["health"]["flags"] == ["R"]


def test_unmeasured_condition_reads_unknown_not_healthy(llm_bench, monkeypatch):
    from bench import model_info
    from bench.web import create_app
    monkeypatch.setattr(model_info, "catalog", lambda: {})
    _insert_game("legacy-only", "fake-llm", "random-baseline", 10, served={"home": {"fake/model@Old": 9}, "away": {}})
    c = create_app(start_scheduler=False).test_client()
    h = c.get("/api/models/fake-llm").get_json()["health"]
    assert h["status"] == "unmeasured" and h["hosts"][0]["host"] == "Old"


def test_admin_release_needs_the_admin_token(seeded_app, monkeypatch):
    from bench.scheduler import get_bench
    c = seeded_app
    get_bench().place_hold("fake-llm", placed_by="auto", evidence={"games": ["ok1"]})
    url = "/api/admin/health-holds/fake-llm/release"
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert c.post(url, headers={"Authorization": "Bearer anything"}).status_code == 403
    monkeypatch.setenv("ADMIN_TOKEN", "tok-123")
    assert c.post(url).status_code == 403
    assert c.post(url, headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert c.get(url).status_code == 405
    assert "fake-llm" in get_bench().held_models()
    assert c.get("/api/health").get_json()["holds"][0]["model_id"] == "fake-llm"
    lb = {m["id"]: m for m in c.get("/api/leaderboard").get_json()["models"]}
    assert lb["fake-llm"]["health"]["hold"]["placed_by"] == "auto"
    r = c.post(url, headers={"Authorization": "Bearer tok-123"}, json={"note": "provider recovered"})
    assert r.status_code == 200 and r.get_json()["released"] is True
    assert "fake-llm" not in get_bench().held_models()
    assert c.post(url, headers={"Authorization": "Bearer tok-123"}).status_code == 404
    assert c.post("/api/admin/health-holds/fake-llm", headers={"Authorization": "Bearer tok-123"}).status_code == 200
    assert c.post("/api/admin/health-holds/nope", headers={"Authorization": "Bearer tok-123"}).status_code == 404
    assert c.post("/api/admin/health-holds/fake-llm").status_code == 403


def test_status_marks_held_fixtures(seeded_app):
    from bench.scheduler import get_bench
    get_bench().place_hold("random-two", placed_by="admin")
    q = seeded_app.get("/api/status").get_json()["queued"]
    held = [m for m in q if not m.get("tbc") and "random-two" in (m["home_model"], m["away_model"])]
    assert held and all(m["held"] for m in held)
    assert not any(m.get("held") for m in q if not m.get("tbc") and "random-two" not in (m["home_model"], m["away_model"]))


def test_pages_render_health(seeded_app):
    c = seeded_app
    page = c.get("/models/claude-test").get_data(as_text=True)
    assert 'id="health"' in page
    lb = c.get("/leaderboard").get_data(as_text=True)
    assert "health" in lb.lower()


def test_about_page_explains_the_implemented_policy(seeded_app):
    html = seeded_app.get("/about").get_data(as_text=True)
    text = " ".join(html.split())
    for needle in ["180 min", "since the last successful game action", "Bot Bowl scripted bot",
                   "16,384", "300 s", "120 s", "45 s",
                   f"first {R['window']}", f"{round(R['low_share'] * 100)}%", f"fewer than {R['min_reasoning_tokens']} reasoning tokens",
                   "5%", "latest 3", "2 of the latest 3", "host mix",
                   "tool_choice: required", "Claude Test", "Reasoner (reasoning)",
                   "Illustrative", "release", "/api/health"]:
        assert needle in text, needle
    assert "Proposal" not in text and "not deployed" not in text.lower()
    assert "\u2014" not in html.split("<main")[1]                    # no em dashes in the new page body
    assert 'href="/matches"' in html and 'href="/data"' in html and 'href="/leaderboard"' in html
    assert "/bench-static/about.css" in html and "/bench-static/about.js" in html


def test_new_files_have_no_em_dashes():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for rel in ("bench/health.py", "bench/templates/about.html", "bench/static/about.css", "bench/static/about.js",
                "bench/static/health.js", "bench/docs/HEALTH.md"):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            assert "\u2014" not in f.read(), rel


def test_about_is_the_only_page_loading_its_assets(seeded_app):
    for url in ("/", "/leaderboard", "/matches", "/data"):
        html = seeded_app.get(url).get_data(as_text=True)
        assert "about.css" not in html and "about.js" not in html, url
