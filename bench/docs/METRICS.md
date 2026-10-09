# Metrics

Every finished game stores a stats object per side (`home_stats` / `away_stats` in `matches.jsonl`). The leaderboard
aggregates them per model. Rates are per **team turn** unless stated otherwise. A 5-a-side game has 8 turns per half,
so each side normally plays 16 turns, plus any kick-off Blitz or Quick Snap turns.

## Results

| metric | definition |
|---|---|
| Elo | A Bradley-Terry fit on the Elo scale over all completed games at once (win = 1, draw = 0.5), so it doesn't depend on game order and models that never met are compared through common opponents. A weak prior (mean 1000, sd 400) keeps unbeaten or winless models finite. The chart redoes the fit as games accumulate. |
| 95% range | rating ± 1.96 standard errors, from the curvature of the fit. Few games means a wide range. Overlapping ranges mean the order between two models is not established. |
| W-D-L, win % | From the final score. |
| TD diff | Touchdowns scored minus conceded. Usually a less noisy signal than wins, because Blood Bowl has many low-scoring draws. |
| points | Tournament tables only: 3 for a win, 1 for a draw. Ties are broken by TD diff, then TDs scored. |

## Play style

| metric | definition |
|---|---|
| `aggression` | (blocks + blitzes + fouls) / turns |
| `risk_taking` | (dodges + GFIs attempted, successful or not) / turns |
| `passing_game` | (passes + hand-offs) / turns |
| `dirty_play` | fouls / turns |
| `chattiness` | messages sent / turns. `message_chars` / `messages_sent` gives the average length. |
| `casualties_inflicted`, `knockdowns_inflicted`, `kos_inflicted` | Damage done to the opposing team |
| `turnovers`, `turnover_causes` | Number of turnovers, and the failure that caused each one (`failed_dodge`, `failed_gfi`, `failed_pickup`, `fumble`, `failed_catch`, `failed_pass`, `interception`, `knocked_down`, `ejected`, ...) |

## Decision quality

These are computed from the **action log**. Every move, block, pass, hand-off or foul made through a tool is logged
*before* it executes, together with the odds botbowl itself computes (pathfinding probabilities and roll targets).

| metric | definition |
|---|---|
| `success_est` (per action) | `reach_prob × roll_prob`. `reach_prob` is the probability of reaching the square (dodges, GFIs, pick-ups on the path). `roll_prob` is the probability of the final roll: the pass, hand-off or catch target, the foul's armour break, or, for a block, the probability the attacker is *not* knocked down. For a block, each die has a 1/6 chance of Attacker Down and 1/6 of Both Down, which is harmless if the attacker has Block. If the attacker picks the die, the block only fails when every die is bad; if the defender picks, it fails when any die is bad. Team re-rolls are ignored. |
| `risky_actions`, `long_shots` | Actions with `success_est` < 0.99, and with `success_est` < 0.5 |
| `avg_risky_success` | Mean `success_est` over the risky actions |
| `safe_first_rate` | Over every pair of actions (i before j) in the same turn, the share where i was at least as safe as j. 1.0 means the model always did safe actions before risky ones, which is Blood Bowl's golden rule. |
| `unactivated_at_turnover` | Players left unactivated when a turnover ended the turn: the activation's `unused_before` − 1, averaged over turnovers. High values mean risk was taken too early. |
| `blocks_against_odds` | Blocks where the defender picked the die |
| `monitoring_rate` | Share of non-infrastructure tool calls spent observing rather than acting: (`get_state` + `get_legal_actions` + `get_player`) / (those + action tools). This is the analogue of CivBench's Proactive Monitoring Rate (PMR). `end_turn`, `reflect` and `send_message` are excluded. |
| `illegal_rate` | `invalid_tool_calls` / `tool_calls`. Invalid means an illegal move, unparseable arguments, or a schema error. Harness bugs are never counted (see below). |
| `reflections`, `reflection_coverage` | Reflections written, and reflections / turns. A reflection is written at `end_turn`, or through the one-shot `reflect` prompt after a turnover. |

## Budget and cost

| metric | definition |
|---|---|
| `tool_calls`, `llm_calls`, `episodes` | Counts. An episode is one fresh conversation: a team turn, a setup, a coin toss... |
| `prompt_tokens`, `completion_tokens`, `cached_tokens`, `reasoning_tokens`, `cost` | As reported by OpenRouter's `usage` field. `cost` is in USD. |
| `cache_hit_rate` | `cached_tokens / prompt_tokens` |
| `latency` | Total seconds spent waiting for the model. The leaderboard shows the average per call. |
| `forced_actions`, `budget_exhausted`, `timeouts` | Decisions taken by the default policy, and the reasons |
| `no_tool_replies`, `llm_errors` | Replies without a tool call (and without `finish_reason=length`), and API errors after retries |
| `output_truncations` | Replies with `finish_reason=length` and no tool call — the completion budget was spent (often on reasoning) before a tool call. Counted separately from `no_tool_replies`. The count since the last successful game action (`max_truncation_streak`, default same as `max_illegal_streak`) auto-finishes the turn; it is a running total, not strictly consecutive, and only a successful action tool resets it (info/chat/reflect alone do not). |
| `http_timeouts`, `uncertain_spend_calls` | HTTP read timeouts (never retried) and calls whose provider-side cost is unknown because no usage came back; the real spend may exceed `cost`. |
| `deadline_cutoffs` | Model calls cancelled because the turn's `turn_time_limit` ran out while waiting for the reply. The turn auto-finishes (also counted in `budget_exhausted`); not an `llm_error`. |
| `served` | `{"<model>@<provider>": calls}`: what OpenRouter actually routed to |
| `restart_cost` | Only after a server restart mid-game: USD spent on the part of a turn that was played again. Not included in `cost` or the game budget. |

## Offline metrics (from the dataset)

These need a judge model or extra analysis, so they are computed from the [dataset](DATA.md) rather than live:

* **Plan follow-through (RAG@K, as in CivBench):** extract the commitments in each `reflection.plan`, then check
  the following K turns' `action` records for matching actions. Score executed = 1, partial = 0.5. In a 16-turn
  game, K = 1 or 2 is the natural window.
* **Forecast accuracy:** compare each `reflection.prediction` with what the opponent actually did on its next turn
  (that side's `action` records and the game events).
* **Honesty / bluffing:** compare what a model says in `message` records with its private `reflection.plan` from the
  same turn.
* **Message classification:** taunt, bluff, information leak, sportsmanship, ...

Validate any judge-based labelling against a human-labelled sample, as CivBench did (they reported Cohen's κ),
before relying on it.

## Admissibility

A game is `admissible` when it ended naturally under the pinned protocol with complete logs. Reasons it may not be:

| reason | meaning |
|---|---|
| `engine_error` | The botbowl engine raised an exception |
| `harness_error` | A bug in the bench's own tools. These are never counted against the model, which is told "not your fault". |
| `time_capped` | The game exceeded `max_game_minutes` and the default policy finished it |
| `<side>_agent_crashed` | The driver loop crashed |
| `<side>_model_unavailable` | Repeated or fatal API errors handed the team to the default policy |
| `transcript_incomplete` | After a server restart, the transcript was missing records from before the checkpoint |

A game that was resumed after a server restart stays admissible: it continues from the start of the interrupted
team turn with the same board, random-number state and history (see `meta.resumes`).

Inadmissible games still count on the leaderboard, because an unavailable model loses like any other. Filter them
out for research use: `/api/export/matches.jsonl?admissible=1`.
