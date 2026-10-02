"""Profile check-ins: the assistant's weekly heartbeat.

Contract
--------
Once a week (`scheduler.JOBS`, job ``profile_checkin_heartbeat``) this module looks
at each person who used the AI assistant recently and queues a few short, *reasoned*
follow-ups in ``user_profile_checkins`` (table in ``app1/user_profile_db.py``):

* ``progress``  - a course they completed recently ("did you get anything out of it,
  use it, share it?")
* ``goal``      - an active learning goal nobody has touched for a while
* ``direction`` - no target role yet, though the profile has something to build on
* ``gap``       - the weakest profile area, with the reason it would sharpen advice

The queue holds a topic and a reason, never wording. The next time the person opens
the assistant, ``checkin_layer`` shows the open ones to the model as optional
background (priority below the person's own request) and the model decides whether
and how to bring one up. ``resolve_checkin`` / ``record_learning_outcome`` (AI tools)
close an item once it has been talked through, so it is not asked twice.

Guard rails (this is the "never a form-filler" surface, keep it that way):

* at most ``MAX_OPEN`` open items per person, ``MAX_SHOWN`` shown per conversation,
  an asked item rests ``CHECKIN_COOLDOWN_DAYS`` and gives up after ``CHECKIN_MAX_ASKS``
  asks; unanswered items expire after ``CHECKIN_TTL_DAYS``;
* the person can switch it off (``set_checkins_muted``, tool outcome ``stop_all``);
* ``AI_PROFILE_CHECKINS=0`` is the platform kill switch;
* rows are the person's own, erased/exported by ``gdpr_service``; HR never sees them.

``build_candidates`` is pure (data in, dicts out) so it is tested offline.
"""
import datetime as _dt
import os

MAX_OPEN = 3
MAX_SHOWN = 2
PROGRESS_WINDOW_DAYS = (7, 120)   # completed this long ago (min, max)
GOAL_STALE_DAYS = 14
GAP_STRENGTH_BELOW = 0.5

KIND_PROGRESS = "progress"
KIND_GOAL = "goal"
KIND_DIRECTION = "direction"
KIND_GAP = "gap"


def checkins_enabled():
    return os.environ.get("AI_PROFILE_CHECKINS", "1").strip().lower() not in ("0", "false", "no", "off")


def _days_ago(value, now):
    """Whole days since ``value`` (datetime or ISO-ish string), None if unknown."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = _dt.datetime.fromisoformat(value.strip()[:19])
        except ValueError:
            return None
    if isinstance(value, _dt.date) and not isinstance(value, _dt.datetime):
        value = _dt.datetime.combine(value, _dt.time())
    try:
        return max(0, (now - value).days)
    except TypeError:
        return None


def _section_reason(key):
    try:
        from app1.agent import _SECTION_WHY
        return _SECTION_WHY.get(key)
    except Exception:
        return None


def build_candidates(*, goals=(), completed_courses=(), target_role="", has_profile_depth=False,
                     completeness=None, now=None):
    """Check-in candidates for one person, most valuable first.

    ``goals`` / ``completed_courses`` are the raw DB rows (``updated_at`` /
    ``added_at`` are used for recency). Returns dicts with kind, ref, topic, reason.
    """
    now = now or _dt.datetime.now()
    out = []

    lo, hi = PROGRESS_WINDOW_DAYS
    recent = []
    for row in completed_courses or ():
        title = (row.get("course_title") or "").strip()
        age = _days_ago(row.get("added_at"), now)
        if title and age is not None and lo <= age <= hi:
            recent.append((age, title))
    for _age, title in sorted(recent)[:2]:
        out.append({
            "kind": KIND_PROGRESS, "ref": title,
            "topic": f"Opfølgning på kurset «{title}»",
            "reason": ("De har gennemført kurset for nylig. Hør naturligt, om de har fået noget ud af det, "
                       "brugt det i praksis eller delt det med kolleger, og om det peger videre mod et næste skridt."),
        })

    for g in goals or ():
        if (g.get("status") or "aktiv") != "aktiv":
            continue
        age = _days_ago(g.get("updated_at"), now)
        title = (g.get("title") or "").strip()
        if title and age is not None and age >= GOAL_STALE_DAYS:
            out.append({
                "kind": KIND_GOAL, "ref": f"goal:{g.get('id')}",
                "topic": f"Status på målet «{title}»",
                "reason": (f"Målet er ikke opdateret i ca. {max(1, age // 7)} uger. Hør, om det stadig passer, "
                           "hvad der er sket siden, og om der er noget, du kan hjælpe med."),
            })
            break

    role = (target_role or "").strip()
    if not role and has_profile_depth:
        out.append({
            "kind": KIND_DIRECTION, "ref": "target_role",
            "topic": "Hvor vil de gerne hen?",
            "reason": ("Retningen er ukendt. Den afgør, hvilke kompetencegab og kurser der betyder noget, "
                       "så den er ofte det mest værdifulde at finde ud af."),
        })

    for section in sorted((completeness or {}).get("sections") or [], key=lambda s: s.get("strength", 0)):
        key = section.get("key")
        if key in (None, "headline") or section.get("strength", 0) >= GAP_STRENGTH_BELOW:
            continue
        if key == "goals" and not role:
            continue  # the direction item above already covers it
        why = _section_reason(key)
        if why:
            out.append({
                "kind": KIND_GAP, "ref": key,
                "topic": f"Det der mangler for at kunne rådgive skarpt: {section.get('label') or key}",
                "reason": f"Det ville give dig: {why}. Tag det kun op, hvis samtalen naturligt peger den vej.",
            })
        break
    return out


def run_heartbeat(app, *, limit=500):
    """The weekly job body. Never raises; returns a summary dict."""
    if not checkins_enabled():
        return {"skipped": "disabled"}
    summary = {"users": 0, "queued": 0, "muted": 0, "full": 0, "errors": 0}
    with app.app_context():
        from app1 import user_profile_db as db
        try:
            db.ensure_tables()
            users = db.active_assistant_users(limit=limit)
        except Exception as exc:
            return {"error": f"audience lookup failed: {exc}"}
        for username in users:
            summary["users"] += 1
            try:
                queued = queue_for_user(db, username, summary)
                summary["queued"] += queued
            except Exception as exc:  # one person's failure must not stop the rest
                summary["errors"] += 1
                print(f"[Check-in heartbeat] {username}: {exc}")
                try:
                    app.mysql.connection.rollback()
                except Exception:
                    pass
    return summary


def queue_for_user(db, username, summary=None):
    """Queue up to ``MAX_OPEN`` open items for one person. Returns how many were added."""
    if db.checkins_muted(username):
        if summary is not None:
            summary["muted"] += 1
        return 0
    room = MAX_OPEN - db.open_checkin_count(username)
    if room <= 0:
        if summary is not None:
            summary["full"] += 1
        return 0
    profile = db.get_full_profile(username)
    completeness = db.profile_completeness(username, profile=profile)
    depth = bool(profile.get("skills") or profile.get("experience"))
    candidates = build_candidates(
        goals=db.get_learning_goals(username),
        completed_courses=db.get_completed_courses(username),
        target_role=profile.get("target_role") or "",
        has_profile_depth=depth,
        completeness=completeness,
    )
    added = 0
    for cand in candidates:
        if added >= room:
            break
        if db.add_checkin(username, cand["kind"], cand["ref"], cand["topic"], cand["reason"]):
            added += 1
    return added


def checkin_layer(rows):
    """Body for the ``checkins`` context layer (empty string when nothing to show).

    Background, not a directive: the person's own request always comes first, and
    nothing here is worded for the model to read out."""
    rows = list(rows or [])[:MAX_SHOWN]
    if not rows:
        return ""
    lines = [
        "Der ligger få opfølgninger fra de seneste uger. Tag dem kun op, hvis det passer ind i, "
        "hvad brugeren skriver, ellers lad dem ligge. Formulér dig med dine egne ord, og stil højst "
        "ét spørgsmål ad gangen. Luk dem med resolve_checkin, når de er talt igennem eller afvist.",
    ]
    for row in rows:
        lines.append(f"- [#c{row['id']}] {row['topic']}. {row.get('reason') or ''}".rstrip())
    return "\n".join(lines)


def execute_resolve_checkin(args, username):
    """Tool body for ``resolve_checkin``. Returns a JSON-able dict."""
    if not username:
        return {"status": "error", "message": "Brugeren er ikke logget ind."}
    from app1 import user_profile_db as db
    outcome = (args.get("outcome") or "answered").strip().lower()
    if outcome == "stop_all":
        db.ensure_tables()
        db.set_checkins_muted(username, True)
        return {"status": "ok", "muted": True, "message": "Jeg spørger ikke ind til sådan noget igen."}
    if outcome not in ("answered", "dismissed", "snooze"):
        return {"status": "error", "message": "outcome skal være answered, dismissed, snooze eller stop_all."}
    try:
        cid = int(str(args.get("checkin_id")).lstrip("#c"))
    except (TypeError, ValueError):
        return {"status": "error", "message": "Angiv checkin_id fra [#c…] i konteksten."}
    db.ensure_tables()
    if outcome == "snooze":
        # Already rests for the cooldown after being shown; nothing to change.
        return {"status": "ok", "outcome": "snooze"}
    if not db.resolve_checkin(username, cid, outcome):
        return {"status": "not_found", "message": "Den opfølgning findes ikke (eller er allerede lukket)."}
    return {"status": "ok", "outcome": outcome}


def execute_record_learning_outcome(args, username):
    """Tool body for ``record_learning_outcome``: what a completed course led to.

    Saved as one long-term memory (so it informs later advice and shows in the
    Mind-Map) and closes the matching progress check-in."""
    if not username:
        return {"status": "error", "message": "Brugeren er ikke logget ind."}
    course = (args.get("course_title") or "").strip()
    if not course:
        return {"status": "error", "message": "Angiv course_title."}
    learned = (args.get("learned") or "").strip()
    applied = args.get("applied")
    taught = args.get("taught_others")
    parts = []
    if learned:
        parts.append(f"Lærte: {learned}")
    if applied is not None:
        parts.append("Har brugt det i praksis" if applied else "Har endnu ikke brugt det i praksis")
    if taught is not None:
        parts.append("Har delt det med andre" if taught else "Har ikke delt det med andre endnu")
    note = (args.get("note") or "").strip()
    if note:
        parts.append(note)
    if not parts:
        return {"status": "error", "message": "Angiv mindst ét udbytte (learned, applied, taught_others eller note)."}
    from app1 import user_profile_db as db
    db.ensure_tables()
    memory_id = db.add_memory(username, f"Udbytte af {course}", category="kontekst",
                              detail=". ".join(parts), source="ai", confidence=0.9)
    closed = db.resolve_checkins_for(username, KIND_PROGRESS, course, "answered")
    return {"status": "memory_saved", "category": "kontekst", "id": memory_id,
            "label": f"Udbytte af {course}", "closed_checkins": closed}
