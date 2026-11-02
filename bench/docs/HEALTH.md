# Condition health (Tripwire)

Play first, flag automatically, change a condition only when our own request is the cause. A new model starts its
placement under the same settings as everyone else. Every game it plays feeds a few counters, recorded per **served
host**, and fixed rules turn them into public flags. Flags describe the serving route, never playing strength, and
never change a rating, a setting, a placement or `models.yaml`.

Code: `bench/health.py` (thresholds, the per-game record and the pure aggregation), `bench/driver.py` (counting),
`bench/llm.py` (`LLMError.kind/status/host`), `bench/session.py` (fallback causes), `bench/scheduler.py` (holds).

## Thresholds

Proposed starting values, the same for every condition, defined once in `bench/health.py` `THRESHOLDS`. The About
page, `/api/health` and the scheduler read them from there.

| | fires when | response |
|---|---|---|
| **R** reasoning not returned | reasoning is requested and, on one host, at least **80%** of the condition's **first 30** measured ordinary team-turn replies are *low evidence* | public flag; the model page suggests one successor condition whose only change is `tool_choice: auto`. Nothing is applied: a maintainer must add it to `models.yaml` as a new condition (own ID, own placement). |
| **P** prose instead of tools | replies with no tool call (and not cut off) are **above 5%** of replies over the **latest 3** completed instrumented games, pooled or for one host with at least 30 of those replies | public flag only |
| **L** output cap reached | replies with `finish_reason=length` are **above 5%** over the same window and rule | public flag only; caps are never raised per model |
| **S** substituted play | provider-caused fallback in **2 of the latest 3** completed instrumented games | public flag and a scheduling **hold** (below) |
| **H** host mix | always | share of calls per host, from `served` (also available for old games) |

**Low evidence** (R): fewer than 16 reported reasoning tokens, *and* fewer than 40 non-space characters of readable
reasoning (`reasoning`, plus `reasoning_details` text and summaries), *and* no `reasoning.encrypted` block with
non-empty `data`, *and* fewer than 40 non-space characters of ordinary content. Anything that may be reasoning counts
in the model's favour; nothing judges the text's meaning. A newline-only, one-token reply is low evidence. Missing
reasoning does not prove that a model did no internal thinking.

R counts only unhurried replies in the seat's own team-turn episodes, and only where reasoning is requested
(`reasoning_enabled`, a thinking budget, or `extra.reasoning` that isn't `enabled: false` / `effort: none` /
`exclude: true`) and OpenRouter does not list the model as lacking reasoning support. Setup, coin toss, kick-off and
other decisions outside the seat's turn, reflection replies, last-chance calls and the short reply after a stopped
reply are excluded. R is a **first-window** rule per host: once a host has 30 counted replies its verdict is fixed; a
host first seen later gets its own first window ("warming up" until then). P, L and S are **rolling** windows over the
latest completed instrumented games.

## Denominators

* A **response** is one model reply, however many tool calls it contains. P and L rates are per response.
* Reflection replies (the one-tool `reflect` call after a turnover) are counted apart (`reflection.*`) and never enter
  P, L or R.
* `bad_args` counts tool calls whose arguments could not be parsed, per tool call (denominator `tool_calls`). It is
  separate from P and L.
* S counts **games**, not calls. Its evidence lists, per game, the decisions the default policy made because of the
  provider (`llm_error`: a model call failed after retries; `unavailable`: the model was marked unavailable) and the
  failed calls by host and kind. Fallback caused by the model's own illegal calls, truncations, tool or time budget,
  the game cap or a harness crash is listed but never counts for S.
* A failed call's host is recorded only when the error itself names it (`error.metadata.provider_name`, or the
  `provider` of a streamed reply that failed). Otherwise it is `unknown`; it is never guessed from an earlier reply.
  Only kind, HTTP status and a validated host name are kept: no message text, headers or keys.

## Coverage and states

Per-game records carry `v` (currently 1). Games without a record, or with another version, are **not measured**:
unknown, never healthy. A condition with no measured game is `unmeasured`. A game resumed from a checkpoint written
before the counters existed is `coverage: partial` and left out of every window. Statuses: `flagged`, `clear` (every
applicable signal measured and quiet), `warming_up` (not enough replies or games yet), `unmeasured`,
`not_applicable` (baselines). Old games are never backfilled from transcripts.

## Holds

S places a hold on an LLM condition, only after a newly finished instrumented game, never at startup and never from
old data. A hold:

* is a row in `health_holds` (created on start-up, additive), so it survives restarts;
* lets a game in progress finish, keeps every queued game's id, order, status and placement role, and keeps the
  condition enabled and listed (unlike `enabled: false`, which cancels queued games, including other newcomers'
  placement games);
* makes the scheduler skip queued games involving the condition and play the rest; if every queued game is held, the
  status is `held` with the waiting count, not `idle`;
* is released only by a maintainer: `POST /api/admin/health-holds/<model_id>/release` (admin bearer token, optional
  JSON `{"note": "..."}`). After a release only games finished later can put it back. No test calls are made to find
  out whether a provider has recovered.

`POST /api/admin/health-holds/<model_id>` places a hold by hand: the safe way to pause a condition.

Admissibility is unchanged. A game a provider failure reached is rated as before; `meta.provider_fallback`
(per side) and the match page show how many decisions the default policy made because of it.

## Where it shows

* `GET /api/health`: thresholds, signal texts, holds and every condition's status and flags.
* `GET /api/models`: every condition with its summary. `GET /api/models/<id>`: `health` with per-host detail.
* `GET /api/leaderboard`: `health` per row (status, flags, flagged hosts, hold).
* Leaderboard rows show flag chips; the model page has a "Condition health" card; the About page explains the rules
  and lists current flags live.
