"""Cross-surface handoff context for the learner AI (chat, profiler, mind-map, profile, CV).

The learner moves between five surfaces that share one profile and one memory:
the AI assistant (`/chat`; `/ai-profiler` redirects there), the Mind-Map,
the profile page and its CV import. When one surface hands the user to an AI
surface ("Spørg AI om dette" on a mind-map node, "Uddyb med AI" on a profile
section, "Gennemgå med AI" after a CV upload), the target surface should know
*where the user came from and what they were looking at*, the way the HR panel
knows its page (`hr_agent.handle_hr_ask(page=...)`).

Contract:
  * The browser sends ``context = {"from": <surface>, "focus": <ref>}`` with the
    FIRST message after a handoff only (``chat.js`` reads ``?from=`` / ``?focus=``).
  * ``normalize_context`` whitelists both values. ``from`` must be a key of
    ``SURFACES``; ``focus`` is either ``section:<key>`` (a key of ``SECTIONS``)
    or a mind-map node id (``skill:42``, ``exp:7``, ``mem:3`` ...), the same
    vocabulary ``open_in_app(open_mind_map, node=...)`` and the mind-map deep
    link ``#n=`` already use. Anything else is dropped.
  * ``resolve_focus`` looks the id up in the user's OWN profile and memories.
    An id that does not resolve is dropped: the client can never inject text,
    only point at something the user already owns.
  * ``build_layer`` returns ``(header, body, fenced)`` for the ``surface_context``
    context layer. The header is platform-authored; the body quotes profile
    text and is fenced as data when it does. The wording is need-driven (a
    natural place to start, never a task to finish).
  * ``origin_tool_names`` maps origin + focus to READ-ONLY tools the selector
    may add, mirroring ``ai_tool_registry._HR_PAGE_TOOLS``: arriving from a page
    says what the user looked at, not that they want a write.

Pure functions, no Flask or DB imports; the agent passes the profile it already
fetched for the turn.
"""
import hashlib
import re

# Surfaces a handoff may come from -> how the AI refers to it (Danish).
SURFACES = {
    "chat": "AI-assistenten",
    "profiler": "AI-assistenten",
    "mind_map": "Mind-Map (overblikket over det, AI'en ved om brugeren)",
    "profile": "profilsiden",
    "cv_upload": "CV-importen på profilsiden",
    "my_learning": "Min læring",
    "goals": "Mine mål",
    "timeline": "tidslinjen med frister",
}

# Profile sections a handoff may point at -> Danish label + profile key(s) counted.
SECTIONS = {
    "summary": ("Om mig (overskrift, bio og præferencer)", ()),
    "target_role": ("ønsket retning", ()),
    "skills": ("kompetencer", ("skills",)),
    "experience": ("erhvervserfaring", ("experience",)),
    "education": ("uddannelse", ("education",)),
    "certifications": ("certificeringer", ("certifications",)),
    "languages": ("sprog", ("languages",)),
    "portfolio": ("portfolio og links", ("portfolio_links",)),
    "goals": ("mål", ("learning_goals",)),
    "courses": ("gennemførte kurser", ("completed_courses",)),
    "learning-paths": ("læringsstier", ("learning_paths",)),
    "memories": ("det AI'en husker (hukommelse)", ()),
}

# Node-id prefixes the resolver understands (mind-map vocabulary, api.get_mindmap_api).
_FOCUS_RE = re.compile(r"^(section|skill|exp|edu|cert|lang|goal|link|path|course|mem):([A-Za-z0-9_\-]{1,40})$")

# Tools worth offering for an origin / focus kind. Reads, plus forget_about_user,
# which only PROPOSES (the removal runs on the user's confirm click). Writes never
# come from a handoff: they stay behind their own keyword gate and confirm card.
_ORIGIN_TOOLS = {
    "mind_map": ("show_mindmap_preview", "recall_about_user"),
    "profile": ("show_cv_summary", "show_skill_gaps"),
    "cv_upload": ("show_cv_summary", "show_skill_gaps", "recommend_for_profile"),
    "my_learning": ("get_my_agenda",),
    "goals": ("get_learning_goals", "track_goal_progress"),
    "timeline": ("get_my_agenda",),
}
_FOCUS_TOOLS = {
    "skill": ("show_skill_gaps", "recommend_for_profile"),
    "goal": ("get_learning_goals", "track_goal_progress", "recommend_for_profile"),
    "path": ("get_learning_path",),
    "mem": ("recall_about_user", "forget_about_user"),
    "cert": ("find_certification_path",),
    "course": ("get_course_sequel",),
}
_SECTION_TOOLS = {
    "skills": ("show_skill_gaps", "recommend_for_profile"),
    "goals": ("get_learning_goals", "track_goal_progress"),
    "learning-paths": ("get_learning_path",),
    "memories": ("show_mindmap_preview", "recall_about_user", "forget_about_user"),
}


def normalize_context(raw):
    """Whitelist a client-sent context dict. Returns ``{}`` or ``{"from", "focus"}``
    with only the keys that passed (each may be missing)."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    origin = str(raw.get("from") or "").strip().lower()
    if origin in SURFACES:
        out["from"] = origin
    focus = str(raw.get("focus") or "").strip()
    m = _FOCUS_RE.match(focus)
    if m and (m.group(1) != "section" or m.group(2) in SECTIONS):
        out["focus"] = focus
    return out


def focus_kind(ctx):
    """``"skill"`` for ``skill:42``, ``"section"`` for ``section:skills``, else ``""``."""
    focus = (ctx or {}).get("focus") or ""
    return focus.split(":", 1)[0] if ":" in focus else ""


def _stable_id(prefix, value):
    """Same hash as ``api.get_mindmap_api.stable_text_id`` (skills/courses without a row id)."""
    digest = hashlib.sha1(str(value or "").casefold().encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _matches(prefix, ref, row, text_key):
    """A row matches ``prefix:ref`` by its numeric id, or by the stable text hash."""
    rid = row.get("id")
    if rid is not None and str(rid) == ref:
        return True
    return _stable_id(prefix, row.get(text_key)) == f"{prefix}:{ref}"


def _years(e):
    start, end = e.get("start_year"), e.get("end_year")
    if e.get("is_current"):
        return f"{start or '?'}–nu"
    if start or end:
        return f"{start or '?'}–{end or '?'}"
    return ""


def resolve_focus(focus, profile, memories=None, gaps=None):
    """Look a focus ref up in the user's own data.

    Returns ``{"kind", "label", "lines": [...], "section": <section key>}`` or
    ``None`` when the ref does not resolve. ``lines`` are short Danish facts
    quoting profile text (untrusted; the caller fences them).
    """
    m = _FOCUS_RE.match(str(focus or ""))
    if not m:
        return None
    kind, ref = m.group(1), m.group(2)
    p = profile or {}

    if kind == "section":
        if ref not in SECTIONS:
            return None
        label, keys = SECTIONS[ref]
        count = sum(len(p.get(k) or []) for k in keys)
        if ref == "memories":
            count = len(memories or [])
        return {"kind": "section", "label": label, "section": ref, "count": count,
                "lines": []}

    if kind == "skill":
        row = next((s for s in p.get("skills") or [] if _matches("skill", ref, s, "name")), None)
        if not row:
            return None
        name = row.get("name") or ""
        lines = [f"Kompetence: {name} (niveau: {row.get('level') or 'ikke angivet'})"]
        try:
            from competency import skill_key
            want = skill_key(name)
            gap = next((g for g in gaps or [] if skill_key(g.get("skill") or "") == want), None)
        except Exception:
            gap = None
        if gap:
            lines.append(f"Gab: {gap.get('current_label') or 'intet niveau'} → {gap.get('target_label')} "
                         f"(kilde: {gap.get('source') or 'mål'})")
        return {"kind": "skill", "label": name, "section": "skills", "lines": lines}

    if kind == "exp":
        row = next((e for e in p.get("experience") or [] if str(e.get("id")) == ref), None)
        if not row:
            return None
        label = (row.get("title") or "") + (f" @ {row['company']}" if row.get("company") else "")
        lines = [f"Erfaring: {label}" + (f" ({_years(row)})" if _years(row) else "")]
        if row.get("description"):
            lines.append("Beskrivelse: " + str(row["description"])[:300])
        return {"kind": "exp", "label": label, "section": "experience", "lines": lines}

    if kind == "edu":
        row = next((e for e in p.get("education") or [] if str(e.get("id")) == ref), None)
        if not row:
            return None
        label = row.get("degree") or ""
        extra = ", ".join(str(x) for x in (row.get("institution"), row.get("year_completed")) if x)
        return {"kind": "edu", "label": label, "section": "education",
                "lines": [f"Uddannelse: {label}" + (f" ({extra})" if extra else "")]}

    if kind == "cert":
        row = next((c for c in p.get("certifications") or [] if str(c.get("id")) == ref), None)
        if not row:
            return None
        label = row.get("name") or ""
        extra = []
        if row.get("issuer"):
            extra.append(f"udsteder {row['issuer']}")
        if row.get("expiry_date"):
            extra.append(f"udløber {row['expiry_date']}")
        return {"kind": "cert", "label": label, "section": "certifications",
                "lines": [f"Certificering: {label}" + (f" ({', '.join(extra)})" if extra else "")]}

    if kind == "lang":
        row = next((lang for lang in p.get("languages") or [] if str(lang.get("id")) == ref), None)
        if not row:
            return None
        label = row.get("language") or ""
        return {"kind": "lang", "label": label, "section": "languages",
                "lines": [f"Sprog: {label} ({row.get('proficiency') or 'niveau ikke angivet'})"]}

    if kind == "goal":
        row = next((g for g in p.get("learning_goals") or [] if str(g.get("id")) == ref), None)
        if not row:
            return None
        label = row.get("title") or ""
        lines = [f"Mål: {label} (status: {row.get('status') or 'aktiv'}"
                 + (f", deadline {row['target_date']}" if row.get("target_date") else "") + ")"]
        if row.get("description"):
            lines.append("Beskrivelse: " + str(row["description"])[:300])
        return {"kind": "goal", "label": label, "section": "goals", "lines": lines}

    if kind == "link":
        row = next((lk for lk in p.get("portfolio_links") or [] if str(lk.get("id")) == ref), None)
        if not row:
            return None
        label = row.get("label") or row.get("url") or "Link"
        return {"kind": "link", "label": label, "section": "portfolio",
                "lines": [f"Portfolio-link: {label} ({row.get('url') or ''})"]}

    if kind == "path":
        row = next((lp for lp in p.get("learning_paths") or [] if str(lp.get("id")) == ref), None)
        if not row:
            return None
        label = row.get("title") or "Læringssti"
        steps = row.get("steps") or []
        done = sum(1 for s in steps if isinstance(s, dict) and s.get("done"))
        lines = [f"Læringssti: {label} (status: {row.get('status') or 'aktiv'}, {done}/{len(steps)} trin gjort)"]
        if row.get("goal"):
            lines.append("Mål for stien: " + str(row["goal"])[:200])
        return {"kind": "path", "label": label, "section": "learning-paths", "lines": lines}

    if kind == "course":
        row = next((c for c in p.get("completed_courses") or []
                    if _stable_id("course", c.get("handle") or c.get("title")) == f"course:{ref}"), None)
        if not row:
            return None
        label = row.get("title") or ""
        extra = ", ".join(str(x) for x in (row.get("vendor"), row.get("completed_date")) if x)
        return {"kind": "course", "label": label, "section": "courses",
                "lines": [f"Gennemført kursus: {label}" + (f" ({extra})" if extra else "")]}

    if kind == "mem":
        row = next((x for x in memories or [] if str(x.get("id")) == ref), None)
        if not row:
            return None
        label = row.get("label") or ""
        line = f"Hukommelse [#{row.get('id')}]: {label}"
        if row.get("detail"):
            line += f" — {str(row['detail'])[:200]}"
        return {"kind": "mem", "label": label, "section": "memories", "lines": [line]}

    return None


def build_layer(ctx, resolved=None, mode="default"):
    """Text for the ``surface_context`` layer: ``(header, body, fenced)``.

    Returns ``None`` when there is nothing to say. ``fenced`` is True when the
    body quotes the user's profile text (render it with a data fence).
    """
    ctx = ctx or {}
    origin = ctx.get("from")
    if not origin and not resolved:
        return None
    where = SURFACES.get(origin, "")
    head = []
    if where:
        head.append(f"Brugeren åbnede denne samtale fra {where}.")
    if resolved and resolved.get("kind") == "section":
        count = resolved.get("count") or 0
        state = f"{count} punkter på profilen nu" if count else "intet gemt endnu"
        head.append(
            f"De havde sektionen '{resolved['label']}' i fokus ({state}). "
            "Det er et naturligt sted at begynde, hvis beskeden handler om det; "
            "profilen ovenfor viser hvad der allerede står."
        )
        return " ".join(head), "", False
    if resolved:
        head.append(
            "De havde dette element fra deres egen profil åbent (se nedenfor). "
            "Tag udgangspunkt i det, når beskeden handler om det — uddyb, ret eller byg videre "
            "sammen med brugeren i stedet for at spørge om det, der allerede står."
        )
        if resolved.get("kind") == "mem":
            head.append("Er hukommelsen forkert eller forældet, kan forget_about_user foreslå at fjerne den.")
        return " ".join(head), "\n".join(resolved.get("lines") or []), True
    if origin == "cv_upload":
        head.append("De har lige arbejdet med deres CV; det, der blev gemt, står på profilen.")
    elif origin == "mind_map":
        head.append("Der ser de alt det, AI'en ved om dem, så rettelser og 'glem det' er naturlige her.")
    return " ".join(head), "", False


def origin_tool_names(ctx, resolved=None):
    """Read-only tools the selector may add for this handoff (may be empty)."""
    ctx = ctx or {}
    names = list(_ORIGIN_TOOLS.get(ctx.get("from") or "", ()))
    kind = (resolved or {}).get("kind") or focus_kind(ctx)
    if kind == "section":
        section = (resolved or {}).get("section") or (ctx.get("focus") or "").split(":", 1)[-1]
        names.extend(_SECTION_TOOLS.get(section, ()))
    else:
        names.extend(_FOCUS_TOOLS.get(kind, ()))
    return list(dict.fromkeys(names))
