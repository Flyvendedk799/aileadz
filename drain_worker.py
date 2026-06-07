#!/usr/bin/env python3
"""Background job worker — runs all DUE scheduled jobs in the app context.

Originally this only drained the integration-event outbox; it now drives the
full `scheduler` registry (outbox drain + daily insights + agreement alerts +
compliance recheck). The outbox drain is still here — it is simply registered as
the `outbox_drain` job, so the old behaviour is preserved and reliable.

Runs DIRECTLY in the app context — so there is no token, no HTTP round-trip, and
no public URL to secure. Use it as a PythonAnywhere Scheduled Task (single-shot)
or an Always-on Task (--loop).

    python3 drain_worker.py                  # run all due jobs once and exit (Scheduled Task)
    python3 drain_worker.py --loop           # run due jobs forever every INTERVAL secs (Always-on Task)
    python3 drain_worker.py --only outbox_drain   # restrict to one job (repeatable / comma list)
    python3 drain_worker.py --force          # ignore intervals; run every job now
    python3 drain_worker.py --loop --interval 60

Env overrides: OUTBOX_DRAIN_INTERVAL (loop cadence). DB connection comes from
run.py's config (the prod MySQL fallbacks / MYSQL_* env), so it needs no extra
setup beyond being run from the project directory.
"""
import argparse
import os
import sys
import time

# Run from the project directory regardless of where the scheduler invokes us.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# A worker must NOT warm the RAG/AI subsystems — that's request-path work.
os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
# The worker drives the scheduler explicitly; the opportunistic request hook is
# irrelevant in this process, so leave it off to avoid any double-driving.
os.environ.setdefault("SCHEDULER_OPPORTUNISTIC", "0")


def _run_once(app, only=None, force=False):
    """Run all due jobs once. Never raises (scheduler is fully guarded)."""
    try:
        import scheduler
        summary = scheduler.run_due_jobs(app, only=only, force=force)
        ran = ", ".join(summary.get('ran') or []) or "(none due)"
        errors = summary.get('errors') or {}
        print(f"[drain_worker] ran: {ran}", flush=True)
        if summary.get('results'):
            for name, res in summary['results'].items():
                print(f"[drain_worker]   {name}: {res}", flush=True)
        if errors:
            print(f"[drain_worker] errors: {errors}", flush=True)
        return summary
    except Exception as e:  # never crash the scheduler — log and move on
        print(f"[drain_worker] error: {e}", flush=True)
        return None


def main():
    ap = argparse.ArgumentParser(description="aileadz background job worker")
    ap.add_argument("--loop", action="store_true",
                    help="run forever, running due jobs every --interval seconds (Always-on Task)")
    ap.add_argument("--interval", type=int,
                    default=int(os.getenv("OUTBOX_DRAIN_INTERVAL", "60")),
                    help="seconds between passes in --loop mode (default 60)")
    ap.add_argument("--only", default=None,
                    help="restrict to one or more job names (comma-separated), e.g. outbox_drain")
    ap.add_argument("--force", action="store_true",
                    help="ignore each job's interval and run it now")
    args = ap.parse_args()

    only = None
    if args.only:
        only = [n.strip() for n in args.only.split(",") if n.strip()]

    from run import create_app
    app = create_app()

    if args.loop:
        print(f"[drain_worker] loop: every {args.interval}s, only={only or 'all due'}", flush=True)
        while True:
            _run_once(app, only=only, force=args.force)
            time.sleep(max(5, args.interval))
    else:
        _run_once(app, only=only, force=args.force)


if __name__ == "__main__":
    main()
