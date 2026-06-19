"""competency.py — the missing "B" in the A→C learner journey.

The platform has strong *capture* (CV portal, profiler, profile page) and strong
*act* (recommender, learning paths) bolted onto a **missing middle**: there was
no competency model and no per-learner skill-gap computation. Recommendations
therefore reasoned over "skills you rated low" instead of "the gap between where
you are and where you want to go". This module is that middle, and the single
source of truth for turning free-text skills into a clean competency signal the
AI can reason over:

  * ``canonical_skill()``  — case-fold, trim, alias → canonical display name, so
    "python"/"Python3"/"PYTHON" collapse to one skill and "JS" == "JavaScript".
  * ``skill_category()``   — bucket a skill into a Danish category for grouping.
  * ``level_to_score()`` / ``score_to_level()`` — bridge the employee 4-label
    enum (begynder/mellem/avanceret/ekspert) onto the canonical **1-5 scale**
    the HR matrix + ``company_skill_targets`` already live on. The scale itself
    is owned by ``hr_tools.SKILL_LEVEL_MAP`` (begynder=1, mellem=2, avanceret=4,
    ekspert=5) — this module REUSES it, never forks it, so employee self-reports
    and HR targets can never flip a gap's sign.
  * ``compute_skill_gaps(username)`` — the per-learner gap engine: required
    competencies (company targets + target role + learning goals) minus current
    competencies, on the 1-5 scale. This is what makes recommendations
    gap-grounded.

Design constraints (mirror ``skill_history`` / the DB layer):
  * FULLY GUARDED — never raise into a caller; degrade to a safe default ([] / "").
  * OFFLINE-SAFE — the canon + scale are pure functions; the gap engine does a
    couple of bounded, company-scoped SELECTs and tolerates a missing DB (so it
    yields role/goal gaps even with no database).
  * REUSES, never duplicates, the canonical 1-5 scale (``hr_tools``).
"""

import logging

logger = logging.getLogger(__name__)

# ── Canonical 1-5 scale — REUSE hr_tools, never fork it ──
# hr_tools.SKILL_LEVEL_MAP is the single source of truth ({begynder:1, mellem:2,
# avanceret:4, ekspert:5}); _skill_level_to_int is idempotent on ints (the HR
# matrix case) and maps Danish labels exactly once. We import lazily-with-fallback
# so this module stays importable even if hr_tools (which pulls flask/MySQLdb) is
# unavailable in some minimal context.
try:  # pragma: no cover - exercised in the full app
    from hr_tools import SKILL_LEVEL_MAP as _HR_SCALE, _skill_level_to_int as _hr_to_int
except Exception:  # pragma: no cover - minimal/offline import fallback
    _HR_SCALE = {"begynder": 1, "mellem": 2, "avanceret": 4, "ekspert": 5}

    def _hr_to_int(value, default=0):
        if value is None or isinstance(value, bool):
            return default
        if isinstance(value, (int, float)):
            return max(0, min(5, int(value)))
        text = str(value).strip().lower()
        if not text:
            return default
        if text.isdigit():
            return max(0, min(5, int(text)))
        return _HR_SCALE.get(text, default)


# The employee-facing enum, ordered weakest→strongest (for UI + score_to_level).
LEVELS = ("begynder", "mellem", "avanceret", "ekspert")


def level_to_score(level, default=0):
    """Map any skill level (Danish label OR already-int) to the canonical 1-5
    score. Idempotent on ints (delegates to the HR normalizer)."""
    return _hr_to_int(level, default=default)


def score_to_level(score):
    """Map a 1-5 score back to the nearest employee enum label (for storage)."""
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "begynder"
    if s >= 5:
        return "ekspert"
    if s >= 4:
        return "avanceret"
    if s >= 2:
        return "mellem"
    return "begynder"


def score_label_da(score):
    """Human Danish label for a 1-5 score, incl. 0 → 'ingen' (used in gap cards)."""
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "ukendt"
    if s <= 0:
        return "ingen"
    if s >= 5:
        return "ekspert"
    if s >= 4:
        return "avanceret"
    if s >= 2:
        return "mellem"
    return "begynder"


# ── Skill name canonicalization ──
# Acronyms render upper-case; they also double as aliases (lower → UPPER).
_ACRONYMS = {
    "sql", "html", "css", "api", "aws", "gcp", "it", "hr", "ux", "ui", "seo",
    "sem", "php", "crm", "erp", "etl", "ai", "ml", "bi", "qa", "saas", "iso",
    "gdpr", "pmp", "scrum", "ci", "cd", "vpn", "dns", "json", "xml", "rest",
    "tcp", "ip", "sap", "vba", "kpi", "roi", "b2b", "b2c", "cms", "orm",
}

# Lowercased alias → canonical display name. Curated + conservative; collapses
# the most common case/spelling variants so the gap engine and dedupe see one
# skill. Acronyms are added automatically below.
_ALIASES = {
    "js": "JavaScript", "javascript": "JavaScript", "ecmascript": "JavaScript",
    "ts": "TypeScript", "typescript": "TypeScript",
    "py": "Python", "python3": "Python", "python 3": "Python", "python": "Python",
    "reactjs": "React", "react.js": "React", "react js": "React", "react": "React",
    "vuejs": "Vue", "vue.js": "Vue",
    "nodejs": "Node.js", "node.js": "Node.js", "node js": "Node.js", "node": "Node.js",
    "k8s": "Kubernetes", "kubernetes": "Kubernetes",
    "ml": "Machine Learning", "machine learning": "Machine Learning",
    "maskinlæring": "Machine Learning",
    "kunstig intelligens": "AI",
    "power bi": "Power BI", "powerbi": "Power BI",
    "powerpoint": "PowerPoint", "power point": "PowerPoint",
    "ms excel": "Excel", "microsoft excel": "Excel", "excel": "Excel",
    "word": "Word", "ms word": "Word",
    "c sharp": "C#", "csharp": "C#", "c#": "C#",
    "c++": "C++", "cpp": "C++",
    ".net": ".NET", "dotnet": ".NET", "dot net": ".NET",
    "golang": "Go",
    "projektledelse": "Projektledelse", "project management": "Projektledelse",
    "agil": "Agile", "agile": "Agile",
    "ledelse": "Ledelse", "leadership": "Ledelse", "management": "Ledelse",
    "kommunikation": "Kommunikation", "communication": "Kommunikation",
    "forhandling": "Forhandling", "negotiation": "Forhandling",
    "statistik": "Statistik", "statistics": "Statistik",
    "dataanalyse": "Dataanalyse", "data analysis": "Dataanalyse",
    "data visualisering": "Datavisualisering", "datavisualisering": "Datavisualisering",
    "it-sikkerhed": "IT-sikkerhed", "it sikkerhed": "IT-sikkerhed",
    "cybersecurity": "Cybersecurity", "cyber security": "Cybersecurity",
    "salg": "Salg", "sales": "Salg",
    "regnskab": "Regnskab", "bogføring": "Bogføring",
    "design thinking": "Design Thinking",
}
# Acronyms are self-aliasing (lower → UPPER) unless already mapped above.
for _ac in _ACRONYMS:
    _ALIASES.setdefault(_ac, _ac.upper())


def _titlecase(text):
    """Capitalize words, upper-casing known acronyms; preserve symbol tokens."""
    out = []
    for word in text.split(" "):
        if not word:
            continue
        low = word.lower()
        if low in _ACRONYMS:
            out.append(low.upper())
        elif word[0].isalpha():
            out.append(word[0].upper() + word[1:])
        else:
            out.append(word)
    return " ".join(out)


def canonical_skill(name):
    """Normalize a free-text skill into one canonical display form.

    Order: collapse whitespace → alias map → preserve deliberate mixed-case
    (iOS, DevOps, JavaScript typed correctly) → title-case with acronym fixups.
    Returns "" for empty/garbage input. Capped to 100 chars (DB-safe).
    """
    if not name:
        return ""
    s = " ".join(str(name).split())
    if not s:
        return ""
    s = s[:100]
    low = s.lower()
    if low in _ALIASES:
        return _ALIASES[low]
    # Trust deliberate internal capitals (iOS, DevOps, JavaScript).
    if any(c.isupper() for c in s[1:]):
        return s
    return _titlecase(s)


def skill_key(name):
    """Case-insensitive dedupe/match key for a skill (canonical, lowercased)."""
    return canonical_skill(name).lower()


# ── Categories (Danish; soft grouping for the UI + gap cards) ──
# Ordered: a skill takes the first category whose keyword it contains.
_CATEGORY_RULES = [
    ("Data & AI", ("machine learning", "statistik", "statistics", "dataanaly",
                   "datavisual", "power bi", "tableau", "excel", "analytics",
                   "pandas", "big data", "spark", " ai", "ai ", "kunstig intel")),
    ("Cloud & DevOps", ("cloud", "aws", "azure", "gcp", "devops", "docker",
                        "kubernetes", "terraform", "ci/cd", "linux", "netværk",
                        "server", "vpn", "dns")),
    ("Programmering", ("python", "java", "typescript", "c++", "c#", ".net", "go",
                       "rust", "php", "ruby", "swift", "kotlin", "react", "vue",
                       "angular", "node", "html", "css", "sql", "programmer",
                       "kodning", "udvikling", "git", "api", "rest")),
    ("Design & UX", ("design", "ux", "ui", "figma", "sketch", "adobe",
                     "photoshop", "illustrator", "grafisk")),
    ("Projektledelse", ("projektledelse", "project management", "prince2", "pmp",
                        "scrum", "agile", "kanban", "risikostyring")),
    ("Sikkerhed", ("sikkerhed", "security", "gdpr", "iso 27001", "compliance",
                   "databeskyttelse")),
    ("Marketing", ("marketing", "markedsføring", "seo", "sem", "content",
                   "sociale medier", "social media", "branding")),
    ("Forretning & Salg", ("salg", "sales", "forhandling", "crm", "økonomi",
                           "finance", "budget", "regnskab", "bogføring",
                           "forretning", "business", "erp")),
    ("Ledelse", ("ledelse", "leadership", "management", "coaching", "strategi",
                 "motivation", "konflikt", "team")),
    ("Kommunikation", ("kommunikation", "communication", "præsentation",
                       "formidling", "skrivning")),
    ("Sprog", ("engelsk", "tysk", "fransk", "spansk", "dansk", "sprog")),
]


def skill_category(name):
    """Return a Danish category label for a skill (default 'Andet')."""
    key = skill_key(name)
    if not key:
        return "Andet"
    for cat, kws in _CATEGORY_RULES:
        for kw in kws:
            if kw in key:
                return cat
    return "Andet"


# ── target_role → required-skill hints (curated, conservative seed) ──
# Matched as substrings against a lowercased role string; multiple may match.
ROLE_SKILL_HINTS = {
    "data analyst": ["SQL", "Python", "Statistik", "Excel", "Power BI", "Datavisualisering"],
    "dataanalytiker": ["SQL", "Python", "Statistik", "Excel", "Power BI", "Datavisualisering"],
    "data scientist": ["Python", "Machine Learning", "Statistik", "SQL", "Dataanalyse"],
    "data engineer": ["Python", "SQL", "ETL", "Cloud", "Big Data"],
    "dataingeniør": ["Python", "SQL", "ETL", "Cloud", "Big Data"],
    "projektleder": ["Projektledelse", "PRINCE2", "Agile", "Scrum", "Risikostyring", "Kommunikation"],
    "project manager": ["Projektledelse", "PRINCE2", "Agile", "Scrum", "Risikostyring", "Kommunikation"],
    "udvikler": ["Programmering", "Git", "API", "SQL"],
    "developer": ["Programmering", "Git", "API", "SQL"],
    "programmør": ["Programmering", "Git", "API", "SQL"],
    "frontend": ["JavaScript", "React", "CSS", "HTML", "TypeScript"],
    "backend": ["Python", "SQL", "API", "Node.js"],
    "leder": ["Ledelse", "Kommunikation", "Coaching", "Strategi", "Budget"],
    "manager": ["Ledelse", "Kommunikation", "Coaching", "Strategi", "Budget"],
    "teamleder": ["Ledelse", "Kommunikation", "Coaching", "Konflikthåndtering"],
    "marketing": ["SEO", "Content", "Google Analytics", "Sociale Medier", "Branding"],
    "markedsføring": ["SEO", "Content", "Google Analytics", "Sociale Medier", "Branding"],
    "designer": ["UX", "UI", "Figma", "Design Thinking"],
    "ux": ["UX", "UI", "Figma", "Design Thinking"],
    "hr": ["Rekruttering", "GDPR", "Arbejdsret", "Kommunikation"],
    "human resources": ["Rekruttering", "GDPR", "Arbejdsret", "Kommunikation"],
    "sælger": ["Salg", "Forhandling", "CRM", "Kommunikation"],
    "sales": ["Salg", "Forhandling", "CRM", "Kommunikation"],
    "konsulent": ["Kommunikation", "Forretning", "Projektledelse"],
    "cybersecurity": ["IT-sikkerhed", "Netværk", "ISO 27001", "Risikostyring"],
    "sikkerhed": ["IT-sikkerhed", "Netværk", "ISO 27001", "Risikostyring"],
    "økonomi": ["Regnskab", "Budget", "Excel", "Finance"],
    "controller": ["Regnskab", "Budget", "Excel", "Finance"],
}

# Tokens we can confidently spot inside free-text goals → a desired skill.
_GOAL_SKILL_TOKENS = sorted(
    {k for k in _ALIASES if len(k) >= 3}
    | {v.lower() for v in _ALIASES.values() if len(v) >= 3},
    key=len, reverse=True,
)

_SOURCE_RANK = {"company": 3, "role": 2, "goal": 1}
_PRIORITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1}
_DEFAULT_ROLE_TARGET = 4   # avanceret — a credible bar for a target role's core skills
_DEFAULT_GOAL_TARGET = 4


def _role_skills(role_lower):
    out = []
    seen = set()
    for hint, skills in ROLE_SKILL_HINTS.items():
        if hint in role_lower:
            for sk in skills:
                k = sk.lower()
                if k not in seen:
                    seen.add(k)
                    out.append(sk)
    return out


def _skills_in_text(text, *, limit=5):
    """Spot known skill tokens inside free-text (a learning goal / goals blob)."""
    if not text:
        return []
    low = " " + str(text).lower() + " "
    out = []
    seen = set()
    for tok in _GOAL_SKILL_TOKENS:
        if tok in low:
            canon = canonical_skill(tok)
            k = canon.lower()
            if k and k not in seen:
                seen.add(k)
                out.append(canon)
        if len(out) >= limit:
            break
    return out


def _company_targets(username):
    """Company skill targets for the learner's company/department, or [].

    Fully guarded + offline-safe: any DB problem (no app context, no MySQL,
    missing table) degrades to no company targets so the gap engine still
    yields role/goal gaps.
    """
    if not username:
        return []
    try:
        import MySQLdb.cursors
        from flask import current_app
        conn = current_app.mysql.connection
        cur = conn.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            "SELECT cu.company_id, cu.department FROM company_users cu "
            "JOIN users u ON cu.user_id = u.id "
            "WHERE u.username = %s AND cu.status = 'active' LIMIT 1",
            (username,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return []
        company_id = row.get("company_id")
        dept = row.get("department")
        cur.execute(
            "SELECT skill_name, target_level, priority FROM company_skill_targets "
            "WHERE company_id = %s AND (department = %s OR department IS NULL OR department = '')",
            (company_id, dept or ""),
        )
        rows = cur.fetchall()
        cur.close()
        return list(rows or [])
    except Exception as e:
        logger.debug("competency: company targets unavailable for %s: %s", username, e)
        return []


def compute_skill_gaps(username, profile=None, *, limit=12):
    """The per-learner skill-gap engine — the keystone for gap-grounded AI.

    Required competencies are derived from three sources, most authoritative
    first:
      1. ``company_skill_targets`` (the learner's company/department) — real
         1-5 targets, source 'company'.
      2. ``target_role`` → curated required skills (``ROLE_SKILL_HINTS``),
         target = avanceret (4), source 'role'.
      3. learning goals (free text) → recognised skill tokens, source 'goal'.

    Each required skill is diffed against the learner's current level (their
    ``user_skills``, canonicalized + mapped onto the 1-5 scale). Only positive
    gaps are returned, sorted by priority → gap size → source authority.

    Returns a list of dicts (never raises):
      {skill, category, current_level, current_label, target_level,
       target_label, gap, source, priority}
    """
    try:
        if profile is None:
            from app1.user_profile_db import get_full_profile
            profile = get_full_profile(username) or {}
    except Exception:
        profile = profile or {}
    profile = profile or {}

    # Current competence: canonical key → best (highest) score seen.
    current = {}
    for s in (profile.get("skills") or []):
        nm = s.get("name") or s.get("skill_name") if isinstance(s, dict) else None
        if not nm:
            continue
        canon = canonical_skill(nm)
        if not canon:
            continue
        key = canon.lower()
        score = level_to_score(s.get("level") or s.get("skill_level"))
        if key not in current or score > current[key]["score"]:
            current[key] = {"display": canon, "score": score}

    # Required competence: canonical key → strongest target / most authoritative source.
    required = {}

    def _want(display, target, source, priority):
        canon = canonical_skill(display)
        if not canon:
            return
        key = canon.lower()
        try:
            target = max(1, min(5, int(target)))
        except (TypeError, ValueError):
            target = _DEFAULT_ROLE_TARGET
        ex = required.get(key)
        if ex is None:
            required[key] = {"display": canon, "target": target,
                             "source": source, "priority": priority}
            return
        # Keep the strongest target; let the more authoritative source own the row.
        new_target = max(target, ex["target"])
        if _SOURCE_RANK.get(source, 0) >= _SOURCE_RANK.get(ex["source"], 0):
            required[key] = {"display": ex["display"], "target": new_target,
                             "source": source, "priority": priority}
        else:
            ex["target"] = new_target

    for t in _company_targets(username):
        _want(t.get("skill_name"), t.get("target_level") or 3, "company",
              (t.get("priority") or "medium").lower())

    role = (profile.get("target_role") or "").strip().lower()
    if role:
        for sk in _role_skills(role):
            _want(sk, _DEFAULT_ROLE_TARGET, "role", "medium")

    goal_blobs = []
    if (profile.get("goals") or "").strip():
        goal_blobs.append(profile["goals"])
    for g in (profile.get("learning_goals") or []):
        if not isinstance(g, dict):
            continue
        if g.get("status") in (None, "", "aktiv"):
            goal_blobs.append(g.get("title") or "")
    for blob in goal_blobs:
        for sk in _skills_in_text(blob):
            _want(sk, _DEFAULT_GOAL_TARGET, "goal", "low")

    gaps = []
    for key, info in required.items():
        cur = current.get(key, {"display": info["display"], "score": 0})
        gap = info["target"] - cur["score"]
        if gap <= 0:
            continue
        gaps.append({
            "skill": info["display"],
            "category": skill_category(info["display"]),
            "current_level": cur["score"],
            "current_label": score_label_da(cur["score"]),
            "target_level": info["target"],
            "target_label": score_label_da(info["target"]),
            "gap": gap,
            "source": info["source"],
            "priority": info["priority"],
        })

    gaps.sort(
        key=lambda g: (_PRIORITY_RANK.get(g["priority"], 1), g["gap"],
                       _SOURCE_RANK.get(g["source"], 0)),
        reverse=True,
    )
    return gaps[:limit]


def gaps_to_query(gaps, *, limit=4):
    """Compact search query (top gap skills) for grounding the recommender."""
    return " ".join(g["skill"] for g in (gaps or [])[:limit])
