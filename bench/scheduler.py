"""
The tournament scheduler: watches models.yaml, creates tournaments and plays queued matches one
after the other in a background thread.

* First start with >= 2 enabled models  -> a round-robin tournament between all of them.
* A model appears that wasn't known before -> a "gauntlet": the newcomer vs every other enabled model.
* A model is disabled/removed -> its queued matches are cancelled (history is kept).
* Admins can also queue a fresh full round robin from the web API or CLI.
"""
import itertools
import json
import os
import struct
import threading
import time
import traceback
import uuid
import zlib
from typing import Dict, List, Optional

from bench import config, db
from bench.match import MatchRunner


# ---- frame storage (for replays) ------------------------------------------------------------------
def frames_path(match_id: str) -> str:
    d = os.path.join(config.data_dir(), "frames")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{match_id}.frames")


def save_frames(match_id: str, frames: List[bytes]):
    tmp = frames_path(match_id) + ".tmp"
    with open(tmp, "wb") as f:
        for fr in frames:
            f.write(struct.pack("<I", len(fr)))
            f.write(fr)
    os.replace(tmp, frames_path(match_id))


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
        db.init(os.path.join(config.data_dir(), "bench.db"))
        self._recover()

    # ---- startup ---------------------------------------------------------------------------------
    def _recover(self):
        """Matches interrupted by a restart/redeploy are replayed from scratch."""
        for m in db.rows("SELECT id FROM matches WHERE status='running'"):
            db.execute("DELETE FROM events WHERE match_id=?", (m["id"],))
            db.execute("UPDATE matches SET status='queued', started_at=NULL WHERE id=?", (m["id"],))

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
                    self.create_gauntlet(mid, others)
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

    def _create(self, kind: str, name: str, fixtures, focus: Optional[str] = None) -> int:
        now = time.time()
        tid = db.execute("INSERT INTO tournaments(kind, name, status, focus_model, settings, created_at) "
                         "VALUES(?,?,?,?,?,?)", (kind, name, "queued", focus, json.dumps(self.settings), now))
        for i, (h, a) in enumerate(fixtures):
            db.execute("INSERT INTO matches(id, tournament_id, seq, home_model, away_model, status, created_at) "
                       "VALUES(?,?,?,?,?,?,?)", (str(uuid.uuid4()), tid, i, h, a, "queued", now))
        self.wake()
        return tid

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

    def create_gauntlet(self, model_id: str, opponents: List[str]) -> int:
        name = self.models.get(model_id, {}).get("name", model_id)
        return self._create("gauntlet", f"Gauntlet: {name}", self._pairings([(model_id, o) for o in opponents]),
                            focus=model_id)

    def _finish_tournaments(self):
        for t in db.rows("SELECT id FROM tournaments WHERE status IN ('queued','running')"):
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
                        self.status_detail = "No matches queued. Add a model to models.yaml to start a gauntlet."
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
        db.execute("UPDATE matches SET status='running', started_at=? WHERE id=?", (now, match_id))
        db.execute("UPDATE tournaments SET status='running', started_at=COALESCE(started_at, ?) WHERE id=?",
                   (now, m["tournament_id"]))
        home_cfg, away_cfg = self._model_cfg(m["home_model"]), self._model_cfg(m["away_model"])
        self.status = "playing"
        self.status_detail = f"{home_cfg['name']} vs {away_cfg['name']}"
        runner = None
        try:
            runner = MatchRunner(match_id, home_cfg, away_cfg, self.settings,
                                 api_key=os.environ.get("OPENROUTER_API_KEY"),
                                 event_sink=lambda mid, side, kind, payload: db.add_event(mid, side, kind, payload))
            self.live[match_id] = runner
            result = runner.run()
            if result.get("infra_error"):
                # bad API key / no credits: not the models' fault -> put the match back in the queue
                db.execute("DELETE FROM events WHERE match_id=?", (match_id,))
                db.execute("UPDATE matches SET status='queued', started_at=NULL WHERE id=?", (match_id,))
                self.status = "waiting"
                self.status_detail = f"OpenRouter problem, retrying later: {result['infra_error']}"
                self._wake.wait(300)
                self._wake.clear()
                return result
            save_frames(match_id, runner.session.frames)
            status = "error" if result["error"] else "completed"
            db.execute("UPDATE matches SET status=?, home_score=?, away_score=?, winner=?, home_stats=?, "
                       "away_stats=?, error=?, finished_at=?, duration=?, frames=? WHERE id=?",
                       (status, result["home_score"], result["away_score"], result["winner"],
                        json.dumps(result["home_stats"]), json.dumps(result["away_stats"]), result["error"],
                        time.time(), result["duration"], len(runner.session.frames), match_id))
            return result
        except Exception as e:
            traceback.print_exc()
            db.execute("UPDATE matches SET status='error', error=?, finished_at=? WHERE id=?",
                       (f"{type(e).__name__}: {e}", time.time(), match_id))
            db.add_event(match_id, None, "error", {"text": f"Match failed: {e}"})
            return {"error": str(e)}
        finally:
            self.live.pop(match_id, None)
            self._finish_tournaments()

    # ---- live access for the web layer ------------------------------------------------------------------
    def live_state(self, match_id: str) -> Optional[str]:
        runner = self.live.get(match_id)
        if runner is not None:
            return runner.session.latest_json
        frames = load_frames(match_id)
        return frame_json(frames[-1]) if frames else None


_bench: Optional[Bench] = None
_bench_lock = threading.Lock()


def get_bench() -> Bench:
    global _bench
    with _bench_lock:
        if _bench is None:
            _bench = Bench()
        return _bench
