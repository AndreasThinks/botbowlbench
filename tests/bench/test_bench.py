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
    for key in ("aggression", "risk_taking", "passing_game", "chattiness", "illegal_rate", "monitoring_rate",
                "safe_first_rate", "turnover_causes", "reflection_coverage"):
        assert key in hs
    assert hs["actions_logged"] > 0 and hs["reflections"] > 0
    for st in (hs, as_):
        assert sum(st["turnover_causes"].values()) == st.get("turnovers", 0)
        assert st["turnovers_logged"] == st.get("turnovers", 0)   # every turnover attributed to an action
    assert result["meta"]["admissible"] and not result["meta"]["harness_errors"]


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
        out = tools.end_turn(plan="Push up the left wing.", prediction="They will blitz H1.")
        assert not out.startswith("ILLEGAL")
        assert s.reflections and s.reflections[0]["plan"] == "Push up the left wing."
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
    assert hs["cached_tokens"] == 600 * hs["llm_calls"] and hs["cache_hit_rate"] == 0.6
    assert hs["served"] == {"fake/model-20260901@FakeCloud": hs["llm_calls"]}
    assert hs["reflections"] > 0
    assert result["meta"]["seed"] == runner.seed and result["meta"]["harness"]["prompt_fingerprint"]


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
    assert all(e["side"] in ("home", "away") and e["payload"]["ht"] + e["payload"]["at"] > 0
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
