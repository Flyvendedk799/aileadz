"""Learner-side view of the learner's OWN HR data, for the employee-facing AI.

Why this module exists
----------------------
HR assigns learning paths (``employee_learning_progress``), rates skills
(``employee_skills_matrix``), sets department competency targets
(``company_skill_targets``) and — optionally — writes goals
(``employee_goals``). The course advisor and the AI Profiler never read any of
it, so the chatbot happily recommends courses while the learner already has an
overdue HR assignment covering the same skill. This module reads those rows for
exactly ONE learner and renders a compact Danish fact block that the caller
injects as a per-turn context layer (fenced as untrusted data by the caller).

Design rules:
  * Strictly self-scoped: every query is parameterised with this learner's own
    ids (resolved server-side from the username) plus their company_id. No
    colleague data is ever read.
  * Each source degrades independently (the ``_execute_get_my_agenda`` pattern
    in app1/tools.py): a failing source rolls back, records its name in
    ``failed_sources`` and never blocks the others. Nothing here raises.
  * Plain facts only — no instructions to the model — and the formatted body is
    capped so it can never crowd out the rest of the prompt.

Id convention (verified against the writers, not the column names)
-------------------------------------------------------------------
``employee_skills_matrix.employee_id`` and ``employee_goals.employee_id`` hold
``users.id`` (== ``company_users.user_id``), NOT ``company_users.id``: the HR
assign-skill route inserts ``company_users.user_id`` values, confirm-uplift
validates ``employee_id`` against ``company_users.user_id``, bulk_assign.html
posts ``e.user_id``, and the only ``employee_goals`` reader joins
``eg.employee_id = cu.user_id``. We therefore key those tables on ``user_id``.
``company_users.id`` is still returned as ``employee_id`` for callers that want
the HR row id.
"""
import copy
import logging
import os
import re
import datetime

from flask import current_app

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 120
_CACHE_PREFIX = "learner_context.hr"
_MAX_CHARS = 1800
_FIELD_MAX = 80

_ASSIGNMENT_LIMIT = 8
_SKILL_GAP_LIMIT = 8
_DEPT_TARGET_LIMIT = 8
_GOAL_LIMIT = 5
# Upper bound on matrix rows read for one learner. The matrix is read whole
# (bounded) rather than pre-filtered on target > current, because HR's writers
# only ever set ``current_level`` — the learner's current levels are what make
# the department targets meaningful ("nu 2 → mål 4").
_MATRIX_READ_LIMIT = 100

_TRUTHY = ("1", "true", "yes", "on", "ja")
_FALSY = ("0", "false", "no", "off", "nej")

_STATUS_LABELS = {
    "not_started": "ikke påbegyndt",
    "in_progress": "i gang",
    "started": "i gang",
    "overdue": "overskredet",
    "paused": "sat på pause",
}
_PRIORITY_LABELS = {"critical": "kritisk", "high": "høj"}


# ── Feature flags ──────────────────────────────────────────────────────────

def _env_flag(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    val = str(raw).strip().lower()
    if val in _TRUTHY:
        return True
    if val in _FALSY:
        return False
    return default


def hr_context_enabled() -> bool:
    """Master switch (env ``AI_LEARNER_HR_CONTEXT``, default ON)."""
    return _env_flag("AI_LEARNER_HR_CONTEXT", True)


def hr_goals_enabled() -> bool:
    """HR-written goals (env ``AI_LEARNER_HR_GOALS``, default OFF).

    Off by default: learners cannot see ``employee_goals`` anywhere in the UI
    today, so surfacing them through the chatbot needs product/privacy sign-off.
    """
    return _env_flag("AI_LEARNER_HR_GOALS", False)


# ── DB helpers ─────────────────────────────────────────────────────────────

def _connection():
    return current_app.mysql.connection


def _cursor():
    """A dict cursor on the request connection (refreshed if stale)."""
    try:
        from db_compat import refresh_flask_mysql_connection
        refresh_flask_mysql_connection(current_app.mysql)
    except Exception:
        pass
    try:
        import MySQLdb.cursors
        return _connection().cursor(MySQLdb.cursors.DictCursor)
    except ImportError:  # pragma: no cover - driver always present in prod
        return _connection().cursor()


def _close(cur):
    try:
        if cur is not None:
            cur.close()
    except Exception:
        pass


def _rollback():
    """Clear an aborted statement so the next source gets a usable connection."""
    try:
        _connection().rollback()
    except Exception:
        pass


def _fetch(sql, params, *, one=False):
    """Run one parameterised read; always closes the cursor. Raises on error."""
    cur = None
    try:
        cur = _cursor()
        cur.execute(sql, params)
        if one:
            return cur.fetchone()
        return list(cur.fetchall() or [])
    finally:
        _close(cur)


# ── Learner resolution ─────────────────────────────────────────────────────

_RESOLVE_BY_CU_USERNAME = (
    "SELECT id, company_id, user_id, department FROM company_users "
    "WHERE username = %s AND status = 'active'{company} LIMIT 1"
)
# Fallback for rows created by SSO / the enterprise API, which do not fill
# company_users.username — resolve through users.username instead (the same
# join competency._company_targets uses).
_RESOLVE_BY_USERS_JOIN = (
    "SELECT cu.id, cu.company_id, cu.user_id, cu.department FROM company_users cu "
    "JOIN users u ON cu.user_id = u.id "
    "WHERE u.username = %s AND cu.status = 'active'{company} LIMIT 1"
)


def _resolve_or_raise(username, company_id=None):
    if not username:
        return None
    for template, col in ((_RESOLVE_BY_CU_USERNAME, "company_id"),
                          (_RESOLVE_BY_USERS_JOIN, "cu.company_id")):
        if company_id:
            sql = template.format(company=" AND {} = %s".format(col))
            params = (username, company_id)
        else:
            sql = template.format(company="")
            params = (username,)
        row = _fetch(sql, params, one=True)
        if row:
            return {
                "employee_id": row.get("id"),
                "user_id": row.get("user_id"),
                "company_id": row.get("company_id"),
                "department": row.get("department"),
            }
    return None


def resolve_company_user(username, company_id=None):
    """The learner's active ``company_users`` row as
    ``{employee_id, user_id, company_id, department}``, or None.

    ``employee_id`` is ``company_users.id``. Never raises (a DB error → None).
    """
    try:
        return _resolve_or_raise(username, company_id)
    except Exception as e:
        _rollback()
        logger.debug("learner_context: resolve failed for %s: %s", username, e)
        return None


# ── Sources ────────────────────────────────────────────────────────────────

def _load_assignments(user_id, company_id):
    rows = _fetch(
        """
        SELECT elp.course_handle, elp.content_name, elp.status,
               elp.progress_percentage, elp.due_date,
               lp.path_name, lp.path_category, lp.difficulty_level
        FROM employee_learning_progress elp
        LEFT JOIN learning_paths lp
               ON lp.id = elp.learning_path_id
              AND (lp.company_id = elp.company_id OR lp.company_id IS NULL)
        WHERE elp.user_id = %s AND elp.company_id = %s
          AND COALESCE(elp.status, '') <> 'completed'
          AND elp.completed_at IS NULL
          AND COALESCE(elp.progress_percentage, 0) < 100
        ORDER BY (elp.due_date IS NULL), elp.due_date ASC, elp.id DESC
        LIMIT %s
        """,
        (user_id, company_id, _ASSIGNMENT_LIMIT),
    )
    out = []
    for r in rows:
        out.append({
            "title": r.get("content_name") or r.get("path_name") or r.get("course_handle") or "",
            "course_handle": r.get("course_handle"),
            "path_name": r.get("path_name"),
            "path_category": r.get("path_category"),
            "status": r.get("status"),
            "progress": r.get("progress_percentage"),
            "due_date": r.get("due_date"),
        })
    return out


def _load_skill_matrix(user_id, company_id):
    return _fetch(
        """
        SELECT skill_name, current_level, target_level
        FROM employee_skills_matrix
        WHERE employee_id = %s AND company_id = %s
        ORDER BY skill_name
        LIMIT %s
        """,
        (user_id, company_id, _MATRIX_READ_LIMIT),
    )


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _skill_gaps_from_matrix(matrix_rows):
    gaps = []
    for r in matrix_rows:
        name = r.get("skill_name")
        cur = _to_int(r.get("current_level")) or 0
        tgt = _to_int(r.get("target_level")) or 0
        if name and tgt > cur:
            gaps.append({"skill": name, "current_level": cur,
                         "target_level": tgt, "gap": tgt - cur})
    gaps.sort(key=lambda g: (-g["gap"], str(g["skill"]).lower()))
    return gaps[:_SKILL_GAP_LIMIT]


def _load_dept_targets(username, current_levels):
    """Department targets via competency._company_targets, annotated with the
    learner's HR-rated current level where one exists. Targets already met are
    dropped — they are not actionable for the advisor."""
    import competency  # ImportError → recorded as a failed source by caller
    rows = competency._company_targets(username) or []
    prio_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    out = []
    for r in rows:
        name = r.get("skill_name")
        tgt = _to_int(r.get("target_level"))
        if not name or tgt is None:
            continue
        cur = current_levels.get(str(name).strip().lower())
        if cur is not None and cur >= tgt:
            continue
        out.append({"skill": name, "target_level": tgt, "current_level": cur,
                    "priority": (r.get("priority") or "medium")})
    out.sort(key=lambda t: (prio_rank.get(str(t["priority"]).lower(), 2),
                            str(t["skill"]).lower()))
    return out[:_DEPT_TARGET_LIMIT]


def _load_hr_goals(user_id, company_id):
    rows = _fetch(
        """
        SELECT goal_title, goal_description, target_date, status, progress
        FROM employee_goals
        WHERE employee_id = %s AND company_id = %s
          AND status IN ('active', 'in_progress')
        ORDER BY (target_date IS NULL), target_date ASC
        LIMIT %s
        """,
        (user_id, company_id, _GOAL_LIMIT),
    )
    return [{
        "title": r.get("goal_title") or "",
        "description": r.get("goal_description") or "",
        "target_date": r.get("target_date"),
        "status": r.get("status"),
        "progress": r.get("progress"),
    } for r in rows]


# ── Public builder ─────────────────────────────────────────────────────────

def _empty_context():
    return {"employee": None, "assignments": [], "skill_gaps": [],
            "dept_targets": [], "hr_goals": [], "failed_sources": []}


def _cache_key(username, company_id):
    return (_CACHE_PREFIX, str(username), str(company_id) if company_id else "")


def clear_cache():
    """Drop all cached learner contexts (e.g. after an HR write in tests)."""
    try:
        from perf_cache import cache_clear
        cache_clear(_CACHE_PREFIX)
    except Exception:
        pass


def build_learner_hr_context(username, *, user_id=None, company_id=None) -> dict:
    """Collect this learner's HR-side data. Never raises.

    Returns ``{"employee", "assignments", "skill_gaps", "dept_targets",
    "hr_goals", "failed_sources"}``. A learner with no active company_users row
    (a private user) gets the empty context with no failures. Results are cached
    per (username, company_id) for 120s per worker; degraded results (any failed
    source) are NOT cached so a transient DB blip heals on the next turn.
    """
    ctx = _empty_context()
    try:
        if not username or not hr_context_enabled():
            return ctx

        key = _cache_key(username, company_id)
        try:
            from perf_cache import cache_get
            cached, hit = cache_get(key)
            if hit:
                return copy.deepcopy(cached)
        except Exception:
            pass

        try:
            employee = _resolve_or_raise(username, company_id)
        except Exception as e:
            _rollback()
            logger.debug("learner_context: employee lookup failed for %s: %s", username, e)
            ctx["failed_sources"].append("employee")
            return ctx

        if employee:
            ctx["employee"] = employee
            # Prefer the id stored on the HR row; the caller's session user_id
            # only fills in when the row has none (never overrides it).
            uid = employee.get("user_id") or user_id
            cid = employee.get("company_id")

            if uid and cid:
                try:
                    ctx["assignments"] = _load_assignments(uid, cid)
                except Exception as e:
                    _rollback()
                    logger.debug("learner_context: assignments failed for %s: %s", username, e)
                    ctx["failed_sources"].append("assignments")

            current_levels = {}
            if uid and cid:
                try:
                    matrix = _load_skill_matrix(uid, cid)
                    for r in matrix:
                        lvl = _to_int(r.get("current_level"))
                        if r.get("skill_name") and lvl is not None:
                            current_levels[str(r["skill_name"]).strip().lower()] = lvl
                    ctx["skill_gaps"] = _skill_gaps_from_matrix(matrix)
                except Exception as e:
                    _rollback()
                    logger.debug("learner_context: skill matrix failed for %s: %s", username, e)
                    ctx["failed_sources"].append("skill_gaps")

            try:
                gap_skills = {str(g["skill"]).strip().lower() for g in ctx["skill_gaps"]}
                ctx["dept_targets"] = [
                    t for t in _load_dept_targets(username, current_levels)
                    if str(t["skill"]).strip().lower() not in gap_skills
                ]
            except Exception as e:
                _rollback()
                logger.debug("learner_context: dept targets failed for %s: %s", username, e)
                ctx["failed_sources"].append("dept_targets")

            if hr_goals_enabled() and uid and cid:
                try:
                    ctx["hr_goals"] = _load_hr_goals(uid, cid)
                except Exception as e:
                    _rollback()
                    logger.debug("learner_context: hr goals failed for %s: %s", username, e)
                    ctx["failed_sources"].append("hr_goals")

        if not ctx["failed_sources"]:
            try:
                from perf_cache import cache_set
                cache_set(key, copy.deepcopy(ctx), _CACHE_TTL_SECONDS)
            except Exception:
                pass
        return ctx
    except Exception as e:  # belt and braces: never break the chat turn
        logger.warning("learner_context: build failed for %s: %s", username, e)
        return ctx


# ── Formatting ─────────────────────────────────────────────────────────────

def _clean(value, limit=_FIELD_MAX):
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _as_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _deadline_text(value, today):
    d = _as_date(value)
    if d is None:
        return ""
    days = (d - today).days
    if days < 0:
        return "frist {} (overskredet)".format(d.isoformat())
    return "frist {}".format(d.isoformat())


def _pct(value):
    """"40 %" for a positive percentage, "" for zero/unknown (not worth a token)."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return ""
    return "{} %".format(n) if n > 0 else ""


def format_learner_hr_context(ctx: dict, *, today=None) -> str:
    """Compact Danish fact block, or "" when there is nothing relevant.

    Plain facts only (no instructions to the model); capped at 1800 chars by
    dropping whole trailing lines so no half-sentence is ever emitted.
    """
    try:
        if not isinstance(ctx, dict):
            return ""
        today = today or datetime.date.today()
        lines = []

        assignments = ctx.get("assignments") or []
        if assignments:
            lines.append("Tildelt af HR:")
            for a in assignments:
                title = _clean(a.get("title")) or "Læringsforløb"
                path = _clean(a.get("path_name"), 60)
                if path and path.lower() != title.lower():
                    title = "{} (forløb: {})".format(title, path)
                bits = []
                status = str(a.get("status") or "").lower()
                if status in _STATUS_LABELS:
                    bits.append(_STATUS_LABELS[status])
                if _pct(a.get("progress")):
                    bits.append(_pct(a.get("progress")))
                dl = _deadline_text(a.get("due_date"), today)
                if dl:
                    bits.append(dl)
                lines.append("- {}{}".format(title, " — " + ", ".join(bits) if bits else ""))

        gaps = ctx.get("skill_gaps") or []
        if gaps:
            lines.append("Kompetencemål fra HR (nu → mål, skala 1-5):")
            for g in gaps:
                lines.append("- {}: {} → {}".format(
                    _clean(g.get("skill"), 60), g.get("current_level"), g.get("target_level")))

        targets = ctx.get("dept_targets") or []
        if targets:
            lines.append("Afdelingens kompetencemål (skala 1-5):")
            for t in targets:
                cur = t.get("current_level")
                level = ("nu {} → mål {}".format(cur, t.get("target_level"))
                         if cur is not None else "mål {}".format(t.get("target_level")))
                prio = _PRIORITY_LABELS.get(str(t.get("priority") or "").lower())
                lines.append("- {}: {}{}".format(
                    _clean(t.get("skill"), 60), level,
                    ", prioritet {}".format(prio) if prio else ""))

        goals = ctx.get("hr_goals") or []
        if goals:
            lines.append("Mål sat af HR:")
            for g in goals:
                title = _clean(g.get("title")) or "Mål"
                bits = []
                if _pct(g.get("progress")):
                    bits.append(_pct(g.get("progress")))
                d = _as_date(g.get("target_date"))
                if d is not None:
                    bits.append("måldato {}".format(d.isoformat()))
                desc = _clean(g.get("description"), 100)
                line = "- {}{}".format(title, " — " + ", ".join(bits) if bits else "")
                if desc:
                    line += ": " + desc
                lines.append(line)

        if not lines:
            return ""

        out, used = [], 0
        for line in lines:
            extra = len(line) + (1 if out else 0)
            if used + extra > _MAX_CHARS:
                break
            out.append(line)
            used += extra
        # Never end on a dangling section header.
        while out and out[-1].endswith(":") and not out[-1].startswith("- "):
            out.pop()
        return "\n".join(out)
    except Exception as e:
        logger.debug("learner_context: format failed: %s", e)
        return ""
