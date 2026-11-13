"""Files a match leaves in the data directory besides its transcript: replay frames with their timeline index, and
the checkpoint an interrupted game resumes from."""
import json
import os
import pickle
import struct
import zlib
from typing import Dict, List, Optional

from bench import config, db, version
from bench.match import CHECKPOINT_VERSION
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
    _frame_cache.pop(match_id, None)   # e.g. the empty answer given to a spectator who looked before kick-off
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


def prune_checkpoints(keep: set):
    """Delete every checkpoint whose match is not in ``keep`` (the games that may still resume)."""
    for name in os.listdir(os.path.dirname(checkpoint_path("x"))):
        if name.split(".")[0] not in keep:
            delete_checkpoint(name.split(".")[0])


def delete_checkpoint(match_id: str):
    for path in (checkpoint_path(match_id), checkpoint_path(match_id) + ".tmp"):
        try:
            os.remove(path)
        except OSError:
            pass


def delete_match_files(match_id: str):
    """Everything a match wrote to disk: its checkpoint, replay and transcript (finished or partial)."""
    delete_checkpoint(match_id)
    _frame_cache.pop(match_id, None)
    for path in (frames_path(match_id), timeline_path(match_id), transcript_path(match_id),
                 transcript_path(match_id) + ".partial"):
        if os.path.exists(path):
            os.remove(path)
