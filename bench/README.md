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
| `end_turn(plan, prediction, notes)` | End the turn with a short reflection: the plan for next turn, what the opponent will do, and the notebook (or `unchanged`). Returns when you're needed again. |
| `reflect(plan, prediction, notes)` | The same reflection without ending the turn. The model is asked for it once when a turnover ends its turn. |
| `update_notes(notes)` | Replace the notebook at any time (not a game action). |
| `send_message(text)` | Chat to the opponent: max 2 per turn, 280 characters |

### Protocol and fairness

* 5-a-side, 8 turns per half, Human vs Human mirror matches, each pairing played home and away. All of this is
  configurable in `models.yaml` → `settings`.
* Per team turn: at most `max_tool_calls_per_turn` (40) tool calls and `turn_time_limit` (300 s). Three invalid calls
  or three output truncations (`finish_reason=length` with no tool call), counted separately as running totals since
  the last successful game action (not strictly consecutive; info, chat, reflect and notebook calls do not reset them), or an
  exhausted budget, lets a safe default policy finish the turn. Per game: `max_game_minutes` (180, above the 160 minutes of turn clocks), and an optional
  `budget_usd_per_game` per model (off by default: cost is tracked, not capped).
* An HTTP 401/402 from OpenRouter (bad key, no credit) puts the game back in the queue instead of scoring it.
  Bugs in the bench's own tools are reported to the model as "not your fault", never counted as invalid moves, and
  make the game inadmissible.
* The ending reflection is part of the protocol. It changes behaviour a little, as any
  scaffold does, but it is identical for every model. It is what makes plan follow-through and forecast accuracy
  measurable.
* **Notebook (`PROTOCOL_VERSION` 1.3):** every decision starts a fresh conversation, so the only memory a model
  carries between decisions is what it writes down. Each side has a private notebook of at most 1,500 characters
  that starts empty every game, is rewritten through `notes` on `end_turn` / `reflect` or `update_notes`, and is
  shown at the top of every decision together with the model's last plan and prediction. What goes in it is up to
  the model; the harness never writes to it. Since 1.4 the instructions also ask it to carry over lessons that still
  apply and not to copy board positions, which it sees anyway. Spectators see it on the match page; the opponent
  never does.
* **Turn time limit:** `turn_time_limit` is a hard deadline. The last `last_chance_seconds` (45) are held back: a
  reply still running when they start is cancelled and the model gets one short call ("Time is almost up… reply now
  with a tool call") with `last_chance_max_tokens` (2048) and a 1024-token thinking budget (or the lowest effort the model lists), and keeps those short
  settings for the rest of the turn. A reply still running at the deadline itself is cancelled and the turn
  auto-finishes, exactly like the between-calls time check. Cut-offs are logged as time-limit events, not errors,
  counted in `deadline_cutoffs` (and `last_chance_calls`, `uncertain_spend_calls`), and never count towards marking
  the model unavailable. httpx's timeout is per socket read and OpenRouter keeps slow requests alive, so the HTTP
  timeout alone never bounded a long generation. The clock only counts the model's own time: it pauses while a tool
  call waits for the opponent (for example when the defender picks the block die). The one-shot `reflect` call after
  a turnover runs during the opponent's turn, so it gets the same short settings and `last_chance_seconds` limit.
* **Reply time limit (`PROTOCOL_VERSION` 1.6):** one reply may run for at most `reply_time_limit` (120) seconds. A
  reply still running then is cancelled, and the model's next message hands back the end of the thinking it had
  streamed (last 6,000 characters) with "act on it now"; the model keeps its normal settings and the rest of the turn.
  The last-chance note hands back a cancelled reply's thinking the same way. Before 1.6 a turn's opening call could
  think for 3-4 minutes and then lose all of it to the clock or the output ceiling (Mistral Large 4: 37 of 51 opening
  calls never produced a move), while a fresh call for block dice or setup with the same intro took seconds: the time
  goes on planning the whole turn, not on reading the situation. When the model's own thinking is in the
  conversation, its next call acts in seconds. Cuts are counted in `thinking_cuts` (and `uncertain_spend_calls`).
  Models whose providers hide their thinking get the note without it. `reply_time_limit: null` turns this off.
* **Tool calls are required:** requests send `tool_choice: required` (setting `tool_choice`), so a reply must be a
  tool call rather than prose. If a provider rejects it (for example Anthropic with extended thinking), the request is
  re-sent with `auto` and that model stays on `auto` for the rest of the game.
* **Thinking (`PROTOCOL_VERSION` 1.5):** every model is sent `reasoning: {enabled: true}` (setting
  `reasoning_enabled`), so it thinks at its own default effort as listed by OpenRouter (Opus 5.5 high, GPT-6 Luna
  medium...), and models where thinking is optional (DeepSeek V3.2, Gemini 2.5 Flash Lite, Haiku 5.5) have it
  switched on. Protocol 1.2-1.4 sent a shared 12k `reasoning.max_tokens` instead; effort-only models had that mapped to
  an effort level below their default. A shared budget is still available as `reasoning_max_tokens`, a model's own
  `extra.reasoning` overrides both, and `reasoning_enabled: false` sends nothing. Runaway thinking is bounded by the
  output ceiling and the turn clock below, and counts against the model.
* **Output length:** every model gets the same default ceiling, `max_output_tokens` (16384), sent as `max_tokens`. It
  is far above the old 2048 cap that starved reasoning models, and exists to turn runaway or looping generations into
  classified `output_truncations` instead of turns lost to the clock. A per-model `max_tokens` overrides it, and
  `max_tokens: null` removes it for that model. The dollar cap is checked between calls and one response can overshoot
  it. A cancelled or timed-out call is never retried (the provider may still generate and bill it) and its cost is
  unknown, so it is counted in `uncertain_spend_calls`, not in `cost` or the dollar cap.
* **Host routing:** every request asks OpenRouter to sort a model's hosts by recent throughput
  (`provider_sort: throughput`), to prefer hosts that recently ran at `provider_min_throughput` (50) tokens/s or more
  (slower ones go to the back of the queue, not out of it), and never to pay more than `provider_max_price_factor` (3)
  times the model's listed price per token. The listing is often the cheapest, slowest host, so a tighter cap shut
  out every fast host for models like DeepSeek. Claude seats also send a `session_id` (`<match>:<side>`) so a game's
  calls stay on one host and its prompt cache stays warm; other models are not pinned, so a slow first host isn't
  kept for the whole game. This is routing, not the prompt, so it does not change the protocol version; the host
  that served each call is still logged.
* **Prompt caching:** Anthropic models also get a top-level `cache_control` so the cache follows the growing
  conversation inside a turn (on top of the fixed breakpoints on the system prompt and first message). Other providers
  cache automatically. If a host rejects the field it is dropped for the rest of that client's life.
* **Empty content-filter replies (`PROTOCOL_VERSION` 1.4):** a reply with `finish_reason=content_filter`, no tokens,
  no text and no tool call means a provider-side filter stopped the call before the model answered. It is retried
  like any other provider error (counted in `content_filter_retries`) instead of counting as a reply without a tool
  call; if every retry is filtered, the call fails as an LLM error. Leaderboard, ratings,
  tournament standings and placement use only games whose `meta.harness.protocol_version` equals the current version
  (missing = legacy 1.0); older games stay available as archives, replays and transcripts.

### Cost

Measured with a fake API: each conversation starts at about 3.5k tokens (system prompt, tool schemas, board and
rosters) and grows by a few hundred tokens per tool call. A model that activates every player will make very roughly
150–300 calls per game, which is on the order of 1–2M input tokens. That's cents for cheap models and a few dollars
for frontier ones. This is an estimate until real games have been played. Prompt caching (automatic for
OpenAI/DeepSeek/Gemini, enabled by the bench for Anthropic models) reduces it. `budget_usd_per_game` can cap it (off by default).

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
