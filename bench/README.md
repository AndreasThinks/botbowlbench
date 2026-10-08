# BotBowl Bench

An LLM benchmark built on the [botbowl](https://github.com/njustesen/botbowl) Blood Bowl engine. Models from
[OpenRouter](https://openrouter.ai) coach teams against each other, acting **only through MCP tools**, while anyone
can watch the games live in the browser.

* **Live games.** One game at a time. The board updates as the models act, next to their chat, their plans and a
  play-by-play of every tool call.
* **Automatic tournaments.** Competitors are listed in [`models.yaml`](../models.yaml). The first start plays a
  round robin. Every model added later automatically gets a *placement*: five anchors spread across the table, home and away,
  then up to six top-up games against its closest opponents, so adding a model costs the same however big the list is.
* **Replays of every game.** The [Matches](#replays) archive lists every finished game. Each one can be watched again
  on the board with play/pause, speed, a scrubber with turn and touchdown markers, and the chat, plans and
  play-by-play kept in sync. It can hide the result until the end, and a link can point to any moment.
* **Rankings.** Elo-scale ratings (a Bradley-Terry fit over all games) with 95% ranges, TD difference, past tournaments, head-to-head records.
* **Play style and decision quality.** Aggression, risk taking, passing, fouling, chattiness, plus "safe actions
  first", odds of the risks taken, players left idle at a turnover, monitoring rate and reflection coverage. See
  [docs/METRICS.md](docs/METRICS.md).
* **Messaging.** `send_message` lets models talk (or trash-talk) to each other. Spectators see it.
* **Research-grade data.** Full transcripts of every prompt, response and tool call, seeds, harness and prompt
  versions, the model and provider that actually served each call, and an admissibility flag. All of it can be
  exported as JSON lines. See [docs/DATA.md](docs/DATA.md).

| Docs | |
|---|---|
| [docs/DEPLOY.md](docs/DEPLOY.md) | Deploying on Railway and day-to-day operations |
| [docs/METRICS.md](docs/METRICS.md) | Every metric, how it's computed, admissibility |
| [docs/DATA.md](docs/DATA.md) | Exports, transcript format, how to rebuild a conversation |

## Quick start (local)

```bash
pip install -r requirements-bench.txt
cythonize -i -3 botbowl/core/pathfinding/cython_pathfinding.pyx   # optional: much faster pathfinding
export OPENROUTER_API_KEY=sk-or-...        # optional: without it only the baselines play
python -m bench serve                      # http://localhost:8080
```

Other commands:

```bash
python -m bench models                                      # list competitors from models.yaml
python -m bench play scripted-baseline random-baseline -v   # one game in the terminal (no database)
python -m bench round-robin                                 # queue a fresh full round robin
python -m bench export ./dataset                            # dump all data as JSON lines
pytest tests/bench                                          # the bench's test suite
```

## Adding a model

Add an entry to `models.yaml` and deploy:

```yaml
models:
  - name: My Model            # display name; the id is derived from it (my-model)
    model: vendor/model-id    # any OpenRouter model that supports tool calling
    # optional: temperature, max_tokens, budget_usd_per_game, max_tool_calls_per_turn, turn_time_limit,
    #           prompt_cache, extra: {reasoning: {effort: low}}
```

The scheduler notices the new id and queues its placement. Setting `enabled: false` (or deleting the entry) retires a
model: its queued games are cancelled but its history stays. Renaming creates a *new* competitor.

Classic botbowl bots can compete too, as free baselines. `provider: botbowl` plays any bot from botbowl's registry
through the same MCP tools, so its games are scored like everyone else's:

```yaml
  - name: Bot Bowl scripted bot
    provider: botbowl
    bot: scripted                           # id passed to botbowl.make_bot
    module: examples.scripted_bot_example   # imported first so the bot registers itself
    max_tool_calls_per_turn: 200            # these bots move one square per call
```

## How a game is played

```
 botbowl Game (thread) ──act()──▶ Seat ◀──submit()── MCP tools ◀──call_tool── driver ◀──▶ OpenRouter
                         ▲  blocks until that seat's driver supplies an action
```

* **`session.py`** runs a botbowl `Game` in a thread. Each team is a `Seat` (a botbowl `Agent`) whose `act()` blocks
  until its driver submits an action. The session also keeps the live snapshot for spectators, replay frames, the
  chat, reflections and the action log.
* **`tools.py`** is the MCP server, one per seat, built with the official `mcp` SDK. High-level tools compile into
  botbowl's primitive actions using its pathfinding. Interrupting decisions (block dice, re-rolls, push squares,
  follow-ups) are handed back to the model. Every action's success probability is logged before it executes.
* **`driver.py`** is the agent loop. Each *episode* (a team turn, a setup, a coin toss...) starts a fresh
  conversation with the full situation; the MCP tools are offered as OpenAI-style function tools, and budgets are
  enforced. It passes `reasoning_details` back to thinking models and adds prompt-cache breakpoints for Anthropic
  models.
* **`render.py`** produces what the models read: an ASCII board, rosters, an event log and the legal options.
* **`scheduler.py`** syncs `models.yaml`, creates tournaments and plays matches one after the other.
  **`db.py`** stores everything in SQLite. **`transcript.py`** writes the full per-game record.
* **`web.py`** and `templates/` are the site. **`static/board.js`** draws the board (the "stadium"): a compact,
  responsive renderer of botbowl's game JSON that reuses botbowl's pitch images and player sprites. It shows the
  score, re-rolls and whose turn it is, the pitch, the dugouts and the last few game events. Live games poll the
  latest snapshot, cheaply, using ETag/304 and gzip. The original botbowl Angular UI is still available at `/board`.

### Replays

Every decision point of a game is stored as a frame: the full board state, zlib-compressed, about 0.6 MB per game.
A timeline holds one point per frame (time, half, turns, score, side to move). Finished games are listed at
`/matches`, filterable by model, tournament, result and admissibility. Each match page becomes a replay player:

| control | |
|---|---|
| ▶ / ❚❚, Space | play / pause (0.5×–8×) |
| ◀ ▶\|, ← → | step one decision |
| ⏮ ⏭, Shift+← → | previous / next team turn |
| scrubber | ticks mark turns, ▲ marks touchdowns (click to jump to the start of the scoring turn) |
| Sync with replay | the chat, plans and play-by-play only show what had happened at that point |
| Hide result | hides the final score and match stats until the replay reaches the end (remembered per browser) |
| `#f=<frame>` | the URL updates as you pause or seek, so you can share a link to a specific moment |

The stadium fetches frames from `/api/matches/<id>/frames/<i>`, prefetching a few ahead. They are served exactly as
stored (zlib is HTTP `deflate`) and cached as immutable, so scrubbing is cheap. Games recorded before timelines existed get theirs rebuilt from
their frames on first view.

### The tools models get

| tool | what it does |
|---|---|
| `get_state` | Score, turn, ASCII board, both rosters, opponent messages, legal options |
| `get_legal_actions` | Only the pending decision and its options |
| `get_player(player_id)` | Stats and skills. For your own players, reachable squares grouped by success probability. |
| `move(player_id, x, y)` | Move action along the safest path |
| `block(player_id, target_id)` | Block an adjacent standing opponent |
| `blitz(player_id, target_id, via_x?, via_y?)` | Move and block (once per turn) |
| `pass_ball(player_id, target_x, target_y, move_to_x?, move_to_y?)` | Pass (once per turn) |
| `handoff(player_id, target_id, move_to_x?, move_to_y?)` | Hand-off (once per turn) |
| `foul(player_id, target_id)` | Foul a prone opponent (once per turn) |
| `take_action(action_type, player_id?, x?, y?)` | Any legal primitive: block dice, re-rolls, push, follow-up, setup formation, coin toss, kick |
| `end_turn(plan, prediction)` | End the turn with a short reflection: the plan for next turn and what the opponent will do. Returns when you're needed again. |
| `reflect(plan, prediction)` | The same reflection without ending the turn. The model is asked for it once when a turnover ends its turn. |
| `send_message(text)` | Chat to the opponent: max 2 per turn, 280 characters |

### Protocol and fairness

* 5-a-side, 8 turns per half, Human vs Human mirror matches, each pairing played home and away. All of this is
  configurable in `models.yaml` → `settings`.
* Per team turn: at most `max_tool_calls_per_turn` (40) tool calls and `turn_time_limit` (300 s). Three invalid calls
  in a row, three consecutive output truncations (`finish_reason=length` with no tool call), or an exhausted budget,
  lets a safe default policy finish the turn. Per game: `budget_usd_per_game`
  ($2) per model and `max_game_minutes` (120).
* An HTTP 401/402 from OpenRouter (bad key, no credit) puts the game back in the queue instead of scoring it.
  Bugs in the bench's own tools are reported to the model as "not your fault", never counted as invalid moves, and
  make the game inadmissible.
* The ending reflection is part of the protocol (`PROTOCOL_VERSION` 1.1). It changes behaviour a little, as any
  scaffold does, but it is identical for every model. It is what makes plan follow-through and forecast accuracy
  measurable.
* **Output length:** the harness no longer imposes a default response-token cap on any model. When
  `max_tokens` is absent or null, requests omit the field entirely; no `max_completion_tokens` cap is substituted.
  Provider defaults and model/context limits still apply, so this is not unlimited generation. An explicit
  per-model `max_tokens` remains available for opt-in experiments, but no shipped model uses it. Dollar, HTTP,
  turn/game-time and bounded retry guards remain; the dollar cap is checked between calls and one response can
  overshoot it. Removing the cap is not yet validated as a gameplay improvement; do not pool protocol 1.1
  results with older games. No paid API calls were made for this change.

### Cost

Measured with a fake API: each conversation starts at about 3.5k tokens (system prompt, tool schemas, board and
rosters) and grows by a few hundred tokens per tool call. A model that activates every player will make very roughly
150–300 calls per game, which is on the order of 1–2M input tokens. That's cents for cheap models and a few dollars
for frontier ones. This is an estimate until real games have been played. Prompt caching (automatic for
OpenAI/DeepSeek/Gemini, enabled by the bench for Anthropic models) reduces it. `budget_usd_per_game` caps it.

## Development notes

* The botbowl web UI is AngularJS. After editing `botbowl/web/static/js/*.js`, rebuild the bundle with
  `python bench/build_js.py`, which does the same as the original gulp task, without Node.
* `tests/bench/conftest.py` contains a fake OpenRouter server used by the tests. It returns tool calls, malformed
  arguments, text-only replies, `finish_reason=length` truncations, `reasoning_details` and usage data, so the full
  driver path is tested without spending money.
* The original botbowl test suite still passes: `pytest tests`.

## Known limitations

* No real-model run has validated the prompts yet. Run `python -m bench play <model> scripted-baseline -v` with a
  real key before the first tournament.
* Two games per pairing is a small sample for a dice game. Watch the 95% ranges, and raise `legs` if they stay wide.
* The scripted baseline is deliberately simple. It's a floor, not a strong bot. The *Bot Bowl scripted bot* (botbowl's
  own scripted bot) is a stronger reference. The Bot Bowl IV and V champion, Drefsante, isn't included: it is a
  closed-source Java program with no published license.
