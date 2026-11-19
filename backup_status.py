"""Backup freshness for the platform-admin status page (ROADMAP T0-7).

Contract: ``status(now=None)`` reads the directory named by ``BACKUP_DIR`` (the target
that ``scripts/backup.sh`` writes ``<UTC stamp>/MANIFEST`` folders into) and returns a
dict with ``state`` in {"unconfigured", "missing", "ok", "stale", "failed"}, the newest
backup's ``stamp``, its ``age_hours`` and ``db_bytes``. A backup older than
``BACKUP_MAX_AGE_HOURS`` (default 30) is stale; a ``LAST_FAILURE`` file newer than the
newest backup (written by the host's failure hook) means the last run failed. Never raises.
Reads only names, sizes and the manifest's ``db_bytes`` line; no secrets live there.
"""
import os
import re
from datetime import datetime, timezone

_STAMP = re.compile(r"^\d{8}T\d{6}Z$")


def _parse(stamp):
    return datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)


def status(now=None):
    root = (os.environ.get("BACKUP_DIR") or "").strip()
    if not root:
        return {"state": "unconfigured"}
    try:
        max_age = float(os.environ.get("BACKUP_MAX_AGE_HOURS", "30"))
    except ValueError:
        max_age = 30.0
    now = now or datetime.now(timezone.utc)
    try:
        stamps = sorted(n for n in os.listdir(root)
                        if _STAMP.match(n) and os.path.isfile(os.path.join(root, n, "MANIFEST")))
    except OSError:
        return {"state": "missing", "dir": root}
    if not stamps:
        return {"state": "missing", "dir": root}
    newest = stamps[-1]
    taken = _parse(newest)
    out = {"dir": root, "stamp": newest, "taken": taken, "count": len(stamps),
           "age_hours": round((now - taken).total_seconds() / 3600.0, 1), "max_age_hours": max_age}
    try:
        with open(os.path.join(root, newest, "MANIFEST"), encoding="utf-8") as fh:
            m = re.search(r"^db_bytes=(\d+)$", fh.read(), re.M)
            out["db_bytes"] = int(m.group(1)) if m else None
    except OSError:
        out["db_bytes"] = None
    failed_at = None
    try:
        with open(os.path.join(root, "LAST_FAILURE"), encoding="utf-8") as fh:
            failed_at = datetime.strptime(fh.read().strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (OSError, ValueError):
        pass
    if failed_at and failed_at > taken:
        out["state"], out["failed_at"] = "failed", failed_at
    elif out["age_hours"] > max_age:
        out["state"] = "stale"
    else:
        out["state"] = "ok"
    return out
