"""Nightly AI eval result for the platform-admin status page (ROADMAP T0-3 / T3-8).

Contract: ``status(now=None)`` reads ``latest-<provider>.json`` files in ``AI_EVAL_DIR``
(written by ``scripts/nightly_ai_eval.sh``) and returns a list of dicts, one per provider:
``provider``, ``state`` in {"ok", "regression", "stale"}, ``taken`` (UTC datetime),
``age_hours``, ``passed``/``total`` cases, ``overall_pct`` and the gate's ``messages``.
A result older than ``AI_EVAL_MAX_AGE_HOURS`` (default 36) is stale: the job stopped.
Returns ``[]`` when unconfigured or nothing has run. Never raises.
"""
import json
import os
from datetime import datetime, timezone


def status(now=None):
    root = (os.environ.get("AI_EVAL_DIR") or "").strip()
    if not root:
        return []
    try:
        max_age = float(os.environ.get("AI_EVAL_MAX_AGE_HOURS", "36"))
    except ValueError:
        max_age = 36.0
    now = now or datetime.now(timezone.utc)
    out = []
    try:
        names = sorted(n for n in os.listdir(root) if n.startswith("latest-") and n.endswith(".json"))
    except OSError:
        return []
    for name in names:
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                d = json.load(fh)
            taken = datetime.fromtimestamp(int(d["timestamp"]), tz=timezone.utc)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        gate = d.get("gate") or {}
        age = round((now - taken).total_seconds() / 3600.0, 1)
        if age > max_age:
            state = "stale"
        elif gate.get("ok") is False or int(d.get("exit_code") or 0) != 0:
            state = "regression"
        else:
            state = "ok"
        out.append({
            "provider": d.get("provider") or name[len("latest-"):-len(".json")],
            "state": state, "taken": taken, "age_hours": age, "max_age_hours": max_age,
            "passed": d.get("cases_passed"), "total": d.get("cases_total"),
            "overall_pct": (d.get("metrics") or {}).get("overall_pass_pct"),
            "messages": [m for m in (gate.get("messages") or []) if "REGRESSION" in m][:5],
            "commit": (d.get("commit") or "")[:7],
        })
    return out
