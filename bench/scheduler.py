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
* Admins can also queue a fresh full round robin from the web API or CLI.
"""
import itertools
import json
import os
import pickle
import struct
import threading
import time
import traceback
import uuid
import zlib
from typing import Dict, List, Optional

from bench import config, db, ratings, version
from bench.match import CHECKPOINT_VERSION, MatchRunner
from bench.session import timeline_point
from bench.transcript import transcript_path


# ---- frame storage (for replays) ------------------------------------------------------------------
def frames_path(match_id: str) -> str:
    d = os.path.join(config.data_dir(), "frames")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{match_id}.frames")


def save_frames(match_id: str, frames: List[bytes], timeline: Optional[List[dict]] = None):
    tmp = frames_path(match_id) + ".tmp"
    with open(tmp, "wb") as f:
        for fr in frames:
            f.write(struct.pack("<I", len(fr)))
            f.write(fr)
    os.replace(tmp, frames_path(match_id))
    if timeline is not None and len(timeline) == len(frames):
        _write_timeline(match_id, timeline)


def timeline_path(match_id: str) -> str:
    return frames_path(match_id)[:-len(".frames")] + ".timeline.json"


def _write_timeline(match_id: str, timeline: List[dict]):
    tmp = timeline_path(match_id) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(timeline, f, separators=(",", ":"))
    os.replace(tmp, timeline_path(match_id))


def load_timeline(match_id: str) -> List[dict]:
    """Per-frame index for the replay scrubber. Rebuilt (once) from the frames for games recorded before
    timelines existed; those frames have no timestamps (ts=None)."""
    path = timeline_path(match_id)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    frames = load_frames(match_id)
    if not frames:
        return []
    timeline = []
    for blob in frames:
        data = json.loads(frame_json(blob))
        timeline.append(timeline_point(data, (data.get("bench") or {}).get("ts")))
    _write_timeline(match_id, timeline)
    return timeline


_frame_cache: Dict[str, List[bytes]] = {}


def load_frames(match_id: str) -> List[bytes]:
    if match_id in _frame_cache:
        return _frame_cache[match_id]
    path = frames_path(match_id)
    frames = []
    if os.path.exists(path):
        with open(path, "rb") as f:
            data = f.read()
        i = 0
        while i + 4 <= len(data):
            (n,) = struct.unpack_from("<I", data, i)
            frames.append(data[i + 4:i + 4 + n])
            i += 4 + n
    if len(_frame_cache) > 8:
        _frame_cache.pop(next(iter(_frame_cache)))
    _frame_cache[match_id] = frames
    return frames


def frame_json(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")


# ---- checkpoints (resume a game interrupted by a restart/redeploy) ----------------------------------
def checkpoint_path(match_id: str) -> str:
    d = os.path.join(config.data_dir(), "checkpoints")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{match_id}.ckpt")


def save_checkpoint(match_id: str, state: dict):
    row = db.row("SELECT MAX(id) AS n FROM events WHERE match_id=?", (match_id,))
    state["last_event_id"] = (row and row["n"]) or 0
    tmp = checkpoint_path(match_id) + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, checkpoint_path(match_id))


def load_checkpoint(match_id: str) -> Optional[dict]:
    """The match's checkpoint, or None if there is none or it can't be resumed under the current protocol
    (a game must be played under one set of rules and prompts; otherwise it restarts from kick-off)."""
    path = checkpoint_path(match_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            cp = pickle.load(f)
    except Exception as e:
        print(f"checkpoint {match_id}: unreadable ({type(e).__name__}: {e}), restarting the game")
        return None
    current = (CHECKPOINT_VERSION, version.PROTOCOL_VERSION, version.prompt_fingerprint())
    saved = (cp.get("version"), cp.get("protocol_version"), cp.get("prompt_fingerprint"))
    if saved != current:
        print(f"checkpoint {match_id}: made under {saved}, now {current}; restarting the game")
        return None
    return cp


def delete_checkpoint(match_id: str):
    for path in (checkpoint_path(match_id), checkpoint_path(match_id) + ".tmp"):
        try:
            os.remove(path)
        except OSError:
            pass


# games a seat played entirely on a host far below the minimum speed (see _replay_key_limit_matches)
SLOW_HOST_REPLAYS = ("f52d7abe-aeb9-4724-9192-19135a01ca55",   # Claude Haiku 5.5 1-0 DeepSeek V4.1 Flash
                     "d8c2fe72-f6aa-4366-ad60-08c96e324b90")   # DeepSeek V3.2 1-0 DeepSeek V4.1 Flash


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
        self._replay_key_limit_matches()
        queued = {r["id"] for r in db.rows("SELECT id FROM matches WHERE status='queued'")}
        for name in os.listdir(os.path.dirname(checkpoint_path("x"))):
            if name.split(".")[0] not in queued:
                delete_checkpoint(name.split(".")[0])

    @staticmethod
    def _replay_key_limit_matches():
        """Before a 403 "Key limit exceeded" counted as an infra error, hitting the OpenRouter key's spending limit
        marked both models unavailable and the game was played out on default actions. Those games say nothing
        about the models: put them back in the queue as fresh games. A no-op once they have been replayed."""
        rows = db.rows("SELECT DISTINCT m.id FROM matches m JOIN events e ON e.match_id=m.id "
                       "WHERE m.status='completed' AND e.kind='error' AND e.payload LIKE '%Key limit exceeded%'")
        Bench._requeue_fresh([r["id"] for r in rows])
        if rows:
            print(f"requeued {len(rows)} games played after the OpenRouter key limit was hit", flush=True)
        # Likewise games where one host's content filter answered every retry with an empty reply until the model
        # was marked unavailable (Opus 5.5 pinned to Azure, before filtering hosts were skipped). An interrupted
        # game (queued again after a restart) starts over too, rather than resuming with the model unavailable.
        rows = db.rows("SELECT DISTINCT m.id FROM matches m JOIN events e ON e.match_id=m.id "
                       "JOIN events u ON u.match_id=m.id AND u.side=e.side "
                       "WHERE m.status IN ('completed', 'queued') AND e.kind='error' "
                       "AND e.payload LIKE '%provider content filter returned an empty reply%' "
                       "AND u.kind='system' AND u.payload LIKE '%Model unavailable%'")
        Bench._requeue_fresh([r["id"] for r in rows])
        if rows:
            print(f"requeued {len(rows)} games a provider content filter put on default actions", flush=True)
        # And games where a seat was stuck for the whole game on a host answering at ~20 tokens/s, before such
        # hosts were skipped (DeepSeek V4.1 Flash on OpenInference: 32 deadline cutoffs a game). Found by checking
        # every game's transcript for 2026-10-14; a replayed game records slow_host_skips, so this runs once.
        rows = [r for r in db.rows("SELECT id, home_stats, away_stats FROM matches WHERE status='completed' AND id IN "
                                   f"({','.join('?' * len(SLOW_HOST_REPLAYS))})", tuple(SLOW_HOST_REPLAYS))
                if "slow_host_skips" not in (r["home_stats"] or "") + (r["away_stats"] or "")]
        Bench._requeue_fresh([r["id"] for r in rows])
        if rows:
            print(f"requeued {len(rows)} games played on a host far below the minimum speed", flush=True)

    @staticmethod
    def _requeue_fresh(ids):
        """Put games back in the queue as if they had never been played."""
        for mid in ids:
            db.execute("DELETE FROM events WHERE match_id=?", (mid,))
            delete_checkpoint(mid)
            for path in (frames_path(mid), timeline_path(mid), transcript_path(mid), transcript_path(mid) + ".partial"):
                if os.path.exists(path):
                    os.remove(path)
            db.execute("UPDATE matches SET status='queued', started_at=NULL, finished_at=NULL, error=NULL, seed=NULL, "
                       "home_score=NULL, away_score=NULL, winner=NULL, home_stats=NULL, away_stats=NULL, "
                       "duration=NULL, frames=0, meta=NULL, admissible=NULL WHERE id=?", (mid,))
            db.execute("UPDATE tournaments SET status='queued', finished_at=NULL WHERE id=(SELECT tournament_id FROM "
                       "matches WHERE id=?) AND status='completed'", (mid,))

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
        # models removed from the file are disabled, not deleted (keeps their history)
        for mid in known:
            if mid not in self.models:
                db.execute("UPDATE models SET enabled=0 WHERE id=?", (mid,))
        enabled_ids = [r["id"] for r in db.rows("SELECT id FROM models WHERE enabled=1 ORDER BY added_at, id")]
        # cancel queued matches involving disabled models
        db.execute(f"UPDATE matches SET status='cancelled' WHERE status='queued' AND "
                   f"(home_model NOT IN ({','.join('?' * len(enabled_ids)) or 'NULL'}) OR "
                   f"away_model NOT IN ({','.join('?' * len(enabled_ids)) or 'NULL'}))",
                   tuple(enabled_ids) * 2)
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

    # ---- tournament creation -------------------------------------------------------------------------
    def _pairings(self, pairs):
        legs = int(self.settings.get("legs", 2))
        out = []
        for a, b in pairs:
            out.append((a, b))
            if legs >= 2:
                out.append((b, a))
        return out

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
            model_ids = [r["id"] for r in db.rows("SELECT id FROM models WHERE enabled=1 ORDER BY added_at, id")]
        if len(model_ids) < 2:
            raise ValueError("Need at least two enabled models for a tournament")
        # interleave fixtures so no model plays many games in a row
        pairs = list(itertools.combinations(model_ids, 2))
        first_legs = self._round_robin_order(model_ids) if len(model_ids) > 2 else pairs
        fixtures = [(a, b) for a, b in first_legs]
        if int(self.settings.get("legs", 2)) >= 2:
            fixtures += [(b, a) for a, b in first_legs]
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
                      "target_range) VALUES(?,?,?,?,?,?)",
                      (tid, model_id, time.time(), 1, int(self.settings.get("placement_extra_games", 6)),
                       float(self.settings.get("placement_target_range", 300))))
            for mid in ids:
                c.execute("INSERT INTO placement_fixtures(match_id, model_id, role) VALUES(?,?,'anchor')", (mid, model_id))
        self.wake()
        return tid

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
                c.execute("UPDATE placement_members SET extra_games=?, target_range=? WHERE tournament_id=? AND model_id=?",
                          (int(self.settings.get("placement_extra_games", 6)),
                           float(self.settings.get("placement_target_range", 300)), mem["tournament_id"], mem["model_id"]))
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
        c.execute(f"DELETE FROM placement_members WHERE tournament_id IN ({marks}) AND tournament_id!=?", ids + [target["id"]])
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

    def _member_budget(self, member: dict) -> int:
        extra = member["extra_games"] if member["extra_games"] is not None else self.settings.get("placement_extra_games", 6)
        return int(extra)

    def member_top_ups(self, t: dict) -> Dict[str, int]:
        """How many top-up games each newcomer of a placement may still add. They aren't scheduled yet: the opponents
        are picked once its queued games are done, and they are skipped if its rating is already settled."""
        if t["kind"] != "placement" or t["status"] in ("completed", "merged", "cancelled"):
            return {}
        progress = self.placement_progress(t["id"])
        out = {}
        for mem in self.placement_members(t["id"]):
            if not mem["adaptive"] or not db.row("SELECT 1 AS ok FROM models WHERE id=? AND enabled=1", (mem["model_id"],)):
                continue
            used = progress.get(mem["model_id"], {}).get("top_up_scheduled", 0)
            if self._member_budget(mem) - used > 0:
                out[mem["model_id"]] = self._member_budget(mem) - used
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
        enabled = [r["id"] for r in db.rows("SELECT id FROM models WHERE enabled=1")]
        rated = None
        wanted: Dict[str, list] = {}
        for mem in self.placement_members(t["id"]):
            focus = mem["model_id"]
            if focus not in left or progress.get(focus, {}).get("active"):
                continue
            rated = rated if rated is not None else self._current_ratings()
            me = rated.get(focus)
            target = mem["target_range"] if mem["target_range"] is not None else self.settings.get("placement_target_range", 300)
            if me is None or me["hi"] - me["lo"] <= float(target):
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
            seq = (c.execute("SELECT MAX(seq) AS last FROM matches WHERE tournament_id=?", (t["id"],)).fetchone()["last"]
                   or 0) + 1
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
                left = db.row("SELECT COUNT(*) AS n FROM matches WHERE tournament_id=? AND status IN ('queued','running')",
                              (t["id"],))["n"]
                if left == 0:
                    db.execute("UPDATE tournaments SET status='completed', finished_at=? WHERE id=?", (time.time(), t["id"]))

    # ---- main loop ------------------------------------------------------------------------------------
    def next_match(self) -> Optional[dict]:
        return db.row("SELECT m.* FROM matches m JOIN tournaments t ON t.id=m.tournament_id "
                      "WHERE m.status='queued' ORDER BY t.created_at, t.id, m.seq LIMIT 1")

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
                        self.status = "idle"
                        self.status_detail = "No matches queued. Add a model to models.yaml to start its placement."
            except Exception as e:
                traceback.print_exc()
                self.status = "error"
                self.status_detail = f"{type(e).__name__}: {e}"
            self._wake.wait(20)
            self._wake.clear()

    def _needs_key(self, m) -> bool:
        for mid in (m["home_model"], m["away_model"]):
            cfg = self._model_cfg(mid)
            if cfg.get("provider", "openrouter") == "openrouter":
                return True
        return False

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
                      event_sink=lambda mid, side, kind, payload: db.add_event(mid, side, kind, payload),
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
