#!/usr/bin/env python3
"""Outbox drain worker — delivers queued integration events (webhooks).

Runs the SAME event_bus.drain_outbox() the HTTP endpoint runs, but DIRECTLY in
the app context — so there is no token, no HTTP round-trip, and no public URL to
secure. Use it as a PythonAnywhere Scheduled Task (single-shot) or an Always-on
Task (--loop).

    python3 drain_worker.py            # drain once and exit  (Scheduled Task)
    python3 drain_worker.py --loop     # drain forever every INTERVAL secs (Always-on Task)
    python3 drain_worker.py --limit 200 --interval 60

Env overrides: OUTBOX_DRAIN_LIMIT, OUTBOX_DRAIN_INTERVAL.
DB connection comes from run.py's config (the prod MySQL fallbacks / MYSQL_* env),
so it needs no extra setup beyond being run from the project directory.
"""
import argparse
import os
import sys
import time

# Run from the project directory regardless of where the scheduler invokes us.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# A worker must NOT warm the RAG/AI subsystems — that's request-path work.
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")


def _drain_once(app, limit):
    with app.app_context():
        try:
            from event_bus import drain_outbox
            counts = drain_outbox(limit=limit)
            print(f"[drain_worker] drained: {counts}", flush=True)
            return counts
        except Exception as e:  # never crash the scheduler — log and move on
            print(f"[drain_worker] error: {e}", flush=True)
            return None


def main():
    ap = argparse.ArgumentParser(description="aileadz outbox drain worker")
    ap.add_argument("--loop", action="store_true",
                    help="run forever, draining every --interval seconds (Always-on Task)")
    ap.add_argument("--interval", type=int,
                    default=int(os.getenv("OUTBOX_DRAIN_INTERVAL", "60")),
                    help="seconds between drains in --loop mode (default 60)")
    ap.add_argument("--limit", type=int,
                    default=int(os.getenv("OUTBOX_DRAIN_LIMIT", "200")),
                    help="max outbox rows delivered per drain (default 200)")
    args = ap.parse_args()

    from run import create_app
    app = create_app()

    if args.loop:
        print(f"[drain_worker] loop: every {args.interval}s, limit {args.limit}", flush=True)
        while True:
            _drain_once(app, args.limit)
            time.sleep(max(5, args.interval))
    else:
        _drain_once(app, args.limit)


if __name__ == "__main__":
    main()
