"""
The tournament scheduler: watches models.yaml, creates tournaments and plays queued matches one
after the other in a background thread.

* First start with >= 2 enabled models  -> a round-robin tournament between all of them.
* A model appears that wasn't known before -> a "placement": the newcomer plays a handful of anchor models spread
  across the current ratings, then a few top-up games against the opponents that tell us most about it, until its
  rating interval is narrow enough or the top-up allowance is used. The cost per newcomer stays flat as the list grows.
  Newcomers that are still completely unstarted and compatible (same protocol, same game settings) are combined into
  one *placement cohort*: their games are interleaved so all of them play early, a game two newcomers have in common
  is played once and counts for both, and each newcomer keeps its own anchors, top-up budget and stopping rule.
* A model is disabled/removed -> its queued matches are cancelled (history is kept).
* A condition under a Tripwire health hold (bench/health.py) keeps its queued matches, in order: the scheduler plays
  the others and starts none involving it until an admin releases the hold. Unlike enabled: false, nothing is cancelled.
* Admins can also queue a fresh full round robin from the web API or CLI.
"""
import json
import os
import threading
import time
import traceback
import uuid
from typing import Dict, List, Optional

from bench import config, db, health, ratings, repairs, version
from bench.match import MatchRunner
from bench.storage import (checkpoint_path, delete_checkpoint, frame_json, load_checkpoint, load_frames,
                           prune_checkpoints, save_checkpoint, save_frames)


class Bench:
    """Process-wide coordinator. Create one with :func:`get_bench`."""

    def __init__(self, models_file: Optional[str] = None):
        self.models_file = models_file or config.models_path()
        self.settings = dict(config.DEFAULT_SETTINGS)
        self.models: Dict[str, dict] = {}
        self.live: Dict[str, MatchRunner] = {}
        self.status = "starting"
        self.status_detail = ""
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._models_mtime = None
        self.thread: Optional[threading.Thread] = None
        # serialises everything that rewrites the queue: model syncs (scheduler thread or admin reload), cohort merges,
        # top-ups, and a match being claimed to play
        self._lock = threading.RLock()
        db.init(os.path.join(config.data_dir(), "bench.db"))
        self._recover()

    # ---- startup ---------------------------------------------------------------------------------
    def _recover(self):
        """Matches interrupted by a restart/redeploy go back to the front of the queue; :meth:`play` resumes them
        from their last checkpoint (the start of the latest team turn)."""
        db.execute("UPDATE matches SET status='queued' WHERE status='running'")
        repairs.replay_spoiled_games()
        prune_checkpoints(keep={r["id"] for r in db.rows("SELECT id FROM matches WHERE status='queued'")})

    def start(self):
        self.thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        for runner in list(self.live.values()):
            runner.session.abort()

    def wake(self):
        self._wake.set()

    # ---- models.yaml sync --------------------------------------------------------------------------
    def sync_models(self, force=False) -> List[str]:
        """Load models.yaml, upsert models and schedule tournaments. Returns ids of newly added models."""
        with self._lock:
            return self._sync_models(force)

    def _sync_models(self, force=False) -> List[str]:
        try:
            mtime = os.path.getmtime(self.models_file)
        except OSError:
            self.status_detail = f"models file not found: {self.models_file}"
            return []
        if not force and mtime == self._models_mtime:
            return []
        self._models_mtime = mtime
        try:
            cfg = config.load_models_file(self.models_file)
        except Exception as e:
            self.status_detail = f"invalid models file: {e}"
            return []
        self.settings = cfg["settings"]
        known = {m["id"]: m for m in db.rows("SELECT * FROM models")}
        first_run = len(known) == 0
        added = []
        now = time.time()
        self.models = {}
        for m in cfg["models"]:
            self.models[m["id"]] = m
            enabled = 1 if m.get("enabled", True) else 0
            if m["id"] in known:
                db.execute("UPDATE models SET name=?, provider=?, model=?, config=?, enabled=? WHERE id=?",
                           (m["name"], m["provider"], m.get("model"), json.dumps(m), enabled, m["id"]))
            else:
                db.execute("INSERT INTO models(id, name, provider, model, config, enabled, added_at) "
                           "VALUES(?,?,?,?,?,?,?)",
                           (m["id"], m["name"], m["provider"], m.get("model"), json.dumps(m), enabled, now))
                if enabled:
                    added.append(m["id"])
        # Explicit operator opt-in, not ordinary disabling: remove failed-condition results from active standings
        # while retaining records and files. Archive before cancellation so queued fixtures retain the reason too.
        for m in cfg["models"]:
            if m.get("archive_reason"):
                db.archive_condition(m["id"], m["archive_reason"])
        # models removed from the file are disabled, not deleted (keeps their history)
        for mid in known:
            if mid not in self.models:
                db.execute("UPDATE models SET enabled=0 WHERE id=?", (mid,))
        enabled_ids = self._enabled_ids()
        # cancel queued matches involving disabled models
        marks = ",".join("?" * len(enabled_ids)) or "NULL"
        db.execute("UPDATE matches SET status='cancelled' WHERE status='queued' AND "
                   f"(home_model NOT IN ({marks}) OR away_model NOT IN ({marks}))", tuple(enabled_ids) * 2)
        if first_run:
            if len(enabled_ids) >= 2:
                self.create_round_robin(enabled_ids, name="Opening tournament")
        else:
            for mid in added:
                others = [o for o in enabled_ids if o != mid]
                if others:
                    self.create_placement(mid, others)
        db.backfill_placements()
        self.merge_placements()
        self._finish_tournaments()
        return added

    @staticmethod
    def _enabled_ids() -> List[str]:
        return [r["id"] for r in db.rows("SELECT id FROM models WHERE enabled=1 ORDER BY added_at, id")]

    # ---- tournament creation -------------------------------------------------------------------------
    def _two_legs(self) -> bool:
        return int(self.settings.get("legs", 2)) >= 2

    def _pairings(self, pairs):
        """Each pairing, followed at once by its return leg when games are played home and away."""
        if not self._two_legs():
            return list(pairs)
        return [fixture for a, b in pairs for fixture in ((a, b), (b, a))]

    def _create(self, kind: str, name: str, fixtures, focus: Optional[str] = None, extra: Optional[dict] = None) -> int:
        with db.transaction() as c:
            tid = self._insert_tournament(c, kind, name, fixtures, focus, extra)[0]
        self.wake()
        return tid

    def _insert_tournament(self, c, kind: str, name: str, fixtures, focus: Optional[str] = None,
                           extra: Optional[dict] = None):
        """(tournament id, [match ids]) inserted through ``c`` (the caller's transaction)."""
        snapshot = {**self.settings, "protocol_version": version.PROTOCOL_VERSION, **(extra or {})}
        tid = c.execute("INSERT INTO tournaments(kind, name, status, focus_model, settings, created_at) "
                        "VALUES(?,?,?,?,?,?)", (kind, name, "queued", focus, json.dumps(snapshot),
                                                time.time())).lastrowid
        return tid, self._insert_matches(c, tid, fixtures, 0)

    @staticmethod
    def _insert_matches(c, tid: int, fixtures, first_seq: int) -> List[str]:
        now = time.time()
        ids = []
        for i, (h, a) in enumerate(fixtures):
            ids.append(str(uuid.uuid4()))
            c.execute("INSERT INTO matches(id, tournament_id, seq, home_model, away_model, status, created_at) "
                      "VALUES(?,?,?,?,?,?,?)", (ids[-1], tid, first_seq + i, h, a, "queued", now))
        return ids

    def create_round_robin(self, model_ids: Optional[List[str]] = None, name: Optional[str] = None) -> int:
        if model_ids is None:
            model_ids = self._enabled_ids()
        if len(model_ids) < 2:
            raise ValueError("Need at least two enabled models for a tournament")
        # interleave fixtures so no model plays many games in a row; all first legs, then all return legs
        fixtures = self._round_robin_order(model_ids)
        if self._two_legs():
            fixtures += [(b, a) for a, b in fixtures]
        count = db.row("SELECT COUNT(*) AS n FROM tournaments WHERE kind='round_robin'")["n"]
        return self._create("round_robin", name or f"Round robin #{count + 1}", fixtures)

    @staticmethod
    def _round_robin_order(ids: List[str]):
        """Circle method so each round has every model at most once."""
        players = list(ids)
        if len(players) % 2:
            players.append(None)
        n = len(players)
        out = []
        for _ in range(n - 1):
            for i in range(n // 2):
                a, b = players[i], players[n - 1 - i]
                if a is not None and b is not None:
                    out.append((a, b))
            players = [players[0]] + [players[-1]] + players[1:-1]
        return out

    # ---- placement for newcomers -----------------------------------------------------------------------
    @staticmethod
    def _current_ratings() -> Dict[str, dict]:
        rows = db.rows("SELECT home_model, away_model, winner, meta FROM matches WHERE status='completed'")
        return ratings.fit_ratings(r for r in rows if version.is_current_protocol(r))

    def placement_anchors(self, model_id: str, opponents: List[str]) -> List[str]:
        """The opponents a newcomer is placed against: settings.placement_anchors if given (names or ids), otherwise
        settings.placement_size models spread evenly from the top to the bottom of the current ratings."""
        listed = self.settings.get("placement_anchors")
        if listed:
            wanted = {config.slugify(str(a)) for a in listed} | {str(a) for a in listed}
            chosen = [o for o in opponents if o in wanted]
            if chosen:
                return chosen
        size = int(self.settings.get("placement_size", 5))
        if size <= 0 or size >= len(opponents):
            return list(opponents)
        rated = self._current_ratings()
        # models that have played, best to worst; untested ones (e.g. another newcomer) only as filler
        ranked = sorted((o for o in opponents if o in rated), key=lambda o: -rated[o]["elo"])
        ranked += [o for o in opponents if o not in rated][:size - len(ranked)]
        if size == 1:
            return ranked[:1]
        return [ranked[round(i * (len(ranked) - 1) / (size - 1))] for i in range(size)]

    def create_placement(self, model_id: str, opponents: List[str]) -> int:
        """A standalone placement for a newcomer; :meth:`merge_placements` later folds it into a cohort with any
        other compatible, completely unstarted placement."""
        name = self.models.get(model_id, {}).get("name", model_id)
        fixtures = self._pairings([(model_id, o) for o in self.placement_anchors(model_id, opponents)])
        with db.transaction() as c:
            tid, ids = self._insert_tournament(c, "placement", f"Placement: {name}", fixtures, focus=model_id)
            c.execute("INSERT INTO placement_members(tournament_id, model_id, joined_at, adaptive, extra_games, "
                      "target_range) VALUES(?,?,?,?,?,?)", (tid, model_id, time.time(), 1, *self._top_up_rule()))
            c.executemany("INSERT INTO placement_fixtures(match_id, model_id, role) VALUES(?,?,'anchor')",
                          [(mid, model_id) for mid in ids])
        self.wake()
        return tid

    def _top_up_rule(self, member: Optional[dict] = None) -> tuple:
        """(most top-up games, rating range to stop at) for a newcomer: what was frozen when it joined, else (new
        newcomers, and placements made before cohorts) the live settings."""
        member = member or {}
        extra, target = member.get("extra_games"), member.get("target_range")
        return (int(self.settings.get("placement_extra_games", 6) if extra is None else extra),
                float(self.settings.get("placement_target_range", 300) if target is None else target))

    # ---- placement cohorts -------------------------------------------------------------------------------
    @staticmethod
    def _tournament_settings(t: dict) -> dict:
        try:
            return json.loads(t.get("settings") or "{}")
        except ValueError:
            return {}

    def _settings_for(self, tournament_id: int, c=None) -> dict:
        """The settings a tournament's games run under. A cohort is pinned to the settings it was merged under, so
        its games keep them across hot reloads (that is what makes two placements 'compatible'); everything else
        follows the live settings, as before."""
        if c is not None:       # inside a db.transaction(): read through its connection
            t = c.execute("SELECT settings FROM tournaments WHERE id=?", (tournament_id,)).fetchone()
            t = dict(t) if t else None
        else:
            t = db.row("SELECT settings FROM tournaments WHERE id=?", (tournament_id,))
        pinned = self._tournament_settings(t) if t else {}
        if not pinned.get("pinned"):
            return self.settings
        return {**self.settings, **config.exec_settings(pinned)}

    @staticmethod
    def _unstarted(c, t) -> bool:
        """A placement nothing has ever happened to: queued, never started (recovery puts an interrupted game back to
        'queued' but leaves its started_at, and its events and checkpoint), at least one game still to play and every
        other game merely cancelled."""
        if t["kind"] != "placement" or t["status"] != "queued" or t["started_at"] is not None:
            return False
        ms = c.execute("SELECT id, status, started_at FROM matches WHERE tournament_id=?", (t["id"],)).fetchall()
        if not any(m["status"] == "queued" for m in ms):
            return False
        for m in ms:
            if m["status"] not in ("queued", "cancelled") or m["started_at"] is not None:
                return False
            if c.execute("SELECT 1 FROM events WHERE match_id=?", (m["id"],)).fetchone() or \
                    os.path.exists(checkpoint_path(m["id"])):
                return False
        return True

    def merge_placements(self) -> List[int]:
        """Combine compatible, completely unstarted placements into one cohort, so even two newcomers share a
        tournament. Safe to call any number of times (a no-op once merged) and atomic: all or nothing. The oldest
        placement of a group becomes the cohort and keeps its games; a game another placement has in common (same home
        and away) is dropped from it and counts for both newcomers; the rest move over, and all games are re-sequenced
        so each newcomer gets one every few games. The emptied placements are marked 'merged'. Returns cohort ids."""
        merged = []
        with self._lock, db.transaction() as c:
            current = version.PROTOCOL_VERSION
            groups: Dict[str, list] = {}
            for t in c.execute("SELECT * FROM tournaments WHERE kind='placement' AND status='queued' "
                               "ORDER BY created_at, id").fetchall():
                t = dict(t)
                if not self._unstarted(c, t):
                    continue
                snap = self._tournament_settings(t)
                if snap.get("pinned") and snap.get("protocol_version") != current:
                    # unstarted, so its games will be played under the current protocol whatever the label says
                    snap["protocol_version"] = current
                    c.execute("UPDATE tournaments SET settings=? WHERE id=?", (json.dumps(snap), t["id"]))
                    t["settings"] = json.dumps(snap)
                key = json.dumps(config.exec_settings(self._settings_for(t["id"], c)), sort_keys=True, default=str)
                groups.setdefault(key, []).append(t)
            for group in groups.values():
                if len(group) > 1:
                    merged.append(self._merge_group(c, group))
        if merged:
            self.wake()
        return merged

    def _merge_group(self, c, group: List[dict]) -> int:
        target, sources = group[0], group[1:]
        pin = next((self._tournament_settings(t) for t in group if self._tournament_settings(t).get("pinned")), None)
        snapshot = pin or {**self.settings, "pinned": True, "protocol_version": version.PROTOCOL_VERSION}
        snapshot = {**snapshot, "pinned": True}
        ids = [t["id"] for t in group]
        marks = ",".join("?" * len(ids))
        members = [dict(r) for r in c.execute(
            f"SELECT * FROM placement_members WHERE tournament_id IN ({marks}) "
            f"ORDER BY joined_at, model_id", ids).fetchall()]
        # budgets of placements made before cohorts followed the live settings: freeze them with the merge
        for mem in members:
            if mem["adaptive"] and mem["extra_games"] is None:
                c.execute("UPDATE placement_members SET extra_games=?, target_range=? "
                          "WHERE tournament_id=? AND model_id=?",
                          (*self._top_up_rule(), mem["tournament_id"], mem["model_id"]))
        queued = {r["id"]: dict(r) for r in c.execute(
            f"SELECT * FROM matches WHERE tournament_id IN ({marks}) AND status='queued'", ids).fetchall()}
        # one surviving game per directed fixture; the cohort's own games win so their ids and order are kept
        survivor: Dict[tuple, str] = {}
        for m in sorted(queued.values(), key=lambda m: (m["tournament_id"] != target["id"],
                                                         ids.index(m["tournament_id"]), m["seq"])):
            survivor.setdefault((m["home_model"], m["away_model"]), m["id"])
        lists: Dict[str, List[str]] = {}
        for mem in members:
            lists[mem["model_id"]] = [r["match_id"] for r in c.execute(
                "SELECT f.match_id FROM placement_fixtures f JOIN matches m ON m.id=f.match_id "
                "WHERE f.model_id=? AND m.status='queued' AND m.tournament_id=? ORDER BY m.seq",
                (mem["model_id"], mem["tournament_id"])).fetchall()]
        order: List[str] = []
        key_of = {mid: (m["home_model"], m["away_model"]) for mid, m in queued.items()}
        resolved: Dict[str, str] = {}
        for i in range(max((len(v) for v in lists.values()), default=0)):    # one game per newcomer per round
            for mem in members:
                if i >= len(lists[mem["model_id"]]):
                    continue
                mid = lists[mem["model_id"]][i]
                if mid not in resolved:
                    resolved[mid] = survivor[key_of[mid]]
                keep = resolved[mid]
                if keep != mid and mid in queued:
                    for link in c.execute("SELECT model_id, role FROM placement_fixtures WHERE match_id=?",
                                          (mid,)).fetchall():
                        c.execute("INSERT OR IGNORE INTO placement_fixtures(match_id, model_id, role) VALUES(?,?,?)",
                                  (keep, link["model_id"], link["role"]))
                    c.execute("DELETE FROM placement_fixtures WHERE match_id=?", (mid,))
                    c.execute("DELETE FROM matches WHERE id=?", (mid,))
                if keep != mid:
                    queued.pop(mid, None)
                if keep not in order:
                    order.append(keep)
        for seq, mid in enumerate(order):
            c.execute("UPDATE matches SET seq=?, tournament_id=? WHERE id=?", (seq, target["id"], mid))
        for r in c.execute(f"SELECT id FROM matches WHERE tournament_id IN ({marks}) AND status!='queued' "
                           "ORDER BY tournament_id, seq", ids).fetchall():     # cancelled leftovers go last
            c.execute("UPDATE matches SET seq=?, tournament_id=? WHERE id=?", (len(order), target["id"], r["id"]))
        c.execute("UPDATE OR IGNORE placement_members SET tournament_id=? WHERE tournament_id!=? AND "
                  f"tournament_id IN ({marks})", [target["id"], target["id"]] + ids)
        c.execute(f"DELETE FROM placement_members WHERE tournament_id IN ({marks}) AND tournament_id!=?",
                  ids + [target["id"]])
        for t in sources:
            c.execute("UPDATE tournaments SET status='merged', finished_at=?, settings=? WHERE id=?",
                      (time.time(), json.dumps({**self._tournament_settings(t), "merged_into": target["id"]}), t["id"]))
        names = [self.models.get(m["model_id"], {}).get("name", m["model_id"]) for m in members]
        c.execute("UPDATE tournaments SET name=?, settings=? WHERE id=?",
                  (f"Placement cohort: {', '.join(names)}", json.dumps(snapshot), target["id"]))
        return target["id"]

    @staticmethod
    def placement_members(tid: int) -> List[dict]:
        return db.rows("SELECT * FROM placement_members WHERE tournament_id=? ORDER BY joined_at, model_id", (tid,))

    @staticmethod
    def placement_progress(tid: int) -> Dict[str, dict]:
        """Per newcomer: its anchor games and top-up games, finished / in all (cancelled ones counted apart)."""
        out: Dict[str, dict] = {}
        for r in db.rows("SELECT f.model_id, f.role, m.status, COUNT(*) AS n FROM placement_fixtures f JOIN matches m "
                         "ON m.id=f.match_id WHERE m.tournament_id=? GROUP BY f.model_id, f.role, m.status", (tid,)):
            p = out.setdefault(r["model_id"], {"anchor_done": 0, "anchor_total": 0, "top_up_done": 0, "top_up_total": 0,
                                               "top_up_scheduled": 0, "cancelled": 0, "active": 0})
            if r["role"] == "top_up":       # the allowance is spent on scheduling a game, whatever became of it
                p["top_up_scheduled"] += r["n"]
            if r["status"] == "cancelled":
                p["cancelled"] += r["n"]
                continue
            p[f"{r['role']}_total"] += r["n"]
            if r["status"] in ("completed", "error"):
                p[f"{r['role']}_done"] += r["n"]
            if r["status"] in ("queued", "running"):
                p["active"] += r["n"]
        return out

    def member_top_ups(self, t: dict) -> Dict[str, int]:
        """How many top-up games each newcomer of a placement may still add. They aren't scheduled yet: the opponents
        are picked once its queued games are done, and they are skipped if its rating is already settled."""
        if t["kind"] != "placement" or t["status"] in ("completed", "merged", "cancelled"):
            return {}
        progress = self.placement_progress(t["id"])
        out = {}
        enabled = set(self._enabled_ids())
        for mem in self.placement_members(t["id"]):
            if not mem["adaptive"] or mem["model_id"] not in enabled:
                continue
            left = self._top_up_rule(mem)[0] - progress.get(mem["model_id"], {}).get("top_up_scheduled", 0)
            if left > 0:
                out[mem["model_id"]] = left
        return out

    def possible_top_ups(self, t: dict) -> int:
        return sum(self.member_top_ups(t).values())

    def _top_up(self, t: dict) -> bool:
        """For each newcomer of a placement whose games are all done: queue one more pairing (home and away) against
        the opponent whose result is least predictable, while its 95% interval is wider than its placement_target_range
        and its own placement_extra_games allows. Newcomers decide independently; a fixture two of them want (or one
        already queued) is played once and counts for both. Returns True if games were added."""
        left = self.member_top_ups(t)
        if not left:
            return False
        progress = self.placement_progress(t["id"])
        enabled = self._enabled_ids()
        rated = None
        wanted: Dict[str, list] = {}
        for mem in self.placement_members(t["id"]):
            focus = mem["model_id"]
            if focus not in left or progress.get(focus, {}).get("active"):
                continue
            rated = rated if rated is not None else self._current_ratings()
            me = rated.get(focus)
            if me is None or me["hi"] - me["lo"] <= self._top_up_rule(mem)[1]:
                continue
            games = {}
            for r in db.rows("SELECT home_model, away_model, meta FROM matches WHERE status='completed' AND "
                             "(home_model=? OR away_model=?)", (focus, focus)):
                if not version.is_current_protocol(r):
                    continue
                opp = r["away_model"] if r["home_model"] == focus else r["home_model"]
                games[opp] = games.get(opp, 0) + 1

            def information(o):
                p = ratings.expected(me["elo"], rated.get(o, {}).get("elo", ratings.START_ELO))
                return p * (1 - p) / (1 + games.get(o, 0))

            opponents = [o for o in enabled if o != focus]
            if opponents:
                best = max(sorted(opponents), key=information)
                wanted[focus] = self._pairings([(focus, best)])[:left[focus]]
        if not wanted:
            return False
        with self._lock, db.transaction() as c:
            queued = {(r["home_model"], r["away_model"]): r["id"] for r in c.execute(
                "SELECT id, home_model, away_model FROM matches WHERE tournament_id=? AND status='queued'", (t["id"],))}
            last = c.execute("SELECT MAX(seq) AS last FROM matches WHERE tournament_id=?", (t["id"],)).fetchone()
            seq = (last["last"] or 0) + 1
            for i in range(max(len(v) for v in wanted.values())):
                for focus, fixtures in wanted.items():
                    if i >= len(fixtures):
                        continue
                    if fixtures[i] not in queued:
                        queued[fixtures[i]] = self._insert_matches(c, t["id"], [fixtures[i]], seq)[0]
                        seq += 1
                    c.execute("INSERT OR IGNORE INTO placement_fixtures(match_id, model_id, role) VALUES(?,?,'top_up')",
                              (queued[fixtures[i]], focus))
        self.wake()
        return True

    def _finish_tournaments(self):
        with self._lock:
            for t in db.rows("SELECT * FROM tournaments WHERE status IN ('queued','running')"):
                if t["kind"] == "placement":
                    self._top_up(t)
                left = db.row("SELECT COUNT(*) AS n FROM matches WHERE tournament_id=? "
                              "AND status IN ('queued','running')", (t["id"],))["n"]
                if left == 0:
                    db.execute("UPDATE tournaments SET status='completed', finished_at=? WHERE id=?",
                               (time.time(), t["id"]))

    # ---- Tripwire health holds ---------------------------------------------------------------------------
    @staticmethod
    def held_models() -> Dict[str, dict]:
        """Active holds by model id."""
        out = {}
        for h in db.rows("SELECT * FROM health_holds WHERE status='active' ORDER BY placed_at, id"):
            try:
                h["evidence"] = json.loads(h["evidence"]) if h["evidence"] else None
            except ValueError:
                h["evidence"] = None
            out.setdefault(h["model_id"], h)
        return out

    def place_hold(self, model_id: str, signal: str = "S", placed_by: str = "admin",
                   evidence: Optional[dict] = None) -> bool:
        """Hold a condition: no new game involving it starts until :meth:`release_hold`. A game it is playing finishes,
        its queued games keep their ids, order and placement roles, and it stays enabled. False if the model is unknown
        or already held."""
        with self._lock, db.transaction() as c:
            if not c.execute("SELECT 1 FROM models WHERE id=?", (model_id,)).fetchone():
                return False
            if c.execute("SELECT 1 FROM health_holds WHERE model_id=? AND status='active'", (model_id,)).fetchone():
                return False
            c.execute("INSERT INTO health_holds(model_id, signal, status, placed_at, placed_by, evidence) "
                      "VALUES(?,?,'active',?,?,?)", (model_id, signal, time.time(), placed_by,
                                                     json.dumps(evidence) if evidence is not None else None))
        self.wake()
        return True

    def release_hold(self, model_id: str, released_by: str = "admin", note: str = "") -> bool:
        """Lift a hold; its queued games are played again in their original order. False if there was none."""
        with self._lock, db.transaction() as c:
            n = c.execute("UPDATE health_holds SET status='released', released_at=?, released_by=?, release_note=? "
                          "WHERE model_id=? AND status='active'",
                          (time.time(), released_by, (note or "")[:500], model_id)).rowcount
        self.wake()
        return n > 0

    @staticmethod
    def _completed_games(model_id: str) -> List[dict]:
        return [db.decode_match(m) for m in db.rows(
            "SELECT * FROM matches WHERE status='completed' AND (home_model=? OR away_model=?)",
            (model_id, model_id))]

    def condition_health(self, model_id: str, matches: Optional[List[dict]] = None,
                         holds: Optional[Dict[str, dict]] = None) -> dict:
        """The Tripwire signals of one condition from its completed games (any protocol: they describe the route,
        not the rating)."""
        if matches is None:
            matches = self._completed_games(model_id)
        holds = self.held_models() if holds is None else holds
        return health.condition_health(health.games_for(model_id, matches), model_id=model_id,
                                       llm=config.is_llm(self._model_cfg(model_id)), hold=holds.get(model_id))

    def check_health_holds(self, model_ids: List[str]) -> List[str]:
        """After a game: hold any of these LLM conditions whose S signal fires on instrumented games finished since
        its last release. Only S places a hold; R, P and L are flags and never change anything. Returns held ids."""
        placed = []
        held = self.held_models()
        for mid in dict.fromkeys(model_ids):
            if mid in held or not config.is_llm(self._model_cfg(mid)):
                continue
            last = db.row("SELECT MAX(released_at) AS t FROM health_holds WHERE model_id=?", (mid,))["t"]
            evidence = health.s_hold_evidence(health.games_for(mid, self._completed_games(mid)), since=last)
            if evidence and self.place_hold(mid, signal="S", placed_by="auto", evidence=evidence):
                print(f"health hold on {mid}: provider-caused substituted play in {len(evidence['games'])} of the "
                      f"latest {len(evidence['window'])} games", flush=True)
                placed.append(mid)
        return placed

    def queue_state(self) -> dict:
        """Queued games, and how many of them wait on a hold."""
        held = self.held_models()
        queued = db.rows("SELECT home_model, away_model FROM matches WHERE status='queued'")
        waiting = [m for m in queued if m["home_model"] in held or m["away_model"] in held]
        return {"queued": len(queued), "held": len(waiting), "playable": len(queued) - len(waiting),
                "held_models": sorted({x for m in waiting for x in (m["home_model"], m["away_model"]) if x in held})}

    def idle_status(self):
        """(status, detail) for the status pill: honest about a queue that only waits on holds."""
        q = self.queue_state()
        if q["playable"]:
            return "ready", ""
        if q["held"]:
            names = ", ".join(self._model_cfg(x).get("name", x) for x in q["held_models"])
            return "held", (f"{q['held']} queued game{'s' if q['held'] != 1 else ''} wait on a health hold ({names}). "
                            "They keep their place; a maintainer can release the hold.")
        return "idle", "No matches queued. Add a model to models.yaml to start its placement."

    # ---- main loop ------------------------------------------------------------------------------------
    def next_match(self) -> Optional[dict]:
        """The first queued game in play order that involves no held condition."""
        held = list(self.held_models())
        skip = ""
        if held:
            marks = ",".join("?" * len(held))
            skip = f" AND m.home_model NOT IN ({marks}) AND m.away_model NOT IN ({marks})"
        return db.row("SELECT m.* FROM matches m JOIN tournaments t ON t.id=m.tournament_id "
                      f"WHERE m.status='queued'{skip} ORDER BY t.created_at, t.id, m.seq LIMIT 1", tuple(held) * 2)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sync_models()
                if os.environ.get("BENCH_PAUSED", "").lower() in ("1", "true", "yes"):
                    self.status = "paused"
                    self.status_detail = "BENCH_PAUSED is set"
                else:
                    m = self.next_match()
                    if m is not None:
                        if self._needs_key(m) and not os.environ.get("OPENROUTER_API_KEY"):
                            self.status = "waiting"
                            self.status_detail = "OPENROUTER_API_KEY is not set"
                        else:
                            self.play(m)
                            pause = float(self.settings.get("pause_between_matches", 5))
                            self._wake.wait(pause)
                            self._wake.clear()
                            continue
                    else:
                        self.status, self.status_detail = self.idle_status()
            except Exception as e:
                traceback.print_exc()
                self.status = "error"
                self.status_detail = f"{type(e).__name__}: {e}"
            self._wake.wait(20)
            self._wake.clear()

    def _needs_key(self, m) -> bool:
        return any(config.is_llm(self._model_cfg(mid)) for mid in (m["home_model"], m["away_model"]))

    def _model_cfg(self, model_id: str) -> dict:
        if model_id in self.models:
            return self.models[model_id]
        r = db.row("SELECT config FROM models WHERE id=?", (model_id,))
        return json.loads(r["config"]) if r else {"id": model_id, "name": model_id, "provider": "random"}

    def play(self, m: dict) -> dict:
        match_id = m["id"]
        now = time.time()
        with self._lock:
            # re-read under the lock: a cohort merge may have moved or dropped this game since it was picked
            m = db.row("SELECT * FROM matches WHERE id=? AND status='queued'", (match_id,))
            if m is None:
                return {"error": None, "skipped": True}
            held = self.held_models()
            if m["home_model"] in held or m["away_model"] in held:   # held since it was picked: leave it queued
                return {"error": None, "skipped": True, "held": True}
            restarted = m.get("started_at") is not None   # an earlier attempt was interrupted
            db.execute("UPDATE matches SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?",
                       (now, match_id))
            db.execute("UPDATE tournaments SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?",
                       (now, m["tournament_id"]))
            settings = self._settings_for(m["tournament_id"])
        home_cfg, away_cfg = self._model_cfg(m["home_model"]), self._model_cfg(m["away_model"])
        self.status = "playing"
        self.status_detail = f"{home_cfg['name']} vs {away_cfg['name']}"
        runner = None
        try:
            runner = self._runner(m, home_cfg, away_cfg, restarted, settings)
            db.execute("UPDATE matches SET seed=? WHERE id=?", (runner.seed, match_id))
            self.live[match_id] = runner
            result = runner.run()
            if result.get("infra_error"):
                # bad API key / no credits: not the models' fault -> back in the queue; it resumes from the
                # last checkpoint (taken before the failure) once the key works again
                db.execute("UPDATE matches SET status='queued' WHERE id=?", (match_id,))
                self.status = "waiting"
                self.status_detail = f"OpenRouter problem, retrying later: {result['infra_error']}"
                self._wake.wait(300)
                self._wake.clear()
                return result
            delete_checkpoint(match_id)
            save_frames(match_id, runner.session.frames, runner.session.timeline)
            status = "error" if result["error"] else "completed"
            db.execute("UPDATE matches SET status=?, home_score=?, away_score=?, winner=?, home_stats=?, "
                       "away_stats=?, error=?, finished_at=?, duration=?, frames=?, meta=?, admissible=? WHERE id=?",
                       (status, result["home_score"], result["away_score"], result["winner"],
                        json.dumps(result["home_stats"]), json.dumps(result["away_stats"]), result["error"],
                        time.time(), result["duration"], len(runner.session.frames),
                        json.dumps(result["meta"], default=str), 1 if result["meta"]["admissible"] else 0, match_id))
            if status == "completed":
                try:
                    self.check_health_holds([m["home_model"], m["away_model"]])
                except Exception:
                    traceback.print_exc()   # health checks never cost a finished game
            return result
        except Exception as e:
            traceback.print_exc()
            delete_checkpoint(match_id)
            db.execute("UPDATE matches SET status='error', error=?, finished_at=? WHERE id=?",
                       (f"{type(e).__name__}: {e}", time.time(), match_id))
            db.add_event(match_id, None, "error", {"text": f"Match failed: {e}"})
            return {"error": str(e)}
        finally:
            self.live.pop(match_id, None)
            self._finish_tournaments()

    def _runner(self, m: dict, home_cfg: dict, away_cfg: dict, restarted: bool, settings: dict) -> MatchRunner:
        """A runner for the match: resumed from its checkpoint if it has a usable one, else from kick-off (with
        the seed of the interrupted attempt, if any)."""
        match_id = m["id"]
        kwargs = dict(api_key=os.environ.get("OPENROUTER_API_KEY"),
                      event_sink=db.add_event,
                      transcript=True, restarted=restarted,
                      checkpoint_sink=lambda state: save_checkpoint(match_id, state))
        cp = load_checkpoint(match_id)
        if cp is not None:
            try:
                runner = MatchRunner(match_id, home_cfg, away_cfg, settings, resume=cp, **kwargs)
                # the feed keeps what happened up to the checkpoint; the interrupted rest of that turn is replayed
                db.execute("DELETE FROM events WHERE match_id=? AND id>?", (match_id, cp["last_event_id"]))
                return runner
            except Exception:
                traceback.print_exc()
                print(f"checkpoint {match_id}: could not be restored, restarting the game")
        delete_checkpoint(match_id)
        db.execute("DELETE FROM events WHERE match_id=?", (match_id,))
        return MatchRunner(match_id, home_cfg, away_cfg, settings, seed=m.get("seed"), **kwargs)

    # ---- live access for the web layer ------------------------------------------------------------------
    def live_state(self, match_id: str):
        """(version tag, json) of the latest board state - live snapshot or the final replay frame."""
        runner = self.live.get(match_id)
        if runner is not None:
            with runner.session.lock:
                return f"{match_id}-{runner.session.snapshot_seq}", runner.session.latest_json
        frames = load_frames(match_id)
        if not frames:
            return None, None
        return f"{match_id}-final-{len(frames)}", frame_json(frames[-1])


_bench: Optional[Bench] = None
_bench_lock = threading.Lock()


def get_bench() -> Bench:
    global _bench
    with _bench_lock:
        if _bench is None:
            _bench = Bench()
        return _bench
