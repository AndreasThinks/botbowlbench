"""Putting games back in the queue: on request (an errored game), and once on start-up for games that an outage
spoiled before the harness learned to treat that kind of outage as its own problem rather than the models'."""
from typing import Iterable

from bench import db
from bench.storage import delete_match_files

# games a seat played entirely on a host far below the minimum speed (see replay_spoiled_games)
SLOW_HOST_REPLAYS = ("f52d7abe-aeb9-4724-9192-19135a01ca55",   # Claude Haiku 5.5 1-0 DeepSeek V4.1 Flash
                     "d8c2fe72-f6aa-4366-ad60-08c96e324b90")   # DeepSeek V3.2 1-0 DeepSeek V4.1 Flash


def requeue_fresh(ids: Iterable[str]):
    """Put games back in the queue as if they had never been played."""
    for mid in ids:
        db.execute("DELETE FROM events WHERE match_id=?", (mid,))
        delete_match_files(mid)
        db.execute("UPDATE matches SET status='queued', started_at=NULL, finished_at=NULL, error=NULL, seed=NULL, "
                   "home_score=NULL, away_score=NULL, winner=NULL, home_stats=NULL, away_stats=NULL, "
                   "duration=NULL, frames=0, meta=NULL, admissible=NULL WHERE id=?", (mid,))
        db.execute("UPDATE tournaments SET status='queued', finished_at=NULL WHERE id=(SELECT tournament_id FROM "
                   "matches WHERE id=?) AND status='completed'", (mid,))


def _replay(ids, why: str):
    if ids:
        requeue_fresh(ids)
        print(f"requeued {len(ids)} games {why}", flush=True)


def replay_spoiled_games():
    """Each rule is a no-op once its games have been replayed."""
    # Before a 403 "Key limit exceeded" counted as an infra error, hitting the OpenRouter key's spending limit
    # marked both models unavailable and the game was played out on default actions. Those games say nothing
    # about the models.
    _replay([r["id"] for r in db.rows(
        "SELECT DISTINCT m.id FROM matches m JOIN events e ON e.match_id=m.id "
        "WHERE m.status='completed' AND e.kind='error' AND e.payload LIKE '%Key limit exceeded%'")],
        "played after the OpenRouter key limit was hit")
    # Likewise games where one host's content filter answered every retry with an empty reply until the model
    # was marked unavailable (Opus 5.5 pinned to Azure, before filtering hosts were skipped). An interrupted
    # game (queued again after a restart) starts over too, rather than resuming with the model unavailable.
    _replay([r["id"] for r in db.rows(
        "SELECT DISTINCT m.id FROM matches m JOIN events e ON e.match_id=m.id "
        "JOIN events u ON u.match_id=m.id AND u.side=e.side "
        "WHERE m.status IN ('completed', 'queued') AND e.kind='error' "
        "AND e.payload LIKE '%provider content filter returned an empty reply%' "
        "AND u.kind='system' AND u.payload LIKE '%Model unavailable%'")],
        "a provider content filter put on default actions")
    # And games where a seat was stuck for the whole game on a host answering at ~20 tokens/s, before such
    # hosts were skipped (DeepSeek V4.1 Flash on OpenInference: 32 deadline cutoffs a game). Found by checking
    # every game's transcript for 2026-10-14; a replayed game records slow_host_skips, so this runs once.
    _replay([r["id"] for r in db.rows(
        "SELECT id, home_stats, away_stats FROM matches WHERE status='completed' AND id IN "
        f"({','.join('?' * len(SLOW_HOST_REPLAYS))})", tuple(SLOW_HOST_REPLAYS))
        if "slow_host_skips" not in (r["home_stats"] or "") + (r["away_stats"] or "")],
        "played on a host far below the minimum speed")
