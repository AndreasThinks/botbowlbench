"""Dataset export: everything needed to analyse the benchmark offline, as JSON lines."""
import json
import os
import shutil
from typing import Iterable, Iterator, Optional

from bench import db
from bench.transcript import transcript_path


def iter_matches(status: Optional[Iterable[str]] = ("completed", "error")) -> Iterator[dict]:
    names = {m["id"]: m["name"] for m in db.rows("SELECT id, name FROM models")}
    tours = {t["id"]: t for t in db.rows("SELECT id, name, kind FROM tournaments")}
    where = ""
    args = ()
    if status:
        where = f"WHERE status IN ({','.join('?' * len(tuple(status)))})"
        args = tuple(status)
    for m in db.rows(f"SELECT * FROM matches {where} ORDER BY COALESCE(finished_at, created_at)", args):
        m = db.decode_match(m)
        m["home_name"] = names.get(m["home_model"], m["home_model"])
        m["away_name"] = names.get(m["away_model"], m["away_model"])
        t = tours.get(m["tournament_id"]) or {}
        m["tournament_name"], m["tournament_kind"] = t.get("name"), t.get("kind")
        m["admissible"] = bool(m.get("admissible"))
        m["has_transcript"] = os.path.exists(transcript_path(m["id"]))
        yield m


def iter_events(kinds: Optional[Iterable[str]] = None) -> Iterator[dict]:
    where, args = "", ()
    if kinds:
        kinds = tuple(kinds)
        where, args = f"WHERE kind IN ({','.join('?' * len(kinds))})", kinds
    last = 0
    while True:
        batch = db.rows(f"SELECT * FROM events {where} {'AND' if where else 'WHERE'} id>? ORDER BY id LIMIT 5000",
                        args + (last,))
        if not batch:
            return
        for e in batch:
            e["payload"] = json.loads(e["payload"])
            yield e
        last = batch[-1]["id"]


def jsonl(rows: Iterable[dict]) -> Iterator[str]:
    for r in rows:
        yield json.dumps(r, default=str, ensure_ascii=False) + "\n"


def export_all(out_dir: str, transcripts: bool = True) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    counts = {}

    def dump(name, rows):
        n = 0
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            for line in jsonl(rows):
                f.write(line)
                n += 1
        counts[name] = n

    dump("models.jsonl", db.rows("SELECT id, name, provider, model, enabled, added_at, config FROM models"))
    dump("tournaments.jsonl", db.rows("SELECT * FROM tournaments"))
    dump("matches.jsonl", iter_matches(status=None))
    dump("events.jsonl", iter_events())
    if transcripts:
        tdir = os.path.join(out_dir, "transcripts")
        os.makedirs(tdir, exist_ok=True)
        n = 0
        for m in db.rows("SELECT id FROM matches"):
            src = transcript_path(m["id"])
            if os.path.exists(src):
                shutil.copy2(src, tdir)
                n += 1
        counts["transcripts"] = n
    return counts
