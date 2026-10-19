"""SQLite persistence for models, tournaments, matches and the live event feed."""
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS models (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    config TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tournaments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- round_robin | gauntlet
    name TEXT NOT NULL,
    status TEXT NOT NULL,            -- queued | running | completed | cancelled
    focus_model TEXT,                -- the newly added model for a gauntlet
    settings TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
    seq INTEGER NOT NULL,
    home_model TEXT NOT NULL REFERENCES models(id),
    away_model TEXT NOT NULL REFERENCES models(id),
    status TEXT NOT NULL,            -- queued | running | completed | error | cancelled
    home_score INTEGER,
    away_score INTEGER,
    winner TEXT,                     -- home | away | draw
    home_stats TEXT,
    away_stats TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    duration REAL,
    frames INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status, tournament_id, seq);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id TEXT NOT NULL,
    ts REAL NOT NULL,
    side TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_match ON events(match_id, id);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- Newcomers of a placement (one, or several in a cohort) and which games count for whom. A game shared by two
-- newcomers has a row for each. extra_games / target_range are frozen when the newcomer joins (NULL = read the live
-- settings, as placements made before cohorts did); adaptive=0 means no top-ups.
CREATE TABLE IF NOT EXISTS placement_members (
    tournament_id INTEGER NOT NULL REFERENCES tournaments(id),
    model_id TEXT NOT NULL REFERENCES models(id),
    joined_at REAL NOT NULL,
    adaptive INTEGER NOT NULL DEFAULT 1,
    extra_games INTEGER,
    target_range REAL,
    PRIMARY KEY (tournament_id, model_id)
);
CREATE TABLE IF NOT EXISTS placement_fixtures (
    match_id TEXT NOT NULL REFERENCES matches(id),
    model_id TEXT NOT NULL REFERENCES models(id),
    role TEXT NOT NULL,              -- anchor | top_up
    PRIMARY KEY (match_id, model_id)
);
CREATE INDEX IF NOT EXISTS idx_placement_fixtures_model ON placement_fixtures(model_id);
"""

_local = threading.local()
_DB_PATH = None


# columns added after the first release; created on start-up for existing databases
MIGRATIONS = {
    "matches": [("seed", "INTEGER"), ("meta", "TEXT"), ("admissible", "INTEGER")],
}


def init(path: str):
    global _DB_PATH
    _DB_PATH = path
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, typ in cols:
                if name not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {typ}")
    backfill_placements()


def backfill_placements():
    """Placements made before cohorts have no member or fixture rows: derive them (focus model, games before
    settings.placement_base are anchors, later ones top-ups). Idempotent: only touches placements without members."""
    with transaction() as c:
        todo = c.execute("SELECT * FROM tournaments WHERE kind='placement' AND status!='merged' AND focus_model IS NOT NULL AND id NOT IN "
                         "(SELECT tournament_id FROM placement_members)").fetchall()
        for t in todo:
            if not c.execute("SELECT 1 FROM models WHERE id=?", (t["focus_model"],)).fetchone():
                continue
            try:
                base = json.loads(t["settings"] or "{}").get("placement_base")
            except ValueError:
                base = None
            c.execute("INSERT INTO placement_members(tournament_id, model_id, joined_at, adaptive) VALUES(?,?,?,?)",
                      (t["id"], t["focus_model"], t["created_at"], 0 if base is None else 1))
            for i, m in enumerate(c.execute("SELECT id FROM matches WHERE tournament_id=? ORDER BY seq", (t["id"],))
                                  .fetchall()):
                role = "top_up" if base is not None and i >= base else "anchor"
                c.execute("INSERT OR IGNORE INTO placement_fixtures(match_id, model_id, role) VALUES(?,?,?)",
                          (m["id"], t["focus_model"], role))


def _connect():
    c = sqlite3.connect(_DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


@contextmanager
def conn():
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != _DB_PATH:
        c = _connect()
        _local.conn = c
        _local.path = _DB_PATH
        _local.in_tx = False
    if getattr(_local, "in_tx", False):
        # inside transaction(): the helpers below join it; only the transaction commits or rolls back
        yield c
        return
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise


@contextmanager
def transaction():
    """One atomic write transaction (takes the write lock up front). Helpers called inside it (rows, row, execute,
    conn) join it instead of committing, so it stays all-or-nothing. Nesting is an error, never a silent commit."""
    with conn() as c:
        if getattr(_local, "in_tx", False):
            raise RuntimeError("db.transaction() cannot be nested")
        if c.in_transaction:
            c.commit()
        c.execute("BEGIN IMMEDIATE")
        _local.in_tx = True
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            _local.in_tx = False


def rows(sql: str, args=()) -> List[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def row(sql: str, args=()) -> Optional[dict]:
    r = rows(sql, args)
    return r[0] if r else None


def execute(sql: str, args=()) -> int:
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


# ---- meta -------------------------------------------------------------------------------------
def get_meta(key, default=None):
    r = row("SELECT value FROM meta WHERE key=?", (key,))
    return default if r is None else r["value"]


def set_meta(key, value):
    execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))


# ---- events -----------------------------------------------------------------------------------
def add_event(match_id: str, side: Optional[str], kind: str, payload: dict):
    execute("INSERT INTO events(match_id, ts, side, kind, payload) VALUES(?,?,?,?,?)",
            (match_id, time.time(), side, kind, json.dumps(payload, default=str)))


def get_events(match_id: str, after: int = 0, limit: int = 500) -> List[dict]:
    out = rows("SELECT id, ts, side, kind, payload FROM events WHERE match_id=? AND id>? ORDER BY id LIMIT ?",
               (match_id, after, limit))
    for e in out:
        e["payload"] = json.loads(e["payload"])
    return out


# ---- decoding helpers ---------------------------------------------------------------------------
def decode_match(m: Optional[dict]) -> Optional[dict]:
    if m is None:
        return None
    for k in ("home_stats", "away_stats", "meta"):
        if m.get(k):
            m[k] = json.loads(m[k])
    return m
