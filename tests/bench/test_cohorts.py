"""Newcomer placement cohorts: compatible, completely unstarted placements share one tournament."""
import json
import os
import time

import pytest

from bench import db, version

BASE = [f"M{i}" for i in range(6)]
EVERYONE = "placement_size: 0\n  placement_extra_games: 4\n"     # anchors = every other model, so newcomers overlap
_clock = [time.time() + 100]


class Env:
    """A bench whose models.yaml can be rewritten (new models, other settings) and re-synced."""

    def __init__(self, bench_env, bench_cls, settings="placement_size: 3\n  placement_extra_games: 4\n", base=BASE):
        self.path, self.settings = bench_env, settings
        self.models = list(base)
        self.disabled = set()
        self.write()
        self.b = bench_cls()
        self.b.sync_models(force=True)
        current = json.dumps({"harness": {"protocol_version": version.PROTOCOL_VERSION}})
        for m in db.rows("SELECT * FROM matches"):   # opening round robin: lower numbers win
            db.execute("UPDATE matches SET status='completed', winner=?, finished_at=?, meta=? WHERE id=?",
                       ("home" if m["home_model"] < m["away_model"] else "away", time.time(), current, m["id"]))
        self.b._finish_tournaments()

    def write(self, force=True):
        text = f"settings:\n  pause_between_matches: 0\n  {self.settings}models:\n"
        for n in self.models:
            text += f"  - name: {n}\n    provider: random\n" + ("    enabled: false\n" if n in self.disabled else "")
        self.path.write_text(text)
        _clock[0] += 10
        os.utime(self.path, (_clock[0], _clock[0]))

    def add(self, *names, sync=True):
        self.models += names
        self.write()
        return self.b.sync_models() if sync else None

    def sync(self):
        self.write()
        return self.b.sync_models(force=True)


@pytest.fixture
def env(bench_env):
    from bench.scheduler import Bench
    return Env(bench_env, Bench)


def placements(status=None):
    return [t for t in db.rows("SELECT * FROM tournaments WHERE kind='placement' ORDER BY id")
            if status is None or t["status"] == status]


def queue(tid):
    return db.rows("SELECT * FROM matches WHERE tournament_id=? AND status='queued' ORDER BY seq", (tid,))


def snapshot():
    return (db.rows("SELECT id, tournament_id, seq, home_model, away_model, status FROM matches ORDER BY id"),
            db.rows("SELECT * FROM placement_fixtures ORDER BY match_id, model_id"),
            db.rows("SELECT * FROM placement_members ORDER BY tournament_id, model_id"),
            db.rows("SELECT id, status, name FROM tournaments ORDER BY id"))


def complete(match_ids, winner="draw"):
    current = json.dumps({"harness": {"protocol_version": version.PROTOCOL_VERSION}})
    for mid in match_ids:
        db.execute("UPDATE matches SET status='completed', winner=?, started_at=?, finished_at=?, meta=? WHERE id=?",
                   (winner, time.time(), time.time(), current, mid))


def test_two_newcomers_in_separate_syncs_share_one_cohort(env):
    assert env.add("N1") == ["n1"]
    assert len(placements("queued")) == 1
    first_ids = {m["id"] for m in queue(placements()[0]["id"])}
    assert env.add("N2") == ["n2"]
    cohort = placements("queued")
    assert len(cohort) == 1 and cohort[0]["name"] == "Placement cohort: N1, N2"
    merged = placements("merged")
    assert len(merged) == 1 and json.loads(merged[0]["settings"])["merged_into"] == cohort[0]["id"]
    q = queue(cohort[0]["id"])
    assert len(q) == 6 + 6                       # 3 anchors x 2 legs for each
    assert first_ids <= {m["id"] for m in q}     # the older placement's games keep their ids
    assert [m["model_id"] for m in db.rows("SELECT model_id FROM placement_members WHERE tournament_id=? "
                                           "ORDER BY joined_at", (cohort[0]["id"],))] == ["n1", "n2"]
    assert db.row("SELECT COUNT(*) n FROM matches WHERE tournament_id=?", (merged[0]["id"],))["n"] == 0


def test_shared_directed_fixtures_are_played_once_and_count_for_both(env):
    env.settings = EVERYONE
    assert sorted(env.add("N1", "N2")) == ["n1", "n2"]
    cohort = placements("queued")
    assert len(cohort) == 1
    q = queue(cohort[0]["id"])
    pairs = [(m["home_model"], m["away_model"]) for m in q]
    assert len(pairs) == len(set(pairs)) == 14 + 14 - 2           # (N1,N2) and (N2,N1) are in both lists
    assert ("n1", "n2") in pairs and ("n2", "n1") in pairs        # both legs kept, home and away
    shared = [m for m in q if {m["home_model"], m["away_model"]} == {"n1", "n2"}]
    for m in shared:
        assert {r["model_id"] for r in db.rows("SELECT model_id FROM placement_fixtures WHERE match_id=?",
                                               (m["id"],))} == {"n1", "n2"}
    prog = env.b.placement_progress(cohort[0]["id"])
    assert prog["n1"]["anchor_total"] == 14 and prog["n2"]["anchor_total"] == 14


def test_three_newcomers_repeated_syncs_are_idempotent(env):
    env.add("N1")
    env.add("N2")
    env.add("N3")
    cohort = placements("queued")
    assert len(cohort) == 1 and len(env.b.placement_members(cohort[0]["id"])) == 3
    before = snapshot()
    for _ in range(3):
        assert env.sync() == []
        assert env.b.merge_placements() == []
    assert snapshot() == before
    # a fresh scheduler process (startup sync) changes nothing either
    from bench.scheduler import Bench
    Bench().sync_models(force=True)
    assert snapshot() == before


def test_cohort_games_are_interleaved_so_every_newcomer_plays_early(env):
    env.add("N1", "N2", "N3")
    q = queue(placements("queued")[0]["id"])
    first = q[:6]
    for n in ("n1", "n2", "n3"):
        assert sum(n in (m["home_model"], m["away_model"]) for m in first) >= 2
    # no newcomer is starved: the first game of each is within the first rounds
    assert {m for g in q[:3] for m in (g["home_model"], g["away_model"])} & {"n1", "n2", "n3"} == {"n1", "n2", "n3"}
    assert [m["seq"] for m in q] == list(range(len(q)))


def test_progress_and_top_ups_are_independent_per_newcomer(env):
    env.add("N1")
    env.add("N2")
    cohort = placements("queued")[0]
    # give N1 a smaller allowance than N2
    db.execute("UPDATE placement_members SET extra_games=2 WHERE model_id='n1'")
    assert env.b.member_top_ups(cohort) == {"n1": 2, "n2": 4}
    n1_anchor = {r["match_id"] for r in db.rows("SELECT match_id FROM placement_fixtures WHERE model_id='n1'")}
    complete(n1_anchor)                  # N1's anchors are done, N2's are not
    env.b._finish_tournaments()
    t = db.row("SELECT * FROM tournaments WHERE id=?", (cohort["id"],))
    prog = env.b.placement_progress(cohort["id"])
    assert prog["n1"]["anchor_done"] == 6 and prog["n2"]["anchor_done"] == 0
    assert prog["n1"]["top_up_total"] == 2           # N1 topped up on its own, N2 still on its anchors
    assert prog["n2"]["top_up_total"] == 0 and t["status"] != "completed"
    assert env.b.member_top_ups(t) == {"n2": 4}        # N1's allowance is fully scheduled
    # N1's allowance (2) is used up: finishing its top-up adds nothing more
    complete(m["id"] for m in queue(cohort["id"]) if db.row(
        "SELECT 1 FROM placement_fixtures WHERE match_id=? AND model_id='n1' AND role='top_up'", (m["id"],)))
    env.b._finish_tournaments()
    assert env.b.placement_progress(cohort["id"])["n1"]["top_up_total"] == 2
    # N2 finishes its anchors, then tops up within its own 4
    for _ in range(4):
        complete(m["id"] for m in queue(cohort["id"]))
        env.b._finish_tournaments()
    t = db.row("SELECT * FROM tournaments WHERE id=?", (cohort["id"],))
    prog = env.b.placement_progress(cohort["id"])
    assert prog["n2"]["top_up_total"] <= 4 and prog["n1"]["top_up_total"] == 2
    assert t["status"] == "completed" and env.b.possible_top_ups(t) == 0


def test_top_up_fixture_wanted_by_two_newcomers_is_played_once(env):
    env.settings = EVERYONE
    env.add("N1", "N2")
    cohort = placements("queued")[0]
    complete(m["id"] for m in queue(cohort["id"]))
    env.b._finish_tournaments()
    q = queue(cohort["id"])
    pairs = [(m["home_model"], m["away_model"]) for m in q]
    assert len(pairs) == len(set(pairs))
    prog = env.b.placement_progress(cohort["id"])
    for n in ("n1", "n2"):
        assert prog[n]["top_up_total"] == sum(
            1 for r in db.rows("SELECT * FROM placement_fixtures WHERE model_id=? AND role='top_up'", (n,)))


def test_incompatible_settings_stay_separate_and_execution_is_pinned(env):
    env.add("N1", "N2")                                                     # a cohort, pinned to the default game mode
    env.settings = "game_mode: '3'\n  placement_size: 3\n  placement_extra_games: 4\n"
    env.add("N3")
    assert len(placements("queued")) == 2 and len(placements("merged")) == 1   # N3 is created under other settings
    env.add("N4")                                                           # N4 is compatible with N3, not the cohort
    queued = placements("queued")
    assert len(queued) == 2
    by_members = {tuple(m["model_id"] for m in env.b.placement_members(t["id"])) for t in queued}
    assert by_members == {("n1", "n2"), ("n3", "n4")}
    first, second = queued
    assert env.b._settings_for(first["id"])["game_mode"] == "5"
    assert env.b._settings_for(second["id"])["game_mode"] == "3"
    # a later hot reload doesn't change the settings a cohort's games run under, and nothing new merges across
    env.settings = "game_mode: '7'\n  placement_size: 3\n  placement_extra_games: 4\n"
    env.sync()
    assert env.b._settings_for(first["id"])["game_mode"] == "5"
    assert env.b._settings_for(second["id"])["game_mode"] == "3"
    env.add("N5")                                                           # under 7: neither cohort fits
    assert len(placements("queued")) == 3
    env.settings = "game_mode: '5'\n  placement_size: 3\n  placement_extra_games: 4\n"      # back to the first cohort's
    env.add("N6")       # an unstarted standalone plays under the live settings, so N5 now fits there too
    assert len(placements("queued")) == 2 and len(env.b.placement_members(first["id"])) == 4


def test_cohort_games_run_under_the_pinned_settings(env, monkeypatch):
    import bench.scheduler as sched
    env.settings = "game_mode: '3'\n  placement_size: 3\n"
    env.add("N1", "N2")
    cohort = placements("queued")[0]
    env.settings = "game_mode: '7'\n  placement_size: 3\n"
    env.sync()
    seen = {}

    class Stop(Exception):
        pass

    def fake_runner(match_id, home, away, settings, **kwargs):
        seen["settings"] = settings
        raise Stop()

    monkeypatch.setattr(sched, "MatchRunner", fake_runner)
    env.b.play(env.b.next_match())
    assert seen["settings"]["game_mode"] == "3"
    assert env.b.settings["game_mode"] == "7"


def test_stale_protocol_label_on_unstarted_cohort_is_refreshed(env, monkeypatch):
    env.add("N1", "N2")
    cohort = placements("queued")[0]
    snap = json.loads(cohort["settings"])
    assert snap["pinned"] and snap["protocol_version"] == version.PROTOCOL_VERSION
    db.execute("UPDATE tournaments SET settings=? WHERE id=?", (json.dumps({**snap, "protocol_version": "0.9"}), cohort["id"]))
    env.add("N3")
    assert len(placements("queued")) == 1
    assert json.loads(placements("queued")[0]["settings"])["protocol_version"] == version.PROTOCOL_VERSION


def _legacy_placement(model, opponents, base=None, started=False, status="queued", match_started=False, events=False,
                      created=None):
    """A placement as made before cohorts: no member or fixture rows."""
    created = created or time.time()
    settings = json.dumps({"placement_base": len(opponents) * 2 if base is None else base})
    tid = db.execute("INSERT INTO tournaments(kind, name, status, focus_model, settings, created_at, started_at) "
                     "VALUES('placement', ?, ?, ?, ?, ?, ?)",
                     (f"Placement: {model}", status, model, settings, created, created if started else None))
    ids = []
    for i, (h, a) in enumerate([x for o in opponents for x in ((model, o), (o, model))]):
        mid = f"legacy-{model}-{i}"
        db.execute("INSERT INTO matches(id, tournament_id, seq, home_model, away_model, status, created_at, started_at) "
                   "VALUES(?,?,?,?,?,?,?,?)", (mid, tid, i, h, a, "queued", created,
                                               created if (match_started and i == 0) else None))
        if events and i == 0:
            db.add_event(mid, None, "system", {"text": "begun"})
        ids.append(mid)
    return tid, ids


def _models(env, *ids):
    env.models += [i.upper() for i in ids]          # keep them in models.yaml, or the next sync retires them
    for i in ids:
        db.execute("INSERT OR IGNORE INTO models(id, name, provider, config, enabled, added_at) VALUES(?,?,?,?,1,?)",
                   (i, i.upper(), "random", "{}", time.time()))


def test_legacy_unstarted_placements_join_on_sync_and_keep_their_games(env):
    _models(env, "l1", "l2")
    t1, ids1 = _legacy_placement("l1", ["m0", "m3"], created=time.time() - 50)
    t2, ids2 = _legacy_placement("l2", ["m0", "m5"], created=time.time() - 40)
    assert db.row("SELECT COUNT(*) n FROM placement_members")["n"] == 0
    env.sync()
    cohort = placements("queued")
    assert [t["id"] for t in cohort] == [t1]
    q = queue(t1)
    assert len(q) == 4 + 4                       # l1 and l2 share no fixture
    prog = env.b.placement_progress(t1)
    assert prog["l1"]["anchor_total"] == 4 and prog["l2"]["anchor_total"] == 4
    assert {m["id"] for m in q} == set(ids1 + ids2)         # fixture identities kept
    before = snapshot()
    env.sync()
    assert snapshot() == before


def test_legacy_started_or_recovered_placements_are_never_merged(env):
    _models(env, "l1", "l2", "l3", "l4", "l5")
    started, _ = _legacy_placement("l1", ["m0"], started=True)                       # tournament started
    recovered, ids_r = _legacy_placement("l2", ["m0"], match_started=True)             # game interrupted, back to queued
    evented, _ = _legacy_placement("l3", ["m0"], events=True)
    running, _ = _legacy_placement("l4", ["m0"], status="running", started=True)
    fresh, _ = _legacy_placement("l5", ["m0"])
    db.execute("UPDATE matches SET status='completed' WHERE tournament_id=? AND seq=0", (started,))
    before = db.rows("SELECT id, tournament_id, seq, status FROM matches WHERE tournament_id IN (?,?,?,?)",
                     (started, recovered, evented, running))
    env.sync()
    assert db.rows("SELECT id, tournament_id, seq, status FROM matches WHERE tournament_id IN (?,?,?,?)",
                   (started, recovered, evented, running)) == before
    for tid in (started, recovered, evented, running):
        assert db.row("SELECT status FROM tournaments WHERE id=?", (tid,))["status"] != "merged"
    assert db.row("SELECT status FROM tournaments WHERE id=?", (fresh,))["status"] == "queued"    # a lone unstarted one


def test_recovery_after_restart_keeps_a_started_cohort_out_of_merges(env):
    env.add("N1", "N2")
    cohort = placements("queued")[0]
    m = env.b.next_match()
    db.execute("UPDATE matches SET status='running', started_at=?  WHERE id=?", (time.time(), m["id"]))
    db.execute("UPDATE tournaments SET status='running', started_at=? WHERE id=?", (time.time(), cohort["id"]))
    from bench.scheduler import Bench
    b2 = Bench()                      # restart: the running game goes back to the queue
    assert db.row("SELECT status FROM tournaments WHERE id=?", (cohort["id"],))["status"] == "running"
    env.models += ["N3"]
    env.write()
    b2.sync_models(force=True)
    assert len(placements("queued")) == 1                  # N3 alone: the started cohort did not take it
    assert len(b2.placement_members(cohort["id"])) == 2
    # and a tournament recovery put back to 'queued' after the game ended is still "started"
    db.execute("UPDATE tournaments SET status='queued' WHERE id=?", (cohort["id"],))
    b2.sync_models(force=True)
    assert len(b2.placement_members(cohort["id"])) == 2


def test_manual_round_robins_and_opening_tournament_are_never_merged(env):
    env.b.create_round_robin(name="Manual A")
    env.b.create_round_robin(name="Manual B")
    env.add("N1", "N2")
    kinds = db.rows("SELECT kind, status, name FROM tournaments ORDER BY id")
    assert [k["kind"] for k in kinds].count("round_robin") == 3
    assert all(k["status"] != "merged" for k in kinds if k["kind"] == "round_robin")
    assert db.row("SELECT COUNT(*) n FROM matches m JOIN tournaments t ON t.id=m.tournament_id "
                  "WHERE t.kind='round_robin' AND m.status='queued'")["n"] == 2 * 30
    assert db.row("SELECT COUNT(*) n FROM placement_members m JOIN tournaments t ON t.id=m.tournament_id "
                  "WHERE t.kind!='placement'")["n"] == 0


def test_disabled_and_removed_models_are_safe(env):
    env.add("N1", "N2", "N3")
    cohort = placements("queued")[0]
    env.disabled = {"N2"}
    env.sync()
    assert db.row("SELECT COUNT(*) n FROM matches WHERE status='queued' AND (home_model='n2' OR away_model='n2')")["n"] == 0
    assert env.b.member_top_ups(cohort).keys() == {"n1", "n3"}       # no top-ups for a retired newcomer
    prog = env.b.placement_progress(cohort["id"])
    assert prog["n2"]["cancelled"] > 0 and prog["n1"]["anchor_total"] > 0
    before = snapshot()
    env.sync()
    assert snapshot() == before
    # another newcomer still joins the (never started) cohort, cancelled leftovers staying put
    env.add("N4")
    assert len(placements("queued")) == 1
    assert {m["model_id"] for m in db.rows("SELECT model_id FROM placement_members")} >= {"n1", "n2", "n3", "n4"}
    # the retired newcomer's games never come back
    assert db.row("SELECT COUNT(*) n FROM matches WHERE status='queued' AND (home_model='n2' OR away_model='n2')")["n"] == 0
    # removing the model from the file entirely behaves the same
    env.models.remove("N3")
    env.sync()
    assert db.row("SELECT COUNT(*) n FROM matches WHERE status='queued' AND (home_model='n3' OR away_model='n3')")["n"] == 0


def test_a_failed_merge_leaves_everything_as_it_was(env, monkeypatch):
    env.add("N1")
    env.models += ["N2"]
    env.write()
    from bench.scheduler import Bench
    original = Bench._merge_group

    def boom(self, c, group):
        original(self, c, group)
        raise RuntimeError("crash mid-merge")

    monkeypatch.setattr(Bench, "_merge_group", boom)
    with pytest.raises(RuntimeError):
        env.b.sync_models(force=True)
    monkeypatch.setattr(Bench, "_merge_group", original)
    assert not placements("merged")
    assert len(placements("queued")) == 2           # both standalone placements intact (N2's was committed first)
    for t in placements("queued"):
        assert len(queue(t["id"])) > 0
    env.sync()
    assert len(placements("queued")) == 1


def test_backfill_for_databases_from_before_cohorts(env):
    tid, ids = _legacy_placement("m0", ["m1", "m2"], base=4)
    # two of its games were top-ups, played long ago
    db.execute("INSERT INTO matches(id, tournament_id, seq, home_model, away_model, status, created_at, started_at) "
               "VALUES('legacy-extra', ?, 4, 'm0', 'm3', 'completed', 1, 1)", (tid,))
    db.execute("DROP TABLE placement_fixtures")
    db.execute("DROP TABLE placement_members")
    db.init(db._DB_PATH)       # the start-up migration creates the tables and derives members and links
    db.init(db._DB_PATH)       # ... and is a no-op the second time
    mem = db.rows("SELECT * FROM placement_members WHERE tournament_id=?", (tid,))
    assert len(mem) == 1 and mem[0]["model_id"] == "m0" and mem[0]["adaptive"] == 1 and mem[0]["extra_games"] is None
    roles = {r["match_id"]: r["role"] for r in db.rows("SELECT * FROM placement_fixtures")}
    assert roles["legacy-extra"] == "top_up" and roles[ids[0]] == "anchor" and len(roles) == 5
    # placement_base absent -> no top-ups, all anchors
    tid2, _ = _legacy_placement("m1", ["m2"], base=None)
    db.execute("UPDATE tournaments SET settings='{}' WHERE id=?", (tid2,))
    db.backfill_placements()
    assert db.row("SELECT adaptive FROM placement_members WHERE tournament_id=?", (tid2,))["adaptive"] == 0


def test_standalone_placement_behaviour_is_unchanged(env):
    env.add("N1")
    t = placements("queued")[0]
    assert t["name"] == "Placement: N1" and t["focus_model"] == "n1"
    assert [(m["home_model"], m["away_model"]) for m in queue(t["id"])][:2] == [("n1", "m0"), ("m0", "n1")]
    assert env.b.possible_top_ups(t) == 4
    prog = env.b.placement_progress(t["id"])
    assert prog["n1"] == {**prog["n1"], "anchor_total": 6, "anchor_done": 0, "top_up_total": 0}
    assert not json.loads(t["settings"]).get("pinned")


def test_budget_is_frozen_when_a_newcomer_joins(env):
    env.add("N1")
    env.settings = "placement_size: 3\n  placement_extra_games: 40\n"
    env.sync()
    t = placements("queued")[0]
    assert env.b.possible_top_ups(t) == 4                  # a later reload doesn't widen a running placement's allowance


def test_api_and_pages_show_cohort_membership_and_progress(env):
    from bench.web import create_app
    env.settings = EVERYONE
    env.add("N1", "N2")
    cohort = placements("queued")[0]
    complete([m["id"] for m in queue(cohort["id"])][:2])
    app = create_app(start_scheduler=False)
    c = app.test_client()
    d = json.loads(c.get(f"/api/tournaments/{cohort['id']}").data)
    assert [m["name"] for m in d["members"]] == ["N1", "N2"]
    by = {m["model"]: m for m in d["members"]}
    assert by["n1"]["anchor_total"] == 14 and by["n2"]["anchor_total"] == 14
    assert by["n1"]["anchor_done"] + by["n2"]["anchor_done"] >= 2
    assert by["n1"]["top_ups_possible"] == 4 and by["n2"]["top_ups_possible"] == 4
    shared = [m for m in d["matches"] if not m.get("tbc") and len(m["for_models"]) == 2]
    assert len(shared) == 2 and all(set(m["for_models"]) == {"n1", "n2"} for m in shared)
    tbc = [m for m in d["matches"] if m.get("tbc")]
    assert {m["home_model"] for m in tbc} == {"n1", "n2"} and all(m["games"] == 4 for m in tbc)
    lst = json.loads(c.get("/api/tournaments").data)
    assert [t["id"] for t in lst if t["kind"] == "placement"] == [cohort["id"]]          # merged shells are hidden
    assert next(t for t in lst if t["id"] == cohort["id"])["newcomers"] == ["N1", "N2"]
    merged = placements("merged")[0]
    md = json.loads(c.get(f"/api/tournaments/{merged['id']}").data)
    assert md["tournament"]["settings"]["merged_into"] == cohort["id"] and md["matches"] == []
    st = json.loads(c.get("/api/status").data)
    assert [q for q in st["queued"] if q.get("tbc") is None]
    for page in (f"/tournaments/{cohort['id']}", "/tournaments", "/about"):
        assert c.get(page).status_code == 200


def test_standalone_api_reports_its_single_newcomer(env):
    from bench.web import create_app
    env.add("N1")
    t = placements("queued")[0]
    c = create_app(start_scheduler=False).test_client()
    d = json.loads(c.get(f"/api/tournaments/{t['id']}").data)
    assert len(d["members"]) == 1 and d["members"][0]["top_ups_possible"] == 4
    assert all(m.get("tbc") or m["for_models"] == ["n1"] for m in d["matches"])


def test_play_skips_a_game_a_merge_dropped(env):
    env.add("N1", "N2")
    stale = env.b.next_match()
    db.execute("DELETE FROM placement_fixtures WHERE match_id=?", (stale["id"],))
    db.execute("DELETE FROM matches WHERE id=?", (stale["id"],))
    assert env.b.play(stale).get("skipped") is True


def test_merge_reads_and_writes_stay_in_one_transaction_until_it_fails(env, monkeypatch):
    """Selection, relabel and merge writes all happen inside the BEGIN IMMEDIATE block; a failure after them undoes
    every one, including a write made during selection (a committing read helper would have kept it)."""
    from bench.scheduler import Bench
    orig_mp = Bench.merge_placements
    monkeypatch.setattr(Bench, "merge_placements", lambda self: [])      # two standalone placements, unmerged
    env.add("N1")
    env.add("N2")
    monkeypatch.setattr(Bench, "merge_placements", orig_mp)
    assert len(placements("queued")) == 2
    before = snapshot()
    seen = []
    orig_unstarted, orig_settings, orig_merge = Bench._unstarted, Bench._settings_for, Bench._merge_group

    def unstarted(c, t):
        assert c.in_transaction
        c.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('selection_marker', '1')")
        seen.append("unstarted")
        return orig_unstarted(c, t)

    def settings_for(self, tid, c=None):
        out = orig_settings(self, tid, c)
        assert c is not None and c.in_transaction
        assert db.row("SELECT 1 AS x FROM tournaments")        # a helper read must not end the transaction
        assert c.in_transaction
        seen.append("settings")
        return out

    def merge(self, c, group):
        assert c.in_transaction
        out = orig_merge(self, c, group)
        assert c.in_transaction
        seen.append("merge")
        raise RuntimeError("crash after merge writes")

    monkeypatch.setattr(Bench, "_unstarted", staticmethod(unstarted))
    monkeypatch.setattr(Bench, "_settings_for", settings_for)
    monkeypatch.setattr(Bench, "_merge_group", merge)
    with pytest.raises(RuntimeError):
        env.b.merge_placements()
    assert {"unstarted", "settings", "merge"} <= set(seen)
    assert db.get_meta("selection_marker") is None
    assert snapshot() == before
    with db.conn() as c:
        assert not c.in_transaction


def test_db_transaction_cannot_be_nested_and_helpers_join_it(env):
    with pytest.raises(RuntimeError):
        with db.transaction() as c:
            c.execute("INSERT INTO meta(key, value) VALUES('tx_probe', '1')")
            db.set_meta("tx_probe2", "1")                  # helper write joins the transaction
            assert c.in_transaction
            with db.transaction():
                pass
    assert db.get_meta("tx_probe") is None and db.get_meta("tx_probe2") is None


def test_cancelled_top_ups_still_spend_the_allowance(env):
    env.add("N1")
    cohort = placements("queued")[0]
    allowance = 4
    complete(m["id"] for m in queue(cohort["id"]))          # the anchors finish
    seen_cancelled = 0
    for _ in range(allowance + 4):
        env.b._finish_tournaments()
        tops = [m for m in queue(cohort["id"]) if db.row(
            "SELECT 1 FROM placement_fixtures WHERE match_id=? AND role='top_up'", (m["id"],))]
        if not tops:
            break
        victim = tops[0]["away_model"]                      # disabling the opponent cancels the queued top-up
        env.disabled.add(next(n for n in env.models if n.lower() == victim))
        env.sync()
        env.b._finish_tournaments()
        env.disabled.clear()
        env.sync()
        prog = env.b.placement_progress(cohort["id"])["n1"]
        assert prog["top_up_scheduled"] <= allowance
        assert prog["cancelled"] >= seen_cancelled
        seen_cancelled = prog["cancelled"]
        assert prog["top_up_total"] + db.row(
            "SELECT COUNT(*) n FROM placement_fixtures f JOIN matches m ON m.id=f.match_id WHERE f.model_id='n1' "
            "AND f.role='top_up' AND m.status='cancelled'")["n"] == prog["top_up_scheduled"]   # totals stay accurate
    prog = env.b.placement_progress(cohort["id"])["n1"]
    assert prog["top_up_scheduled"] == allowance and seen_cancelled > 0
    t = db.row("SELECT * FROM tournaments WHERE id=?", (cohort["id"],))
    assert env.b.member_top_ups(t) == {}
    env.b._finish_tournaments()
    assert env.b.placement_progress(cohort["id"])["n1"]["top_up_scheduled"] == allowance
