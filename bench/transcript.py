"""
Full, uncompromised per-match transcripts (gzip'd JSON lines), the raw research record of a game.

Record types (field ``type``):

* ``meta``        - first line: match id, models, seed, harness version, prompt fingerprint, settings
* ``episode``     - a seat starts a fresh conversation: full system prompt + opening user message
* ``llm_call``    - one model call: the messages appended since the previous call of this episode, the
                    complete response (content, reasoning, tool calls), usage, latency, served model/provider
* ``tool``        - one tool execution: name, arguments, full result, ok flag, duration
* ``action``      - one game action chosen through a tool, with its success probability (decision quality)
* ``reflection``  - the end-of-turn plan + opponent prediction
* ``message``     - a chat message to the opponent
* ``resume``      - the server restarted mid-game: the game continues from the checkpoint at the start of a team
                    turn (``checkpoint`` = that turn's episode key) or, without one, from kick-off. Records written
                    after the checkpoint are dropped; ``restart_cost`` is what the dropped model calls cost.
* ``result``      - last line: final score, winner, per-side stats

Only the messages *added* since the previous call are stored, so an episode can be reconstructed exactly by
concatenating ``episode.messages`` and every following ``llm_call.new_messages`` for that seat/episode.
"""
import gzip
import json
import os
import threading
import time
import zlib
from typing import Optional

from bench import config


def transcript_path(match_id: str) -> str:
    d = os.path.join(config.data_dir(), "transcripts")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{match_id}.jsonl.gz")


class Transcript:
    def __init__(self, match_id: str, path: Optional[str] = None):
        self.match_id = match_id
        self.path = path or transcript_path(match_id)
        self._lock = threading.Lock()
        self._tmp = self.path + ".partial"
        self._f = gzip.open(self._tmp, "wt", encoding="utf-8")
        self.records = 0

    @classmethod
    def reopen(cls, match_id: str, keep: int):
        """Continue the partial transcript of an interrupted attempt, keeping its first ``keep`` records.
        Returns (transcript, dropped records, records kept)."""
        tmp = transcript_path(match_id) + ".partial"
        lines = _read_complete_lines(tmp) if os.path.exists(tmp) else []
        t = cls(match_id)
        kept = lines[:keep]
        for line in kept:
            t._f.write(line + "\n")
        t.records = len(kept)
        return t, [json.loads(line) for line in lines[keep:]], len(kept)

    def write(self, rtype: str, side: Optional[str] = None, **data):
        rec = {"type": rtype, "ts": round(time.time(), 3), "side": side}
        rec.update(data)
        line = json.dumps(rec, default=str, ensure_ascii=False)
        with self._lock:
            if self._f is None:
                return
            self._f.write(line + "\n")
            self.records += 1
            if rtype == "llm_call":
                self._f.flush()   # so the spend of a call survives a crash (see ``resume`` records)

    def flush(self) -> int:
        """Make everything written so far durable; returns the number of records."""
        with self._lock:
            if self._f is not None:
                self._f.flush()
            return self.records

    def close(self, complete: bool = True):
        with self._lock:
            if self._f is None:
                return
            self._f.close()
            self._f = None
        if complete:
            os.replace(self._tmp, self.path)
        # else: the match is re-queued and resumed later; the partial transcript is continued then


def _read_complete_lines(path: str):
    """Lines of a gzip file that may have been cut off mid-write (the process was killed)."""
    lines = []
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                if not line.endswith("\n"):
                    break
                lines.append(line[:-1])
    except (EOFError, OSError, zlib.error, UnicodeDecodeError):
        pass
    return lines


class NullTranscript:
    """Used when transcripts are disabled (e.g. `python -m bench play`)."""
    records = 0

    @classmethod
    def reopen(cls, match_id: str, keep: int):
        return cls(), [], 0

    def write(self, *args, **kwargs):
        pass

    def flush(self) -> int:
        return 0

    def close(self, complete: bool = True):
        pass


def read_transcript(match_id: str):
    path = transcript_path(match_id)
    if not os.path.exists(path):
        return
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)
