import json
import os
import re
import threading
import time

import pytest

from bench import db, ratings, render
from bench.match import MatchRunner
from bench.session import GameSession, fallback_action
from bench.tools import SeatTools


RANDOM = {"name": "Random", "provider": "random", "seed": 1}
SCRIPTED = {"name": "Scripted", "provider": "scripted", "seed": 2}


def test_random_vs_scripted_match_completes():
    result = MatchRunner("t1", SCRIPTED, RANDOM, {"max_tool_calls_per_turn": 60}).run()
    assert result["error"] is None
    assert result["winner"] in ("home", "away", "draw")
    hs, as_ = result["home_stats"], result["away_stats"]
    assert hs["turns"] >= 8 and as_["turns"] >= 8
    assert hs["tool_calls"] > 0 and as_["tool_calls"] > 0
    assert hs["score"] == result["home_score"]
    for key in ("aggression", "risk_taking", "passing_game", "chattiness", "illegal_rate", "monitoring_rate",
                "safe_first_rate", "turnover_causes", "reflection_coverage"):
        assert key in hs
    assert hs["actions_logged"] > 0 and hs["reflections"] > 0
    for st in (hs, as_):
        assert sum(st["turnover_causes"].values()) == st.get("turnovers", 0)
        assert st["turnovers_logged"] == st.get("turnovers", 0)   # every turnover attributed to an action
    assert result["meta"]["admissible"] and not result["meta"]["harness_errors"]
    for st in (hs, as_):   # re-roll and other optional choices are counted for every seat
        assert st["rerolls_available"] > 0 and st["rerolls_spent_share"] <= 1 and st["rerolls_spent_share"] is not None
        for kind in ("reroll", "block_reroll", "apothecary", "skill"):
            assert 0 <= st[f"{kind}_taken"] <= st[f"{kind}_offers"]
        assert st["reroll_taken"] + st["block_reroll_taken"] == st.get("rerolls_used", 0)
    assert hs["reroll_offers"] + hs["block_reroll_offers"] + as_["reroll_offers"] + as_["block_reroll_offers"] > 0


def test_choice_take_rates_pool_offers_across_games():
    ms = [{"home_model": "a", "away_model": "b", "winner": "home", "home_score": 1, "away_score": 0,
           "home_stats": {"reroll_offers": 1, "reroll_taken": 1, "rerolls_spent_share": 0.5, "skill_rerolls": 2},
           "away_stats": {}},
          {"home_model": "a", "away_model": "b", "winner": "home", "home_score": 1, "away_score": 0,
           "home_stats": {"reroll_offers": 3, "reroll_taken": 0, "rerolls_spent_share": 0.0, "skill_rerolls": 0},
           "away_stats": {}}]
    a = ratings.aggregate(ms)["a"]
    assert a["style"]["reroll_take_rate"] == 0.25          # 1 of 4 offers, not the mean of 100% and 0%
    assert a["style"]["block_reroll_take_rate"] is None     # never offered
    assert a["style"]["rerolls_spent_share"] == 0.25 and a["style"]["skill_rerolls"] == 1
    assert ratings.aggregate(ms)["b"]["style"]["reroll_take_rate"] is None   # games from before tracking



def test_botbowl_scripted_bot_plays_through_the_tools():
    bot = {"name": "Bot Bowl scripted", "provider": "botbowl", "bot": "scripted",
           "module": "examples.scripted_bot_example", "seed": 3}
    result = MatchRunner("t1b", bot, RANDOM, {"max_tool_calls_per_turn": 200}).run()
    assert result["error"] is None
    hs = result["home_stats"]
    assert hs["illegal_actions"] == 0 and hs["forced_actions"] == 0
    assert result["home_score"] > result["away_score"]
    assert result["meta"]["admissible"]

def _first_turn_session():
    """Play until the home team has a regular turn with players on the pitch; return (session, tools)."""
    s = GameSession("t2", "A", "B", seed=11)
    ready = threading.Event()

    def other(seat):
        while True:
            d = seat.wait_for_decision()
            if d is None:
                return
            if seat is s.home and d.our_turn and d.proc == "Turn":
                ready.set()
                return
            seat.submit(fallback_action(s.game, seat.team))

    threads = [threading.Thread(target=other, args=(x,), daemon=True) for x in (s.home, s.away)]
    s.start()
    for t in threads:
        t.start()
    assert ready.wait(30)
    return s, SeatTools(s, s.home), threads


def test_state_text_and_illegal_inputs():
    s, tools, threads = _first_turn_session()
    try:
        txt = tools.get_state()
        assert "YOUR TURN" in txt and "YOUR PLAYERS" in txt and "choose a player to activate" in txt
        assert render.board_text(s.game).count("\n") == s.game.arena.height - 2
        assert tools.move("Z9", 1, 1).startswith("ILLEGAL")
        opp = s.game.get_players_on_pitch(s.away.team)[0]
        assert "not one of your players" in tools.move(render.pid(opp), 1, 1)
        assert tools.take_action("NOT_AN_ACTION").startswith("ILLEGAL")
        assert "Message delivered" in tools.send_message("good luck!")
        assert "Message delivered" in tools.send_message("you'll need it")
        assert "limit" in tools.send_message("third message")
        assert s.unread_messages(s.away) == ["good luck!", "you'll need it"]
        mine = [p for p in s.game.get_players_on_pitch(s.home.team) if p.state.up]
        assert "Reachable" in tools.get_player(render.pid(mine[0]))
        # ending the turn requires the structured reflection
        assert tools.end_turn().startswith("ILLEGAL")
        assert "Use the end_turn tool" in tools.take_action("END_TURN")
        assert "notes" in tools.end_turn(plan="Push up the left wing.", prediction="They will blitz H1.")
        out = tools.end_turn(plan="Push up the left wing.", prediction="They will blitz H1.",
                             notes="Dodging out of a tackle zone needs a roll.")
        assert not out.startswith("ILLEGAL")
        assert s.reflections and s.reflections[0]["plan"] == "Push up the left wing."
        assert s.home.notes == "Dodging out of a tackle zone needs a roll."
    finally:
        s.abort()


def test_notebook_is_private_capped_and_shown_at_every_decision():
    from bench.driver import DriverLimits, SeatDriver
    from bench.session import NOTES_MAX_CHARS
    s, tools, threads = _first_turn_session()
    try:
        d = SeatDriver(s, s.home, llm=None, display_name="A", limits=DriverLimits())
        assert "YOUR NOTEBOOK: (empty)" in d._intro(s.home.decision)
        assert tools.update_notes("").startswith("ILLEGAL")
        assert tools.update_notes("Blitz once per turn.") == "Notebook saved."
        # 'unchanged' keeps the notebook; an identical rewrite is not counted as an update
        assert tools.reflect("Score next.", "They blitz.", "unchanged") == "Reflection recorded."
        assert tools.update_notes("Blitz once per turn.") == "Notebook saved."
        assert s.home.notes == "Blitz once per turn." and s.home.counters["notes_updates"] == 1
        assert s.away.notes == ""
        intro = d._intro(s.home.decision)
        assert "YOUR NOTEBOOK:\nBlitz once per turn." in intro
        assert "YOUR LAST PLAN (half 1, turn" in intro and "Score next." in intro and "They blitz." in intro
        out = tools.update_notes("x" * (NOTES_MAX_CHARS + 50))
        assert "cut" in out and len(s.home.notes) == NOTES_MAX_CHARS
        # the notebook survives a checkpoint
        state = s.state_dict()
        assert state["seats"]["home"]["notes"] == s.home.notes
    finally:
        s.abort()


def test_push_and_follow_up_name_their_squares():
    from botbowl.core.model import ActionChoice, Square
    from botbowl.core.table import ActionType, OutcomeType
    from botbowl.core.procedure import Outcome
    s, tools, threads = _first_turn_session()
    try:
        g = s.game
        attacker = [p for p in g.get_players_on_pitch(s.home.team) if p.state.up][0]
        defender = g.get_players_on_pitch(s.away.team)[0]
        # an auto-resolved push leaves the model only this line, so it has to say where the player went
        assert render.report_text(g, Outcome(OutcomeType.PUSHED, player=defender, position=Square(11, 7))) \
            == f"{render.pid(defender)} was pushed to (11,7)."
        assert render.report_text(g, Outcome(OutcomeType.FOLLOW_UP, player=attacker, position=Square(10, 6))) \
            == f"{render.pid(attacker)} follows up to (10,6)."
        empty = next(Square(x, y) for x in range(1, g.arena.width - 1) for y in range(1, g.arena.height - 1)
                     if g.get_player_at(Square(x, y)) is None)
        g.state.available_actions = [ActionChoice(ActionType.FOLLOW_UP, team=s.home.team,
                                                  positions=[attacker.position, empty])]
        txt = render.legal_actions_text(g, s.home.team)
        assert f"{render.sq(attacker.position)}=stay {render.sq(empty)}=follow up" in txt
    finally:
        s.abort()


@pytest.mark.parametrize("stream", [False, True])
def test_openrouter_driver_against_fake_api(fake_openrouter, stream):
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 12}
    events = []
    runner = MatchRunner("t3", cfg, RANDOM, {"budget_usd_per_game": 10, "stream": stream},
                         api_key="test-key", event_sink=lambda *a: events.append(a))
    result = runner.run()
    assert result["error"] is None
    hs = result["home_stats"]
    assert hs["llm_calls"] > 10
    assert hs["prompt_tokens"] == 1000 * hs["llm_calls"]
    assert abs(hs["cost"] - 0.0001 * hs["llm_calls"]) < 1e-6
    assert hs["invalid_tool_calls"] > 0        # malformed JSON args are counted
    assert hs["no_tool_replies"] > 0          # text-only replies are nudged
    assert hs["messages_sent"] > 0
    kinds = {e[2] for e in events}
    assert {"episode", "tool", "message", "thought"} <= kinds
    assert hs["cached_tokens"] == 600 * hs["llm_calls"] and hs["cache_hit_rate"] == 0.6
    assert hs["served"] == {"fake/model-20260901@FakeCloud": hs["llm_calls"]}
    assert hs["reflections"] > 0
    # the notebook written at end_turn comes back in the next decision's prompt
    assert hs["notes_updates"] > 0 and "notes" in kinds
    assert 0 < hs["notes_shown"] <= hs["episodes"] and 0 < hs["notes_shown_rate"] <= 1
    assert hs["notes_update_rate"] > 0 and hs["notes_chars"] > 0
    from bench.ratings import aggregate
    agg = aggregate([{"home_model": "fake", "away_model": "rnd", "winner": "draw", "home_score": 0, "away_score": 0,
                      "home_stats": hs, "away_stats": result["away_stats"]}])["fake"]
    assert agg["sums"]["notes_updates"] == hs["notes_updates"]
    assert agg["style"]["notes_shown_rate"] == hs["notes_shown_rate"]
    intros = [json.dumps(m["content"]) for b in fake_openrouter.bodies for m in b["messages"] if m["role"] == "user"]
    assert any("YOUR NOTEBOOK:\\nTurn note " in i for i in intros)
    assert result["meta"]["seed"] == runner.seed and result["meta"]["harness"]["prompt_fingerprint"]
    assert all(b.get("stream", False) == stream for b in fake_openrouter.bodies)
    # streamed replies are put back together exactly: reasoning blocks and tool calls go back to the model whole
    sent_back = [m for b in fake_openrouter.bodies for m in b["messages"] if m["role"] == "assistant"]
    assert sent_back and all(re.fullmatch(r"sig-\d+", d["data"]) for m in sent_back for d in m.get("reasoning_details", []))
    assert all(tc["id"] and tc["function"]["name"] for m in sent_back for tc in m.get("tool_calls", []))


def test_transcript_reconstructs_conversation(fake_openrouter, tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from bench.transcript import read_transcript
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model"}
    result = MatchRunner("t5", cfg, RANDOM, {}, api_key="k", transcript=True).run()
    recs = list(read_transcript("t5"))
    assert recs[0]["type"] == "meta" and recs[-1]["type"] == "result"
    types = {r["type"] for r in recs}
    assert {"episode", "llm_call", "tool", "action", "reflection"} <= types
    # rebuild each home episode: episode.messages + (new_messages + response.message) per call
    convo = {}
    for r in recs:
        if r["side"] != "home":
            continue
        if r["type"] == "episode":
            convo[r["episode"]] = list(r["messages"])
        elif r["type"] == "llm_call" and r["episode"] in convo:
            convo[r["episode"]] += r["new_messages"] + [r["response"]["message"]]
    for msgs in convo.values():
        assert msgs[0]["role"] == "system"
        # every tool result answers a tool call made earlier in the same conversation
        ids = set()
        for m in msgs:
            for tc in m.get("tool_calls") or []:
                ids.add(tc["id"])
            if m["role"] == "tool":
                assert m["tool_call_id"] in ids
    assert result["meta"]["transcript_records"] == len(recs) - 1  # the result line is written last


def test_openrouter_auth_failure_is_infra_error(fake_openrouter):
    fake_openrouter.status = 401
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model"}
    result = MatchRunner("t4", cfg, RANDOM, {}, api_key="bad").run()
    assert result["infra_error"] and "401" in result["infra_error"]


def test_length_truncation_is_not_counted_as_no_tool(fake_openrouter):
    """finish_reason=length with no tools is output truncation, not a no-tool refusal."""
    fake_openrouter.length_truncations_left = 2
    fake_openrouter.length_truncation_mode = "main"
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 20}
    result = MatchRunner("t-trunc", cfg, RANDOM, {"budget_usd_per_game": 10}, api_key="k").run()
    hs = result["home_stats"]
    assert hs["output_truncations"] >= 2
    # text-only n%11 nudges still exist and must stay on no_tool_replies
    assert hs["no_tool_replies"] > 0
    # the two length replies must not have been double-counted as no_tool
    assert hs["no_tool_replies"] + hs["output_truncations"] <= hs["llm_calls"]
    assert result["error"] is None


def test_length_truncation_streak_auto_finishes_without_unlimited_retries(fake_openrouter):
    """Bounded truncation streak ends the episode; no unlimited retries."""
    fake_openrouter.length_truncations_left = 50
    fake_openrouter.length_truncation_mode = "main"
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model",
           "max_tool_calls_per_turn": 40, "max_tokens": 512}
    events = []
    result = MatchRunner("t-trunc-cap", cfg, RANDOM,
                         {"budget_usd_per_game": 10, "max_illegal_streak": 3, "max_game_minutes": 5},
                         api_key="k", event_sink=lambda *a: events.append(a)).run()
    hs = result["home_stats"]
    assert hs["output_truncations"] >= 3
    assert hs["budget_exhausted"] >= 1
    system_texts = [e[3].get("text", "") for e in events if e[2] == "system"]
    assert any("truncat" in t.lower() for t in system_texts)
    # no unlimited retries: at most max_truncation_streak per episode (+ a small slack)
    assert hs["output_truncations"] <= hs["episodes"] * 3 + 5
    assert result["error"] is None


def test_true_no_tool_replies_still_count_separately(fake_openrouter):
    """finish_reason=stop without tools stays no_tool_replies (not output_truncations)."""
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 12}
    result = MatchRunner("t-notool", cfg, RANDOM, {"budget_usd_per_game": 10}, api_key="k").run()
    hs = result["home_stats"]
    assert hs["no_tool_replies"] > 0
    assert hs.get("output_truncations", 0) == 0


def test_successful_action_resets_truncation_streak(fake_openrouter):
    """A single length truncation then normal tools must not auto-finish on truncation alone."""
    fake_openrouter.length_truncations_left = 1
    fake_openrouter.length_truncation_mode = "main"
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 30}
    events = []
    result = MatchRunner("t-trunc-reset", cfg, RANDOM, {"budget_usd_per_game": 10, "max_illegal_streak": 3},
                         api_key="k", event_sink=lambda *a: events.append(a)).run()
    hs = result["home_stats"]
    assert hs["output_truncations"] >= 1
    assert hs["tool_calls"] > 0
    system_texts = [e[3].get("text", "") for e in events if e[2] == "system"]
    assert not any("truncat" in t.lower() and "auto-finish" in t.lower() for t in system_texts)
    assert result["error"] is None


def test_reflect_length_truncation_is_counted():
    """_missed_reflection: finish_reason=length is output_truncations; no reflect tool is executed."""
    import anyio
    from bench.driver import DriverLimits, SeatDriver
    from bench.llm import LLMResponse
    from bench.session import GameSession

    class TruncLLM:
        async def chat(self, messages, tools, deadline=None):
            return LLMResponse(content="", reasoning="…" * 50, finish_reason="length",
                               raw_message={"role": "assistant", "content": ""})

        async def aclose(self):
            pass

    class BoomClient:
        async def call_tool(self, *a, **k):
            raise AssertionError("reflect must not be called after a length truncation")

    s = GameSession("t-reflect-unit", "A", "B", seed=1)
    d = SeatDriver(s, s.home, TruncLLM(), "A", DriverLimits(), opponent_name="B")
    d._oa_tools = [{"type": "function", "function": {"name": "reflect", "description": "", "parameters": {}}}]
    anyio.run(d._missed_reflection, [{"role": "user", "content": "turn over"}], BoomClient(), "turn-1-1", 0)
    assert d.usage["output_truncations"] == 1
    assert d.usage["no_tool_replies"] == 0


def test_per_model_max_tokens_reaches_openrouter_payload(fake_openrouter):
    """models.yaml max_tokens must land on the OpenRouter request body (not only the default 2048)."""
    cfg = {"name": "Big context", "provider": "openrouter", "model": "fake/model",
           "max_tokens": 8192, "max_tool_calls_per_turn": 6}
    MatchRunner("t-maxtok", cfg, RANDOM, {"max_game_minutes": 5}, api_key="k").run()
    assert fake_openrouter.bodies
    assert all(b.get("max_tokens") == 8192 for b in fake_openrouter.bodies)


def test_models_yaml_does_not_impose_output_caps():
    from bench.config import load_models_file, models_path
    data = load_models_file(models_path())
    for model in data["models"]:
        assert "max_tokens" not in model, model["name"]
        assert "max_tokens" not in model.get("extra", {}), model["name"]
        assert "max_completion_tokens" not in model.get("extra", {}), model["name"]


def test_default_and_null_output_caps_are_omitted_from_payload(fake_openrouter):
    import anyio
    from bench.llm import OpenRouterLLM
    from bench.match import make_llm

    async def exercise():
        clients = [OpenRouterLLM("fake/model", "k")]
        for cfg in ({}, {"max_tokens": None}):
            clients.append(make_llm({"model": "fake/model", **cfg}, None, "k"))
        for client in clients:
            try:
                await client.chat([{"role": "system", "content": "test"}],
                                  [{"type": "function", "function": {"name": "reflect"}}])
            finally:
                await client.aclose()

    anyio.run(exercise)
    assert len(fake_openrouter.bodies) == 3
    for body in fake_openrouter.bodies:
        assert "max_tokens" not in body
        assert "max_completion_tokens" not in body


def test_output_truncations_aggregate_on_leaderboard():
    from bench import ratings
    ms = [{
        "home_model": "a", "away_model": "b", "winner": "home",
        "home_score": 1, "away_score": 0,
        "home_stats": {"output_truncations": 4, "no_tool_replies": 1, "tool_calls": 10, "turns": 16},
        "away_stats": {"output_truncations": 0, "no_tool_replies": 2, "tool_calls": 8, "turns": 16},
    }]
    agg = ratings.aggregate(ms)
    assert agg["a"]["sums"]["output_truncations"] == 4
    assert agg["b"]["sums"].get("output_truncations", 0) == 0
    assert agg["a"]["sums"]["no_tool_replies"] == 1


def test_scheduler_round_robin_placement_and_retire(bench_env):
    from bench.scheduler import Bench, load_frames
    b = Bench()
    assert sorted(b.sync_models(force=True)) == ["random-baseline", "random-two", "scripted-baseline"]
    rr = db.rows("SELECT * FROM tournaments")
    assert len(rr) == 1 and rr[0]["kind"] == "round_robin"
    assert db.row("SELECT COUNT(*) n FROM matches WHERE status='queued'")["n"] == 6   # 3 pairs x 2 legs
    m = b.next_match()
    res = b.play(m)
    assert res["error"] is None
    done = db.decode_match(db.row("SELECT * FROM matches WHERE id=?", (m["id"],)))
    assert done["status"] == "completed" and done["home_stats"]["turns"] > 0
    assert len(load_frames(m["id"])) == done["frames"] > 10
    assert db.get_events(m["id"])

    # adding a model schedules a placement; with only 3 existing models (fewer than placement_size) it plays them all
    time.sleep(0.01)
    bench_env.write_text(bench_env.read_text() + "  - name: Newcomer\n    provider: random\n")
    os.utime(bench_env, (time.time() + 5, time.time() + 5))
    assert b.sync_models() == ["newcomer"]
    g = db.row("SELECT * FROM tournaments WHERE kind='placement'")
    assert g["focus_model"] == "newcomer"
    assert db.row("SELECT COUNT(*) n FROM matches WHERE tournament_id=?", (g["id"],))["n"] == 6

    # removing a model cancels its queued games but keeps history
    bench_env.write_text(bench_env.read_text().replace("  - name: Newcomer\n    provider: random\n", ""))
    os.utime(bench_env, (time.time() + 10, time.time() + 10))
    b.sync_models()
    assert db.row("SELECT COUNT(*) n FROM matches WHERE status='queued' AND "
                  "(home_model='newcomer' OR away_model='newcomer')")["n"] == 0
    assert db.row("SELECT enabled FROM models WHERE id='newcomer'")["enabled"] == 0


def test_placement_anchors_and_top_ups(bench_env):
    from bench.scheduler import Bench
    names = [f"M{i}" for i in range(8)]
    bench_env.write_text("settings:\n  placement_size: 3\n  placement_extra_games: 4\nmodels:\n" +
                         "".join(f"  - name: {n}\n    provider: random\n" for n in names))
    from bench import version
    current = json.dumps({"harness": {"protocol_version": version.PROTOCOL_VERSION}})  # as a real finished game has
    b = Bench()
    b.sync_models(force=True)
    # the opening round robin: lower numbers always win, so m0 is top and m7 bottom
    for m in db.rows("SELECT * FROM matches"):
        winner = "home" if m["home_model"] < m["away_model"] else "away"
        db.execute("UPDATE matches SET status='completed', winner=?, finished_at=?, meta=? WHERE id=?",
                   (winner, time.time(), current, m["id"]))
    b._finish_tournaments()

    bench_env.write_text(bench_env.read_text() + "  - name: Newcomer\n    provider: random\n")
    os.utime(bench_env, (time.time() + 5, time.time() + 5))
    assert b.sync_models() == ["newcomer"]
    t = db.row("SELECT * FROM tournaments WHERE kind='placement'")
    fixtures = db.rows("SELECT home_model, away_model FROM matches WHERE tournament_id=? ORDER BY seq", (t["id"],))
    opponents = [f["away_model"] for f in fixtures if f["home_model"] == "newcomer"]
    assert opponents == ["m0", "m4", "m7"] and len(fixtures) == 6     # top, middle, bottom; home and away
    assert b.possible_top_ups(t) == 4     # not scheduled yet: shown as unconfirmed in the upcoming list

    def finish_queued():
        for m in db.rows("SELECT id FROM matches WHERE tournament_id=? AND status='queued'", (t["id"],)):
            db.execute("UPDATE matches SET status='completed', winner='draw', finished_at=?, meta=? WHERE id=?",
                       (time.time(), current, m["id"]))
        b._finish_tournaments()
        return db.rows("SELECT * FROM matches WHERE tournament_id=? AND status='queued' ORDER BY seq", (t["id"],))

    # 6 games leave a wide range, so a top-up pairing (home and away) is queued against a close, little-played model
    top_up = finish_queued()
    assert len(top_up) == 2 and {top_up[0]["home_model"], top_up[1]["away_model"]} == {"newcomer"}
    assert top_up[0]["away_model"] not in ("m0", "m4", "m7")
    assert db.row("SELECT status FROM tournaments WHERE id=?", (t["id"],))["status"] != "completed"
    assert b.possible_top_ups(t) == 2
    assert len(finish_queued()) == 2      # the allowance of 4 extra games is now used up
    assert b.possible_top_ups(t) == 0
    assert finish_queued() == []
    assert db.row("SELECT status FROM tournaments WHERE id=?", (t["id"],))["status"] == "completed"
    assert db.row("SELECT COUNT(*) n FROM matches WHERE tournament_id=?", (t["id"],))["n"] == 10


def test_ratings():
    ms = [
        {"home_model": "a", "away_model": "b", "winner": "home", "home_score": 2, "away_score": 0, "finished_at": 1},
        {"home_model": "b", "away_model": "a", "winner": "draw", "home_score": 1, "away_score": 1, "finished_at": 2},
    ]
    elo = ratings.compute_elo(ms)
    assert elo["a"]["elo"] > 1000 > elo["b"]["elo"]
    assert elo["a"]["lo"] < elo["a"]["elo"] < elo["a"]["hi"] and len(elo["a"]["history"]) == 2

    # the fit doesn't depend on game order, stays finite for an unbeaten model, and gets surer with more games
    games = [{"home_model": "a", "away_model": "b", "winner": "home"}, {"home_model": "b", "away_model": "c",
             "winner": "home"}, {"home_model": "c", "away_model": "a", "winner": "draw"}]
    assert ratings.fit_ratings(games) == ratings.fit_ratings(games[::-1])
    few = ratings.fit_ratings(games)
    many = ratings.fit_ratings(games * 20)
    assert many["a"]["hi"] - many["a"]["lo"] < few["a"]["hi"] - few["a"]["lo"]
    unbeaten = ratings.fit_ratings([{"home_model": "x", "away_model": "y", "winner": "home"}] * 10)
    assert 1000 < unbeaten["x"]["elo"] < 2000
    table = ratings.standings(ms)
    assert table[0]["model"] == "a" and table[0]["points"] == 4 and table[1]["points"] == 1


def test_web_endpoints(bench_env):
    from bench.web import create_app
    app = create_app(start_scheduler=False)
    from bench.scheduler import get_bench
    b = get_bench()
    m = b.next_match()
    b.play(m)
    c = app.test_client()
    for url in ["/", "/leaderboard", "/tournaments", "/tournaments/1", "/about", "/board", f"/match/{m['id']}",
                "/models/random-baseline", "/health", "/api/status", "/api/leaderboard", "/api/tournaments",
                "/api/tournaments/1", f"/api/matches/{m['id']}", f"/api/matches/{m['id']}/events",
                f"/api/matches/{m['id']}/state", f"/replays/{m['id']}", f"/steps/{m['id']}/0/5",
                "/api/models/random-baseline", "/bench-static/bench.css"]:
        r = c.get(url)
        assert r.status_code == 200, url
    state = json.loads(c.get(f"/api/matches/{m['id']}/state").data)
    assert state["state"]["game_over"] is True
    lb = json.loads(c.get("/api/leaderboard").data)
    assert {x["id"] for x in lb["models"]} >= {m["home_model"], m["away_model"]}
    assert c.post("/api/admin/round-robin").status_code == 403
    assert c.get("/match/nope").status_code == 404


def test_model_about(bench_env, monkeypatch):
    from bench import model_info
    monkeypatch.setattr(model_info, "_catalog", None)
    monkeypatch.setattr(model_info, "_fetched_at", 0.0)
    monkeypatch.setattr(model_info, "_failed_at", 0.0)
    fetches = []

    def fetch():
        fetches.append(1)
        entry = {"name": "OpenAI: GPT-5 Mini", "description": "A compact GPT-5.", "context_length": 400000,
                 "created": 1, "knowledge_cutoff": "2024-05-31", "prompt_price": "0.00000025",
                 "completion_price": "0.000002"}
        return {"openai/gpt-5-mini": entry, "openai/gpt-5-mini-2025-08-07": entry}
    monkeypatch.setattr(model_info, "_fetch", fetch)
    a = model_info.about({"provider": "openrouter", "model": "openai/gpt-5-mini"})
    assert a["description"] == "A compact GPT-5." and a["source"] == "openrouter"
    assert a["url"] == "https://openrouter.ai/openai/gpt-5-mini" and a["context_length"] == 400000
    assert model_info.about({"model": "openai/gpt-5-mini-2025-08-07"})["description"] == "A compact GPT-5."
    # a description in models.yaml wins; unknown models still get their OpenRouter link
    own = model_info.about({"model": "openai/gpt-5-mini", "description": "Ours."})
    assert own["description"] == "Ours." and own["source"] == "bench"
    unknown = model_info.about({"model": "acme/nope"})
    assert unknown["description"] == "" and unknown["url"] == "https://openrouter.ai/acme/nope"
    assert len(fetches) == 1  # cached
    # baselines describe themselves, offline
    assert "random" in model_info.about({"provider": "random"})["description"]
    assert model_info.about({"provider": "botbowl", "description": "Bot."})["description"] == "Bot."
    # the cache survives a restart without a refetch, and a failed refresh keeps serving it
    monkeypatch.setattr(model_info, "_catalog", None)
    monkeypatch.setattr(model_info, "_fetched_at", 0.0)
    assert "openai/gpt-5-mini" in model_info.catalog() and len(fetches) == 1

    def down():
        fetches.append(1)
        raise model_info.httpx.ConnectError("down")
    monkeypatch.setattr(model_info, "_fetch", down)
    monkeypatch.setattr(model_info, "_fetched_at", 0.0)
    assert "openai/gpt-5-mini" in model_info.catalog() and len(fetches) == 2
    assert "openai/gpt-5-mini" in model_info.catalog() and len(fetches) == 2  # no retry storm


def test_reasoning_passthrough_and_cache_breakpoints(fake_openrouter):
    cfg = {"name": "Claude-ish", "provider": "openrouter", "model": "anthropic/fake", "max_tool_calls_per_turn": 6}
    MatchRunner("t6", cfg, RANDOM, {"max_game_minutes": 5}, api_key="k").run()
    bodies = fake_openrouter.bodies
    # anthropic/* models get cache breakpoints on the system prompt and the opening situation
    first = bodies[0]["messages"]
    assert first[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert first[1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    # reasoning_details returned by the model are sent back on the following calls of the conversation
    echoed = [m for b in bodies for m in b["messages"] if m.get("role") == "assistant" and m.get("reasoning_details")]
    assert echoed and echoed[0]["reasoning_details"][0]["data"].startswith("sig-")


def test_game_time_cap(monkeypatch):
    slow = {"name": "Slow", "provider": "random", "delay": 0.05}
    runner = MatchRunner("t7", slow, RANDOM, {"max_game_minutes": 0.02})
    result = runner.run()
    assert runner.session.time_capped
    assert result["meta"]["admissible"] is False and "time_capped" in result["meta"]["inadmissible_reasons"]
    assert result["error"] is None and runner.session.game.state.game_over


def test_live_state_etag_gzip_and_exports(bench_env):
    import gzip as gz
    from bench.web import create_app
    from bench.scheduler import get_bench
    app = create_app(start_scheduler=False)
    b = get_bench()
    m = b.next_match()
    b.play(m)
    c = app.test_client()
    r = c.get(f"/api/matches/{m['id']}/state", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["Content-Encoding"] == "gzip"
    assert json.loads(gz.decompress(r.data))["state"]["game_over"]
    r2 = c.get(f"/api/matches/{m['id']}/state", headers={"If-None-Match": r.headers["ETag"]})
    assert r2.status_code == 304 and not r2.data
    lines = [json.loads(l) for l in c.get("/api/export/matches.jsonl").data.decode().splitlines()]
    assert len(lines) == 1 and lines[0]["meta"]["seed"] is not None and lines[0]["has_transcript"]
    assert lines[0]["admissible"] is True
    refl = c.get("/api/export/events.jsonl?kind=reflection").data.decode().splitlines()
    assert refl and all(json.loads(l)["kind"] == "reflection" for l in refl)
    t = c.get(f"/api/matches/{m['id']}/transcript")
    assert t.status_code == 200 and gz.decompress(t.data).startswith(b'{"type": "meta"')
    lb = json.loads(c.get("/api/leaderboard").data)
    assert all("elo_ci" in x and "td_diff" in x for x in lb["models"])
    assert c.get("/data").status_code == 200


def test_db_migration_adds_columns(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE matches (id TEXT PRIMARY KEY, tournament_id INTEGER, seq INTEGER, "
                      "home_model TEXT, away_model TEXT, status TEXT, created_at REAL);")
    con.close()
    db.init(path)
    cols = {r["name"] for r in db.rows("PRAGMA table_info(matches)")}
    assert {"seed", "meta", "admissible"} <= cols


def test_internal_errors_are_not_blamed_on_the_model(monkeypatch):
    import bench.tools as T
    def boom(self, player_id):
        raise RuntimeError("simulated harness bug")
    monkeypatch.setattr(T.SeatTools, "get_player", boom)
    s, tools, threads = _first_turn_session()
    try:
        server = T.build_mcp_server(tools)
        import anyio
        from mcp import Client

        async def go():
            async with Client(server) as c:
                r = await c.call_tool("get_player", {"player_id": "H1"})
                return "\n".join(x.text for x in r.content)
        text = anyio.run(go)
        assert text.startswith(T.INTERNAL_ERROR)
        assert s.harness_errors and "simulated harness bug" in s.harness_errors[0]
    finally:
        s.abort()


def test_replays_and_match_archive(bench_env):
    import zlib
    from bench.web import create_app
    from bench.scheduler import get_bench, timeline_path
    app = create_app(start_scheduler=False)
    b = get_bench()
    played = []
    for _ in range(3):
        m = b.next_match()
        b.play(m)
        played.append(m["id"])
    c = app.test_client()
    mid = played[0]

    tl = json.loads(c.get(f"/api/matches/{mid}/timeline").data)
    assert tl["live"] is False and tl["frames"] > 10 and len(tl["points"]) == tl["frames"]
    first, last = tl["points"][0], tl["points"][-1]
    assert first["i"] == 0 and last["over"] is True and first["ts"] <= last["ts"]
    assert {"half", "ht", "at", "hs", "as", "side"} <= set(first)

    # frames are served as stored (zlib == HTTP deflate) and cached forever
    r = c.get(f"/api/matches/{mid}/frames/5", headers={"Accept-Encoding": "gzip, deflate"})
    assert r.status_code == 200 and r.headers["Content-Encoding"] == "deflate"
    assert "immutable" in r.headers["Cache-Control"]
    frame = json.loads(zlib.decompress(r.data))
    assert frame["state"]["home_team"]["state"]["turn"] == tl["points"][5]["ht"]
    assert json.loads(c.get(f"/api/matches/{mid}/frames/5").data) == frame   # plain JSON without deflate
    assert c.get(f"/api/matches/{mid}/frames/{tl['frames']}").status_code == 404

    # games recorded before timelines existed: rebuilt from the frames, timestamps interpolated
    os.remove(timeline_path(mid))
    tl2 = json.loads(c.get(f"/api/matches/{mid}/timeline").data)
    assert [(p["hs"], p["as"], p["ht"], p["at"]) for p in tl2["points"]] == \
           [(p["hs"], p["as"], p["ht"], p["at"]) for p in tl["points"]]

    # game log: every outcome line is stored once, filed under a turn and keyed to a recorded frame
    plays = [e for e in db.get_events(mid, limit=100000) if e["kind"] == "play"]
    assert plays
    frames = [e["payload"]["frame"] for e in plays]
    assert frames == sorted(frames) and 0 <= frames[0] and frames[-1] < tl["frames"]
    lines = [l for e in plays for l in e["payload"]["lines"]]
    assert all(l["t"] and "<" not in l["t"] for l in lines)
    assert "Game started." in [l["t"] for l in lines]
    # a turnover is filed under the team that suffered it (turn 0 is fine: a Blitz! kick-off gives a free turn)
    assert all(e["side"] in ("home", "away")
               for e in plays if any(l["k"] == "turnover" for l in e["payload"]["lines"]))
    m = json.loads(c.get(f"/api/matches/{mid}").data)
    tds = sum(1 for l in lines if l["k"] == "td")
    assert tds == m["home_score"] + m["away_score"]

    # archive: newest first, pagination, filters
    page1 = json.loads(c.get("/api/matches?limit=2").data)
    assert len(page1["matches"]) == 2 and page1["next_before"]
    page2 = json.loads(c.get(f"/api/matches?limit=2&before={page1['next_before']}").data)
    ids = [m["id"] for m in page1["matches"] + page2["matches"]]
    assert sorted(ids) == sorted(played) and page2["next_before"] is None
    model = page1["matches"][0]["home_model"]
    only = json.loads(c.get(f"/api/matches?model={model}").data)["matches"]
    assert only and all(model in (m["home_model"], m["away_model"]) for m in only)
    draws = json.loads(c.get("/api/matches?result=draw").data)["matches"]
    assert all(m["winner"] == "draw" for m in draws)
    assert c.get("/matches").status_code == 200


def test_resume_from_checkpoint(tmp_path, monkeypatch):
    """A game interrupted mid-way continues from the start of the last team turn, with its history intact."""
    import pickle
    from bench.transcript import read_transcript
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    checkpoints = []
    first = MatchRunner("t-resume", SCRIPTED, RANDOM, {}, transcript=True,
                        checkpoint_sink=lambda st: checkpoints.append(pickle.dumps(st)))
    full = first.run()
    assert full["error"] is None and len(checkpoints) >= 16   # one per team turn
    keys = [pickle.loads(c)["key"] for c in checkpoints]
    assert {k.split(":")[0] for k in keys} == {"home", "away"}
    # a regular team turn (keys ending in b/q are kick-off blitz / quick-snap turns, numbered 0)
    regular = [c for c, k in zip(checkpoints, keys) if k.split("-")[-1].isdigit()]
    cp = pickle.loads(regular[len(regular) // 2])
    side, _, episode = cp["key"].partition(":")
    half, turn = int(episode.split("-")[1]), int(episode.split("-")[2])

    # pretend the process died right after that checkpoint: the transcript has more records than it kept
    os.replace(tmp_path / "transcripts" / "t-resume.jsonl.gz", tmp_path / "transcripts" / "t-resume.jsonl.gz.partial")
    resumed = MatchRunner("t-resume", SCRIPTED, RANDOM, {}, transcript=True, resume=cp, restarted=True)
    g = resumed.session.game
    assert (g.state.half, g.current_turn().team.state.turn) == (half, turn)
    assert g.current_turn().team == (g.state.home_team if side == "home" else g.state.away_team)
    assert resumed.seed == first.seed
    assert len(resumed.session.frames) == len(pickle.loads(cp["session"]["shared"])["frames"]) - 1
    result = resumed.run()
    assert result["error"] is None and g.state.game_over
    hs = result["home_stats"]
    assert hs["turns"] >= 8 and hs["actions_logged"] > 0
    assert hs["tool_calls"] > cp["drivers"]["home"]["usage"]["tool_calls"]   # usage carried over
    resumes = result["meta"]["resumes"]
    assert len(resumes) == 1 and resumes[0]["checkpoint"] == cp["key"] and not resumes[0]["transcript_gap"]
    assert result["meta"]["admissible"]
    recs = list(read_transcript("t-resume"))
    types = [r["type"] for r in recs]
    assert types[0] == "meta" and types.count("meta") == 1 and types.count("resume") == 1
    assert types.index("resume") == cp["transcript_records"] and types[-1] == "result"


def test_scheduler_resumes_interrupted_match(bench_env, monkeypatch):
    import bench.scheduler as sch
    b = sch.Bench()
    b.sync_models(force=True)
    m = b.next_match()
    saved = []
    real_save = sch.save_checkpoint

    def save_then_die(match_id, state):
        real_save(match_id, state)
        saved.append(state["key"])
        if len(saved) == 4:   # the "process dies" right after the 4th checkpoint
            runner = b.live[match_id]
            runner.session.infra_error = "simulated outage"
            runner.session.abort()

    monkeypatch.setattr(sch, "save_checkpoint", save_then_die)
    assert b.play(m).get("infra_error")
    monkeypatch.setattr(sch, "save_checkpoint", real_save)
    row = db.row("SELECT * FROM matches WHERE id=?", (m["id"],))
    assert row["status"] == "queued" and row["started_at"] and os.path.exists(sch.checkpoint_path(m["id"]))
    cp = sch.load_checkpoint(m["id"])
    assert cp["key"] == saved[3] and cp["seed"] == row["seed"]
    kept_events = db.get_events(m["id"], limit=100000)
    assert kept_events

    # a redeploy: the match is still marked running when the new process starts
    db.execute("UPDATE matches SET status='running' WHERE id=?", (m["id"],))
    monkeypatch.setattr(sch, "_bench", None)
    b2 = sch.Bench()
    assert db.row("SELECT status FROM matches WHERE id=?", (m["id"],))["status"] == "queued"
    assert b2.next_match()["id"] == m["id"]
    res = b2.play(b2.next_match())
    assert res["error"] is None
    done = db.decode_match(db.row("SELECT * FROM matches WHERE id=?", (m["id"],)))
    assert done["status"] == "completed" and done["seed"] == row["seed"]
    assert [r["checkpoint"] for r in done["meta"]["resumes"]] == [saved[3]]
    assert not os.path.exists(sch.checkpoint_path(m["id"]))
    events = db.get_events(m["id"], limit=100000)
    ids = {e["id"] for e in events}
    assert all(e["id"] in ids for e in kept_events if e["id"] <= cp["last_event_id"])   # history up to the checkpoint
    assert any("Resuming from the start of" in e["payload"].get("text", "") for e in events)
    assert sum("Kick-off" in e["payload"].get("text", "") for e in events) == 1


def test_transcript_reopen_survives_a_kill(tmp_path, monkeypatch):
    """What was flushed before the process died is recovered, without the gzip trailer and with a cut-off tail."""
    import shutil
    from bench.transcript import Transcript
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    t = Transcript("t-kill")
    t.write("meta", None)
    t.write("llm_call", "home", usage={"cost": 0.25})   # flushed on write
    on_disk = (tmp_path / "on_disk.gz")
    shutil.copy(t._tmp, on_disk)                          # all a kill would leave behind
    t.write("tool", "home", result="x" * 50000)
    t._f.close()
    shutil.copy(on_disk, t._tmp)
    t2, dropped, kept = Transcript.reopen("t-kill", keep=1)
    assert kept == 1 and [r["type"] for r in dropped] == ["llm_call"] and dropped[0]["usage"]["cost"] == 0.25
    t2.close()
    with open(t._tmp, "wb") as f:                         # a file cut mid-write keeps its complete lines
        f.write(on_disk.read_bytes()[:-3])
    _, dropped, kept = Transcript.reopen("t-kill", keep=5)
    assert kept <= 2


def test_resume_accounts_for_spend_of_the_replayed_turn(fake_openrouter, tmp_path, monkeypatch):
    import pickle
    from bench.transcript import read_transcript
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model"}
    checkpoints = []
    MatchRunner("t-spend", cfg, RANDOM, {}, api_key="k", transcript=True,
                checkpoint_sink=lambda st: checkpoints.append(pickle.dumps(st))).run()
    cp = pickle.loads(checkpoints[len(checkpoints) // 2])
    calls_before = [r for r in read_transcript("t-spend") if r["type"] == "llm_call" and r["side"] == "home"]
    kept_calls = cp["drivers"]["home"]["usage"]["llm_calls"]
    lost_calls = len(calls_before) - kept_calls   # everything after the checkpoint is replayed
    os.replace(tmp_path / "transcripts" / "t-spend.jsonl.gz", tmp_path / "transcripts" / "t-spend.jsonl.gz.partial")
    result = MatchRunner("t-spend", cfg, RANDOM, {}, api_key="k", transcript=True, resume=cp).run()
    hs = result["home_stats"]
    assert lost_calls > 0 and abs(hs["restart_cost"] - 0.0001 * lost_calls) < 1e-9
    assert result["meta"]["resumes"][0]["restart_cost"]["home"] == hs["restart_cost"]
    assert abs(hs["cost"] - 0.0001 * hs["llm_calls"]) < 1e-9   # the game's own cost excludes the lost spend
    assert hs["llm_calls"] > kept_calls


# ---- review follow-ups for PR13: HTTP timeouts, protocol isolation, streak wording ----------------------------

def _mock_llm(handler, **kw):
    import httpx
    from bench.llm import OpenRouterLLM
    llm = OpenRouterLLM("fake/model", "secret-key-123", **kw)
    llm.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return llm


_TOOLS = [{"type": "function", "function": {"name": "reflect"}}]


def _sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" if not isinstance(c, str) else c + "\n\n" for c in chunks)


def test_streamed_reply_is_reassembled_and_reported_live():
    """Pieces of a streamed reply reach the sink as they come and add up to the same response as a plain reply."""
    import anyio
    import httpx

    def handler(request):
        body = _sse(": OPENROUTER PROCESSING",
                    {"id": "gen-1", "model": "fake/model", "provider": "Host",
                     "choices": [{"delta": {"reasoning": "Block A5 ", "reasoning_details": [
                         {"type": "reasoning.text", "text": "Block A5 ", "index": 0}]}}]},
                    {"choices": [{"delta": {"reasoning": "first.", "reasoning_details": [
                         {"type": "reasoning.text", "text": "first.", "signature": "s1", "index": 0}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                            "function": {"name": "block", "arguments": '{"player'}}]}}]},
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '_id": "H1"}'}}]}}]},
                    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.01,
                                              "completion_tokens_details": {"reasoning_tokens": 3}}},
                    "data: [DONE]")
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    seen = []

    async def go():
        llm = _mock_llm(handler, stream=True)
        llm.stream_sink = lambda r, c: seen.append(r + c)
        try:
            return await llm.chat([{"role": "system", "content": "x"}], _TOOLS)
        finally:
            await llm.aclose()

    resp = anyio.run(go)
    assert seen == ["Block A5 ", "first."]
    assert resp.reasoning == "Block A5 first." and resp.provider == "Host" and resp.finish_reason == "tool_calls"
    assert [(t.id, t.name, t.arguments) for t in resp.tool_calls] == [("c1", "block", {"player_id": "H1"})]
    assert resp.raw_message["reasoning_details"] == [
        {"type": "reasoning.text", "text": "Block A5 first.", "signature": "s1", "index": 0}]
    assert resp.completion_tokens == 5 and resp.reasoning_tokens == 3 and resp.cost == 0.01


def test_streamed_error_is_a_provider_error():
    """An error sent mid-stream is retried like an error reply, not taken for an empty answer."""
    import anyio
    import httpx
    from bench.llm import LLMError
    n = []

    def handler(request):
        n.append(1)
        body = _sse({"id": "gen-1", "error": {"code": 502, "message": "upstream died"},
                     "choices": [{"delta": {}, "finish_reason": "error"}]})
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    async def go():
        llm = _mock_llm(handler, stream=True, max_retries=1)
        try:
            await llm.chat([{"role": "system", "content": "x"}], _TOOLS)
        finally:
            await llm.aclose()

    with pytest.raises(LLMError, match="upstream died"):
        anyio.run(go)
    assert len(n) == 2


def test_cut_off_reply_keeps_its_streamed_thinking(monkeypatch):
    """When the turn clock cancels a streaming reply, what had arrived is shown and kept in the transcript."""
    import anyio
    from bench.driver import DriverLimits, SeatDriver
    from bench.llm import LLMError
    from bench.session import Decision, GameSession

    s = GameSession("t-partial", "A", "B", seed=1)
    s.home.team = s.game.state.home_team
    s.home.decision = Decision(1, "turn-1-1", "Turn", False)
    events, records = [], []
    monkeypatch.setattr(s, "log_event", lambda side, kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(s.transcript, "write", lambda kind, side, **kw: records.append((kind, kw)))

    class Streamer:
        stream_sink = None
        partial = {"reasoning": "", "content": ""}

        async def chat(self, messages, tools, deadline=None, hurry=False):
            self.partial = {"reasoning": "Let me weigh the blitz on A5", "content": ""}
            self.stream_sink("Let me weigh the blitz on A5", "")
            assert d.waiting["chars"] == 28 and d.waiting["text"] == "Let me weigh the blitz on A5"
            raise LLMError("turn time limit reached", uncertain_spend=True, deadline=True)

    d = SeatDriver(s, s.home, Streamer(), "A", DriverLimits(last_chance_seconds=0), opponent_name="B")
    monkeypatch.setattr(d, "_intro", lambda dec: "situation")
    monkeypatch.setattr(s.home, "force_episode", lambda key: None)
    anyio.run(d.play_episode, None, [], s.home.decision)
    assert ("thought", {"text": "Let me weigh the blitz on A5", "cut": True}) in events
    assert any(k == "llm_partial" and kw["reasoning"] == "Let me weigh the blitz on A5" for k, kw in records)
    assert d.waiting is None


def test_http_read_timeout_is_not_retried_and_is_flagged(monkeypatch):
    """A read timeout may still be billed upstream: one request only, error flagged as uncertain spend."""
    import anyio
    import httpx
    from bench.llm import LLMError
    seen = []

    def handler(request):
        seen.append(request)
        raise httpx.ReadTimeout("slow generation", request=request)

    async def go():
        llm = _mock_llm(handler, max_retries=4)
        try:
            await llm.chat([{"role": "system", "content": "x"}], _TOOLS)
        finally:
            await llm.aclose()

    with pytest.raises(LLMError) as ei:
        anyio.run(go)
    assert len(seen) == 1
    assert ei.value.timeout is True and ei.value.uncertain_spend is True
    assert not ei.value.fatal
    assert "secret-key-123" not in str(ei.value)


def test_transient_http_statuses_and_connect_errors_are_still_retried(monkeypatch):
    import anyio
    import httpx
    import bench.llm
    seen = []

    async def no_sleep(_):
        pass
    monkeypatch.setattr(bench.llm.asyncio, "sleep", no_sleep)

    def handler(request):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(503, text="busy")
        if len(seen) == 2:
            raise httpx.ConnectTimeout("no route", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {}})

    async def go():
        llm = _mock_llm(handler)
        try:
            return await llm.chat([{"role": "system", "content": "x"}], _TOOLS)
        finally:
            await llm.aclose()

    assert anyio.run(go).content == "ok"
    assert len(seen) == 3


def test_retries_stop_at_the_deadline(monkeypatch):
    import anyio
    import httpx
    import bench.llm
    from bench.llm import LLMError
    seen = []

    async def no_sleep(_):
        pass
    monkeypatch.setattr(bench.llm.asyncio, "sleep", no_sleep)

    def handler(request):
        seen.append(1)
        return httpx.Response(503, text="busy")

    async def go():
        llm = _mock_llm(handler, max_retries=4)
        try:
            await llm.chat([{"role": "system", "content": "x"}], _TOOLS, deadline=time.time() + 1)
        finally:
            await llm.aclose()

    with pytest.raises(LLMError):
        anyio.run(go)
    assert len(seen) == 1


def test_no_request_is_sent_after_the_deadline():
    import anyio
    import httpx
    from bench.llm import LLMError
    seen = []

    def handler(request):
        seen.append(1)
        return httpx.Response(503, text="busy")

    async def go():
        llm = _mock_llm(handler)
        try:
            await llm.chat([{"role": "system", "content": "x"}], _TOOLS, deadline=time.time() - 1)
        finally:
            await llm.aclose()

    with pytest.raises(LLMError) as ei:
        anyio.run(go)
    assert seen == [] and ei.value.deadline is True


def test_in_flight_call_is_cut_off_at_the_deadline():
    """httpx's timeout is per read, so a reply that trickles in never trips it; the deadline must still hold."""
    import asyncio
    import anyio
    import httpx
    from bench.llm import LLMError, OpenRouterLLM

    class Trickle(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            async def body():
                while True:  # keep-alive whitespace, like OpenRouter sends while a model is generating
                    yield b" "
                    await asyncio.sleep(0.05)
            return httpx.Response(200, content=body())

    async def go():
        llm = OpenRouterLLM("fake/model", "k", timeout=0.2)
        llm.client = httpx.AsyncClient(transport=Trickle(), timeout=0.2)
        t0 = time.time()
        try:
            with pytest.raises(LLMError) as ei:
                await llm.chat([{"role": "system", "content": "x"}], _TOOLS, deadline=time.time() + 0.5)
        finally:
            await llm.aclose()
        return ei.value, time.time() - t0

    err, took = anyio.run(go)
    assert took < 2
    assert err.deadline and err.uncertain_spend
    assert not err.timeout   # the turn's time ran out; not an HTTP timeout


def test_deadline_cutoff_auto_finishes_without_marking_model_unavailable(monkeypatch):
    import bench.match
    from bench.llm import LLMError

    class SlowLLM:
        async def chat(self, messages, tools, deadline=None):
            raise LLMError("turn time limit reached after 300s without a reply", uncertain_spend=True,
                           deadline=True)

        async def aclose(self):
            pass

    real = bench.match.make_llm
    monkeypatch.setattr(bench.match, "make_llm",
                        lambda cfg, seat, key: SlowLLM() if cfg.get("provider") == "openrouter" else real(cfg, seat, key))
    cfg = {"name": "Slow LLM", "provider": "openrouter", "model": "fake/model"}
    events = []
    result = MatchRunner("t-deadline", cfg, RANDOM, {"budget_usd_per_game": 10}, api_key="k",
                         event_sink=lambda *a: events.append(a)).run()
    texts = [e[3].get("text", "") for e in events if e[2] == "system"]
    assert any("time limit (300s) reached while waiting for the model's reply" in t for t in texts), texts
    assert not any("Model unavailable" in t for t in texts)
    assert not [e for e in events if e[2] == "error"]   # a time-limit cut-off is not shown as an error
    stats = result["home_stats"]
    assert stats["deadline_cutoffs"] > 4 and stats["budget_exhausted"] >= stats["deadline_cutoffs"]
    assert stats["llm_errors"] == 0 and stats["http_timeouts"] == 0
    assert stats["uncertain_spend_calls"] == stats["deadline_cutoffs"]


def test_settings_output_ceiling_applies_unless_the_model_sets_its_own(fake_openrouter):
    from bench.version import PROTOCOL_VERSION
    base = {"provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 4}
    for name, extra, want in (("Default", {}, 16384), ("Own cap", {"max_tokens": 4096}, 4096),
                              ("Opt out", {"max_tokens": None}, None)):
        fake_openrouter.bodies = []
        result = MatchRunner(f"t-cap-{want}", {"name": name, **base, **extra}, RANDOM,
                             {"budget_usd_per_game": 10, "max_output_tokens": 16384}, api_key="k").run()
        assert fake_openrouter.bodies
        assert all(b.get("max_tokens") == want for b in fake_openrouter.bodies), name
        assert result["meta"]["harness"]["protocol_version"] == PROTOCOL_VERSION == "1.5"


def test_default_settings_carry_the_output_ceiling():
    from bench.config import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["max_output_tokens"] == 16384


def test_driver_counts_http_timeouts_and_uncertain_spend():
    import anyio
    from bench import ratings
    from bench.driver import DriverLimits, SeatDriver
    from bench.llm import LLMError
    from bench.session import GameSession

    class TimeoutLLM:
        async def chat(self, messages, tools, deadline=None):
            raise LLMError("network error: read timeout", timeout=True, uncertain_spend=True)

        async def aclose(self):
            pass

    s = GameSession("t-timeout-unit", "A", "B", seed=1)
    d = SeatDriver(s, s.home, TimeoutLLM(), "A", DriverLimits(), opponent_name="B")

    async def go():
        try:
            await d._call_llm([{"role": "user", "content": "hi"}], [], "k", 0)
        except LLMError:
            pass
    anyio.run(go)
    assert d.usage["http_timeouts"] == 1
    assert d.usage["uncertain_spend_calls"] == 1
    agg = ratings.aggregate([{"home_model": "a", "away_model": "b", "winner": "home", "home_score": 1,
                              "away_score": 0, "home_stats": d.usage, "away_stats": {}}])
    assert agg["a"]["sums"]["http_timeouts"] == 1
    assert agg["a"]["sums"]["uncertain_spend_calls"] == 1


def test_interleaved_truncations_trip_the_limit_since_last_game_action(fake_openrouter):
    """L, text, L, text, L is not three in a row, but nothing reset the counter: wording must say so."""
    fake_openrouter.script = ["L", "T", "L", "T", "L"]
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 20}
    events = []
    result = MatchRunner("t-interleave", cfg, RANDOM, {"budget_usd_per_game": 10, "max_illegal_streak": 3},
                         api_key="k", event_sink=lambda *a: events.append(a)).run()
    texts = [e[3].get("text", "") for e in events if e[2] == "system"]
    hit = [t for t in texts if "output truncations" in t]
    assert hit, texts
    assert "since last game action" in hit[0]
    assert "output length limit" in hit[0] and "max_tokens" not in hit[0]
    assert "in a row" not in hit[0]
    assert result["home_stats"]["output_truncations"] >= 3


def test_empty_content_filter_replies_are_retried_not_counted_against_the_model(fake_openrouter, monkeypatch):
    """A zero-token content_filter reply means the provider stopped the call before the model answered: retry it."""
    import anyio
    import bench.llm
    from bench.llm import LLMError, OpenRouterLLM
    monkeypatch.setattr(bench.llm.asyncio, "sleep", lambda s: anyio.sleep(0))
    llm = OpenRouterLLM("fake/model", "k")
    fake_openrouter.script = ["F", "F"]
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "choose a player to activate"}]
    tools = [{"type": "function", "function": {"name": "end_turn", "parameters": {}}}]

    async def go():
        try:
            return await llm.chat(msgs, tools)
        finally:
            await llm.aclose()
    resp = anyio.run(go)
    assert resp.tool_calls and resp.filtered_retries == 2 and fake_openrouter.calls == 3
    # only an empty reply qualifies: a filtered reply with text is the model's own answer
    r = bench.llm.LLMResponse(content="I can't help with that", finish_reason="content_filter")
    assert not bench.llm.is_empty_filtered(r)
    # when every retry is filtered the call fails as an LLM error, not as a reply without a tool call
    llm2 = OpenRouterLLM("fake/model", "k", max_retries=1)
    fake_openrouter.script = ["F", "F"]

    async def go2():
        try:
            await llm2.chat(msgs, tools)
        finally:
            await llm2.aclose()
    with pytest.raises(LLMError, match="content filter"):
        anyio.run(go2)


def test_a_host_that_filters_is_skipped_for_the_rest_of_the_game(fake_openrouter, monkeypatch):
    """An empty content-filter reply from one host (Azure for Opus 5.5, game 294e497a) kept coming back on every retry
    while session_id pinned the seat there. The retry goes out at once with that host ignored, and later calls keep
    ignoring it; if that leaves no host, the ignore list is dropped again."""
    import anyio
    import bench.llm
    from bench.llm import OpenRouterLLM
    monkeypatch.setattr(bench.llm.asyncio, "sleep", lambda s: anyio.sleep(0))
    llm = OpenRouterLLM("anthropic/claude-opus-5.5", "k", provider_prefs={"sort": "throughput"}, session_id="m:home")
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "choose a player to activate"}]
    tools = [{"type": "function", "function": {"name": "end_turn", "parameters": {}}}]

    async def go():
        return await llm.chat(msgs, tools)
    fake_openrouter.script = ["F"]
    resp = anyio.run(go)
    assert resp.tool_calls and resp.filtered_retries == 1
    assert "ignore" not in fake_openrouter.bodies[-2]["provider"]
    assert fake_openrouter.bodies[-1]["provider"] == {"sort": "throughput", "ignore": ["fakecloud"]}
    anyio.run(go)
    assert fake_openrouter.bodies[-1]["provider"]["ignore"] == ["fakecloud"]   # still skipped on the next call
    # no host left once it is skipped: OpenRouter answers 404, and the call goes again to every host
    fake_openrouter.no_endpoints_when_ignoring = True
    resp = anyio.run(go)
    assert resp.tool_calls and "ignore" not in fake_openrouter.bodies[-1]["provider"] and not llm.skipped_hosts
    anyio.run(llm.aclose)


def test_content_filter_retries_are_counted_in_match_stats(fake_openrouter, monkeypatch):
    import anyio
    import bench.llm
    monkeypatch.setattr(bench.llm.asyncio, "sleep", lambda s: anyio.sleep(0))
    fake_openrouter.script = ["F", "F", "F"]
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 12}
    result = MatchRunner("t-filter", cfg, RANDOM, {}, api_key="k").run()
    hs = result["home_stats"]
    assert hs["content_filter_retries"] == 3 and hs["llm_errors"] == 0


def test_requests_go_to_the_fastest_affordable_host(fake_openrouter):
    """Every model: hosts sorted by throughput, fast ones preferred, capped at 3x the listed price; only Claude
    (whose cache needs it) is pinned to one host per seat per game."""
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 12}
    result = MatchRunner("t-route", cfg, RANDOM, {"provider_sort": "throughput", "provider_min_throughput": 50,
                                                          "provider_max_price_factor": 3}, api_key="k").run()
    assert result["error"] is None and fake_openrouter.bodies
    for b in fake_openrouter.bodies:
        assert b["provider"] == {"sort": "throughput", "preferred_min_throughput": 50.0,
                                 "max_price": {"prompt": 3.0, "completion": 6.0}}
        assert "session_id" not in b             # not pinned: a slow first host isn't kept for the whole game
        assert "cache_control" not in b          # only Claude models need cache breakpoints
    from bench.match import provider_prefs
    # no listing to price against: sorted, but no cap rather than a wrong one
    assert provider_prefs({"model": "acme/unknown", "provider_sort": "throughput",
                           "provider_max_price_factor": 1.5}) == {"sort": "throughput"}
    assert provider_prefs({"model": "fake/model", "provider_sort": None, "provider_max_price_factor": None}) == {}


def test_claude_requests_cache_the_growing_turn_and_fall_back_if_refused(fake_openrouter):
    import anyio
    from bench.llm import OpenRouterLLM
    llm = OpenRouterLLM("anthropic/claude-haiku-5.5", "k", provider_prefs={"sort": "throughput"}, session_id="m:home")
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "choose a player to activate"}]
    tools = [{"type": "function", "function": {"name": "end_turn", "parameters": {}}}]

    async def go():
        return await llm.chat(msgs, tools)
    anyio.run(go)
    assert fake_openrouter.bodies[-1]["cache_control"] == {"type": "ephemeral"}
    assert fake_openrouter.bodies[-1]["session_id"] == "m:home"   # Claude stays on one host so the cache stays warm
    # a host that refuses top-level cache_control: the same call goes again without it, and it stays off
    fake_openrouter.reject_cache_control = True
    anyio.run(go)
    anyio.run(go)
    assert ["cache_control" in b for b in fake_openrouter.bodies[1:]] == [True, False, False]
    assert llm.auto_cache is False
    anyio.run(llm.aclose)


def test_prompt_wording_is_not_strictly_consecutive():
    from bench.driver import DriverLimits, system_prompt
    p = system_prompt("M", DriverLimits(), "home", "O")
    assert "in a row" not in p and "since your last game action" in p


def _insert_match(mid, tid, a, b, winner, protocol, stats=None):
    meta = None if protocol == "missing" else json.dumps({"harness": {"protocol_version": protocol}})
    st = json.dumps(stats or {"tool_calls": 10, "turns": 16})
    db.execute("INSERT INTO matches (id, tournament_id, seq, home_model, away_model, status, home_score, away_score, "
               "winner, home_stats, away_stats, created_at, finished_at, meta) VALUES "
               "(?,?,?,?,?, 'completed', 1, 0, ?, ?, ?, ?, ?, ?)",
               (mid, tid, 99, a, b, winner, st, st, time.time(), time.time(), meta))


def test_current_protocol_filter_defaults_missing_to_legacy():
    from bench import version
    assert version.match_protocol({"meta": None}) == "1.0"
    assert version.match_protocol({"meta": {}}) == "1.0"
    assert version.match_protocol({"meta": {"harness": {"protocol_version": "1.1"}}}) == "1.1"
    assert version.is_current_protocol({"meta": {"harness": {"protocol_version": version.PROTOCOL_VERSION}}})
    assert not version.is_current_protocol({"meta": None})


def test_leaderboard_and_placement_use_current_protocol_only(bench_env):
    from bench import version
    from bench.scheduler import Bench, get_bench
    from bench.web import create_app
    app = create_app(start_scheduler=False)
    b = get_bench()
    tid = db.row("SELECT id FROM tournaments LIMIT 1")["id"]
    for i in range(6):   # legacy results that would make scripted-baseline dominant if pooled
        _insert_match(f"old10-{i}", tid, "scripted-baseline", "random-baseline", "home", "1.0",
                      {"output_truncations": 7, "tool_calls": 10, "turns": 16})
    _insert_match("oldmissing", tid, "scripted-baseline", "random-two", "home", "missing")
    _insert_match("new11", tid, "random-baseline", "random-two", "home", version.PROTOCOL_VERSION)

    c = app.test_client()
    lb = json.loads(c.get("/api/leaderboard").data)
    assert lb["matches"] == 1
    assert lb["protocol_version"] == version.PROTOCOL_VERSION
    rows = {r["id"]: r for r in lb["models"]}
    assert rows["scripted-baseline"]["played"] == 0
    assert rows["scripted-baseline"]["elo"] == 1000.0
    assert rows["random-baseline"]["played"] == 1 and rows["random-two"]["played"] == 1
    assert rows["scripted-baseline"]["sums"].get("output_truncations", 0) == 0

    model = json.loads(c.get("/api/models/scripted-baseline").data)
    assert model["stats"] is None

    rated = b._current_ratings()
    assert set(rated) == {"random-baseline", "random-two"}

    t = json.loads(c.get(f"/api/tournaments/{tid}").data)
    assert sum(r["played"] for r in t["standings"]) == 2          # only the 1.1 game is ranked
    assert len(t["matches"]) >= 8                                   # history stays visible
    # archives are untouched
    assert c.get("/api/matches/old10-0").status_code == 200
    assert db.row("SELECT COUNT(*) n FROM matches WHERE id LIKE 'old%'")["n"] == 7


# ---- making models act within the turn: thinking budget, required tool calls, last-chance call -----------------

_PROD_LIKE = {"budget_usd_per_game": 10, "max_output_tokens": 16384, "reasoning_max_tokens": None,
              "reasoning_enabled": True, "tool_choice": "required", "last_chance_max_tokens": 2048}


def test_thinking_budget_and_required_tool_choice_reach_the_payload(fake_openrouter):
    base = {"provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 4}
    cases = (("Default", {}, {"enabled": True}),   # each model's own default effort
             ("Thinking off", {"reasoning_enabled": False}, None),
             ("Own reasoning", {"extra": {"reasoning": {"effort": "low"}}}, {"effort": "low"}),
             ("Budget", {"reasoning_max_tokens": 12000}, {"max_tokens": 12000}),
             ("Small cap", {"reasoning_max_tokens": 12000, "max_tokens": 4000}, {"max_tokens": 3000}))   # below max_tokens
    for name, extra, want in cases:
        fake_openrouter.bodies = []
        MatchRunner(f"t-reason-{name}", {"name": name, **base, **extra}, RANDOM, _PROD_LIKE, api_key="k").run()
        assert fake_openrouter.bodies, name
        assert all(b.get("reasoning") == want for b in fake_openrouter.bodies), name
        assert all(b["tool_choice"] == "required" for b in fake_openrouter.bodies), name


def test_required_tool_choice_falls_back_to_auto_when_rejected():
    import anyio
    import httpx
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body["tool_choice"])
        if body["tool_choice"] == "required":
            return httpx.Response(400, text="Thinking may not be enabled when tool_choice forces tool use.")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {}})

    async def go():
        llm = _mock_llm(handler, tool_choice="required")
        try:
            a = await llm.chat([{"role": "system", "content": "x"}], _TOOLS)
            b = await llm.chat([{"role": "system", "content": "x"}], _TOOLS)
            return a, b, llm.tool_choice
        finally:
            await llm.aclose()

    a, b, choice = anyio.run(go)
    assert a.content == b.content == "ok"
    assert seen == ["required", "auto", "auto"] and choice == "auto"


def test_a_real_400_still_fails_and_keeps_required():
    import anyio
    import httpx
    from bench.llm import LLMError
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["tool_choice"])
        return httpx.Response(400, text="context length exceeded")

    async def go():
        llm = _mock_llm(handler, tool_choice="required")
        try:
            await llm.chat([{"role": "system", "content": "x"}], _TOOLS)
        finally:
            await llm.aclose()
        return llm.tool_choice

    with pytest.raises(LLMError):
        anyio.run(go)
    assert seen == ["required", "auto"]


def test_hurried_call_asks_for_a_short_reply():
    import anyio
    import httpx
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {}})

    async def go():
        llm = _mock_llm(handler, max_tokens=16384, reasoning_max_tokens=12000, hurry_max_tokens=2048)
        try:
            await llm.chat([{"role": "system", "content": "x"}], _TOOLS, hurry=True)
        finally:
            await llm.aclose()

    anyio.run(go)
    assert bodies[0]["max_tokens"] == 2048 and bodies[0]["reasoning"] == {"max_tokens": 1024}


def test_last_chance_call_lets_the_model_act_after_a_long_reply(monkeypatch):
    """A reply that runs into the reserve is cancelled; one short call later the model moves itself."""
    import bench.match
    from bench.llm import LLMError, RandomPolicy

    class SlowThenQuick:
        def __init__(self, seat):
            self.policy = RandomPolicy(seat, seed=3)
            self.hurried_prompts = []

        async def chat(self, messages, tools, deadline=None, hurry=False):
            if not hurry:
                raise LLMError("turn time limit reached after 255s without a reply (request cancelled)",
                               uncertain_spend=True, deadline=True)
            self.hurried_prompts.append(messages[-1]["content"] if messages[-1]["role"] == "user" else "")
            return await self.policy.chat(messages, tools)

        async def aclose(self):
            pass

    made = []
    real = bench.match.make_llm

    def fake_make(cfg, seat, key):
        if cfg.get("provider") != "openrouter":
            return real(cfg, seat, key)
        made.append(SlowThenQuick(seat))
        return made[-1]

    monkeypatch.setattr(bench.match, "make_llm", fake_make)
    cfg = {"name": "Slow LLM", "provider": "openrouter", "model": "fake/model"}
    events = []
    result = MatchRunner("t-last-chance", cfg, RANDOM, {"budget_usd_per_game": 10, "last_chance_seconds": 45},
                         api_key="k", event_sink=lambda *a: events.append(a)).run()
    stats = result["home_stats"]
    assert result["error"] is None
    assert stats["last_chance_calls"] > 4 and stats["deadline_cutoffs"] == stats["last_chance_calls"]
    assert stats["tool_calls"] > 0 and stats["llm_errors"] == 0
    texts = [e[3].get("text", "") for e in events if e[2] == "system"]
    assert not any("time limit" in t for t in texts), texts
    assert any("Time is almost up" in p for p in made[0].hurried_prompts)


def test_turn_clock_pauses_while_a_tool_call_waits_on_the_opponent(monkeypatch):
    """Time spent inside a tool call (the game waiting for the opponent's block dice choice, say) is not the
    model's: it must not run down the turn clock or shorten the next call's deadline."""
    import anyio
    import types
    from bench.driver import DriverLimits, SeatDriver
    from bench.llm import LLMResponse, ToolCall
    from bench.session import Decision, GameSession

    s = GameSession("t-clock-pause", "A", "B", seed=1)
    s.home.team = s.game.state.home_team
    s.home.decision = Decision(1, "turn-1-1", "Turn", False)
    deadlines = []

    class QuickLLM:
        async def chat(self, messages, tools, deadline=None):
            deadlines.append(deadline - time.time())
            return LLMResponse(tool_calls=[ToolCall(id="c", name="move", arguments={})],
                               raw_message={"role": "assistant", "content": ""})

        async def aclose(self):
            pass

    class WaitingClient:
        calls = 0

        async def call_tool(self, name, args):
            WaitingClient.calls += 1
            await anyio.sleep(0.4)   # the opponent is deciding
            if WaitingClient.calls == 4:
                s.home.decision = None   # the turn is over
            return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")], is_error=False)

    d = SeatDriver(s, s.home, QuickLLM(), "A", DriverLimits(turn_time_limit=1.0), opponent_name="B")
    monkeypatch.setattr(d, "_intro", lambda dec: "situation")
    forced = []
    monkeypatch.setattr(s.home, "force_episode", lambda key: forced.append(key))
    anyio.run(d.play_episode, WaitingClient(), [], s.home.decision)
    assert WaitingClient.calls == 4          # 1.6s of waiting on the opponent inside a 1s turn
    assert not forced and d.usage["budget_exhausted"] == 0
    assert min(deadlines) > 0.8              # every call still had (nearly) the whole turn


def test_driver_reports_the_reply_it_is_waiting_on(monkeypatch):
    """Spectators see whose reply is in flight, since when and when the turn clock runs out; nothing once it's in."""
    import anyio
    import types
    from bench.driver import DriverLimits, SeatDriver
    from bench.llm import LLMResponse, ToolCall
    from bench.session import Decision, GameSession

    s = GameSession("t-waiting", "A", "B", seed=1)
    s.home.team = s.game.state.home_team
    s.home.decision = Decision(1, "turn-1-1", "Turn", False)
    seen = []

    class LLM:
        async def chat(self, messages, tools, deadline=None):
            seen.append(dict(d.waiting))
            return LLMResponse(tool_calls=[ToolCall(id="c", name="move", arguments={})],
                               raw_message={"role": "assistant", "content": ""})

    class Client:
        async def call_tool(self, name, args):
            s.home.decision = None
            return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")], is_error=False)

    d = SeatDriver(s, s.home, LLM(), "A", DriverLimits(turn_time_limit=300.0), opponent_name="B")
    monkeypatch.setattr(d, "_intro", lambda dec: "situation")
    t = time.time()
    anyio.run(d.play_episode, Client(), [], s.home.decision)
    assert len(seen) == 1 and abs(seen[0]["since"] - t) < 1 and abs(seen[0]["turn_ends"] - (t + 300)) < 1
    assert d.waiting is None


def test_driver_does_not_replay_an_episode_handed_to_the_fallback():
    """After an auto-finish the seat's decision stays set until the game thread takes the fallback action;
    the driver must not start a second (empty) episode for it."""
    import anyio
    from bench.driver import DriverLimits, SeatDriver
    from bench.session import Decision, GameSession

    s = GameSession("t-stale-decision", "A", "B", seed=1)
    d = SeatDriver(s, s.home, None, "A", DriverLimits(), opponent_name="B")
    s.home.decision = Decision(1, "turn-1-1", "Turn", True)
    s.home.autopilot_key = "turn-1-1"
    episodes = []

    async def fake_episode(client, tools, dec):
        episodes.append(dec.key)

    d.play_episode = fake_episode

    def clear_later():
        time.sleep(0.3)
        with s.home.cond:
            s.home.decision = None
            s.finished = True
            s.home.cond.notify_all()

    class NoLLM:
        async def aclose(self):
            pass

    d.llm = NoLLM()
    t = threading.Thread(target=clear_later)
    t.start()
    anyio.run(d.run)
    t.join()
    assert episodes == []


def test_missed_reflection_is_a_short_bounded_call():
    """The reflection after a turnover runs while the opponent plays, so it is kept as short as a last-chance
    call instead of getting the full thinking budget and no deadline."""
    import anyio
    from bench.driver import DriverLimits, SeatDriver
    from bench.llm import LLMResponse

    seen = {}

    class RecordingLLM:
        async def chat(self, messages, tools, deadline=None, hurry=False):
            seen.update(deadline=None if deadline is None else deadline - time.time(), hurry=hurry)
            return LLMResponse(raw_message={"role": "assistant", "content": ""})

        async def aclose(self):
            pass

    s = GameSession("t-reflect-bound", "A", "B", seed=1)
    d = SeatDriver(s, s.home, RecordingLLM(), "A", DriverLimits(last_chance_seconds=45), opponent_name="B")
    d._oa_tools = [{"type": "function", "function": {"name": "reflect", "description": "", "parameters": {}}}]
    anyio.run(d._missed_reflection, [{"role": "user", "content": "turn over"}], None, "turn-1-1", 0)
    assert seen["hurry"] is True and 40 < seen["deadline"] <= 45


def test_claude_models_keep_auto_tool_choice_so_they_can_think():
    from bench.llm import OpenRouterLLM
    assert OpenRouterLLM("anthropic/claude-haiku-5.5", "k", tool_choice="required").tool_choice == "auto"
    assert OpenRouterLLM("openai/gpt-5-mini", "k", tool_choice="required").tool_choice == "required"


def test_deepseek_tool_call_markup_in_content_is_recovered():
    from bench.llm import OpenRouterLLM
    content = ('Receive.\n<｜DSML｜invoke name="take_action">\n'
               '<｜DSML｜parameter name="action_type" string="true">RECEIVE</｜DSML｜parameter>\n'
               '<｜DSML｜parameter name="x" string="false">4</｜DSML｜parameter>\n'
               '</｜DSML｜invoke>\n</｜DSML｜function_calls>')
    resp = OpenRouterLLM._parse({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}, 1.0)
    assert [(t.name, t.arguments) for t in resp.tool_calls] == [("take_action", {"action_type": "RECEIVE", "x": 4})]
    assert resp.content == "Receive."
    assert resp.raw_message["tool_calls"][0]["function"]["name"] == "take_action"
    # an empty invoke (no parameters) works too
    resp = OpenRouterLLM._parse({"choices": [{"message": {
        "content": '<｜DSML｜invoke name="get_legal_actions">\n\n</｜DSML｜invoke>'}}]}, 1.0)
    assert [(t.name, t.arguments) for t in resp.tool_calls] == [("get_legal_actions", {})]


def test_setup_decision_shows_the_half_and_what_a_formation_placed():
    s = GameSession("t-setup", "A", "B", seed=11)
    ready = {}
    ev = threading.Event()

    def other(seat):
        while True:
            d = seat.wait_for_decision()
            if d is None:
                return
            if d.proc == "Setup" and not ev.is_set():
                ready["seat"] = seat
                ev.set()
                return
            seat.submit(fallback_action(s.game, seat.team))

    threads = [threading.Thread(target=other, args=(x,), daemon=True) for x in (s.home, s.away)]
    s.start()
    for t in threads:
        t.start()
    try:
        assert ev.wait(30)
        tools = SeatTools(s, ready["seat"])
        before = tools.get_legal_actions()
        assert "Your half is columns x=" in before and "Players on the pitch: 0" in before
        assert "Not a legal setup yet" in before
        formation = [a.action_type.name for a in s.game.state.available_actions
                     if a.action_type.name.startswith("SETUP_FORMATION_")][0]
        after = tools.take_action(formation)
        assert "This setup is legal: END_SETUP" in after and "Players on the pitch: 5" in after
    finally:
        s.abort()


def test_hurried_call_uses_the_lowest_effort_openrouter_lists(monkeypatch):
    import anyio
    import httpx
    import bench.llm
    monkeypatch.setattr(bench.llm, "_LOWEST_EFFORT", {})
    efforts = {"maker/high-or-none": {"supported_efforts": ["high", "none"]},
               "maker/mandatory": {"mandatory": True, "supported_efforts": ["high", "medium", "low", "minimal", "none"]},
               "maker/budget-only": {"supports_max_tokens": True}}
    bodies, gets = [], []

    def handler(request):
        if request.method == "GET":
            gets.append(1)
            return httpx.Response(200, json={"data": [{"id": k, "reasoning": v} for k, v in efforts.items()]})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                                         "usage": {}})

    async def go(model, hurry, **kw):
        llm = bench.llm.OpenRouterLLM(model, "k", max_tokens=16384, **(kw or {"reasoning_max_tokens": 12000}))
        llm.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await llm.chat([{"role": "system", "content": "x"}], _TOOLS, hurry=hurry)
        finally:
            await llm.aclose()
        return bodies[-1]["reasoning"]

    assert anyio.run(go, "maker/high-or-none", False) == {"max_tokens": 12000}   # normal calls keep the budget
    assert anyio.run(go, "maker/high-or-none", True) == {"effort": "none"}
    assert anyio.run(go, "maker/mandatory", True) == {"effort": "minimal"}
    assert anyio.run(go, "maker/budget-only", True) == {"max_tokens": 1024}
    assert len(gets) == 3   # one lookup per model
    # at the model's default effort, a hurried call still gets the small budget (or lowest effort)
    assert anyio.run(lambda: go("maker/budget-only", False, reasoning_enabled=True)) == {"enabled": True}
    assert anyio.run(lambda: go("maker/budget-only", True, reasoning_enabled=True)) == {"max_tokens": 1024}
    assert anyio.run(lambda: go("maker/mandatory", True, reasoning_enabled=True)) == {"effort": "minimal"}


def test_touchdown_frame_recorded(tmp_path, monkeypatch):
    """Each touchdown gets its own frame: the scorer still on the pitch with the ball, and the new score."""
    import zlib
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    runner = MatchRunner("t-td", SCRIPTED, RANDOM, {})
    result = runner.run()
    assert result["error"] is None
    frames = [json.loads(zlib.decompress(f)) for f in runner.session.frames]
    scores = [(f["state"]["home_team"]["state"]["score"], f["state"]["away_team"]["state"]["score"]) for f in frames]
    goals = [i for i in range(1, len(frames)) if scores[i] != scores[i - 1]]
    assert len(goals) == result["home_score"] + result["away_score"] > 0
    for i in goals:
        f = frames[i]
        assert any("scored a Touchdown" in t for t in f["bench"]["log"])
        ball = f["state"]["pitch"]["balls"][0]
        assert ball["is_carried"] and ball["position"]   # the board still shows the scorer, not the next kick-off


def test_key_spending_limit_403_is_infra_error():
    """Hitting our OpenRouter key's spending limit pauses the game (like running out of credits) instead of marking
    both models unavailable; other 403s, such as moderation, stay model-specific."""
    import anyio
    import httpx
    from bench.llm import LLMError
    bodies = {
        "limit": {"error": {"message": "Key limit exceeded (monthly limit).", "code": 403,
                            "metadata": {"limit_source": "openrouter_key_limit", "scope": "key_limit"}}},
        "moderation": {"error": {"message": "Input was flagged by moderation", "code": 403,
                                 "metadata": {"reasons": ["violence"]}}},
    }
    for kind, infra in (("limit", True), ("moderation", False)):
        async def go():
            llm = _mock_llm(lambda request: httpx.Response(403, json=bodies[kind]))
            try:
                await llm.chat([{"role": "system", "content": "x"}], _TOOLS)
            finally:
                await llm.aclose()

        with pytest.raises(LLMError) as ei:
            anyio.run(go)
        assert ei.value.fatal and ei.value.infra is infra, kind


def test_games_played_past_the_key_limit_are_requeued_on_startup(bench_env, monkeypatch):
    """Games finished on default actions after the key limit was hit go back in the queue as fresh games, once."""
    import bench.scheduler as sch
    b = sch.Bench()
    b.sync_models(force=True)
    bad, good = db.rows("SELECT * FROM matches WHERE status='queued' ORDER BY seq LIMIT 2")
    for m in (bad, good):
        db.execute("UPDATE matches SET status='completed', home_score=1, away_score=0, winner='home', "
                   "finished_at=1, frames=5, admissible=0 WHERE id=?", (m["id"],))
        sch.save_frames(m["id"], [b"x"])
    db.execute("UPDATE tournaments SET status='completed', finished_at=1")
    db.add_event(bad["id"], "away", "error", {"text": 'OpenRouter HTTP 403: {"error":{"message":"Key limit exceeded"'})
    db.add_event(good["id"], "away", "error", {"text": "OpenRouter HTTP 403: flagged by moderation"})

    monkeypatch.setattr(sch, "_bench", None)
    sch.Bench()
    row = db.row("SELECT * FROM matches WHERE id=?", (bad["id"],))
    assert row["status"] == "queued" and row["home_score"] is None and row["frames"] == 0
    assert not db.get_events(bad["id"], limit=10) and not os.path.exists(sch.frames_path(bad["id"]))
    assert db.row("SELECT status FROM tournaments WHERE id=?", (bad["tournament_id"],))["status"] == "queued"
    assert db.row("SELECT status FROM matches WHERE id=?", (good["id"],))["status"] == "completed"
