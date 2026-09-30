"""Scheduled Shopify catalog sync (N-3.1).

Pulls the published products from the Shopify Admin API into the catalog source
file (``catalog_service.source_file_path()``), so the catalog no longer depends on
a 17 MB export committed to git. Credentials come from the environment ONLY:

* ``SHOPIFY_STORE``        e.g. ``mystore.myshopify.com``
* ``SHOPIFY_ADMIN_TOKEN``  Admin API access token (``shpat_...``), never committed
* ``SHOPIFY_API_VERSION``  optional, default ``2024-10``

Without the variables the job skips cleanly (returns ``{"skipped": ...}``). The
file is replaced atomically and only when the fetched list is plausible (a sync
returning far fewer products than we hold is treated as an error, so a bad
response can never wipe the catalog).
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile

logger = logging.getLogger(__name__)

MIN_KEEP_RATIO = 0.5       # refuse to replace the file with < 50% of the current count
PAGE_LIMIT = 250
MAX_PAGES = 40


def configured() -> bool:
    return bool(os.environ.get("SHOPIFY_STORE") and os.environ.get("SHOPIFY_ADMIN_TOKEN"))


def _next_link(link_header):
    """URL of rel="next" from a Shopify ``Link`` header, or None."""
    for part in (link_header or "").split(","):
        m = re.search(r'<([^>]+)>;\s*rel="next"', part)
        if m:
            return m.group(1)
    return None


def fetch_all(http=None):
    """Fetch every product page. ``http`` is injectable (``requests``-like)."""
    if http is None:
        import requests as http
    store = os.environ["SHOPIFY_STORE"].strip().replace("https://", "").rstrip("/")
    version = os.environ.get("SHOPIFY_API_VERSION", "2024-10")
    url = f"https://{store}/admin/api/{version}/products.json?limit={PAGE_LIMIT}&status=active"
    headers = {"X-Shopify-Access-Token": os.environ["SHOPIFY_ADMIN_TOKEN"], "Accept": "application/json"}
    products = []
    for _ in range(MAX_PAGES):
        resp = http.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            raise RuntimeError("Shopify svarede %s" % resp.status_code)
        products.extend(resp.json().get("products") or [])
        url = _next_link(resp.headers.get("Link"))
        if not url:
            break
    return products


def sync(http=None, target=None):
    """Run one sync. Returns ``{"synced": n}``, ``{"skipped": reason}`` or ``{"error": msg}``."""
    if not configured():
        return {"skipped": "SHOPIFY_STORE/SHOPIFY_ADMIN_TOKEN er ikke sat"}
    import catalog_service
    path = target or catalog_service.source_file_path()
    try:
        products = fetch_all(http)
    except Exception as e:
        logger.warning("shopify sync failed: %s", e)
        return {"error": str(e)}
    if not products:
        return {"error": "Shopify returnerede ingen produkter, kataloget er uændret"}
    try:
        current = catalog_service._read_json(path, [])
        if isinstance(current, list) and current and len(products) < len(current) * MIN_KEEP_RATIO:
            return {"error": "Svaret ser ufuldstændigt ud (%d mod %d i dag), kataloget er uændret"
                             % (len(products), len(current))}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".json", dir=os.path.dirname(path) or ".")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(products, f, ensure_ascii=False)
        os.replace(tmp, path)
        catalog_service.clear_catalog_cache()
        catalog_service._notify_catalog_changed()
        return {"synced": len(products)}
    except Exception as e:
        logger.warning("shopify sync write failed: %s", e)
        return {"error": str(e)}
