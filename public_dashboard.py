"""Public dashboard for logged-out visitors at ``/``.

Contract: ``public_blocks()`` returns plain, JSON-safe dicts built ONLY from the
catalogue (``catalog_service``) at list prices (no company scope, so no
negotiated prices, no personal and no company data). It is cached for a few
minutes per worker through ``perf_cache`` and never raises: a catalogue that
cannot be read yields empty blocks and the page still renders.

Popular courses use the same source as the learner fallback
(``futurematch_ui._home_recommendations`` with no skills): the first page of the
catalogue in its default ordering, minus courses whose latest session has passed.
"""
import datetime

import perf_cache

CACHE_SECONDS = 300
POPULAR_LIMIT = 6
CATEGORY_LIMIT = 8
SESSION_LIMIT = 4
# Upcoming sessions are looked for in the first slice of the catalogue only, so
# a cold cache never walks every product on a request.
SESSION_SCAN_LIMIT = 2000


def _course(product):
    return {
        "handle": product.get("handle"),
        "title": product.get("title") or "Kursus",
        "vendor": product.get("vendor") or "",
        "price_label": product.get("price_label") or "",
        "format": product.get("format") or "",
        "image_url": product.get("image_url") or "",
        "excerpt": product.get("description_excerpt") or "",
    }


def _popular(catalog_service):
    result = catalog_service.search_products(filters={}, page=1, per_page=POPULAR_LIMIT * 2) or {}
    products = catalog_service.exclude_stale(result.get("products") or [])
    return [_course(p) for p in products[:POPULAR_LIMIT]]


def _categories(catalog_service):
    return [
        {"name": c["name"], "slug": c["slug"], "count": c["count"]}
        for c in (catalog_service.get_categories() or [])[:CATEGORY_LIMIT]
    ]


def _upcoming_sessions(catalog_service):
    """Next sessions with an explicit date (day, month name and year) in the future."""
    from catalog_freshness import parse_explicit_dates

    today = datetime.date.today()
    found = []
    for product in (catalog_service.get_products() or [])[:SESSION_SCAN_LIMIT]:
        for variant in product.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            future = [d for d in parse_explicit_dates(variant.get("date") or "") if d >= today]
            if future:
                found.append((min(future), product, variant))
    found.sort(key=lambda row: (row[0], row[1].get("title") or ""))
    seen, sessions = set(), []
    for day, product, variant in found:
        if product.get("handle") in seen:
            continue
        seen.add(product.get("handle"))
        sessions.append({
            "handle": product.get("handle"),
            "title": product.get("title") or "Kursus",
            "date": day.isoformat(),
            "date_label": variant.get("date") or day.isoformat(),
            "city": variant.get("city") or "",
        })
        if len(sessions) >= SESSION_LIMIT:
            break
    return sessions


@perf_cache.ttl_cache(CACHE_SECONDS, key=lambda: "public")
def public_blocks():
    blocks = {"popular": [], "categories": [], "sessions": []}
    try:
        import catalog_service
    except Exception:
        return blocks
    for name, builder in (("popular", _popular), ("categories", _categories), ("sessions", _upcoming_sessions)):
        try:
            blocks[name] = builder(catalog_service)
        except Exception:
            blocks[name] = []
    return blocks
