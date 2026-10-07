"""
Command line entry point.

    python -m bench serve                 # web UI + scheduler (what Railway runs)
    python -m bench round-robin           # queue a full round robin of all enabled models
    python -m bench play <home> <away>    # play one match in the terminal (no DB), e.g. to test a model
    python -m bench models                # list configured models
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


def cmd_models(args):
    from bench import config
    cfg = config.load_models_file()
    for m in cfg["models"]:
        print(f"{m['id']:<28} {m['provider']:<10} {m.get('model') or '':<40} {'enabled' if m['enabled'] else 'disabled'}")


def cmd_play(args):
    from bench import config, render
    from bench.match import MatchRunner
    cfg = config.load_models_file()
    models = {m["id"]: m for m in cfg["models"]}
    for mid in (args.home, args.away):
        if mid not in models:
            sys.exit(f"Unknown model id '{mid}'. Run `python -m bench models`.")

    def sink(match_id, side, kind, payload):
        if kind == "tool" and not args.verbose:
            line = f"{payload['name']}({json.dumps(payload['args'])})" + ("" if payload["ok"] else "  [INVALID]")
        elif kind in ("tool",):
            line = f"{payload['name']}({json.dumps(payload['args'])}) ->\n      " + payload["result"].replace("\n", "\n      ")
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
    r = sub.add_parser("round-robin")
    r.add_argument("--name")
    sub.add_parser("models")
    pl = sub.add_parser("play")
    pl.add_argument("home")
    pl.add_argument("away")
    pl.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    {"serve": cmd_serve, "round-robin": cmd_round_robin, "models": cmd_models, "play": cmd_play,
     None: cmd_serve}[args.cmd](args if args.cmd else p.parse_args(["serve"]))


if __name__ == "__main__":
    main()
