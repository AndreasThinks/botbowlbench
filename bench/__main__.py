"""
Command line entry point.

    python -m bench serve                 # web UI + scheduler (what Railway runs)
    python -m bench round-robin           # queue a full round robin of all enabled models
    python -m bench play <home> <away>    # play one match in the terminal (no DB), e.g. to test a model
    python -m bench models                # list configured models
    python -m bench export ./dataset      # dump matches, events and transcripts for offline analysis
"""
import argparse
import json
import os
import sys
import uuid


def cmd_serve(args):
    from waitress import serve
    from bench.web import create_app
    app = create_app(start_scheduler=not args.no_scheduler)
    port = int(os.environ.get("PORT", args.port))
    print(f"BotBowl Bench listening on http://0.0.0.0:{port}", flush=True)
    serve(app, host="0.0.0.0", port=port, threads=int(os.environ.get("WEB_THREADS", 12)), ident="botbowlbench")


def cmd_round_robin(args):
    from bench.scheduler import Bench
    b = Bench()
    b.sync_models(force=True)
    print("Queued tournament", b.create_round_robin(name=args.name))


def cmd_export(args):
    from bench import config, db
    from bench.export import export_all
    db.init(os.path.join(config.data_dir(), "bench.db"))
    print(json.dumps(export_all(args.out, transcripts=not args.no_transcripts), indent=2))


def cmd_models(args):
    from bench import config
    cfg = config.load_models_file()
    for m in cfg["models"]:
        state = "enabled" if m["enabled"] else "disabled"
        print(f"{m['id']:<28} {m['provider']:<10} {m.get('model') or '':<40} {state}")


def cmd_play(args):
    from bench import config
    from bench.match import MatchRunner
    cfg = config.load_models_file()
    models = {m["id"]: m for m in cfg["models"]}
    for mid in (args.home, args.away):
        if mid not in models:
            sys.exit(f"Unknown model id '{mid}'. Run `python -m bench models`.")

    def sink(match_id, side, kind, payload):
        if kind == "tool":
            line = f"{payload['name']}({json.dumps(payload['args'])})"
            if args.verbose:
                line += " ->\n      " + payload["result"].replace("\n", "\n      ")
            elif not payload["ok"]:
                line += "  [INVALID]"
        else:
            line = payload.get("text") or json.dumps(payload)
        print(f"[{side or '-':>4}] {kind:<8} {line}", flush=True)

    runner = MatchRunner(str(uuid.uuid4()), models[args.home], models[args.away], cfg["settings"],
                         api_key=os.environ.get("OPENROUTER_API_KEY"), event_sink=sink)
    result = runner.run()
    print(json.dumps({k: v for k, v in result.items() if k != "messages"}, indent=2, default=str))


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m bench")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8080)
    s.add_argument("--no-scheduler", action="store_true", help="only serve the UI, don't play games")
    s.set_defaults(run=cmd_serve)
    r = sub.add_parser("round-robin")
    r.add_argument("--name")
    r.set_defaults(run=cmd_round_robin)
    sub.add_parser("models").set_defaults(run=cmd_models)
    ex = sub.add_parser("export", help="dump the dataset (matches, events, transcripts) as JSON lines")
    ex.add_argument("out")
    ex.add_argument("--no-transcripts", action="store_true")
    ex.set_defaults(run=cmd_export)
    pl = sub.add_parser("play")
    pl.add_argument("home")
    pl.add_argument("away")
    pl.add_argument("-v", "--verbose", action="store_true")
    pl.set_defaults(run=cmd_play)
    args = p.parse_args(argv)
    if args.cmd is None:   # no subcommand: serve
        args = p.parse_args(["serve"])
    args.run(args)


if __name__ == "__main__":
    main()
