# Deploying on Railway

The bench is a single process: the web server and the tournament scheduler run together. It needs one persistent
volume for its SQLite database, replays and transcripts.

## First deployment

1. **Create the service.** New Project → Deploy from GitHub repo → this repository. Railway detects the
   `Dockerfile`; `railway.json` sets the health check (`/health`), the restart policy and **1 replica**.
2. **Attach a volume** to the service, mounted at **`/data`**. The image sets `DATA_DIR=/data`. Without a volume,
   every redeploy wipes the history.
3. **Set variables** (Service → Variables):

   | variable | required | purpose |
   |---|---|---|
   | `OPENROUTER_API_KEY` | yes, for LLM models | OpenRouter key with credit |
   | `ADMIN_TOKEN` | recommended | Enables the admin API (below) |
   | `PUBLIC_URL` | optional | Your public URL, sent to OpenRouter as `HTTP-Referer` (shows up in their app rankings) |
   | `BENCH_PAUSED` | optional | `1` = serve the site but don't start new games |
   | `MODELS_CONFIG` | optional | Use a models file on the volume (e.g. `/data/models.yaml`) instead of the one in the repo |
   | `WEB_THREADS` | optional | Web server threads (default 12) |

   `PORT` is provided by Railway. `RAILWAY_GIT_COMMIT_SHA` is recorded automatically as the harness version of
   every game.
4. **Networking → Generate Domain.** Open it: the Live page shows the opening round robin starting.

Before spending money on a full round robin, consider deploying with `BENCH_PAUSED=1`. Check the site, run one
supervised game (`python -m bench play <model> scripted-baseline -v` locally), then unpause.

## Day-to-day

* **Add or retire a model:** edit `models.yaml` and push. Railway redeploys; on start the bench sees the new id and
  queues its gauntlet. With `MODELS_CONFIG` on the volume, edits are picked up within ~20 seconds without a redeploy.
* **Redeploys pause the current game.** The game is saved at the start of every team turn; on start the new
  container continues it from the start of the interrupted turn, with the board, dice, score, chat and spend as they
  were. Only that partial turn is played again, and its spend is reported as `restart_cost`. Games resume across
  code changes unless `PROTOCOL_VERSION` or the prompts changed, in which case the game restarts from kick-off with
  the same seed. Railway stops the old container before starting the new one when a volume is attached, so two
  schedulers never run at once.
* **Pause:** set `BENCH_PAUSED=1`. The current game finishes; no new one starts.
* **Costs:** the leaderboard shows cost per game per model. `budget_usd_per_game` caps spend per model per game.
  If the key runs out of credit (HTTP 402), games are re-queued and the status line says so. Top up and they resume.

## Admin API

All admin calls need `Authorization: Bearer $ADMIN_TOKEN`; without `ADMIN_TOKEN` set, they are disabled.

```bash
H="Authorization: Bearer $ADMIN_TOKEN"
curl -X POST -H "$H" https://<host>/api/admin/round-robin            # queue a fresh full round robin
curl -X POST -H "$H" https://<host>/api/admin/reload                 # re-read models.yaml right now
curl -X POST -H "$H" https://<host>/api/admin/requeue/<match_id>     # retry a game that errored or was cancelled
```

## Backups and data

Everything lives on the volume (see [DATA.md](DATA.md)). Download data at any time over HTTP
(`/api/export/matches.jsonl`, per-game transcripts), or from a shell on the service (`railway ssh`):

```bash
python -m bench export /data/export-$(date +%F)
```

## Running elsewhere

Any Docker host works:

```bash
docker build -t botbowlbench .
docker run -p 8080:8080 -v botbowl-data:/data -e OPENROUTER_API_KEY=sk-or-... botbowlbench
```

## Troubleshooting

| symptom | cause |
|---|---|
| Status "waiting: OPENROUTER_API_KEY is not set" | Set the variable. Baseline-only games still run without it. |
| Status "OpenRouter problem, retrying later: HTTP 401/402" | Bad key or no credit. The game is re-queued and retried every 5 minutes, resuming from the turn it stopped in. |
| A model loses every game with many "Auto-finishing" notes | It doesn't call tools reliably, or its budget/time is too tight. Check its match feed. Slow reasoning models may need a higher `turn_time_limit`. |
| "Model unavailable - default actions for the rest of the game" | Repeated API errors (e.g. the model doesn't support tool calling, or a 404 model id). The game counts but is marked inadmissible. |
| Empty history after a redeploy | No volume attached at `/data` |
