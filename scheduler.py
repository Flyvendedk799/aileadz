# scheduler.py
"""Lightweight scheduled-job runner (the async-tier keystone).

Problem this solves (roadmap value-5 follow-on)
-----------------------------------------------
The platform has a growing number of capabilities that only ever fire when a
human happens to open a page: integration-event delivery, daily company
insights, supplier-agreement expiry alerts, compliance re-checks. On
PythonAnywhere there is NO Celery / Redis / cron we can lean on, so these jobs
quietly never run on a cadence.

This module is a tiny, dependency-free scheduler that decides *which jobs are
due* and runs them, driven by any of three mechanisms (none of which require a
real job queue):

  1. a PythonAnywhere **Scheduled Task** that runs a single pass and exits
     (``python3 drain_worker.py``),
  2. the existing always-on-style **worker loop** (``drain_worker.py --loop``),
  3. an **opportunistic request hook** in ``run.py`` that runs at most once per
     ~60s per worker, mirroring ``event_bus.opportunistic_drain``.

Design constraints (matching the rest of the codebase)
------------------------------------------------------
  * ``current_app.mysql.connection`` for DB access; ``autocommit=False`` so we
    ``commit()`` explicitly. Reads scoped by ``company_id`` (no FKs here).
  * Everything is GUARDED: ``run_due_jobs`` and the table helper NEVER raise into
    their caller (request path / boot). The worst case is "a job stays not-yet-
    run and is retried on the next pass", never "the request/boot fails".
  * Schema is idempotent ``CREATE TABLE IF NOT EXISTS``. This module keeps its
    OWN tiny ``scheduled_job_runs`` table self-contained here (deliberately NOT
    in enterprise_tables.py) so it can self-heal and not collide with that file.
  * Jobs reuse existing, already-built functions — this module orchestrates the
    cadence; it does not reimplement the work.
"""

import logging
import time

logger = logging.getLogger(__name__)

# ── Bookkeeping table ────────────────────────────────────────────────────────
# One row per registered job. ``last_run_at`` drives both the "is this due?"
# check and the best-effort claim that stops two workers double-running a job in
# the same window. Kept self-contained here (not in enterprise_tables.py).
SCHEDULED_JOB_RUNS_DDL = """CREATE TABLE IF NOT EXISTS scheduled_job_runs (
    job_name VARCHAR(120) PRIMARY KEY,
    last_run_at DATETIME NULL,
    last_status VARCHAR(20) NULL,
    last_summary TEXT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

# A single opportunistic/worker pass enumerates at most this many companies for
# the per-company daily jobs, so one pass stays bounded even on a large tenant
# base. The remaining companies are picked up on the next pass.
DEFAULT_COMPANY_BATCH = 200


# ── Connection helper (guarded; mirrors event_bus._get_conn) ─────────────────

def _get_conn():
    """Return the request-scoped MySQL connection, or None if unavailable.

    Guarded: importing flask / touching current_app must never raise out of the
    helpers in this module.
    """
    try:
        from flask import current_app
        return current_app.mysql.connection
    except Exception:
        return None


def _ensure_table(app=None, conn=None):
    """Lazily ensure the ``scheduled_job_runs`` table exists (idempotent).

    Self-contained self-heal so the scheduler works even on a fresh DB before
    any other bootstrap runs. Returns True on success, False otherwise. NEVER
    raises.
    """
    try:
        if conn is None:
            if app is not None:
                try:
                    with app.app_context():
                        return _ensure_table(conn=app.mysql.connection)
                except Exception as e:
                    logger.warning("scheduler: _ensure_table app-context failed: %s", e)
                    return False
            conn = _get_conn()
        if conn is None:
            return False
        cur = conn.cursor()
        try:
            cur.execute(SCHEDULED_JOB_RUNS_DDL)
            conn.commit()
        finally:
            try:
                cur.close()
            except Exception:
                pass
        return True
    except Exception as e:
        logger.warning("scheduler: _ensure_table failed: %s", e)
        return False


# ── Active-company enumeration ───────────────────────────────────────────────

def active_company_ids(limit=DEFAULT_COMPANY_BATCH):
    """Return active company ids (best-effort, bounded). Never raises.

    "Active" is ``companies.status = 'active'`` — the same signal branding_service
    and the dashboards use. If the status column/table is unavailable we fall
    back to ALL companies rather than silently skipping every tenant. Returns a
    list of ints (possibly empty).
    """
    conn = _get_conn()
    if conn is None:
        return []
    try:
        cap = int(limit)
    except Exception:
        cap = DEFAULT_COMPANY_BATCH
    if cap <= 0:
        cap = DEFAULT_COMPANY_BATCH

    def _run(sql):
        cur = conn.cursor()
        try:
            cur.execute(sql, (cap,))
            rows = cur.fetchall() or []
        finally:
            try:
                cur.close()
            except Exception:
                pass
        ids = []
        for r in rows:
            cid = r.get('id') if isinstance(r, dict) else r[0]
            if cid is not None:
                ids.append(int(cid))
        return ids

    try:
        return _run(
            "SELECT id FROM companies WHERE status = 'active' ORDER BY id ASC LIMIT %s"
        )
    except Exception as e:
        # status column may not exist on an older schema — fall back to all.
        logger.info("scheduler: active filter unavailable, using all companies (%s)", e)
        try:
            return _run("SELECT id FROM companies ORDER BY id ASC LIMIT %s")
        except Exception as e2:
            logger.warning("scheduler: company enumeration failed: %s", e2)
            return []


# ── Job implementations (orchestrate existing functions; do not reimplement) ──

def _job_outbox_drain(app):
    """Deliver pending integration events via the existing outbox drainer."""
    try:
        from event_bus import drain_outbox
    except Exception as e:
        return {'error': "event_bus import failed: %s" % e}
    with app.app_context():
        counts = drain_outbox()
    return counts if isinstance(counts, dict) else {'result': counts}


def _job_daily_company_insights(app):
    """Generate AI conversation insights for each active company."""
    try:
        from insights_engine import generate_company_insights
    except Exception as e:
        return {'error': "insights_engine import failed: %s" % e}
    companies = 0
    insights_total = 0
    errors = 0
    with app.app_context():
        ids = active_company_ids()
    for cid in ids:
        companies += 1
        try:
            # generate_company_insights manages its own app_context.
            result = generate_company_insights(app, cid)
            if isinstance(result, (list, tuple)):
                insights_total += len(result)
            elif isinstance(result, int):
                insights_total += result
        except Exception as e:
            errors += 1
            logger.warning("scheduler: insights failed for company %s: %s", cid, e)
    return {'companies': companies, 'insights': insights_total, 'errors': errors}


def _job_daily_agreement_alerts(app):
    """Enqueue HR nudges for supplier agreements that are expiring/expired."""
    try:
        from catalog_freshness import notify_expiring_agreements
    except Exception as e:
        return {'error': "catalog_freshness import failed: %s" % e}
    companies = 0
    notified = 0
    errors = 0
    with app.app_context():
        ids = active_company_ids()
        for cid in ids:
            companies += 1
            try:
                notified += int(notify_expiring_agreements(cid) or 0)
            except Exception as e:
                errors += 1
                logger.warning("scheduler: agreement alerts failed for company %s: %s", cid, e)
    return {'companies': companies, 'notifications': notified, 'errors': errors}


def _job_weekly_manager_digest(app):
    """Send the per-manager weekly digest for each active company.

    Iterates active companies and delegates to digest_service.send_company_digest
    (which builds self-contained, company-scoped metrics and emails each
    manager-level recipient best-effort). Fully guarded: one company's failure
    never blocks the rest, and the job never raises into the scheduler.
    """
    try:
        from digest_service import send_company_digest
    except Exception as e:
        return {'error': "digest_service import failed: %s" % e}
    companies = 0
    emails_sent = 0
    skipped = 0
    errors = 0
    with app.app_context():
        ids = active_company_ids()
        for cid in ids:
            companies += 1
            try:
                summary = send_company_digest(cid) or {}
                emails_sent += int(summary.get('sent') or 0)
                if summary.get('skipped'):
                    skipped += 1
            except Exception as e:
                errors += 1
                logger.warning("scheduler: weekly digest failed for company %s: %s", cid, e)
    return {'companies': companies, 'emails_sent': emails_sent,
            'skipped': skipped, 'errors': errors}


def _job_compliance_recheck(app):
    """Re-derive compliance state on a cadence (thin guarded pass for now).

    EXTENSION POINT: today this is a no-op safety pass — it enumerates active
    companies so the cadence/bookkeeping is wired up, but does not yet mutate or
    notify. When the compliance re-derivation is ready (e.g. re-evaluating
    mandatory-training/retention windows per company), call it here per
    ``cid`` inside the loop. Keep it guarded and company-scoped.
    """
    with app.app_context():
        ids = active_company_ids()
    checked = 0
    for cid in ids:
        try:
            # --- extension point: re-derive compliance for `cid` here ---
            # e.g.  from compliance_engine import recheck_company; recheck_company(app, cid)
            checked += 1
        except Exception as e:
            logger.warning("scheduler: compliance recheck failed for company %s: %s", cid, e)
    return {'companies': checked, 'noop': True}


# ── Job registry ─────────────────────────────────────────────────────────────
# Each job: name, interval_seconds, fn(app)->summary(dict), enabled.
# Ordered so the cheap, frequent outbox drain runs first.
JOBS = [
    {
        'name': 'outbox_drain',
        'interval_seconds': 120,          # ~2 min: near-real-time webhook delivery
        'fn': _job_outbox_drain,
        'enabled': True,
    },
    {
        'name': 'daily_company_insights',
        'interval_seconds': 86400,        # daily
        'fn': _job_daily_company_insights,
        'enabled': True,
    },
    {
        'name': 'daily_agreement_alerts',
        'interval_seconds': 86400,        # daily
        'fn': _job_daily_agreement_alerts,
        'enabled': True,
    },
    {
        'name': 'compliance_recheck',
        'interval_seconds': 86400,        # daily
        'fn': _job_compliance_recheck,
        'enabled': True,
    },
    {
        'name': 'weekly_manager_digest',
        'interval_seconds': 604800,       # weekly (7 days)
        'fn': _job_weekly_manager_digest,
        'enabled': True,
    },
]


def _jobs_by_name():
    return {j['name']: j for j in JOBS}


def list_jobs():
    """Public, read-only view of the registry (name -> interval/enabled)."""
    return [
        {'name': j['name'], 'interval_seconds': j['interval_seconds'], 'enabled': j['enabled']}
        for j in JOBS
    ]


# ── Due-logic (pure; testable without a DB) ──────────────────────────────────

def is_due(last_run_epoch, interval_seconds, now_epoch=None, force=False):
    """Return True if a job whose last run was at ``last_run_epoch`` (a unix
    timestamp, or None if it has never run) is due to run again.

    Pure function: no DB, no app context — this is the core that the unit tests
    pin down. ``force`` always returns True. A job that has never run (None) is
    always due.
    """
    if force:
        return True
    if last_run_epoch is None:
        return True
    if now_epoch is None:
        now_epoch = time.time()
    try:
        interval = float(interval_seconds)
    except Exception:
        interval = 0.0
    if interval <= 0:
        return True
    return (now_epoch - last_run_epoch) >= interval


# ── DB-backed due check + best-effort claim ──────────────────────────────────

def _last_run_epoch(conn, job_name):
    """Unix timestamp of the job's last_run_at, or None if never run / unknown."""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT UNIX_TIMESTAMP(last_run_at) AS ts FROM scheduled_job_runs WHERE job_name = %s",
            (job_name,),
        )
        row = cur.fetchone()
    finally:
        try:
            cur.close()
        except Exception:
            pass
    if not row:
        return None
    ts = row.get('ts') if isinstance(row, dict) else row[0]
    if ts is None:
        return None
    try:
        return float(ts)
    except Exception:
        return None


def _claim(conn, job_name, interval_seconds, force=False):
    """Best-effort claim of a run so two workers don't double-run a job.

    Strategy (no row locking needed): atomically set ``last_run_at = NOW()`` only
    if the existing ``last_run_at`` is older than the interval (or NULL / row
    missing). The affected-row count tells us whether *we* won the claim. This is
    a best-effort lock, not a hard mutex — good enough for the "don't run the
    same daily job twice in one window" goal.

    Returns True if this caller claimed the run, False if someone else already
    has it (or the claim could not be made). ``force`` bypasses the interval but
    still requires winning the row update.
    """
    try:
        interval = int(interval_seconds)
    except Exception:
        interval = 0
    cur = conn.cursor()
    try:
        # Ensure a row exists so the conditional UPDATE has something to bite on.
        cur.execute(
            "INSERT IGNORE INTO scheduled_job_runs (job_name, last_run_at) VALUES (%s, NULL)",
            (job_name,),
        )
        if force or interval <= 0:
            cur.execute(
                "UPDATE scheduled_job_runs SET last_run_at = NOW() WHERE job_name = %s",
                (job_name,),
            )
        else:
            cur.execute(
                """UPDATE scheduled_job_runs
                   SET last_run_at = NOW()
                   WHERE job_name = %s
                     AND (last_run_at IS NULL
                          OR last_run_at < DATE_SUB(NOW(), INTERVAL %s SECOND))""",
                (job_name, interval),
            )
        claimed = cur.rowcount > 0
        conn.commit()
        return claimed
    except Exception as e:
        logger.warning("scheduler: claim failed for %s: %s", job_name, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _stamp(conn, job_name, status, summary):
    """Record the outcome of a run (status + truncated summary). Never raises."""
    try:
        import json
        try:
            text = json.dumps(summary, default=str)[:4000]
        except Exception:
            text = str(summary)[:4000]
        cur = conn.cursor()
        try:
            # last_run_at was set by _claim; here we only record the result.
            cur.execute(
                """UPDATE scheduled_job_runs
                   SET last_status = %s, last_summary = %s
                   WHERE job_name = %s""",
                (str(status)[:20], text, job_name),
            )
            conn.commit()
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except Exception as e:
        logger.warning("scheduler: stamp failed for %s: %s", job_name, e)
        try:
            conn.rollback()
        except Exception:
            pass


# ── Public entry point ───────────────────────────────────────────────────────

def run_due_jobs(app, only=None, force=False):
    """Run all due (or named) jobs once and return a per-job summary. NEVER raises.

    Args:
        app: the Flask app (jobs open their own app_context as needed).
        only: optional job name (str) or iterable of names to restrict to.
        force: run regardless of interval (still claims the row to avoid two
               concurrent workers running the same job).

    Returns a dict::

        {
          'ran':     ['outbox_drain', ...],
          'skipped': ['daily_company_insights', ...],   # not due / disabled / not claimed
          'errors':  {'job_name': 'message', ...},
          'results': {'outbox_drain': {...summary...}, ...},
        }

    Each job is wrapped in try/except; one failing job never blocks the others,
    and the whole call never raises into its caller (request hook / worker).
    """
    out = {'ran': [], 'skipped': [], 'errors': {}, 'results': {}}

    if app is None:
        return out

    # Normalise the `only` filter.
    only_names = None
    if only is not None:
        if isinstance(only, str):
            only_names = {only}
        else:
            try:
                only_names = set(only)
            except Exception:
                only_names = None

    # Ensure bookkeeping table exists (guarded; if this fails we still attempt
    # the jobs — a missing table just means no claim/stamp, never a crash).
    try:
        with app.app_context():
            conn = app.mysql.connection
            _ensure_table(conn=conn)
    except Exception as e:
        logger.warning("scheduler: could not ensure table / get connection: %s", e)
        conn = None

    registry = _jobs_by_name()
    names = [j['name'] for j in JOBS]

    for name in names:
        job = registry[name]
        if only_names is not None and name not in only_names:
            continue
        if not job.get('enabled', True):
            out['skipped'].append(name)
            continue

        interval = job.get('interval_seconds', 0)

        # Decide + claim inside the app context so the DB calls have a connection.
        try:
            with app.app_context():
                live_conn = app.mysql.connection
                # Pure due-check first (cheap), then atomic claim (authoritative).
                last = None
                try:
                    last = _last_run_epoch(live_conn, name)
                except Exception:
                    last = None
                if not force and not is_due(last, interval):
                    out['skipped'].append(name)
                    continue
                claimed = _claim(live_conn, name, interval, force=force)
            if not claimed:
                # Another worker already claimed this window, or claim failed.
                out['skipped'].append(name)
                continue
        except Exception as e:
            # If we can't even decide, skip safely rather than risk a crash.
            logger.warning("scheduler: due/claim phase failed for %s: %s", name, e)
            out['skipped'].append(name)
            continue

        # Run the job body (its own app_context inside fn). Fully guarded.
        try:
            summary = job['fn'](app)
            if not isinstance(summary, dict):
                summary = {'result': summary}
            out['ran'].append(name)
            out['results'][name] = summary
            try:
                with app.app_context():
                    _stamp(app.mysql.connection, name, 'ok', summary)
            except Exception as e:
                logger.warning("scheduler: stamp(ok) failed for %s: %s", name, e)
        except Exception as e:
            msg = str(e)[:500]
            out['errors'][name] = msg
            out['results'][name] = {'error': msg}
            logger.warning("scheduler: job %s raised: %s", name, e)
            try:
                with app.app_context():
                    _stamp(app.mysql.connection, name, 'error', {'error': msg})
            except Exception as e2:
                logger.warning("scheduler: stamp(error) failed for %s: %s", name, e2)

    return out


def run_due_jobs_safe(app, only=None, force=False):
    """Outermost guard wrapper — guarantees NO exception escapes (for the request
    hook / worker). Returns the same dict as ``run_due_jobs`` (empty on failure).
    """
    try:
        return run_due_jobs(app, only=only, force=force)
    except Exception as e:
        logger.warning("scheduler: run_due_jobs_safe swallowed: %s", e)
        return {'ran': [], 'skipped': [], 'errors': {'_runner': str(e)[:500]}, 'results': {}}
