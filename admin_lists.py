"""Search + pagination helpers for the platform-admin lists (N-6.5).

Small on purpose: ``filter_rows`` does the text search over named fields,
``paginate`` slices and describes the page. The matching Jinja macros live in
``templates/fm/_admin_list.html``.
"""

from __future__ import annotations

DEFAULT_PER_PAGE = 25


def list_args(request, per_page=DEFAULT_PER_PAGE):
    """(page, per_page, q) from the query string; bad values fall back safely."""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    q = (request.args.get("q") or "").strip()[:100]
    return page, per_page, q


def filter_rows(rows, q, fields):
    """Case-insensitive contains-search over ``fields`` of dict rows."""
    if not q:
        return list(rows)
    needle = q.lower()
    out = []
    for r in rows:
        hay = " ".join(str(r.get(f) or "") for f in fields).lower()
        if needle in hay:
            out.append(r)
    return out


def paginate(rows, page, per_page=DEFAULT_PER_PAGE):
    total = len(rows)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    return {
        "items": rows[start:start + per_page],
        "page": page,
        "pages": pages,
        "total": total,
        "per_page": per_page,
        "has_prev": page > 1,
        "has_next": page < pages,
    }
