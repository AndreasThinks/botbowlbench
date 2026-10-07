"""
Web server: the benchmark site (live games, leaderboard, tournaments, model profiles) plus the
botbowl board UI (served at /board) which renders live and replayed matches.
"""
import gzip
import hmac
import json
import os

from flask import (Flask, Response, abort, jsonify, render_template, request, send_file, send_from_directory,
                   stream_with_context)

from bench import db, export, ratings
from bench.transcript import transcript_path
from bench.scheduler import frame_json, get_bench, load_frames, load_timeline

BOTBOWL_WEB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "botbowl", "web")


def create_app(start_scheduler: bool = True) -> Flask:
    app = Flask(__name__,
                static_folder=os.path.join(BOTBOWL_WEB, "static"), static_url_path="/static",
                template_folder=os.path.join(os.path.dirname(__file__), "templates"))
    bench = get_bench()
    bench.sync_models(force=True)
    if start_scheduler:
        bench.start()

    # ---- helpers -------------------------------------------------------------------------------
    def models_by_id():
        out = {}
        for m in db.rows("SELECT id, name, provider, model, enabled, added_at FROM models"):
            out[m["id"]] = m
        return out

    def match_summary(m, names):
        m = dict(m)
        for k in ("home_stats", "away_stats"):
            m.pop(k, None)
        m["home_name"] = names.get(m["home_model"], {}).get("name", m["home_model"])
        m["away_name"] = names.get(m["away_model"], {}).get("name", m["away_model"])
        return m

    def completed_matches(where="", args=()):
        ms = db.rows(f"SELECT * FROM matches WHERE status='completed' {where} ORDER BY finished_at", args)
        return [db.decode_match(m) for m in ms]

    def require_admin():
        token = os.environ.get("ADMIN_TOKEN")
        given = request.headers.get("Authorization", "").replace("Bearer ", "") or request.args.get("token", "")
        if not token or not hmac.compare_digest(token, given):
            abort(403)

    # ---- pages ---------------------------------------------------------------------------------
    @app.route("/")
    def home():
        return render_template("home.html", page="home")

    @app.route("/leaderboard")
    def leaderboard_page():
        return render_template("leaderboard.html", page="leaderboard")

    @app.route("/tournaments")
    def tournaments_page():
        return render_template("tournaments.html", page="tournaments")

    @app.route("/tournaments/<int:tid>")
    def tournament_page(tid):
        return render_template("tournament.html", page="tournaments", tid=tid)

    @app.route("/match/<match_id>")
    def match_page(match_id):
        if db.row("SELECT id FROM matches WHERE id=?", (match_id,)) is None:
            abort(404)
        return render_template("match.html", page="match", match_id=match_id)

    @app.route("/models/<model_id>")
    def model_page(model_id):
        if db.row("SELECT id FROM models WHERE id=?", (model_id,)) is None:
            abort(404)
        return render_template("model.html", page="leaderboard", model_id=model_id)

    @app.route("/about")
    def about_page():
        return render_template("about.html", page="about")

    @app.route("/matches")
    def matches_page():
        return render_template("matches.html", page="matches")

    @app.route("/data")
    def data_page():
        return render_template("data.html", page="data")

    @app.route("/board")
    def board():
        """The original botbowl Angular UI, used (embedded) to draw the pitch."""
        return send_from_directory(os.path.join(BOTBOWL_WEB, "templates"), "index.html")

    @app.route("/bench-static/<path:path>")
    def bench_static(path):
        return send_from_directory(os.path.join(os.path.dirname(__file__), "static"), path)

    @app.route("/health")
    def health():
        return jsonify({"ok": True, "status": bench.status})

    # ---- API: status / live -------------------------------------------------------------------------
    @app.route("/api/status")
    def api_status():
        names = models_by_id()
        live = [match_summary(m, names) for m in db.rows("SELECT * FROM matches WHERE status='running'")]
        for m in live:
            runner = bench.live.get(m["id"])
            if runner is not None:
                g = runner.session.game
                m["home_score"] = g.state.home_team.state.score
                m["away_score"] = g.state.away_team.state.score
                m["half"] = g.state.half
                m["turn"] = max(g.state.home_team.state.turn, g.state.away_team.state.turn)
        queued = [match_summary(m, names) for m in db.rows(
            "SELECT m.* FROM matches m JOIN tournaments t ON t.id=m.tournament_id WHERE m.status='queued' "
            "ORDER BY t.created_at, t.id, m.seq LIMIT 12")]
        recent = [match_summary(m, names) for m in db.rows(
            "SELECT * FROM matches WHERE status IN ('completed','error') ORDER BY finished_at DESC LIMIT 12")]
        counts = db.row("SELECT SUM(status='queued') AS queued, SUM(status='completed') AS completed, "
                        "SUM(status='running') AS running FROM matches")
        return jsonify({"status": bench.status, "detail": bench.status_detail, "live": live, "queued": queued,
                        "recent": recent, "counts": counts,
                        "settings": {k: bench.settings.get(k) for k in ("game_mode", "team", "legs",
                                                                        "max_tool_calls_per_turn",
                                                                        "turn_time_limit", "budget_usd_per_game")}})

    @app.route("/api/matches")
    def api_matches():
        """Archive of finished games, newest first. Filters: model, tournament, result (decisive|draw),
        admissible=1. Paginate with before=<finished_at of the last row>."""
        names = models_by_id()
        where, args = ["status IN ('completed','error')"], []
        model = request.args.get("model")
        if model:
            where.append("(home_model=? OR away_model=?)")
            args += [model, model]
        tid = request.args.get("tournament", type=int)
        if tid:
            where.append("tournament_id=?")
            args.append(tid)
        result = request.args.get("result")
        if result == "draw":
            where.append("winner='draw'")
        elif result == "decisive":
            where.append("winner IN ('home','away')")
        if request.args.get("admissible") in ("1", "true"):
            where.append("admissible=1")
        before = request.args.get("before", type=float)
        if before:
            where.append("finished_at < ?")
            args.append(before)
        limit = max(1, min(100, request.args.get("limit", 30, type=int)))
        rows = db.rows(f"SELECT * FROM matches WHERE {' AND '.join(where)} ORDER BY finished_at DESC LIMIT ?",
                       tuple(args) + (limit + 1,))
        more = len(rows) > limit
        rows = rows[:limit]
        tours = {t["id"]: t["name"] for t in db.rows("SELECT id, name FROM tournaments")}
        out = []
        for m in rows:
            meta = json.loads(m["meta"]) if m.get("meta") else {}
            sm = match_summary(m, names)
            sm.pop("meta", None)
            sm["tournament_name"] = tours.get(m["tournament_id"])
            sm["admissible"] = bool(m.get("admissible")) if m.get("admissible") is not None else None
            sm["time_capped"] = bool(meta.get("time_capped"))
            out.append(sm)
        return jsonify({"matches": out, "next_before": rows[-1]["finished_at"] if more and rows else None})

    @app.route("/api/matches/<match_id>")
    def api_match(match_id):
        m = db.decode_match(db.row("SELECT * FROM matches WHERE id=?", (match_id,)))
        if m is None:
            abort(404)
        names = models_by_id()
        out = dict(m)
        out.update(match_summary(m, names))
        out["home_stats"], out["away_stats"] = m.get("home_stats"), m.get("away_stats")
        out["meta"] = m.get("meta")
        out["has_transcript"] = os.path.exists(transcript_path(match_id))
        t = db.row("SELECT id, name, kind FROM tournaments WHERE id=?", (m["tournament_id"],))
        out["tournament"] = t
        runner = bench.live.get(match_id)
        if runner is not None:
            g = runner.session.game
            out["home_score"] = g.state.home_team.state.score
            out["away_score"] = g.state.away_team.state.score
            out["half"] = g.state.half
            out["turn"] = max(g.state.home_team.state.turn, g.state.away_team.state.turn)
            out["live_usage"] = {side: {k: round(v, 4) if isinstance(v, float) else v
                                        for k, v in d.usage.items()} for side, d in runner.drivers.items()}
        return jsonify(out)

    _gz_cache = {}

    @app.route("/api/matches/<match_id>/state")
    def api_match_state(match_id):
        """Polled by every spectator ~1.4x/s: answers 304 when nothing changed and gzips the body."""
        tag, js = bench.live_state(match_id)
        if js is None:
            abort(404)
        etag = f'"{tag}"'
        headers = {"ETag": etag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
        if etag in [t.strip() for t in request.headers.get("If-None-Match", "").split(",")]:
            return Response(status=304, headers=headers)
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            body = _gz_cache.get(tag)
            if body is None:
                body = gzip.compress(js.encode("utf-8"), 5)
                _gz_cache.clear()
                _gz_cache[tag] = body
            headers["Content-Encoding"] = "gzip"
            return Response(body, mimetype="application/json", headers=headers)
        return Response(js, mimetype="application/json", headers=headers)

    @app.route("/api/matches/<match_id>/events")
    def api_match_events(match_id):
        after = request.args.get("after", 0, type=int)
        return jsonify(db.get_events(match_id, after=after, limit=1000))

    # ---- replays ----------------------------------------------------------------------------------------
    @app.route("/api/matches/<match_id>/timeline")
    def api_match_timeline(match_id):
        """One point per recorded frame (time, half, turns, score, side to move) for the replay scrubber."""
        m = db.row("SELECT status, started_at, finished_at FROM matches WHERE id=?", (match_id,))
        if m is None:
            abort(404)
        runner = bench.live.get(match_id)
        if runner is not None:
            with runner.session.lock:
                points = [dict(p) for p in runner.session.timeline]
        else:
            points = [dict(p) for p in load_timeline(match_id)]
        # frames recorded before timestamps existed: spread them evenly over the game's duration
        if points and any(p["ts"] is None for p in points) and m["started_at"] and m["finished_at"]:
            span = (m["finished_at"] - m["started_at"]) / max(1, len(points) - 1)
            for i, p in enumerate(points):
                if p["ts"] is None:
                    p["ts"] = round(m["started_at"] + i * span, 3)
        for i, p in enumerate(points):
            p["i"] = i
        return jsonify({"live": runner is not None, "frames": len(points), "points": points})

    @app.route("/api/matches/<match_id>/frames/<int:idx>")
    def api_match_frame(match_id, idx):
        """Board state at one decision. Frames are stored zlib-compressed, which is exactly HTTP 'deflate',
        and never change once written, so they're served as-is and cached forever."""
        runner = bench.live.get(match_id)
        frames = runner.session.frames if runner is not None else load_frames(match_id)
        if not 0 <= idx < len(frames):
            abort(404)
        blob = frames[idx]
        headers = {"Cache-Control": "public, max-age=31536000, immutable", "Vary": "Accept-Encoding"}
        if "deflate" in request.headers.get("Accept-Encoding", ""):
            headers["Content-Encoding"] = "deflate"
            return Response(blob, mimetype="application/json", headers=headers)
        return Response(frame_json(blob), mimetype="application/json", headers=headers)

    # replays, in the format the original botbowl Angular replay viewer expects (#/game/replay/<match_id>/)
    @app.route("/replays/<match_id>")
    def api_replay(match_id):
        frames = load_frames(match_id)
        if not frames:
            abort(404)
        steps = {i: json.loads(frame_json(f)) for i, f in enumerate(frames[:100])}
        return jsonify({"replay_id": match_id, "steps": steps, "actions": {}})

    @app.route("/steps/<match_id>/<int:from_idx>/<int:num_steps>")
    def api_replay_steps(match_id, from_idx, num_steps):
        frames = load_frames(match_id)
        num_steps = min(num_steps, 50)
        return jsonify({i: json.loads(frame_json(frames[i])) for i in range(from_idx, min(len(frames), from_idx + num_steps))})

    # ---- API: rankings ------------------------------------------------------------------------------
    _ci_cache = {}

    @app.route("/api/leaderboard")
    def api_leaderboard():
        names = models_by_id()
        ms = completed_matches()
        elo = ratings.compute_elo(ms)
        agg = ratings.aggregate(ms)
        ci_key = (len(ms), ms[-1]["finished_at"] if ms else None)
        if _ci_cache.get("key") != ci_key:
            _ci_cache.update(key=ci_key, ci=ratings.bootstrap_elo(ms))
        ci = _ci_cache["ci"]
        rows = []
        for mid, info in names.items():
            a = agg.get(mid)
            if a is None and not info["enabled"]:
                continue
            rows.append({"id": mid, "name": info["name"], "provider": info["provider"], "model": info["model"],
                         "added_at": info["added_at"],
                         "enabled": bool(info["enabled"]),
                         "elo": elo.get(mid, {}).get("elo", ratings.START_ELO),
                         "elo_ci": ci.get(mid),
                         "history": elo.get(mid, {}).get("history", []),
                         **(a or {"played": 0, "wins": 0, "draws": 0, "losses": 0, "td_for": 0, "td_against": 0, "td_diff": 0,
                                  "style": {}, "sums": {}, "avg_cost": 0, "avg_tokens": 0, "win_rate": 0,
                                  "avg_latency": 0, "cas_per_game": 0})})
        rows.sort(key=lambda r: (-r["elo"], -r["played"], r["name"]))
        return jsonify({"models": rows, "matches": len(ms)})

    @app.route("/api/models/<model_id>")
    def api_model(model_id):
        names = models_by_id()
        info = names.get(model_id)
        if info is None:
            abort(404)
        ms = completed_matches("AND (home_model=? OR away_model=?)", (model_id, model_id))
        agg = ratings.aggregate(ms).get(model_id)
        elo = ratings.compute_elo(completed_matches()).get(model_id, {"elo": ratings.START_ELO, "history": []})
        # head to head
        h2h = {}
        for m in ms:
            side = "home" if m["home_model"] == model_id else "away"
            opp = m["away_model"] if side == "home" else m["home_model"]
            r = h2h.setdefault(opp, {"opponent": opp, "name": names.get(opp, {}).get("name", opp),
                                     "wins": 0, "draws": 0, "losses": 0})
            r["wins" if m["winner"] == side else ("draws" if m["winner"] == "draw" else "losses")] += 1
        recent = [match_summary(m, names) for m in db.rows(
            "SELECT * FROM matches WHERE (home_model=? OR away_model=?) AND status IN ('completed','running','error') "
            "ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 30", (model_id, model_id))]
        # a sample of the model's chat
        msgs = []
        for e in db.rows("SELECT e.payload, e.side, m.home_model, m.away_model, m.id AS match_id FROM events e "
                         "JOIN matches m ON m.id=e.match_id WHERE e.kind='message' AND "
                         "((e.side='home' AND m.home_model=?) OR (e.side='away' AND m.away_model=?)) "
                         "ORDER BY e.id DESC LIMIT 25", (model_id, model_id)):
            opp = e["away_model"] if e["side"] == "home" else e["home_model"]
            msgs.append({"text": json.loads(e["payload"]).get("text"), "match_id": e["match_id"],
                         "opponent": names.get(opp, {}).get("name", opp)})
        refl = []
        for e in db.rows("SELECT e.payload, e.side, m.home_model, m.away_model, m.id AS match_id FROM events e "
                         "JOIN matches m ON m.id=e.match_id WHERE e.kind='reflection' AND "
                         "((e.side='home' AND m.home_model=?) OR (e.side='away' AND m.away_model=?)) "
                         "ORDER BY e.id DESC LIMIT 12", (model_id, model_id)):
            opp = e["away_model"] if e["side"] == "home" else e["home_model"]
            p = json.loads(e["payload"])
            refl.append({**p, "match_id": e["match_id"], "opponent": names.get(opp, {}).get("name", opp)})
        all_ms = completed_matches()
        if _ci_cache.get("key") != (len(all_ms), all_ms[-1]["finished_at"] if all_ms else None):
            _ci_cache.update(key=(len(all_ms), all_ms[-1]["finished_at"] if all_ms else None),
                             ci=ratings.bootstrap_elo(all_ms))
        return jsonify({"model": info, "elo": elo, "elo_ci": _ci_cache["ci"].get(model_id), "stats": agg,
                        "reflections": refl, "head_to_head": sorted(h2h.values(), key=lambda r: r["name"]),
                        "recent": recent, "messages": msgs})

    @app.route("/api/tournaments")
    def api_tournaments():
        names = models_by_id()
        out = []
        for t in db.rows("SELECT * FROM tournaments ORDER BY created_at DESC, id DESC"):
            c = db.row("SELECT COUNT(*) AS total, SUM(status='completed') AS done FROM matches WHERE tournament_id=?",
                       (t["id"],))
            table = ratings.standings(completed_matches("AND tournament_id=?", (t["id"],)))
            leader = table[0]["model"] if table else None
            t.pop("settings", None)
            out.append({**t, "total": c["total"], "done": c["done"] or 0,
                        "leader": names.get(leader, {}).get("name") if leader else None,
                        "focus_name": names.get(t["focus_model"], {}).get("name") if t["focus_model"] else None})
        return jsonify(out)

    @app.route("/api/tournaments/<int:tid>")
    def api_tournament(tid):
        t = db.row("SELECT * FROM tournaments WHERE id=?", (tid,))
        if t is None:
            abort(404)
        names = models_by_id()
        t["settings"] = json.loads(t["settings"] or "{}")
        table = ratings.standings(completed_matches("AND tournament_id=?", (tid,)))
        for r in table:
            r["name"] = names.get(r["model"], {}).get("name", r["model"])
        matches = [match_summary(m, names) for m in db.rows(
            "SELECT * FROM matches WHERE tournament_id=? ORDER BY seq", (tid,))]
        return jsonify({"tournament": t, "standings": table, "matches": matches})

    # ---- API: dataset export --------------------------------------------------------------------------
    @app.route("/api/matches/<match_id>/transcript")
    def api_match_transcript(match_id):
        path = transcript_path(match_id)
        if not os.path.exists(path):
            abort(404)
        return send_file(path, mimetype="application/gzip", as_attachment=True,
                         download_name=f"botbowlbench-{match_id}.jsonl.gz")

    @app.route("/api/export/matches.jsonl")
    def api_export_matches():
        only = request.args.get("admissible")
        rows = export.iter_matches()
        if only in ("1", "true"):
            rows = (m for m in rows if m["admissible"])
        return Response(stream_with_context(export.jsonl(rows)), mimetype="application/x-ndjson",
                        headers={"Content-Disposition": "attachment; filename=matches.jsonl"})

    @app.route("/api/export/events.jsonl")
    def api_export_events():
        kinds = [k for k in request.args.get("kind", "").split(",") if k] or None
        return Response(stream_with_context(export.jsonl(export.iter_events(kinds))),
                        mimetype="application/x-ndjson",
                        headers={"Content-Disposition": "attachment; filename=events.jsonl"})

    # ---- API: admin ---------------------------------------------------------------------------------
    @app.route("/api/admin/round-robin", methods=["POST"])
    def api_admin_round_robin():
        require_admin()
        try:
            tid = bench.create_round_robin(name=request.args.get("name"))
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify({"tournament_id": tid})

    @app.route("/api/admin/reload", methods=["POST"])
    def api_admin_reload():
        require_admin()
        added = bench.sync_models(force=True)
        bench.wake()
        return jsonify({"added": added})

    @app.route("/api/admin/requeue/<match_id>", methods=["POST"])
    def api_admin_requeue(match_id):
        require_admin()
        m = db.row("SELECT status FROM matches WHERE id=?", (match_id,))
        if m is None or m["status"] not in ("error", "cancelled"):
            return jsonify({"error": "only errored or cancelled matches can be requeued"}), 400
        db.execute("DELETE FROM events WHERE match_id=?", (match_id,))
        db.execute("UPDATE matches SET status='queued', started_at=NULL, finished_at=NULL, error=NULL "
                   "WHERE id=?", (match_id,))
        db.execute("UPDATE tournaments SET status='queued', finished_at=NULL WHERE id=(SELECT tournament_id FROM "
                   "matches WHERE id=?) AND status='completed'", (match_id,))
        bench.wake()
        return jsonify({"ok": True})

    @app.after_request
    def no_cache_api(resp):
        if request.path.startswith("/api/") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-store"
        return resp

    return app
