# Runbook: Background Job Runner (Outbox Drain & Scheduled Maintenance)

aileadz has **no always-on worker**. Integration events (webhooks, etc.) are
written to a durable outbox inside the request and delivered out-of-band by
`event_bus.drain_outbox()`. This runbook sets up the reliable driver: a
PythonAnywhere **Scheduled Task** that POSTs the token-protected drain endpoint.

## The endpoint

`POST /api/v1/_internal/drain-outbox`
(defined at `enterprise_api/__init__.py:1871-1918`).

- **Auth:** shared secret in env `OUTBOX_DRAIN_TOKEN`, compared in constant time
  (`enterprise_api/__init__.py:1881-1898`). Pass it as the `X-Drain-Token`
  header, or as `Authorization: Bearer <token>`.
- **Disabled when unset:** if `OUTBOX_DRAIN_TOKEN` is not configured the endpoint
  returns **503** so an unconfigured deploy can't be drained anonymously
  (`enterprise_api/__init__.py:1882-1886`).
- **`limit` query param:** rows per call, clamped to `1..500`, default `50`
  (`enterprise_api/__init__.py:1908-1911`).
- **Response:** `200 {"status":"ok","counts":{...}}` on success.

## Option A (recommended): the `drain_worker.py` script — no token, no HTTP

`drain_worker.py` calls `event_bus.drain_outbox()` directly in the app context, so
there is no token to manage and no public endpoint to secure.

- **Scheduled Task** (single-shot, runs then exits):
  ```
  cd ~/aileadz && python3 drain_worker.py --limit 200
  ```
- **Always-on Task** (paid; drains continuously, near-real-time):
  ```
  cd ~/aileadz && python3 drain_worker.py --loop --interval 60
  ```
  Use your virtualenv python if you have one (`~/.virtualenvs/<env>/bin/python3`).
  DB connection comes from `run.py`'s config, so no extra env is needed. This is the
  simplest setup — skip Option B unless you specifically want the HTTP endpoint.

## Option B: the token-protected HTTP endpoint

### 1. Set the token env var

Choose a strong random token:
```
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
Set `OUTBOX_DRAIN_TOKEN` to that value in **both** environments:

- **Web app:** in the WSGI config (so the endpoint accepts the token) — see
  `wsgi_pythonanywhere.example.py` for the `os.environ[...]` pattern.
- **Scheduled task:** export it in the task command (the curl call reads it).

Keep the real value out of git.

### 2. Create the PythonAnywhere Scheduled Task

PythonAnywhere → **Tasks** tab → add a task. Command:

```
curl -fsS -X POST \
  -H "X-Drain-Token: $OUTBOX_DRAIN_TOKEN" \
  "https://<your-domain>/api/v1/_internal/drain-outbox?limit=200"
```

(If the task shell doesn't inherit `OUTBOX_DRAIN_TOKEN`, set it inline in the
command or in `~/.bashrc`. Do not paste the token into a committed file.)

### 3. Cadence

- Free PythonAnywhere accounts allow **one daily** scheduled task — pick the
  hour and accept up-to-24h delivery latency.
- Paid accounts can schedule **every few minutes**; every **5 minutes** is a good
  default for near-real-time webhook delivery. Raise `limit` if backlog builds.

---

## What the runner unlocks

Without the scheduled drain, the app still works but degrades to **best-effort,
opportunistic** outbox delivery only (pending integration events are flushed
opportunistically during normal request traffic via the event bus, with no
guarantee and no catch-up when traffic is quiet).

A reliable runner is what makes the following dependable rather than best-effort:

- **Outbox / integration-event delivery** — webhooks fire reliably and retry
  instead of waiting for the next coincidental request.
- **Digests** — periodic summary/notification digests get sent on schedule.
- **Data retention** — retention sweeps (pruning/aging out old records) run.
- **Compliance reminders** — scheduled compliance/training reminders go out.

These maintenance jobs need *something* to call them on a cadence; the same
scheduled-task mechanism (and token-protected internal endpoints) is the hook.

## Verify

1. Confirm the env var is live: with `OUTBOX_DRAIN_TOKEN` **unset**, the endpoint
   returns 503; once set + web app reloaded, it accepts the token.
2. Run the curl command manually once. Expect `200` with a JSON `counts` body.
3. A wrong/missing token returns `401`; a missing token env on the host returns
   `503`.
4. After the scheduled task's first run, check the task log shows a `200` and the
   outbox table has fewer pending rows.

## Done criteria

- `OUTBOX_DRAIN_TOKEN` set in WSGI + task env.
- Scheduled task created at the chosen cadence and returning `200`.
- Manual curl confirms drain works.
