import json
import os
import threading
import time

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
    for key in ("aggression", "risk_taking", "passing_game", "chattiness", "illegal_rate"):
        assert key in hs


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
        # a real action: end the turn -> returns once the opponent's turn is over (or our phase changes)
        out = tools.end_turn()
        assert not out.startswith("ILLEGAL")
    finally:
        s.abort()


def test_openrouter_driver_against_fake_api(fake_openrouter):
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model", "max_tool_calls_per_turn": 12}
    events = []
    runner = MatchRunner("t3", cfg, RANDOM, {"budget_usd_per_game": 10},
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


def test_openrouter_auth_failure_is_infra_error(fake_openrouter):
    fake_openrouter.status = 401
    cfg = {"name": "Fake LLM", "provider": "openrouter", "model": "fake/model"}
    result = MatchRunner("t4", cfg, RANDOM, {}, api_key="bad").run()
    assert result["infra_error"] and "401" in result["infra_error"]


def test_scheduler_round_robin_gauntlet_and_retire(bench_env):
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

    # adding a model schedules a gauntlet against the 3 existing ones, home and away
    time.sleep(0.01)
    bench_env.write_text(bench_env.read_text() + "  - name: Newcomer\n    provider: random\n")
    os.utime(bench_env, (time.time() + 5, time.time() + 5))
    assert b.sync_models() == ["newcomer"]
    g = db.row("SELECT * FROM tournaments WHERE kind='gauntlet'")
    assert g["focus_model"] == "newcomer"
    assert db.row("SELECT COUNT(*) n FROM matches WHERE tournament_id=?", (g["id"],))["n"] == 6

    # removing a model cancels its queued games but keeps history
    bench_env.write_text(bench_env.read_text().replace("  - name: Newcomer\n    provider: random\n", ""))
    os.utime(bench_env, (time.time() + 10, time.time() + 10))
    b.sync_models()
    assert db.row("SELECT COUNT(*) n FROM matches WHERE status='queued' AND "
                  "(home_model='newcomer' OR away_model='newcomer')")["n"] == 0
    assert db.row("SELECT enabled FROM models WHERE id='newcomer'")["enabled"] == 0


def test_ratings():
    ms = [
        {"home_model": "a", "away_model": "b", "winner": "home", "home_score": 2, "away_score": 0, "finished_at": 1},
        {"home_model": "b", "away_model": "a", "winner": "draw", "home_score": 1, "away_score": 1, "finished_at": 2},
    ]
    elo = ratings.compute_elo(ms)
    assert elo["a"]["elo"] > 1000 > elo["b"]["elo"]
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
