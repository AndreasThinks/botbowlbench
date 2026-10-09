# Dataset

Everything the bench records, how to get it, and how to read it.

## Getting the data

| what | where |
|---|---|
| All finished games | `GET /api/export/matches.jsonl`, or `?admissible=1` for research-grade games only |
| Reflections (plans and predictions) | `GET /api/export/events.jsonl?kind=reflection` |
| Chat messages | `GET /api/export/events.jsonl?kind=message` |
| Spectator feed (all events) | `GET /api/export/events.jsonl` (tool results truncated to 800 characters) |
| Full transcript of one game | `GET /api/matches/<id>/transcript` (`.jsonl.gz`) |
| Replay timeline (one point per frame) | `GET /api/matches/<id>/timeline` |
| Board state per decision (replay frame) | `GET /api/matches/<id>/frames/<i>` (botbowl game JSON) |
| Finished games, paginated and filterable | `GET /api/matches?model=&tournament=&result=decisive\|draw&admissible=1&before=<finished_at>&limit=` |
| Everything, offline | `python -m bench export ./dataset` (run where the data volume is mounted) |

`python -m bench export` writes `models.jsonl`, `tournaments.jsonl`, `matches.jsonl`, `events.jsonl` and
`transcripts/*.jsonl.gz`. On Railway, run it from a shell on the service (`railway ssh`), or download the files over
the HTTP endpoints above.

## `matches.jsonl`

One JSON object per game:

```jsonc
{
  "id": "…uuid…", "tournament_id": 3, "tournament_name": "Gauntlet: GPT-5 mini", "tournament_kind": "gauntlet",
  "home_model": "gpt-5-mini", "home_name": "GPT-5 mini", "away_model": "deepseek-v3-2", "away_name": "DeepSeek V3.2",
  "status": "completed",                   // completed | error
  "home_score": 1, "away_score": 0, "winner": "home",
  "started_at": 1791390000.1, "finished_at": 1791393600.2, "duration": 3600.1,
  "seed": 1660748639,                      // dice seed: same seed + same decisions = same game
  "admissible": true,
  "home_stats": { … see METRICS.md … }, "away_stats": { … },
  "meta": {
    "harness": {"git_sha": "c9dd02051b22", "protocol_version": "1.0", "prompt_fingerprint": "392a09dcebf7"},
    "settings": { … models.yaml settings in force … },
    "models": {"home": { … model config … }, "away": { … }},
    "served": {"home": {"openai/gpt-5-mini-2025-08-07@OpenAI": 212}, "away": { … }},
    "time_capped": false, "harness_errors": [], "inadmissible_reasons": [], "transcript_records": 4211,
    "resumes": []
  },
  "has_transcript": true
}
```

`resumes` lists restarts of the server during the game (normally empty). Each entry has `ts`, `checkpoint` (the
turn the game continued from, e.g. `away:turn-2-5`, or null for a restart from kick-off), `git_sha` of the code that
continued it, `dropped_records` and `restart_cost` (see the `resume` transcript record).

Comparability: only compare games that share `meta.harness.protocol_version`. `prompt_fingerprint` changes whenever
the system prompt, rules primer or tool descriptions change. `PROTOCOL_VERSION` (in `bench/version.py`) is bumped by
hand when a change makes results incomparable. Protocol **1.1** classifies `finish_reason=length` (no tool call) as
`output_truncations` rather than `no_tool_replies`, and removes the default harness response-token cap for all
models (provider limits still apply). The leaderboard, rating fit, tournament standings and newcomer placement
include only games of the current protocol (a missing `protocol_version` counts as legacy 1.0); 1.0 games remain in
the database, replays, transcripts and exports.

## Transcripts (`<match_id>.jsonl.gz`)

The complete research record of one game. Each line has `type`, `ts` (unix seconds) and `side`
(`home` / `away` / null):

| type | fields |
|---|---|
| `meta` | first line: same as `matches.meta` (seed, harness, settings, models) |
| `episode` | a fresh conversation for one seat: `episode` (key such as `turn-1-3`, `Setup-1-0`), `our_turn`, `proc`, `half`, `turn`, `messages` (system prompt + opening situation) |
| `llm_call` | `episode`, `new_messages` (sent since the previous call: tool results, nudges), `response` (`content`, `reasoning`, `message` exactly as returned incl. `tool_calls` / `reasoning_details`, `finish_reason`), `usage` (`prompt_tokens`, `completion_tokens`, `cached_tokens`, `reasoning_tokens`, `cost`), `latency`, `served_model`, `provider`, `generation_id` |
| `llm_error` | `episode`, `error`, `infra`, `fatal`, `timeout`, `uncertain_spend` |
| `llm_cutoff` | a model call cancelled at the turn's time limit: `episode`, `error`, `waited` (seconds since the turn began), `last_chance` (true when the model was then asked for a short reply) |
| `tool` | `episode`, `call_id`, `name`, `args`, `raw_args`, `ok`, `result` (full text the model saw), `duration` |
| `action` | `half`, `turn`, `activation` (nth player activated this turn), `action`, `player`, `target`, `target_player`, `reach_prob`, `roll_prob`, `success_est`, `block_dice`, `unused_before` (on START_* actions), `turnover`, `touchdown` |
| `action_outcome` | a turnover or touchdown attributed to the last logged action of the turn |
| `reflection` | `half`, `turn`, `plan`, `prediction`, `trigger` (`end_turn` or `reflect` after a turnover) |
| `message` | `text`, `half`, `turn` |
| `system` | budget auto-finish and similar notes |
| `harness_error` | a bug in the bench (`text`, `traceback`) |
| `resume` | the server restarted mid-game and the game continued from `checkpoint` (see `meta.resumes`). The interrupted team turn is played again from its start, so records written after the checkpoint were dropped (`dropped_records`); `restart_cost` is what their model calls cost per side |
| `result` | last line: scores, winner, both stats objects, final `meta` |

### Rebuilding exactly what a model saw

```python
import gzip, json

convo = {}
for line in gzip.open("match.jsonl.gz", "rt"):
    r = json.loads(line)
    key = (r["side"], r.get("episode"))
    if r["type"] == "episode":
        convo[key] = list(r["messages"])
    elif r["type"] == "llm_call" and key in convo:
        convo[key] += r["new_messages"] + [r["response"]["message"]]
# convo[("home", "turn-1-3")] is the full conversation for the home team's 3rd turn of the first half
```

The follow-up reflection after a turnover uses the episode key `<key>#reflect` and continues the same conversation.

## Events (`events.jsonl`)

The spectator feed: `id`, `match_id`, `ts`, `side`, `kind`, `payload`. Kinds: `episode`, `thought` (model text,
truncated), `tool` (result truncated), `message`, `reflection`, `illegal`, `system`, `error`. For analysis, prefer
transcripts (complete). Events are convenient for messages and reflections across many games.

## Storage

Everything lives under `DATA_DIR` (`/data` on Railway):

```
bench.db                    SQLite: models, tournaments, matches, events
frames/<match_id>.frames    zlib-compressed board JSON per decision (replays), ~0.6 MB per game
frames/<id>.timeline.json   per-frame index: ts, half, ht/at (turns), hs/as (score), side to move, over
transcripts/<id>.jsonl.gz   full transcripts: ~30 KB for a baseline game; LLM games are larger (estimated 0.1-1 MB)
checkpoints/<id>.ckpt       the game in progress, saved at the start of every team turn (< 1 MB); deleted when it ends
```

Back up the volume (or run `python -m bench export`) before deleting it. Nothing else holds the history.
