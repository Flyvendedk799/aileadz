"""Shared, pure competency projection. Unknown is not zero; evidence supports a specific claim.

Legacy rows remain authoritative until replaced by explicit assessments. Numeric manager
levels are never rounded through the four-label profile enum. No writes or LLM calls.
"""
from hashlib import sha256


def identity(name):
    from competency import skill_key
    return sha256(skill_key(name).encode()).hexdigest()[:32]


def score(value):
    from competency import level_to_score
    result = level_to_score(value)
    return result if 1 <= result <= 5 else None


def snapshot(profile=None, matrix=(), *, available=True):
    from competency import canonical_skill, skill_category
    rows = {}
    for kind, entries in (("self", (profile or {}).get("skills", [])), ("manager", matrix or [])):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = canonical_skill(entry.get("name") or entry.get("skill_name"))
            if not name:
                continue
            key = identity(name)
            row = rows.setdefault(key, {"id": key, "name": name, "category": skill_category(name),
                                        "self": None, "manager": None, "evidence": []})
            value = score(entry.get("current_level") if kind == "manager" else (entry.get("numeric_level") or entry.get("level", entry.get("skill_level"))))
            if value is None:
                continue
            claim = {"level": value, "source": entry.get("source") or kind,
                     "assessed_at": entry.get("assessed_at") or entry.get("updated_at"),
                     "reference": entry.get("source_ref") or entry.get("id")}
            previous = row[kind]
            if previous and str(previous.get("assessed_at") or "") >= str(claim.get("assessed_at") or ""):
                continue
            row[kind] = claim
            if kind == 'manager':
                row['evidence'] = [e for e in row['evidence'] if e['kind'] != 'leder']
            if kind == "manager":
                row["evidence"].append({"kind": "leder", "label": "Vurderet af leder", **claim})
            elif claim["source"] in ("course_completion", "post_course"):
                row["evidence"].append({"kind": "kursus", "label": "Selvvurdering efter kursus", **claim})
    for row in rows.values():
        preferred = row["manager"] or row["self"]
        row.update(level=preferred["level"] if preferred else None,
                   provisional=not bool(row["manager"]),
                   disagreement=bool(row["manager"] and row["self"] and row["manager"]["level"] != row["self"]["level"]))
    return {"skills": sorted(rows.values(), key=lambda r: r["name"].lower()), "available": available}


def evaluate(state, requirements, *, version=None):
    """Evaluate a complete requirement set; pagination belongs to the presentation."""
    from competency import canonical_skill
    skills = {r["id"]: r for r in state["skills"]}
    merged = {}
    for req in requirements:
        name = canonical_skill(req.get("skill") or req.get("skill_name") or req.get("name"))
        target = score(req.get("target_level"))
        if not name or target is None:
            continue
        key = identity(name)
        if key not in merged:
            merged[key] = {"id": key, "skill": name, "target_level": target, "sources": []}
        merged[key]["target_level"] = max(target, merged[key]["target_level"])
        merged[key]["sources"].append(dict(req))
    counts = {"satisfied": 0, "below_target": 0, "unknown": 0, "provisional": 0}
    rows = []
    for key, req in merged.items():
        skill = skills.get(key, {})
        level = skill.get("level") if state["available"] else None
        status = "unknown" if level is None else "below_target" if level < req["target_level"] else "satisfied"
        provisional = level is not None and skill.get("provisional", True)
        counts[status] += 1
        counts["provisional"] += int(provisional)
        rows.append({**req, "current_level": level, "gap": req["target_level"] - level if level is not None else None,
                     "status": status, "provisional": provisional, "disagreement": skill.get("disagreement", False)})
    total = len(rows)
    return {"version": version, "requirements": rows, "counts": counts, "total": total,
            "pct": round(100 * counts["satisfied"] / total) if total and state["available"] else None,
            "available": state["available"]}


def load_matrix(username, company_id=None, user_id=None):
    """Explicit tenant context; legacy callers may use only their own session context."""
    from flask import current_app, has_request_context, session
    if company_id is None and has_request_context() and session.get("user") == username:
        company_id = session.get("company_id")
    if not company_id:
        return [], True
    cur = None
    try:
        import MySQLdb.cursors
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute("SELECT esm.skill_name, esm.current_level, esm.target_level, esm.updated_at FROM employee_skills_matrix esm "
                    "JOIN users u ON u.id=esm.employee_id JOIN company_users cu ON cu.user_id=u.id AND cu.company_id=esm.company_id "
                    "WHERE esm.company_id=%s AND u.username=%s AND cu.status='active'", (company_id, username))
        return list(cur.fetchall() or []), True
    except Exception:
        return [], False
    finally:
        if cur:
            cur.close()


def work_requirements(username, company_id=None):
    """Published current-role and linked personal-goal requirements, with independent sources."""
    from flask import current_app, has_request_context, session
    if company_id is None and has_request_context() and session.get('user') == username:
        company_id = session.get('company_id')
    if not company_id:
        return []
    import json
    import MySQLdb.cursors
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT rv.requirements_json FROM talent_role_assignments a JOIN talent_role_versions rv ON rv.id=a.role_version_id AND rv.company_id=a.company_id "
                    "JOIN users u ON u.id=a.user_id JOIN company_users cu ON cu.user_id=u.id AND cu.company_id=a.company_id "
                    "WHERE a.company_id=%s AND u.username=%s AND cu.status='active' AND rv.published=1", (company_id, username))
        out = [{**r, 'source': 'current_role', 'priority': 'high'} for row in cur.fetchall() for r in json.loads(row['requirements_json'])]
        # Goal ownership is compared in Python: the two tables may differ in collation
        # (talent_service._same_owner).
        cur.execute("SELECT l.requirements_json,g.username AS goal_username,u.username AS link_username "
                    "FROM talent_goal_links l JOIN users u ON u.id=l.user_id "
                    "JOIN user_learning_goals g ON g.id=l.goal_id "
                    "WHERE l.company_id=%s AND u.username=%s AND g.status='aktiv'", (company_id, username))
        from talent_service import _same_owner
        out.extend({**r, 'source': 'development_goal', 'priority': 'high'}
                   for row in cur.fetchall() if _same_owner(row) for r in json.loads(row['requirements_json']))
        return out
    finally:
        cur.close()
