"""The completion moment (N-1.4): when a course is completed, skills should
grow.

``completion_moment(order_row)`` is what the learner sees after "Markér som
gennemført" and what the AI reads back to the learner:

* **skill proposals** derived from the course's catalog metadata (the learner
  accepts or adjusts the level; nothing is written silently),
* a **review prompt** (reviews already work at ``/products/<handle>/review``),
* **next-step suggestions** (other courses that build on this one),
* a hint that the AI can talk it through.

``apply_skill_choices`` writes the accepted skills to the learner's profile and
to the skill-history trail (keyed on ``users.id``), linked to the order, and
leaves the manager task "bekræft kompetenceløft" in place.

Everything degrades to empty lists; nothing here raises.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_PROPOSALS = 5
MAX_NEXT_STEPS = 3

_LEVEL_FOR_DIFFICULTY = {
    "begynder": "begynder", "beginner": "begynder", "intro": "begynder",
    "mellem": "mellem", "intermediate": "mellem",
    "avanceret": "avanceret", "advanced": "avanceret",
    "ekspert": "ekspert", "expert": "ekspert",
}


def _product(handle):
    try:
        import catalog_service
        return catalog_service.get_product(handle) if handle else None
    except Exception as e:
        logger.debug("completion_service: catalog lookup failed: %s", e)
        return None


def skills_for_product(product):
    """Candidate skill names + a suggested level for a normalized catalog product."""
    if not product:
        return []
    meta = product.get("metadata") or {}
    diff = str(meta.get("difficulty") or "").strip().lower()
    level = _LEVEL_FOR_DIFFICULTY.get(diff, "mellem")

    names = []

    def _add(n, why):
        n = (n or "").strip()
        if n and n.lower() not in {x["name"].lower() for x in names}:
            names.append({"name": n[:80], "level": level, "why": why})

    _add(meta.get("primary_topic"), "Kursets hovedemne")
    cert = meta.get("certification")
    if cert:
        _add(cert, "Certificering kurset forbereder til")
    try:
        import catalog_service
        skip = {t.lower() for t in getattr(catalog_service, "OPERATIONAL_TAGS", set())}
        skip |= {t.lower() for t in getattr(catalog_service, "FORMAT_TAGS", set())}
    except Exception:
        skip = set()
    for cat in (product.get("categories") or [])[:3]:
        _add(cat, "Kursets kategori")
    for tag in (product.get("tags") or [])[:10]:
        if str(tag).lower() in skip:
            continue
        if len(names) >= MAX_PROPOSALS:
            break
        _add(str(tag).replace("-", " ").strip().title() if str(tag).islower() else tag, "Fra kursets emner")
    return names[:MAX_PROPOSALS]


def next_steps_for(product, limit=MAX_NEXT_STEPS):
    """Courses that naturally follow (shared topic, same or higher level)."""
    if not product:
        return []
    try:
        import catalog_service
        related = catalog_service.get_related_products(product, limit=limit + 2) or []
    except Exception as e:
        logger.debug("completion_service: related lookup failed: %s", e)
        return []
    out = []
    for p in related:
        if p.get("handle") == product.get("handle"):
            continue
        out.append({"title": p.get("title"), "handle": p.get("handle"),
                    "url": "/products/%s" % p.get("handle"),
                    "price_label": p.get("price_label")})
        if len(out) >= limit:
            break
    return out


def completion_moment(row):
    """Everything the UI/AI needs right after a completion."""
    handle = (row or {}).get("product_handle")
    product = _product(handle)
    return {
        "course_title": (row or {}).get("product_title"),
        "product_handle": handle,
        "skill_proposals": skills_for_product(product),
        "next_steps": next_steps_for(product),
        "review_url": ("/products/%s#reviews" % handle) if handle else None,
        "review_prompt": "Hvordan var kurset? Din vurdering hjælper kolleger med at vælge.",
        "ai_prompt": "Tal med AI'en om, hvad du tager med dig, og hvad næste skridt kan være.",
    }


def apply_skill_choices(username, choices, *, company_id=None, user_id=None, order_id=None):
    """Write accepted skills (``[{"name", "level"}]``) to the learner's profile and
    the skill-history trail. Returns ``{"saved": n, "skipped": n}``. Never raises."""
    saved = skipped = 0
    try:
        from app1.user_profile_db import add_skill
        from skill_history import record_user_snapshot
    except Exception as e:
        logger.warning("completion_service: imports failed: %s", e)
        return {"saved": 0, "skipped": len(choices or [])}
    for c in choices or []:
        name = (c.get("name") or "").strip()
        level = _LEVEL_FOR_DIFFICULTY.get(str(c.get("level") or "").lower(), "mellem")
        if not name:
            skipped += 1
            continue
        try:
            if add_skill(username, name, level, source="course_completion"):
                record_user_snapshot(username, name, level, source="post_course",
                                     company_id=company_id, employee_id=user_id)
                saved += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("completion_service: skill save failed (%s): %s", name, e)
            skipped += 1
    return {"saved": saved, "skipped": skipped}
