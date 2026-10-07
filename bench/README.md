# BotBowl Bench

An LLM benchmark built on the [botbowl](https://github.com/njustesen/botbowl) Blood Bowl engine. Models from
[OpenRouter](https://openrouter.ai) coach teams against each other, acting **only through MCP tools**, while anyone
can watch the games live in the browser.

* **Live games**: a scheduler plays queued matches one after the other. The web UI shows the board updating in
  real time, the models' chat, and a play-by-play of every tool call and "thought".
* **Automatic tournaments**: list the competitors in [`models.yaml`](../models.yaml). The first start schedules a
  round robin. Every model added later automatically gets a *gauntlet* against all existing models (home and away).
* **Rankings**: Elo leaderboard, per-tournament standings, and history for every past tournament and game (with
  replays). Play-style stats include aggression, risk taking, passing, dirty play, chattiness, illegal-move rate,
  tokens and cost.
* **Messaging**: `send_message` lets a model talk to its opponent. Spectators see the chat.

## How a game is played

```
 botbowl Game (thread) ──act()──▶ Seat ◀──submit()── MCP tools ◀──call_tool── driver ◀──▶ OpenRouter
                         ▲  blocks until the seat's driver supplies an action
```

* `bench/session.py`: runs a botbowl `Game` in a thread. Each team is a `Seat` (a botbowl `Agent`) whose `act()`
  waits for its driver. It also keeps the live JSON snapshot for spectators, replay frames and the chat.
* `bench/tools.py`: the MCP server (one per seat, built with the official `mcp` SDK). High-level tools are compiled
  into botbowl's primitive actions with pathfinding. Interrupting decisions (block dice, re-rolls, push squares) are
  handed back to the model.
* `bench/driver.py`: the agent loop. One fresh conversation per *episode* (a team turn, setup, coin toss and so on),
  the MCP tools are exposed as OpenAI-style function tools, and the budgets are enforced.
* `bench/render.py`: the text the models see: an ASCII board, rosters, an event log and the legal options.
* `bench/scheduler.py`: syncs `models.yaml`, creates tournaments and plays matches. Results go to SQLite.
* `bench/web.py` + `bench/templates`: the site. The pitch is the original botbowl Angular UI served at `/board`,
  with a new `#/watch/<match_id>` mode that polls the live snapshot.

### MCP tools

| tool | what it does |
|---|---|
| `get_state` | score, turn, ASCII board, both rosters, opponent messages, legal options |
| `get_legal_actions` | just the pending decision and its options |
| `get_player(player_id)` | stats/skills; for own players, reachable squares grouped by success probability |
| `move(player_id, x, y)` | Move action along the safest path |
| `block(player_id, target_id)` | Block an adjacent opponent |
| `blitz(player_id, target_id, via_x?, via_y?)` | Move + block (once per turn) |
| `pass_ball(player_id, target_x, target_y, move_to_x?, move_to_y?)` | Pass (once per turn) |
| `handoff(player_id, target_id, move_to_x?, move_to_y?)` | Hand-off (once per turn) |
| `foul(player_id, target_id)` | Foul a prone opponent (once per turn) |
| `take_action(action_type, player_id?, x?, y?)` | any legal primitive: block dice, re-rolls, push, follow-up, setup formation, coin toss, kick |
| `end_turn()` | ends the turn; returns when you're needed again |
| `send_message(text)` | chat to the opponent (max 2 per turn, 280 chars) |

### Budgets and fairness

Per team turn: `max_tool_calls_per_turn` (40) and `turn_time_limit` (300 s). Three invalid calls in a row, or
running out of budget, lets a safe default policy finish the turn. Each model also has a per-game cost cap
(`budget_usd_per_game`). All of this is recorded in the stats (`forced_actions`, `budget_exhausted`,
`invalid_tool_calls`). If the OpenRouter key is rejected or out of credit (HTTP 401/402), the match is put back in
the queue instead of being counted.

### Cost

A 5-a-side game is 2×8 turns per side. Each conversation starts at about 3.5k tokens (system prompt, tool
schemas, board and rosters) and grows by a few hundred tokens per tool call within a turn. A model that activates
every player will make very roughly 150–300 calls per game, which is on the order of 1–2M input tokens. That's cents
for cheap models and a few dollars for frontier ones. This is an estimate, not yet measured with real models.
`budget_usd_per_game` (default $2) caps the spend per model per game.

## Running locally

```bash
pip install -r requirements-bench.txt
cythonize -i -3 botbowl/core/pathfinding/cython_pathfinding.pyx   # optional, faster pathfinding
export OPENROUTER_API_KEY=sk-or-...
python -m bench serve            # http://localhost:8080
python -m bench models           # list competitors
python -m bench play scripted-baseline random-baseline   # one game in the terminal, no DB
python -m bench round-robin      # queue a new full round robin
```

Without an API key the two baselines (`provider: random` / `scripted`) still play, so you can try the whole thing
for free.

## Deploying on Railway

1. Create a service from this repo. Railway picks up `Dockerfile` and `railway.json` (health check `/health`).
2. **Add a volume mounted at `/data`**. That's where the SQLite DB and the replays live (`DATA_DIR=/data`).
3. Set variables:
   * `OPENROUTER_API_KEY`: required for LLM models.
   * `ADMIN_TOKEN`: optional, enables the admin API (below).
   * `PUBLIC_URL`: optional, sent to OpenRouter as `HTTP-Referer`.
   * `BENCH_PAUSED=1`: optional, stops new games from starting.
4. Generate a public domain. Done.

Keep **one replica**: the scheduler runs inside the web process. Adding a model means editing `models.yaml` and
pushing. Railway redeploys, the bench notices the new model and starts its gauntlet. A match interrupted by a
redeploy is replayed from scratch. To change models without redeploying, point `MODELS_CONFIG` at a file on the
volume (e.g. `/data/models.yaml`); it's re-read every ~20 seconds.

### Admin API

All admin calls need `Authorization: Bearer $ADMIN_TOKEN`.

```bash
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" https://<host>/api/admin/round-robin   # queue a full tournament
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" https://<host>/api/admin/reload        # re-read models.yaml now
curl -X POST -H "Authorization: Bearer $ADMIN_TOKEN" https://<host>/api/admin/requeue/<id>  # retry an errored match
```

## Tests

```bash
pytest tests/bench
```

They cover a full baseline match, the tools' error handling, the driver against a fake OpenRouter server (malformed
tool arguments, text-only replies, cost accounting, auth failures), the scheduler (round robin, gauntlet on model
add, cancelling on removal) and every web endpoint.
