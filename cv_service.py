"""The one CV: assembled from the whole platform, analysed, and exported.

Before this module the CV was four loosely related things (an import on the profile
page, a bare print page, a coach only reachable mid-import, and an AI card that read
stale keys). Every CV surface now goes through here:

* ``/profil/cv`` (the CV studio), ``/profil/cv/print`` and ``/profil/cv/download.<fmt>``
  render ``build_cv`` output,
* ``/api/cv/document`` and ``/api/cv/summary`` serve it as JSON,
* the assistant's ``show_cv_summary`` tool reads ``chat_card``.

What "from the whole platform" means (each source degrades independently):

* the learner's own profile (``user_profile_db.get_full_profile``),
* courses completed through Futurematch orders (``user_completed_courses`` rows that carry
  a ``course_handle``; ``order_service`` writes them) -> marked *verified*,
* skills a completed course proved (``user_skills.source = 'course_completion'``) and
  skills a manager rated in the HR matrix (``learner_context``) -> evidence badges,
* certification validity (``cert_expiry_service._parse_expiry``),
* work in progress: active learning paths, HR assignments, approved/booked orders,
* target-role fit and gaps (``competency.compute_skill_gaps``), with catalog links.

Contract:
* ``assemble`` / ``analyse`` / ``to_markdown`` / ``to_text`` / ``normalise_prefs`` are
  pure (no Flask, no DB) and unit-testable offline.
* ``build_cv`` / ``load_prefs`` / ``save_prefs`` read or write ONLY the given user's own
  rows (company-scoped where company data is touched) and never raise.
* Nothing here invents CV content: every line comes from a stored row. The AI coach
  (``cv_ingest.improve_cv_section``) only proposes; the person applies.
"""
from __future__ import annotations

import datetime
import json
import logging
import re

logger = logging.getLogger(__name__)

LEVEL_LABELS = {"ekspert": "Ekspert", "avanceret": "Avanceret", "mellem": "Mellem", "begynder": "Begynder"}
LEVEL_ORDER = ("ekspert", "avanceret", "mellem", "begynder")
LANG_LABELS = {"modersmaal": "Modersmål", "flydende": "Flydende", "mellem": "Mellem", "begynder": "Begynder"}
_LANG_RANK = {"modersmaal": 4, "flydende": 3, "mellem": 2, "begynder": 1}

# Section keys in document order, with their Danish headings. A person may hide any of
# them (prefs.hidden) except the identity block, which is not a section.
SECTIONS = (
    ("summary", "Profil"),
    ("experience", "Erfaring"),
    ("education", "Uddannelse"),
    ("skills", "Kompetencer"),
    ("certifications", "Certificeringer"),
    ("courses", "Kurser og efteruddannelse"),
    ("in_progress", "Under uddannelse"),
    ("languages", "Sprog"),
    ("links", "Links"),
    ("direction", "Karriereretning"),
)
SECTION_KEYS = tuple(k for k, _ in SECTIONS)
SECTION_LABELS = dict(SECTIONS)

TEMPLATES = {
    "klassisk": "Klassisk",
    "moderne": "Moderne",
    "kompakt": "Kompakt",
}
DEFAULT_PREFS = {
    "template": "klassisk",
    "hidden": ["direction"],
    "targeted": False,
    "show_evidence": True,
}

# Skill sources -> evidence shown next to the skill. 'leder' (HR matrix) is added
# separately; both are things the platform itself observed, not self-reports.
_SOURCE_EVIDENCE = {
    "course_completion": ("kursus", "Bevist via kursus"),
    "post_course": ("kursus", "Bevist via kursus"),
}
_RESULT_RE = re.compile(r"\d|%|\b(øge|øgede|reducer|reducerede|sparede|halverede|fordoblede|leverede|"
                        r"lancerede|opnåede|vandt|ledte|indførte|implementerede|byggede)\w*", re.I)
_SAFE_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.I)
_BARE_DOMAIN_RE = re.compile(r"^(www\.)?[a-z0-9-]+(\.[a-z0-9-]+)+(/[^\s<>\"']*)?$", re.I)


# ── small helpers ────────────────────────────────────────────────────────────

def _s(value, limit=4000):
    return re.sub(r"[ \t]+", " ", str(value or "")).strip()[:limit]


def _year(value):
    try:
        y = int(str(value).strip()[:4])
        return y if 1900 <= y <= 2100 else None
    except (TypeError, ValueError):
        return None


def _period(start, end, current):
    start, end = _year(start), _year(end)
    if current:
        return f"{start} – nu" if start else "Nu"
    if start and end:
        return str(start) if start == end else f"{start} – {end}"
    return str(start or end or "")


def _bullets(text):
    """Split a free-text description into CV bullets (lines / '•' / '- ')."""
    text = str(text or "").replace("\r", "")
    parts = re.split(r"\n+|\s•\s|^•\s", text)
    out = []
    for p in parts:
        p = re.sub(r"^[\s•\-–*]+", "", p).strip()
        if p:
            out.append(p)
    return out


def _years_of_experience(items, this_year):
    """Total years covered by experience, overlaps merged."""
    spans = []
    for e in items:
        s = _year(e.get("start_year"))
        if not s:
            continue
        end = this_year if e.get("is_current") else (_year(e.get("end_year")) or s)
        if end >= s:
            spans.append((s, end))
    spans.sort()
    total, cur = 0, None
    for s, e in spans:
        if cur is None:
            cur = [s, e]
        elif s <= cur[1]:
            cur[1] = max(cur[1], e)
        else:
            total += cur[1] - cur[0]
            cur = [s, e]
    if cur:
        total += cur[1] - cur[0]
    return total


def _cert_state(expiry, today):
    try:
        from cert_expiry_service import _parse_expiry
        d = _parse_expiry(expiry)
    except Exception:
        d = None
    if not d:
        return "gyldig", ""
    if d < today:
        return "udloebet", "Udløbet"
    if (d - today).days <= 90:
        return "udloeber", "Udløber snart"
    return "gyldig", ""


def _skill_key(name):
    try:
        from competency import skill_key
        return skill_key(name) or str(name or "").strip().lower()
    except Exception:
        return str(name or "").strip().lower()


def _score_to_level(score):
    try:
        from competency import score_to_level
        return score_to_level(score) or "mellem"
    except Exception:
        s = int(score or 0)
        return "ekspert" if s >= 5 else "avanceret" if s >= 4 else "mellem" if s >= 2 else "begynder"


def _role_skill_keys(role):
    if not role:
        return set()
    try:
        from competency import _role_skills
        return {_skill_key(s) for s in _role_skills(role.lower())}
    except Exception:
        return set()


def catalog_url(query):
    from urllib.parse import quote
    return "/catalog?q=" + quote(str(query or "")[:120])


# ── prefs ────────────────────────────────────────────────────────────────────

def normalise_prefs(raw):
    """Whitelist a prefs dict (client-sent or stored). Always returns a full dict."""
    out = dict(DEFAULT_PREFS)
    out["hidden"] = list(DEFAULT_PREFS["hidden"])
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return out
    if raw.get("template") in TEMPLATES:
        out["template"] = raw["template"]
    if isinstance(raw.get("hidden"), (list, tuple)):
        out["hidden"] = [k for k in SECTION_KEYS if k in set(map(str, raw["hidden"]))]
    for flag in ("targeted", "show_evidence"):
        if flag in raw:
            out[flag] = bool(raw[flag])
    return out


def load_prefs(username):
    try:
        from app1.user_profile_db import get_cv_prefs
        return normalise_prefs(get_cv_prefs(username))
    except Exception as e:
        logger.debug("cv prefs load failed: %s", e)
        return normalise_prefs(None)


def save_prefs(username, raw):
    """Persist normalised prefs. Returns ``(prefs, saved)``; never raises."""
    prefs = normalise_prefs(raw)
    try:
        from app1.user_profile_db import set_cv_prefs
        set_cv_prefs(username, json.dumps(prefs, ensure_ascii=False))
        return prefs, True
    except Exception as e:
        logger.warning("cv prefs save failed: %s", e)
        return prefs, False


# ── assembly (pure) ──────────────────────────────────────────────────────────

def assemble(profile, *, name="", email="", hr=None, gaps=None, orders=None, prefs=None, today=None):
    """Turn platform data into one structured CV document. Pure; never raises on odd rows.

    ``hr`` is a ``learner_context.build_learner_hr_context`` dict (skill matrix rows under
    ``matrix``, open ``assignments``); ``orders`` are ``{title, state}`` rows of the
    learner's own course orders; ``gaps`` is ``competency.compute_skill_gaps`` output.
    """
    p = profile or {}
    hr = hr or {}
    gaps = list(gaps or [])
    prefs = normalise_prefs(prefs)
    today = today or datetime.date.today()
    this_year = today.year
    target_role = _s(p.get("target_role"), 150)
    relevant = _role_skill_keys(target_role) | {_skill_key(g.get("skill")) for g in gaps if g.get("skill")}

    # Experience: current first, then most recent end/start year.
    experience = []
    for e in p.get("experience") or []:
        if not isinstance(e, dict) or not _s(e.get("title")):
            continue
        experience.append({
            "id": e.get("id"),
            "title": _s(e.get("title"), 255),
            "company": _s(e.get("company"), 255),
            "start_year": _year(e.get("start_year")),
            "end_year": _year(e.get("end_year")),
            "is_current": bool(e.get("is_current")),
            "period": _period(e.get("start_year"), e.get("end_year"), e.get("is_current")),
            "description": str(e.get("description") or "").strip(),
            "bullets": _bullets(e.get("description")),
        })
    experience.sort(key=lambda x: (x["is_current"], x["end_year"] or x["start_year"] or 0, x["start_year"] or 0),
                    reverse=True)

    education = []
    for e in p.get("education") or []:
        if not isinstance(e, dict) or not _s(e.get("degree")):
            continue
        education.append({
            "id": e.get("id"),
            "degree": _s(e.get("degree"), 255),
            "institution": _s(e.get("institution"), 255),
            "year": _year(e.get("year_completed")),
            "description": str(e.get("description") or "").strip(),
        })
    education.sort(key=lambda x: x["year"] or 0, reverse=True)

    # Skills: the person's own list, enriched with what the platform has seen.
    matrix = {}
    for r in hr.get("matrix") or []:
        try:
            lvl = int(r.get("current_level") or 0)
        except (TypeError, ValueError):
            lvl = 0
        if r.get("skill_name") and lvl > 0:
            matrix[_skill_key(r["skill_name"])] = (_s(r["skill_name"], 120), lvl)
    skills, seen = [], set()
    for s in p.get("skills") or []:
        if not isinstance(s, dict):
            continue
        nm = _s(s.get("name") or s.get("skill_name"), 120)
        if not nm:
            continue
        key = _skill_key(nm)
        if key in seen:
            continue
        seen.add(key)
        level = (s.get("level") or s.get("skill_level") or "mellem")
        level = level if level in LEVEL_LABELS else "mellem"
        evidence = []
        src = _SOURCE_EVIDENCE.get(str(s.get("source") or ""))
        if src:
            evidence.append({"kind": src[0], "label": src[1]})
        if key in matrix:
            evidence.append({"kind": "leder", "label": "Vurderet af leder"})
        skills.append({"name": nm, "level": level, "level_label": LEVEL_LABELS[level],
                       "category": _s(s.get("category"), 60), "evidence": evidence,
                       "relevant": key in relevant})
    for key, (nm, lvl) in matrix.items():
        if key in seen:
            continue
        seen.add(key)
        level = _score_to_level(lvl)
        skills.append({"name": nm, "level": level, "level_label": LEVEL_LABELS.get(level, "Mellem"),
                       "category": "", "evidence": [{"kind": "leder", "label": "Vurderet af leder"}],
                       "relevant": key in relevant})
    rank = {lv: i for i, lv in enumerate(LEVEL_ORDER)}
    skills.sort(key=lambda x: (rank.get(x["level"], 9), -len(x["evidence"]), x["name"].lower()))
    if prefs["targeted"] and relevant:
        skills.sort(key=lambda x: not x["relevant"])  # stable: keeps level order inside each half
    skill_groups = []
    for lv in LEVEL_ORDER:
        group = [s for s in skills if s["level"] == lv]
        if group:
            skill_groups.append({"level": lv, "label": LEVEL_LABELS[lv], "skills": group})

    certifications = []
    for c in p.get("certifications") or []:
        if not isinstance(c, dict) or not _s(c.get("name")):
            continue
        state, state_label = _cert_state(c.get("expiry_date"), today)
        url = _s(c.get("credential_url"), 500)
        certifications.append({
            "id": c.get("id"),
            "name": _s(c.get("name"), 255),
            "issuer": _s(c.get("issuer"), 255),
            "issue_date": _s(c.get("issue_date"), 20),
            "expiry_date": _s(c.get("expiry_date"), 20),
            "credential_id": _s(c.get("credential_id"), 120),
            "credential_url": url if _SAFE_URL_RE.match(url) else "",
            "state": state,
            "state_label": state_label,
        })

    courses = []
    for c in p.get("completed_courses") or []:
        if not isinstance(c, dict) or not _s(c.get("title")):
            continue
        handle = _s(c.get("handle") or c.get("course_handle"), 255)
        courses.append({
            "title": _s(c.get("title"), 255),
            "vendor": _s(c.get("vendor"), 255),
            "date": _s(c.get("completed_date"), 20),
            "handle": handle,
            "verified": bool(handle),
        })
    courses.sort(key=lambda c: (c["date"] or ""), reverse=True)

    in_progress = []
    for lp in p.get("learning_paths") or []:
        if not isinstance(lp, dict) or lp.get("status") not in (None, "", "aktiv"):
            continue
        steps = lp.get("steps") or []
        done = sum(1 for st in steps if isinstance(st, dict) and st.get("done"))
        if steps and done >= len(steps):
            continue
        in_progress.append({"title": _s(lp.get("title"), 255), "kind": "sti",
                            "detail": f"Læringssti, {done} af {len(steps)} trin" if steps else "Læringssti"})
    for a in hr.get("assignments") or []:
        title = _s(a.get("title"), 255)
        if title:
            pct = a.get("progress")
            in_progress.append({"title": title, "kind": "tildelt",
                                "detail": "Tildelt af arbejdsgiver" + (f", {int(pct)} %" if pct else "")})
    seen_titles = {i["title"].lower() for i in in_progress} | {c["title"].lower() for c in courses}
    for o in orders or []:
        title = _s(o.get("title"), 255)
        if title and o.get("state") in ("godkendt", "booket") and title.lower() not in seen_titles:
            seen_titles.add(title.lower())
            in_progress.append({"title": title, "kind": "tilmeldt", "detail": "Tilmeldt via Futurematch"})
    in_progress = in_progress[:8]

    languages = []
    for lang in p.get("languages") or []:
        if not isinstance(lang, dict) or not _s(lang.get("language")):
            continue
        prof = lang.get("proficiency") or "mellem"
        languages.append({"language": _s(lang.get("language"), 80), "proficiency": prof,
                          "label": LANG_LABELS.get(prof, _s(prof, 40))})
    languages.sort(key=lambda x: -_LANG_RANK.get(x["proficiency"], 0))

    links = []
    for link in p.get("portfolio_links") or []:
        if not isinstance(link, dict):
            continue
        url = _s(link.get("url"), 500)
        if not url:
            continue
        if _BARE_DOMAIN_RE.match(url):
            url = "https://" + url
        safe = bool(_SAFE_URL_RE.match(url))
        if not safe:
            continue  # javascript:/data:/mailto-less junk never reaches a CV, not even as text
        links.append({"label": _s(link.get("label"), 120) or url, "url": url, "safe": safe,
                      "display": re.sub(r"^https?://(www\.)?", "", url).rstrip("/") if safe else url})

    goals = [_s(g.get("title"), 200) for g in (p.get("learning_goals") or [])
             if isinstance(g, dict) and g.get("status") in (None, "", "aktiv") and _s(g.get("title"))]

    verified_courses = sum(1 for c in courses if c["verified"])
    evidenced_skills = sum(1 for s in skills if s["evidence"])
    stats = {
        "years": _years_of_experience(experience, this_year),
        "roles": len(experience),
        "skills": len(skills),
        "evidenced_skills": evidenced_skills,
        "courses": len(courses),
        "verified_courses": verified_courses,
        "certifications": len(certifications),
        "valid_certifications": sum(1 for c in certifications if c["state"] != "udloebet"),
        "languages": len(languages),
        "in_progress": len(in_progress),
    }

    # Target-role fit: how much of what the role asks for is already on the CV.
    fit = None
    if target_role or gaps:
        required = relevant
        have = {_skill_key(s["name"]) for s in skills}
        gap_keys = {_skill_key(g.get("skill")) for g in gaps if g.get("skill")}
        matched = sorted(s["name"] for s in skills if s["relevant"] and _skill_key(s["name"]) not in gap_keys)
        denominator = len(required) or 0
        pct = round(100 * (denominator - len(gap_keys & required)) / denominator) if denominator else None
        fit = {
            "role": target_role,
            "pct": pct,
            "matched": matched[:10],
            "gaps": [{"skill": g.get("skill"), "current": g.get("current_label") or "", "target": g.get("target_label") or "",
                      "source": g.get("source") or "", "priority": g.get("priority") or "",
                      "url": catalog_url(g.get("skill"))}
                     for g in gaps[:6] if g.get("skill")],
            "known": len(have & required),
        }

    has_content = any((_s(p.get("bio")), experience, education, skills, certifications, courses, languages, links))
    return {
        "name": _s(name, 120),
        "email": _s(email, 200) if "@" in str(email or "") else "",
        "headline": _s(p.get("headline"), 300),
        "target_role": target_role,
        "location": _s(p.get("preferred_location"), 120),
        "summary": str(p.get("bio") or "").strip(),
        "experience": experience,
        "education": education,
        "skills": skills,
        "skill_groups": skill_groups,
        "certifications": certifications,
        "courses": courses,
        "in_progress": in_progress,
        "languages": languages,
        "links": links,
        "goals": goals[:5],
        "fit": fit,
        "stats": stats,
        "prefs": prefs,
        "has_content": has_content,
        "generated": today.isoformat(),
    }


def visible_sections(cv):
    """Section keys to render, in order: not hidden and not empty."""
    hidden = set((cv.get("prefs") or {}).get("hidden") or [])
    present = {
        "summary": bool(cv.get("summary")),
        "experience": bool(cv.get("experience")),
        "education": bool(cv.get("education")),
        "skills": bool(cv.get("skills")),
        "certifications": bool(cv.get("certifications")),
        "courses": bool(cv.get("courses")),
        "in_progress": bool(cv.get("in_progress")),
        "languages": bool(cv.get("languages")),
        "links": bool(cv.get("links")),
        "direction": bool(cv.get("target_role") or cv.get("goals")),
    }
    return [k for k in SECTION_KEYS if present.get(k) and k not in hidden]


# ── analysis (pure) ──────────────────────────────────────────────────────────

def _check(key, label, ok, weight, advice="", action=None, severity="tip"):
    return {"key": key, "label": label, "ok": bool(ok), "weight": weight,
            "advice": "" if ok else advice, "action": None if ok else action,
            "severity": "ok" if ok else severity}


def analyse(cv):
    """Score the CV and say, per issue, what to do and where on the platform to do it.

    Returns ``{score, grade, checks, top, word_count}``. Each failing check carries an
    ``action`` the studio turns into a button: ``coach`` (AI rewrite of a section),
    ``import`` (CV import), ``chat`` (assistant with a prepared intent), ``catalog``
    (course search), ``edit`` (an inline field) or ``template``.
    """
    cv = cv or {}
    summary = cv.get("summary") or ""
    exp = cv.get("experience") or []
    skills = cv.get("skills") or []
    certs = cv.get("certifications") or []
    fit = cv.get("fit") or {}
    stats = cv.get("stats") or {}
    checks = []

    if not exp and not skills and not summary:
        checks.append(_check("start", "Kom i gang med dit CV", False, 30,
                             "Importér dit eksisterende CV, eller fortæl assistenten om dit arbejdsliv.",
                             {"kind": "import"}, "vigtig"))

    n = len(summary)
    checks.append(_check(
        "summary", "Profiltekst, der sælger dig", 250 <= n <= 1100, 12,
        ("Skriv 3-5 sætninger om hvad du kan, hvad du har opnået, og hvor du vil hen."
         if n < 250 else "Profilteksten er lang. Skær den ned til det, en læser husker."),
        {"kind": "coach", "section": "summary"} if summary else
        {"kind": "chat", "intent": "Hjælp mig med at skrive en profiltekst til mit CV"}, "vigtig"))
    checks.append(_check(
        "headline", "Tydelig overskrift", len(cv.get("headline") or "") >= 8, 6,
        "En kort titel under navnet (fx 'Projektleder med 8 års erfaring i byggeri').",
        {"kind": "coach", "section": "headline"} if (summary or exp) else {"kind": "edit", "field": "headline"}))
    checks.append(_check(
        "experience", "Erhvervserfaring", bool(exp), 14,
        "Tilføj dine job, eller importér dem fra dit nuværende CV.", {"kind": "import"}, "vigtig"))
    if exp:
        thin = [e for e in exp if len(e.get("description") or "") < 60]
        checks.append(_check(
            "experience_detail", "Hvert job beskriver dit ansvar", not thin, 12,
            f"{len(thin)} job mangler en beskrivelse af ansvar og resultater." if thin else "",
            {"kind": "coach", "section": "experience", "id": thin[0].get("id")} if thin else None, "vigtig"))
        described = [e for e in exp if e.get("description")]
        with_results = [e for e in described if _RESULT_RE.search(e["description"])]
        missing_results = [e for e in described if e not in with_results]
        checks.append(_check(
            "results", "Konkrete resultater", bool(described) and len(with_results) * 2 >= len(described), 10,
            "Vis effekten af dit arbejde: tal, omfang, hvad der blev bedre.",
            {"kind": "coach", "section": "experience",
             "id": (missing_results or described or exp)[0].get("id")}))
        years = sorted(((e.get("start_year"), e.get("end_year") or (9999 if e.get("is_current") else e.get("start_year")))
                        for e in exp if e.get("start_year")), key=lambda t: t[0])
        gap = None
        reach = None
        for s, e in years:
            if reach is not None and s - reach > 1:
                gap = (reach, s)
            reach = max(reach or e, e)
        checks.append(_check(
            "timeline", "Sammenhængende tidslinje", gap is None, 4,
            f"Der er et hul fra {gap[0]} til {gap[1]}. Uddannelse, orlov eller projekter kan forklare det."
            if gap else "",
            {"kind": "chat", "intent": "Hjælp mig med at forklare et hul i mit CV"}))
    checks.append(_check(
        "skills", "Bredt kompetencebillede", len(skills) >= 6, 8,
        "Nævn mindst 6 kompetencer med niveau: fagligt, værktøjer og personligt.",
        {"kind": "chat", "intent": "Hjælp mig med at finde de kompetencer, mit CV mangler"}))
    checks.append(_check(
        "evidence", "Kompetencer med bevis", stats.get("evidenced_skills", 0) > 0 or stats.get("verified_courses", 0) > 0, 8,
        "Kurser gennemført via Futurematch og kompetencer din leder har vurderet, vises som dokumenterede.",
        {"kind": "catalog", "query": (fit.get("gaps") or [{}])[0].get("skill") or cv.get("target_role") or ""}))
    checks.append(_check(
        "target_role", "Retning, CV'et sigter mod", bool(cv.get("target_role")), 6,
        "Sæt en ønsket rolle, så kan CV'et målrettes og vise, hvad der mangler.",
        {"kind": "edit", "field": "target_role"}))
    if fit.get("gaps"):
        top = fit["gaps"][0]["skill"]
        checks.append(_check(
            "fit", "Dækker din ønskede rolle", False, 6,
            f"Største gab mod {cv.get('target_role') or 'dine mål'}: {', '.join(g['skill'] for g in fit['gaps'][:3])}.",
            {"kind": "catalog", "query": top}))
    expired = [c for c in certs if c.get("state") == "udloebet"]
    if certs:
        checks.append(_check(
            "certs", "Gyldige certificeringer", not expired, 5,
            f"{expired[0]['name']} er udløbet. Forny den, eller marker den som historisk." if expired else "",
            {"kind": "catalog", "query": expired[0]["name"]} if expired else None))
    checks.append(_check(
        "education", "Uddannelse", bool(cv.get("education")), 5,
        "Tilføj din uddannelse, også korte eller faglige forløb.",
        {"kind": "chat", "intent": "Jeg vil tilføje min uddannelse til mit CV"}))
    checks.append(_check(
        "languages", "Sprog", bool(cv.get("languages")), 4,
        "Angiv dine sprog og niveau; mange stillinger spørger efter det.",
        {"kind": "chat", "intent": "Jeg vil tilføje mine sprog til mit CV"}))

    words = len(" ".join([summary] + [e.get("description") or "" for e in exp]).split())
    if words > 900 and (cv.get("prefs") or {}).get("template") != "kompakt":
        checks.append(_check("length", "Kan læses på to minutter", False, 0,
                             "CV'et er langt. Prøv den kompakte skabelon, eller kort de ældste job ned.",
                             {"kind": "template", "template": "kompakt"}))

    total = sum(c["weight"] for c in checks) or 1
    score = round(100 * sum(c["weight"] for c in checks if c["ok"]) / total)
    grade = ("Stærkt" if score >= 85 else "Godt på vej" if score >= 65
             else "Grundlæggende" if score >= 40 else "Tyndt")
    rank = {"vigtig": 0, "tip": 1, "ok": 2}
    failing = sorted([c for c in checks if not c["ok"]], key=lambda c: (rank[c["severity"]], -c["weight"]))
    return {"score": score, "grade": grade, "checks": checks, "top": failing[:3], "word_count": words}


# ── exports (pure) ───────────────────────────────────────────────────────────

def to_markdown(cv):
    """The CV as Markdown (also the base of the plain-text export)."""
    cv = cv or {}
    out = [f"# {cv.get('name') or 'Mit CV'}"]
    if cv.get("headline"):
        out.append(f"**{cv['headline']}**")
    contact = " · ".join(x for x in (cv.get("email"), cv.get("location")) if x)
    if contact:
        out.append(contact)
    for key in visible_sections(cv):
        out.append("")
        out.append(f"## {SECTION_LABELS[key]}")
        if key == "summary":
            out.append(cv["summary"])
        elif key == "experience":
            for e in cv["experience"]:
                head = e["title"] + (f", {e['company']}" if e["company"] else "")
                out.append(f"### {head}" + (f" ({e['period']})" if e["period"] else ""))
                out.extend(f"- {b}" for b in e["bullets"])
        elif key == "education":
            for e in cv["education"]:
                line = e["degree"] + (f", {e['institution']}" if e["institution"] else "")
                out.append(f"- {line}" + (f" ({e['year']})" if e["year"] else ""))
        elif key == "skills":
            for g in cv["skill_groups"]:
                out.append(f"- **{g['label']}:** " + ", ".join(s["name"] for s in g["skills"]))
        elif key == "certifications":
            for c in cv["certifications"]:
                bits = [c["issuer"], c["issue_date"]]
                line = c["name"] + "".join(f", {b}" for b in bits if b)
                if c["state_label"]:
                    line += f" ({c['state_label'].lower()})"
                out.append(f"- {line}")
        elif key == "courses":
            for c in cv["courses"]:
                line = c["title"] + "".join(f", {b}" for b in (c["vendor"], c["date"]) if b)
                out.append(f"- {line}")
        elif key == "in_progress":
            out.extend(f"- {i['title']} ({i['detail']})" for i in cv["in_progress"])
        elif key == "languages":
            out.append(", ".join(f"{lang['language']} ({lang['label'].lower()})" for lang in cv["languages"]))
        elif key == "links":
            out.extend(f"- {lnk['label']}: {lnk['url']}" for lnk in cv["links"])
        elif key == "direction":
            if cv.get("target_role"):
                out.append(f"Ønsket rolle: {cv['target_role']}")
            out.extend(f"- {g}" for g in cv.get("goals") or [])
    return "\n".join(out).strip() + "\n"


def to_text(cv):
    """Plain text for pasting into job portals: Markdown markers stripped."""
    md = to_markdown(cv)
    lines = []
    for line in md.splitlines():
        if line.startswith("## "):
            title = line[3:].upper()
            if lines and lines[-1] != "":
                lines.append("")
            lines.extend([title, "-" * len(title)])
            continue
        line = re.sub(r"^#{1,3} ", "", line)
        line = line.replace("**", "")
        line = re.sub(r"^- ", "• ", line)
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


def chat_card(cv, quality):
    """The compact payload the assistant's show_cv_summary card renders."""
    cv = cv or {}
    stats = cv.get("stats") or {}
    counts = {
        "skills": stats.get("skills", 0),
        "experience": stats.get("roles", 0),
        "education": len(cv.get("education") or []),
        "certifications": stats.get("certifications", 0),
        "languages": stats.get("languages", 0),
        "courses": stats.get("courses", 0),
    }
    return {
        "sections": {
            "skills": [{"name": s["name"], "level": s["level"], "verified": bool(s["evidence"])}
                       for s in (cv.get("skills") or [])[:6]],
            "experience": [{"title": e["title"], "company": e["company"], "years": e["period"]}
                           for e in (cv.get("experience") or [])[:4]],
            "education": [{"degree": e["degree"], "institution": e["institution"], "year": str(e["year"] or "")}
                          for e in (cv.get("education") or [])[:3]],
            "certifications": [{"name": c["name"], "issuer": c["issuer"], "state": c["state"]}
                               for c in (cv.get("certifications") or [])[:4]],
            "languages": [{"language": lang["language"], "proficiency": lang["proficiency"]}
                          for lang in cv.get("languages") or []],
        },
        "counts": counts,
        "total": sum(counts.values()),
        "has_cv": bool(cv.get("has_content")),
        "headline": cv.get("headline") or "",
        "target_role": cv.get("target_role") or "",
        "years": stats.get("years", 0),
        "verified": stats.get("verified_courses", 0) + stats.get("evidenced_skills", 0),
        "score": (quality or {}).get("score", 0),
        "grade": (quality or {}).get("grade", ""),
        "next_steps": [c["label"] + (": " + c["advice"] if c.get("advice") else "")
                       for c in (quality or {}).get("top") or []],
        "fit": {"pct": (cv.get("fit") or {}).get("pct"),
                "gaps": [g["skill"] for g in ((cv.get("fit") or {}).get("gaps") or [])[:3]]},
    }


# ── loaders (Flask + DB, guarded) ────────────────────────────────────────────

def _display_name(username, company_id):
    try:
        from flask import current_app
        from person_names import display_name
        cur = current_app.mysql.connection.cursor()
        try:
            name = display_name(cur, company_id, username=username, default="")
        finally:
            cur.close()
        if name and name != username:
            return name
    except Exception as e:
        logger.debug("cv display name: %s", e)
    local = str(username or "").split("@")[0]
    parts = [w for w in re.split(r"[._\-]+", local) if w]
    return " ".join(w.capitalize() for w in parts) or str(username or "")


def _hr_data(username, company_id, user_id):
    out = {"matrix": [], "assignments": []}
    try:
        import learner_context as lc
        ctx = lc.build_learner_hr_context(username, user_id=user_id, company_id=company_id)
        out["assignments"] = ctx.get("assignments") or []
        emp = ctx.get("employee") or {}
        uid, cid = emp.get("user_id") or user_id, emp.get("company_id")
        if uid and cid:
            out["matrix"] = lc._load_skill_matrix(uid, cid) or []
    except Exception as e:
        logger.debug("cv hr data: %s", e)
    return out


def _orders(username, user_id):
    """The learner's own approved/booked course orders, as {title, state}."""
    try:
        import MySQLdb.cursors
        from flask import current_app
        from futurematch_ui import _ORDER_STATE
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            cur.execute(
                "SELECT product_title, status FROM course_orders "
                "WHERE (username = %s OR (user_id IS NOT NULL AND user_id = %s)) "
                "ORDER BY created_at DESC LIMIT 40",
                (username, user_id),
            )
            rows = cur.fetchall() or []
        finally:
            cur.close()
        return [{"title": r.get("product_title") or "", "state": _ORDER_STATE.get(r.get("status") or "", "")}
                for r in rows]
    except Exception as e:
        logger.debug("cv orders: %s", e)
        try:
            from flask import current_app
            current_app.mysql.connection.rollback()
        except Exception:
            pass
        return []


def build_cv(username, *, company_id=None, user_id=None, prefs=None, profile=None):
    """Load everything the platform knows about ``username`` and assemble the CV.

    Returns ``(cv, quality)``. Never raises: a failing source just leaves its part out.
    """
    if profile is None:
        try:
            from app1.user_profile_db import ensure_tables, get_full_profile
            ensure_tables()
            profile = get_full_profile(username) or {}
        except Exception as e:
            logger.warning("cv profile load failed: %s", e)
            profile = {}
    if prefs is None:
        prefs = load_prefs(username)
    try:
        from competency import compute_skill_gaps
        gaps = compute_skill_gaps(username, profile=profile) or []
    except Exception:
        gaps = []
    cv = assemble(
        profile,
        name=_display_name(username, company_id),
        email=username,
        hr=_hr_data(username, company_id, user_id),
        gaps=gaps,
        orders=_orders(username, user_id),
        prefs=prefs,
    )
    return cv, analyse(cv)


def build_for_session(**kw):
    """``build_cv`` for the logged-in user (identity from the session only)."""
    from flask import session
    return build_cv(session.get("user"), company_id=session.get("company_id"),
                    user_id=session.get("user_id"), **kw)
