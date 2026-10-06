import hashlib
import tempfile
import functools
import threading
import csv
import datetime
import html
import io
import json
import os
import re
import time
import uuid
from collections import Counter, defaultdict
from copy import deepcopy

from flask import current_app, has_app_context


SOURCE_FILE = os.path.join("app1", "shopify_products_all_pages.json")
AUGMENTED_FILE = os.path.join("app1", "shopify_products_augmented.json")
VENDOR_PROFILES_FILE = os.path.join("app1", "vendor_profiles.json")

CATEGORY_OVERRIDES_FILE = "catalog_category_overrides.json"
IMPORT_PRODUCTS_FILE = "catalog_import_products.json"
# Admin edits / unpublish state (N-3.1). One overlay for every reader of the catalog.
CATALOG_OVERLAY_FILE = "catalog_overlay.json"
PRODUCT_STATUSES = ("active", "hidden", "archived")
EDITABLE_FIELDS = ("title", "summary", "vendor", "tags", "image_url", "variants", "cancellation_terms")
IMPORT_DRAFT_DIR = os.path.join("catalog_import_drafts")
AI_CATEGORY_DRAFT_DIR = os.path.join("catalog_ai_category_drafts")

OPERATIONAL_TAGS = {
    "efter aftale",
    "kontakt for pris",
    "no-index",
    "kursus",
    "kurser",
    "uddannelse",
    "training",
    "course",
    "danmark",
    "denmark",
}

FORMAT_TAGS = {
    "e-learning",
    "elearning",
    "blended learning",
    "lukket virksomhedshold",
    "foredrag",
    "konference",
}

# Words that describe how, where, in which language or at which level a course is
# given, not what it teaches: never a competence the learner gains.
_META_TAGS = {
    # format
    "klassekursus", "klasseundervisning", "online", "virtuelt", "virtuel", "webinar", "hybrid",
    "åbent kursus", "åbne kurser", "firmakursus", "workshop", "seminar", "kursus",
    # language
    "dansk", "engelsk", "english", "danish",
    # level
    "begynder", "mellem", "avanceret", "ekspert", "intro", "beginner", "intermediate", "advanced", "expert",
    # places
    "danmark", "sjælland", "fyn", "jylland", "hovedstaden", "midtjylland", "nordjylland", "syddanmark",
    "københavn", "aarhus", "århus", "odense", "aalborg", "ålborg", "esbjerg", "kolding", "vejle",
    "roskilde", "herning", "silkeborg", "horsens", "randers", "viborg", "næstved", "fredericia",
    "taastrup", "ballerup", "lyngby", "hillerød", "slagelse", "holstebro", "skive", "svendborg",
}


def is_meta_tag(tag):
    """True for a tag that names a format, language, level, place or other
    operational detail ("Klassekursus", "E-learning", "Online", "Aarhus"), i.e. not
    something a learner can be said to have learnt."""
    t = str(tag or "").strip().lower()
    if not t:
        return True
    return (t in _META_TAGS or t in FORMAT_TAGS or t in OPERATIONAL_TAGS
            or t.startswith(("region:", "region ", "by:", "land:")))

GENERIC_PRODUCT_TYPES = {"", "kursus"}

_CACHE = {
    "signature": None,
    "raw": None,
    "products": None,
    "by_handle": None,
    "categories": None,
    "vendors": None,
    "filter_options": None,
    "related": None,
    "related_signature": None,
    "all_products": None,
    "all_signature": None,
}

# Throttle the file-signature stat. _signature() stat()s four files and is hit on
# every catalog/derive call; within a single page render that is many redundant
# stat syscalls (slow on networked filesystems). Re-checking the
# files at most every CATALOG_SIGNATURE_TTL_SECONDS collapses those to one stat
# burst per window while still picking up catalog edits within a few seconds.
_SIG_CACHE = {"value": None, "checked_at": 0.0}
try:
    _SIG_TTL_SECONDS = max(0.0, float(os.environ.get("CATALOG_SIGNATURE_TTL_SECONDS", "5")))
except (TypeError, ValueError):
    _SIG_TTL_SECONDS = 5.0


def source_file_path():
    """Where the raw Shopify export lives.

    ``CATALOG_SOURCE_FILE`` (absolute, or relative to the app root) wins, so the
    17 MB export can live on a persistent data volume instead of in git (N-3.1).
    Defaults to the historical ``app1/shopify_products_all_pages.json``.
    """
    override = (os.environ.get("CATALOG_SOURCE_FILE") or "").strip()
    if override:
        return override if os.path.isabs(override) else _data_path(override)
    return _data_path(SOURCE_FILE)


def _root_path():
    if has_app_context():
        return current_app.root_path
    return os.path.dirname(os.path.abspath(__file__))


def _instance_path(*parts):
    if has_app_context():
        base = current_app.instance_path
    else:
        base = os.path.join(_root_path(), "instance")
    return os.path.join(base, *parts)


def _data_path(*parts):
    return os.path.join(_root_path(), *parts)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return deepcopy(default)


_CATALOG_LOCKS = {}
_CATALOG_LOCK_GUARD = threading.Lock()


def _serialized_catalog_write(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        from filelock import FileLock
        path = _instance_path('catalog-write.lock')
        os.makedirs(os.path.dirname(path),exist_ok=True)
        with _CATALOG_LOCK_GUARD:
            lock = _CATALOG_LOCKS.setdefault(path,FileLock(path,timeout=15))
        with lock:
            return fn(*args, **kwargs)
    return wrapped


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.catalog-',dir=os.path.dirname(path))
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump(payload,stream,ensure_ascii=False,indent=2)
            stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def clear_catalog_cache():
    for key in _CACHE:
        _CACHE[key] = None
    # Force the next _signature() to re-stat immediately (explicit invalidation
    # after a catalog edit must not be masked by the signature throttle window).
    _SIG_CACHE["value"] = None
    _SIG_CACHE["checked_at"] = 0.0


def slugify(value):
    value = (value or "").strip().lower()
    replacements = {
        "æ": "ae",
        "ø": "o",
        "å": "aa",
        "ä": "a",
        "ö": "o",
        "ü": "u",
        "é": "e",
        "è": "e",
    }
    for src, dst in replacements.items():
        value = value.replace(src, dst)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "ukendt"


def split_tags(tags):
    if isinstance(tags, list):
        values = tags
    elif isinstance(tags, str):
        values = re.split(r"[,|]", tags)
    else:
        values = []
    cleaned = []
    seen = set()
    for item in values:
        tag = str(item).strip()
        if not tag:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(tag)
    return cleaned


def split_multi_value(value):
    if isinstance(value, list):
        raw_values = value
    else:
        raw_values = re.split(r"[,|]", str(value or ""))
    cleaned = []
    seen = set()
    for item in raw_values:
        text = str(item).strip()
        if not text:
            continue
        key = text.lower()
        if key not in seen:
            cleaned.append(text)
            seen.add(key)
    return cleaned


def is_category_tag(tag):
    tag_lower = (tag or "").strip().lower()
    if not tag_lower:
        return False
    if tag_lower in OPERATIONAL_TAGS or tag_lower in FORMAT_TAGS:
        return False
    if tag_lower.startswith(("region:", "by:", "land:")):
        return False
    if tag_lower.startswith("region "):
        return False
    return True


def clean_html(value):
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", value or "", flags=re.I)
    text = re.sub(r"</\s*(p|div|li|h[1-6])\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def excerpt(text, length=190):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= length:
        return text
    return text[: length - 3].rstrip() + "..."


def parse_price(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"none", "n/a", "efter aftale", "kontakt for pris"}:
        return None
    text = re.sub(r"[^\d,.\-]", "", text).replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def format_price(value):
    price = parse_price(value)
    if price is None:
        return "Pris pa foresporgsel"
    if price == 0:
        return "Gratis"
    if price == int(price):
        return f"{int(price):,}".replace(",", ".") + " kr"
    formatted = f"{price:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} kr"


def price_label_for_product(raw_product, prices):
    tags_lower = {tag.lower() for tag in split_tags(raw_product.get("tags"))}
    if "kontakt for pris" in tags_lower or not prices:
        return "Pris pa foresporgsel"
    minimum = min(prices)
    maximum = max(prices)
    if minimum == 0 and maximum == 0:
        return "Gratis"
    if minimum == maximum:
        return format_price(minimum)
    return f"{format_price(minimum)} - {format_price(maximum)}"


def extract_city_name(address):
    if not address:
        return ""
    address = str(address).strip()
    match = re.search(r"\b\d{4}\s+([A-ZÆØÅa-zæøå]+)", address)
    if match:
        return match.group(1)
    if "," not in address and not re.search(r"\d", address):
        return address
    last_part = address.split(",")[-1].strip()
    cleaned = re.sub(r"\b\d{4}\s*", "", last_part).strip()
    return cleaned or address


def normalize_variant(variant, fallback_price=None):
    price = parse_price(variant.get("price") if isinstance(variant, dict) else None)
    if price is None:
        price = parse_price(fallback_price)
    location = (variant.get("option1") or variant.get("location") or "").strip()
    date = (variant.get("option2") or variant.get("date") or "").strip()
    title = (variant.get("title") or "").strip()
    # Seat availability. inventory_quantity only means anything when the vendor
    # actually tracks stock (inventory_management set) — most of the catalog ships
    # untracked rows with a hardcoded 0, which must NOT read as sold out. Unknown
    # stays None so the UI omits the pill rather than inventing availability.
    seats = None
    if isinstance(variant, dict):
        tracked = bool(str(variant.get("inventory_management") or "").strip())
        if tracked:
            try:
                seats = int(variant.get("inventory_quantity"))
            except (TypeError, ValueError):
                seats = None
            # "continue" lets the vendor oversell, so a zero count is not a stop sign.
            if seats is not None and seats <= 0 and str(variant.get("inventory_policy") or "").lower() == "continue":
                seats = None
        elif variant.get("available") is False:
            seats = 0
        elif variant.get("seats") is not None:
            # Rows from non-Shopify importers may carry a plain seat count.
            try:
                seats = int(variant.get("seats"))
            except (TypeError, ValueError):
                seats = None
    result = {
        "id": variant.get("id") if isinstance(variant, dict) else None,
        "title": title,
        "price": price,
        "price_label": format_price(price),
        "location": location,
        "city": extract_city_name(location),
        "date": date,
        "seats": seats,
    }

    from enrollment_service import session_key
    result['session_id'] = session_key(result)
    return result


def _load_category_overrides():
    payload = _read_json(_instance_path(CATEGORY_OVERRIDES_FILE), {"overrides": {}})
    if isinstance(payload, dict) and "overrides" in payload:
        return payload.get("overrides") or {}
    if isinstance(payload, dict):
        return payload
    return {}


def _load_import_products():
    payload = _read_json(_instance_path(IMPORT_PRODUCTS_FILE), {"products": []})
    if isinstance(payload, dict):
        return payload.get("products") or []
    if isinstance(payload, list):
        return payload
    return []


def _signature():
    now = time.monotonic()
    if _SIG_CACHE["value"] is not None and (now - _SIG_CACHE["checked_at"]) < _SIG_TTL_SECONDS:
        return _SIG_CACHE["value"]
    paths = [
        source_file_path(),
        _data_path(AUGMENTED_FILE),
        _instance_path(CATEGORY_OVERRIDES_FILE),
        _instance_path(IMPORT_PRODUCTS_FILE),
        _instance_path(CATALOG_OVERLAY_FILE),
    ]
    signature = tuple((path, _mtime(path)) for path in paths)
    _SIG_CACHE["value"] = signature
    _SIG_CACHE["checked_at"] = now
    return signature


def _load_overlay():
    payload = _read_json(_instance_path(CATALOG_OVERLAY_FILE), {"products": {}})
    if isinstance(payload, dict):
        return payload.get("products") or {}
    return {}


def _apply_overlay(raw_products):
    """Admin edits + visibility on top of the merged sources (in place)."""
    overlay = _load_overlay()
    for product in raw_products:
        entry = overlay.get(product.get("handle"))
        product["_catalog_status"] = "active"
        if not entry:
            continue
        status = entry.get("status")
        if status in PRODUCT_STATUSES:
            product["_catalog_status"] = status
        if isinstance(entry.get("variants"), list):
            product["variants"] = deepcopy(entry["variants"])
        if "cancellation_terms" in entry:
            product["cancellation_terms"] = entry["cancellation_terms"]
        if entry.get("title"):
            product["title"] = entry["title"]
        if entry.get("summary"):
            product["ai_summary"] = entry["summary"]
        if entry.get("vendor"):
            product["vendor"] = entry["vendor"]
        if isinstance(entry.get("tags"), list):
            product["tags"] = list(entry["tags"])
        if entry.get("image_url"):
            product["image"] = {"src": entry["image_url"]}
            product["images"] = [{"src": entry["image_url"]}]
        product["_catalog_edited"] = True


def catalog_signature():
    """Public cache key for everything derived from the catalog (RAG index...)."""
    return _signature()


def load_raw_products():
    signature = _signature()
    if _CACHE["raw"] is not None and _CACHE["signature"] == signature:
        return _CACHE["raw"]

    source_products = _read_json(source_file_path(), [])
    if not isinstance(source_products, list):
        source_products = []

    by_handle = {}
    for product in source_products:
        handle = product.get("handle")
        if handle:
            by_handle[handle] = deepcopy(product)

    augmented_products = _read_json(_data_path(AUGMENTED_FILE), [])
    if isinstance(augmented_products, list):
        for product in augmented_products:
            handle = product.get("handle")
            if not handle:
                continue
            if handle in by_handle:
                merged = by_handle[handle]
                merged.update({k: deepcopy(v) for k, v in product.items() if k not in {"variants", "images", "image"}})
            else:
                by_handle[handle] = deepcopy(product)

    for product in _load_import_products():
        handle = product.get("handle")
        if not handle:
            continue
        product = deepcopy(product)
        product["_catalog_source"] = product.get("_catalog_source") or "csv"
        if handle in by_handle:
            merged = by_handle[handle]
            merged.update({k: deepcopy(v) for k, v in product.items() if k != "variants"})
            if product.get("variants"):
                merged["variants"] = deepcopy(product["variants"])
        else:
            by_handle[handle] = product

    raw_products = list(by_handle.values())
    _apply_overlay(raw_products)
    _CACHE["raw"] = raw_products
    _CACHE["signature"] = signature
    _CACHE["products"] = None
    _CACHE["all_products"] = None
    _CACHE["by_handle"] = None
    _CACHE["categories"] = None
    _CACHE["vendors"] = None
    _CACHE["filter_options"] = None
    _CACHE["related"] = None
    _CACHE["related_signature"] = None
    return raw_products


def extract_categories(raw_product, overrides=None):
    overrides = overrides if overrides is not None else _load_category_overrides()
    handle = raw_product.get("handle")
    override = overrides.get(handle)
    if isinstance(override, list):
        categories = [str(item).strip() for item in override if str(item).strip()]
        if categories:
            return list(dict.fromkeys(categories))

    categories = [tag for tag in split_tags(raw_product.get("tags")) if is_category_tag(tag)]
    if not categories:
        product_type = (raw_product.get("product_type") or "").strip()
        if product_type.lower() not in GENERIC_PRODUCT_TYPES and product_type.lower() not in FORMAT_TAGS:
            categories = [product_type]
    return list(dict.fromkeys(categories))


def infer_format(raw_product, tags):
    candidates = [raw_product.get("product_type") or ""]
    candidates.extend(tags)
    title = raw_product.get("title") or ""
    haystack = " ".join(candidates + [title]).lower()
    if "e-learning" in haystack or "elearning" in haystack:
        return "E-learning"
    if "blended" in haystack:
        return "Blended learning"
    if "lukket virksomhedshold" in haystack:
        return "Lukket virksomhedshold"
    if "konference" in haystack:
        return "Konference"
    if "foredrag" in haystack:
        return "Foredrag"
    return "Kursus"


def _image_url(raw_product):
    image = raw_product.get("image") or {}
    if isinstance(image, dict) and image.get("src"):
        return image.get("src")
    images = raw_product.get("images") or []
    if images and isinstance(images[0], dict):
        return images[0].get("src") or ""
    return raw_product.get("image_url") or ""


def normalize_product(raw_product, overrides=None):
    tags = split_tags(raw_product.get("tags"))
    categories = extract_categories(raw_product, overrides=overrides)
    raw_variants = raw_product.get("variants") or []
    if not raw_variants:
        raw_variants = [{"price": raw_product.get("price")}]
    variants = [normalize_variant(v, fallback_price=raw_product.get("price")) for v in raw_variants if isinstance(v, dict)]
    prices = [v["price"] for v in variants if v.get("price") is not None]
    locations = list(dict.fromkeys(v["city"] for v in variants if v.get("city")))
    dates = list(dict.fromkeys(v["date"] for v in variants if v.get("date")))
    description_text = clean_html(raw_product.get("body_html") or raw_product.get("description") or "")
    summary = raw_product.get("ai_summary") or raw_product.get("summary") or excerpt(description_text, 210)
    vendor = (raw_product.get("vendor") or "Ukendt").strip()
    handle = raw_product.get("handle") or slugify(raw_product.get("title") or str(raw_product.get("id") or uuid.uuid4()))
    product_type = (raw_product.get("product_type") or "Kursus").strip()
    metadata = raw_product.get("structured_metadata") or {}

    return {
        "id": raw_product.get("id"),
        "handle": handle,
        "title": (raw_product.get("title") or "Unavngivet kursus").strip(),
        "vendor": vendor,
        "vendor_slug": slugify(vendor),
        "product_type": product_type,
        "format": infer_format(raw_product, tags),
        "tags": tags,
        "categories": categories,
        "category_slugs": [slugify(category) for category in categories],
        "price_min": min(prices) if prices else None,
        "price_max": max(prices) if prices else None,
        "price_label": price_label_for_product(raw_product, prices),
        "image_url": _image_url(raw_product),
        "summary": summary,
        "description_text": description_text or summary,
        "description_excerpt": excerpt(description_text or summary, 260),
        "variants": variants,
        "locations": locations,
        "dates": dates,
        "metadata": metadata,
        "cancellation_terms": raw_product.get("cancellation_terms") or "",
        "source": raw_product.get("_catalog_source") or "shopify_json",
        "status": raw_product.get("_catalog_status") or "active",
        "edited": bool(raw_product.get("_catalog_edited")),
        "raw": raw_product,
    }


def get_all_products():
    """Every product including hidden/archived ones (admin product browser only)."""
    signature = _signature()
    if _CACHE.get("all_products") is not None and _CACHE.get("all_signature") == signature:
        return _CACHE["all_products"]
    overrides = _load_category_overrides()
    products = [normalize_product(product, overrides=overrides) for product in load_raw_products()]
    products.sort(key=lambda p: (p["title"].lower(), p["vendor"].lower()))
    _CACHE["all_products"] = products
    _CACHE["all_signature"] = signature
    return products


def get_products():
    """The ONE catalog: published products for every reader (pages, search, AI,
    ordering). Hidden and archived products are excluded."""
    signature = _signature()
    if _CACHE["products"] is not None and _CACHE["signature"] == signature:
        return _CACHE["products"]
    load_raw_products()  # refreshes _CACHE["signature"] when sources changed
    products = [p for p in get_all_products() if p.get("status") == "active"]
    _CACHE["products"] = products
    _CACHE["by_handle"] = {p["handle"]: p for p in products}
    return products


def get_product_any(handle):
    """Look up a product regardless of its visibility (admin)."""
    for p in get_all_products():
        if p["handle"] == handle:
            return p
    return None


def warm_catalog():
    """Eager-load normalized catalog products for chat tools."""
    return get_products()


def get_product(handle):
    # Delegate to get_products() so the file-signature check runs and detail-page
    # lookups stay consistent with the list/search pages (avoids stale by_handle).
    get_products()
    return (_CACHE["by_handle"] or {}).get(handle)


def get_categories(products=None):
    # Cache the global (no-arg) result keyed by the catalog signature — this was
    # recomputed over the whole catalog (~60k products) on every catalog/filter
    # request even though the cache slot already existed. An explicit `products`
    # arg (e.g. a filtered subset) bypasses the cache and is computed fresh.
    use_cache = products is None
    if use_cache:
        signature = _signature()
        if _CACHE["categories"] is not None and _CACHE["signature"] == signature:
            return _CACHE["categories"]
        products = get_products()
    counter = Counter()
    by_slug = {}
    for product in products:
        for category in product["categories"]:
            slug = slugify(category)
            counter[slug] += 1
            by_slug.setdefault(slug, category)
    categories = [
        {"name": by_slug[slug], "slug": slug, "count": count}
        for slug, count in counter.items()
    ]
    categories.sort(key=lambda c: (-c["count"], c["name"].lower()))
    if use_cache:
        _CACHE["categories"] = categories
    return categories


def get_category(slug):
    for category in get_categories():
        if category["slug"] == slug:
            return category
    return None


_PROFILE_LIST_FIELDS = ("specializations", "format_strengths", "locations")
_DB_PROFILE_CACHE = {"at": 0.0, "data": None}


def _json_seed_profiles():
    payload = _read_json(_data_path(VENDOR_PROFILES_FILE), {})
    return payload if isinstance(payload, dict) else {}


def _db_vendor_profiles():
    """Vendor profiles from the ``vendor_profiles`` table (N-3.1: the DB is the
    source; the JSON file is only the seed). None when no database is reachable."""
    now = time.monotonic()
    if _DB_PROFILE_CACHE["data"] is not None and now - _DB_PROFILE_CACHE["at"] < 60:
        return _DB_PROFILE_CACHE["data"]
    if not has_app_context():
        return None
    try:
        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return None
        cur = mysql.connection.cursor()
        cur.execute("SELECT vendor_name, short_name, price_range, reputation, best_for, specializations, "
                    "format_strengths, locations, website, logo_url FROM vendor_profiles")
        rows = cur.fetchall() or []
        cur.close()
    except Exception:
        return None
    profiles = {}
    for r in rows:
        d = r if isinstance(r, dict) else dict(zip(
            ("vendor_name", "short_name", "price_range", "reputation", "best_for", "specializations",
             "format_strengths", "locations", "website", "logo_url"), r))
        prof = {k: v for k, v in d.items() if k != "vendor_name" and v not in (None, "")}
        for field in _PROFILE_LIST_FIELDS:
            if isinstance(prof.get(field), str):
                try:
                    prof[field] = json.loads(prof[field])
                except Exception:
                    prof[field] = [x.strip() for x in prof[field].split(",") if x.strip()]
        profiles[d["vendor_name"]] = prof
    _DB_PROFILE_CACHE["at"], _DB_PROFILE_CACHE["data"] = now, profiles
    return profiles


def _load_vendor_profiles():
    """DB profiles win; the bundled JSON seed fills in vendors that have none."""
    merged = dict(_json_seed_profiles())
    db = _db_vendor_profiles()
    if db:
        for name, prof in db.items():
            merged[name] = {**merged.get(name, {}), **prof}
    return merged


def save_vendor_profile(conn, vendor_name, fields, actor=""):
    """Upsert one vendor profile row (vendor portal + admin use this)."""
    cols = ("short_name", "price_range", "reputation", "best_for", "website", "logo_url")
    values = {c: (fields.get(c) or None) for c in cols}
    for f in _PROFILE_LIST_FIELDS:
        v = fields.get(f)
        values[f] = json.dumps(v, ensure_ascii=False) if isinstance(v, list) else (v or None)
    cur = conn.cursor()
    try:
        cur.execute(
            """INSERT INTO vendor_profiles (vendor_name, short_name, price_range, reputation, best_for,
                   specializations, format_strengths, locations, website, logo_url, updated_by)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE short_name = COALESCE(VALUES(short_name), short_name),
                   price_range = COALESCE(VALUES(price_range), price_range),
                   reputation = COALESCE(VALUES(reputation), reputation),
                   best_for = COALESCE(VALUES(best_for), best_for),
                   specializations = COALESCE(VALUES(specializations), specializations),
                   format_strengths = COALESCE(VALUES(format_strengths), format_strengths),
                   locations = COALESCE(VALUES(locations), locations),
                   website = COALESCE(VALUES(website), website),
                   logo_url = COALESCE(VALUES(logo_url), logo_url),
                   updated_by = VALUES(updated_by)""",
            (vendor_name, values["short_name"], values["price_range"], values["reputation"], values["best_for"],
             values["specializations"], values["format_strengths"], values["locations"], values["website"],
             values["logo_url"], actor or None))
        conn.commit()
    finally:
        cur.close()
    _DB_PROFILE_CACHE["data"] = None
    clear_catalog_cache()


def seed_vendor_profiles(conn):
    """One-time import of ``app1/vendor_profiles.json`` into the table."""
    n = 0
    for name, prof in _json_seed_profiles().items():
        save_vendor_profile(conn, name, prof, actor="seed")
        n += 1
    return n


def get_vendors(products=None):
    # Same as get_categories: cache the global result keyed by the catalog
    # signature; an explicit `products` arg is computed fresh and not cached.
    use_cache = products is None
    if use_cache:
        signature = _signature()
        if _CACHE["vendors"] is not None and _CACHE["signature"] == signature:
            return _CACHE["vendors"]
        products = get_products()
    profile_map = _load_vendor_profiles()
    grouped = defaultdict(list)
    for product in products:
        grouped[product["vendor"]].append(product)

    vendors = []
    for name, items in grouped.items():
        profile = profile_map.get(name) or next(
            (profile for key, profile in profile_map.items() if key.lower() == name.lower()),
            {},
        )
        categories = Counter(cat for item in items for cat in item["categories"])
        prices = [item["price_min"] for item in items if item["price_min"] is not None]
        vendors.append({
            "name": name,
            "slug": slugify(name),
            "course_count": len(items),
            "categories": [name for name, _ in categories.most_common(5)],
            "price_from": min(prices) if prices else None,
            "price_label": format_price(min(prices)) if prices else "Pris pa foresporgsel",
            "profile": profile,
            "image_url": next((item["image_url"] for item in items if item["image_url"]), ""),
        })
    vendors.sort(key=lambda v: (-v["course_count"], v["name"].lower()))
    if use_cache:
        _CACHE["vendors"] = vendors
    return vendors


def get_vendor(slug):
    for vendor in get_vendors():
        if vendor["slug"] == slug:
            return vendor
    return None


def get_filter_options(products=None):
    # The catalog/browse pages call this on every load; cache the assembled
    # result (categories + vendors + formats + locations) keyed by the catalog
    # signature so the whole derivation runs once per catalog change, not once
    # per request.
    use_cache = products is None
    if use_cache:
        signature = _signature()
        if _CACHE["filter_options"] is not None and _CACHE["signature"] == signature:
            return _CACHE["filter_options"]
    products = products or get_products()
    formats = sorted({p["format"] for p in products if p.get("format")})
    locations = Counter(loc for p in products for loc in p["locations"])
    result = {
        "categories": get_categories(products),
        "vendors": get_vendors(products),
        "formats": formats,
        "locations": [{"name": name, "count": count} for name, count in locations.most_common(30)],
    }
    if use_cache:
        _CACHE["filter_options"] = result
    return result


def product_search_text(product):
    pieces = [
        product["title"],
        product["vendor"],
        product.get("summary") or "",
        product.get("description_excerpt") or "",
        product.get("product_type") or "",
        product.get("format") or "",
        " ".join(product.get("categories") or []),
        " ".join(product.get("tags") or []),
        " ".join(product.get("locations") or []),
    ]
    return " ".join(pieces).lower()


def get_company_discount_map(company_id, *, strict=False):
    """Return {vendor_name_lower: agreement_dict} for currently-valid, active
    negotiated supplier agreements for a company.

    Reads company_supplier_agreements (enterprise schema). Fully guarded: any
    missing app context, DB error, or absent company_id yields an empty map so
    callers fall back to list prices. Compute-on-read only; never mutates catalog.
    """
    if not company_id or not has_app_context():
        return {}
    try:
        import MySQLdb.cursors
    except Exception:
        return {}
    discounts = {}
    try:
        mysql = current_app.mysql
        try:
            import db_compat

            db_compat.refresh_flask_mysql_connection(mysql)
        except Exception:
            pass
        cur = mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """
            SELECT vendor_name, discount_type, discount_value,
                   agreement_name, agreement_reference, valid_until, min_participants
            FROM company_supplier_agreements
            WHERE company_id = %s AND is_active = 1
              AND (valid_from IS NULL OR valid_from <= CURDATE())
              AND (valid_until IS NULL OR valid_until >= CURDATE())
            """,
            (company_id,),
        )
        for row in cur.fetchall():
            vendor = (row.get("vendor_name") or "").strip()
            if not vendor:
                continue
            try:
                value = float(row.get("discount_value")) if row.get("discount_value") is not None else 0.0
            except (TypeError, ValueError):
                value = 0.0
            discounts[vendor.lower()] = {
                "vendor_name": vendor,
                "discount_type": (row.get("discount_type") or "percentage"),
                "discount_value": value,
                "agreement_name": row.get("agreement_name") or "",
                "agreement_reference": row.get("agreement_reference") or "",
                "valid_until": row.get("valid_until"),
                "min_participants": row.get("min_participants"),
            }
        cur.close()
    except Exception as exc:
        if strict:
            raise ValueError('Aftaleprisen kunne ikke kontrolleres. Prøv igen, før bestillingen oprettes.') from exc
        try:
            current_app.logger.warning("Company discount lookup failed: %s", exc)
        except Exception:
            pass
        return {}
    return discounts


def apply_discount_to_price(price, agreement, *, participants=1):
    """Compute the effective price for a single list price given an agreement.

    Returns None when no meaningful discount applies (so callers keep the list
    price). Supports percentage / fixed_amount / fixed_price discount types.
    """
    if agreement is None or price is None or int(agreement.get("min_participants") or 1) > participants:
        return None
    try:
        list_price = float(price)
    except (TypeError, ValueError):
        return None
    if list_price <= 0:
        return None
    dtype = (agreement.get("discount_type") or "percentage").lower()
    try:
        value = float(agreement.get("discount_value") or 0)
    except (TypeError, ValueError):
        value = 0.0
    if dtype == "percentage":
        if value <= 0:
            return None
        pct = min(value, 100.0)
        effective = list_price * (1 - pct / 100.0)
    elif dtype == "fixed_amount":
        if value <= 0:
            return None
        effective = list_price - value
    elif dtype == "fixed_price":
        if value <= 0 or value >= list_price:
            return None
        effective = value
    else:
        return None
    effective = max(round(effective, 2), 0.0)
    if effective >= list_price:
        return None
    return effective


def decorate_product_with_discount(product, agreement):
    """Return a shallow copy of a normalized product enriched with negotiated
    pricing fields for the current company. Never mutates the cached product.

    Adds (when an agreement actually lowers the price):
      has_agreement, agreement, discount_percent, discount_label,
      original_price_min, discounted_price_min, discounted_price_label,
      and per-variant discounted_price / discounted_price_label.
    """
    if not agreement:
        return product
    effective_min = apply_discount_to_price(product.get("price_min"), agreement)
    discounted_variants = []
    any_variant_discount = False
    for variant in product.get("variants") or []:
        eff = apply_discount_to_price(variant.get("price"), agreement)
        if eff is not None:
            any_variant_discount = True
            new_variant = dict(variant)
            new_variant["discounted_price"] = eff
            new_variant["discounted_price_label"] = format_price(eff)
            discounted_variants.append(new_variant)
        else:
            discounted_variants.append(variant)
    if effective_min is None and not any_variant_discount:
        return product

    decorated = dict(product)
    decorated["variants"] = discounted_variants
    decorated["has_agreement"] = True
    decorated["agreement"] = agreement

    dtype = (agreement.get("discount_type") or "percentage").lower()
    if dtype == "percentage" and agreement.get("discount_value"):
        try:
            pct = int(round(float(agreement.get("discount_value"))))
            decorated["discount_percent"] = pct
            decorated["discount_label"] = f"-{pct}%"
        except (TypeError, ValueError):
            decorated["discount_label"] = "Aftalepris"
    else:
        decorated["discount_label"] = "Aftalepris"

    if effective_min is not None:
        decorated["original_price_min"] = product.get("price_min")
        decorated["original_price_label"] = product.get("price_label")
        decorated["discounted_price_min"] = effective_min
        decorated["discounted_price_label"] = format_price(effective_min)
    return decorated


def decorate_products_with_discounts(products, company_id=None, discount_map=None):
    """Apply company negotiated discounts to a list of products (compute-on-read).

    For anonymous / non-company users (no company_id and no map) the products are
    returned unchanged so list prices are preserved. Backward-compatible.
    """
    if discount_map is None:
        if not company_id:
            return products
        discount_map = get_company_discount_map(company_id)
    if not discount_map:
        return products
    decorated = []
    for product in products:
        agreement = discount_map.get((product.get("vendor") or "").lower())
        decorated.append(decorate_product_with_discount(product, agreement) if agreement else product)
    return decorated


SORT_OPTIONS = ("relevance", "price_asc", "price_desc", "vendor", "title", "title_desc")


def search_products(filters=None, page=1, per_page=24, company_id=None):
    filters = filters or {}
    q = (filters.get("q") or "").strip().lower()
    # Categories are multi-select (OR): a product matches if it's in ANY selected
    # category. Accept a list ("categories") or a single legacy "category".
    category_slugs = filters.get("categories")
    if not category_slugs:
        category_slugs = [filters["category"]] if filters.get("category") else []
    category_slugs = [c for c in category_slugs if c]
    vendor_slug = filters.get("vendor") or ""
    fmt = (filters.get("format") or "").strip().lower()
    location = (filters.get("location") or "").strip().lower()
    sort = filters.get("sort") or "relevance"
    if sort not in SORT_OPTIONS:
        sort = "relevance"
    price_min = parse_price(filters.get("price_min"))
    price_max = parse_price(filters.get("price_max"))
    # Normalise a swapped price range so "min 5000, max 1000" still behaves.
    if price_min is not None and price_max is not None and price_min > price_max:
        price_min, price_max = price_max, price_min

    # Multi-word query → individual tokens (≥2 chars). Used for AND/OR matching
    # and for ranking. Computed once, not per product.
    q_tokens = [t for t in re.findall(r"[a-zæøå0-9]+", q) if len(t) > 1] if q else []

    def passes_facets(product):
        if category_slugs and not any(cs in product["category_slugs"] for cs in category_slugs):
            return False
        if vendor_slug and product["vendor_slug"] != vendor_slug:
            return False
        if fmt and product["format"].lower() != fmt:
            return False
        if location and not any(location in loc.lower() for loc in product["locations"]):
            return False
        if price_min is not None and (product["price_min"] is None or product["price_min"] < price_min):
            return False
        if price_max is not None and (product["price_min"] is None or product["price_min"] > price_max):
            return False
        return True

    # Only build the (expensive) per-product search text when there is actually a
    # query — the common browse case skips ~60k concatenations — and memoise it so
    # the relevance sort below doesn't recompute it.
    search_text_cache = {}

    def text_of(product):
        cached = search_text_cache.get(product["handle"])
        if cached is None:
            cached = product_search_text(product)
            search_text_cache[product["handle"]] = cached
        return cached

    matched = []
    relaxed = []  # any-token matches, used only if the strict pass finds nothing
    for product in get_products():
        if not passes_facets(product):
            continue
        if not q:
            matched.append(product)
            continue
        text = text_of(product)
        if q in text:
            matched.append(product)
        elif q_tokens:
            hits = sum(1 for tok in q_tokens if tok in text)
            if hits == len(q_tokens):
                matched.append(product)
            elif hits:
                relaxed.append(product)

    # If a multi-word / near-miss query matched nothing strictly, fall back to the
    # closest "any term" matches instead of dead-ending the user on an empty page.
    used_relaxed = False
    if q and not matched and relaxed:
        matched = relaxed
        used_relaxed = True

    def relevance_key(product):
        title = product["title"].lower()
        if not q:
            return (0, title)
        vendor = product["vendor"].lower()
        text = text_of(product)
        score = 0
        if title == q:
            score += 100
        if title.startswith(q):
            score += 40
        if q in title:
            score += 25
        if q_tokens and all(tok in title for tok in q_tokens):
            score += 15
        if q in vendor:
            score += 12
        if any(q in (cat or "").lower() for cat in product.get("categories") or []):
            score += 8
        for tok in q_tokens:
            if tok in title:
                score += 4
            elif tok in vendor:
                score += 2
            elif tok in text:
                score += 1
        return (-score, title)

    if sort == "price_asc":
        matched.sort(key=lambda p: (p["price_min"] is None, p["price_min"] or 0, p["title"].lower()))
    elif sort == "price_desc":
        matched.sort(key=lambda p: (p["price_min"] is None, -(p["price_min"] or 0), p["title"].lower()))
    elif sort == "vendor":
        matched.sort(key=lambda p: (p["vendor"].lower(), p["title"].lower()))
    elif sort == "title":
        matched.sort(key=lambda p: p["title"].lower())
    elif sort == "title_desc":
        matched.sort(key=lambda p: p["title"].lower(), reverse=True)
    else:
        matched.sort(key=relevance_key)

    total = len(matched)
    page = max(int(page or 1), 1)
    per_page = max(min(int(per_page or 24), 60), 1)
    start = (page - 1) * per_page
    end = start + per_page
    total_pages = max((total + per_page - 1) // per_page, 1)
    page_products = matched[start:end]
    # Compute-on-read negotiated discounts for logged-in company users only.
    # Anonymous / non-company callers (company_id falsy) get list prices unchanged.
    if company_id:
        page_products = decorate_products_with_discounts(page_products, company_id=company_id)
    return {
        "products": page_products,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
        "relaxed": used_relaxed,
    }


def get_related_products(product, limit=4):
    if not product:
        return []
    # Scoring scans the whole catalog; the result (undiscounted) only changes when
    # the catalog does, so memoise per (handle, limit) keyed by the file signature.
    # Discounts are applied by the caller AFTER this, so the cache stays per-user-safe.
    handle = product.get("handle")
    signature = _signature()
    cache = _CACHE.get("related")
    if cache is None or _CACHE.get("related_signature") != signature:
        cache = {}
        _CACHE["related"] = cache
        _CACHE["related_signature"] = signature
    cache_key = (handle, limit)
    if cache_key in cache:
        return cache[cache_key]
    scores = []
    category_set = set(product.get("category_slugs") or [])
    for candidate in get_products():
        if candidate["handle"] == product["handle"]:
            continue
        score = 0
        if candidate["vendor"] == product["vendor"]:
            score += 3
        score += len(category_set.intersection(candidate.get("category_slugs") or [])) * 4
        if candidate.get("format") == product.get("format"):
            score += 1
        if score:
            scores.append((score, candidate["title"].lower(), candidate))
    scores.sort(key=lambda item: (-item[0], item[1]))
    related = [item[2] for item in scores[:limit]]
    cache[cache_key] = related
    return related


def build_product_url(handle):
    if str(handle).startswith("internal:"):
        return "/interne-kurser/" + str(handle).split(":",1)[1]
    return f"/products/{handle}"


def build_ask_ai_url(product):
    """Deep link into the learner chat that opens a fresh conversation about this
    course (chat.js sends ``?intent=`` as the first message). The legacy
    ``/app1?product_handle=`` link redirected to /chat and lost the course."""
    from urllib.parse import urlencode
    title = (product.get("title") or product.get("handle") or "").strip()
    return "/chat?" + urlencode({"intent": 'Fortæl mig mere om kurset "%s"' % title[:120]})


def _header_value(row, *names):
    normalized = {
        re.sub(r"[^a-z0-9_]+", "_", key.lower()).strip("_"): value
        for key, value in row.items()
        if key
    }
    for name in names:
        key = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
        value = normalized.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _csv_handle(title, handle):
    return slugify(handle or title)


def _same_vendor(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


def _existing_vendor_by_handle():
    """{handle: vendor name} for every product currently in the catalog."""
    out = {}
    try:
        for product in load_raw_products():
            handle = product.get("handle")
            if handle:
                out[handle] = (product.get("vendor") or "").strip()
    except Exception:
        pass
    return out


def parse_catalog_csv(file_storage, force_vendor=None):
    """Parse a catalog CSV into an import draft payload.

    ``force_vendor`` (S-2.7) is set for vendor-portal uploads: the vendor is
    taken from the logged-in session, NEVER from the CSV's vendor column, and any
    row whose handle already belongs to a DIFFERENT vendor is dropped with an
    issue instead of being allowed to overwrite that vendor's course.
    """
    raw = file_storage.read()
    if isinstance(raw, bytes):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        text = raw
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"

    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    by_handle = {}
    issues = []
    existing_vendors = _existing_vendor_by_handle() if force_vendor else {}
    blocked_handles = set()

    for line_no, row in enumerate(reader, start=2):
        title = _header_value(row, "title", "titel", "course_title", "kursus")
        if not title:
            issues.append({"line": line_no, "message": "Mangler titel"})
            continue
        handle = _csv_handle(title, _header_value(row, "handle", "slug"))
        if force_vendor:
            vendor = str(force_vendor).strip()
            owner = existing_vendors.get(handle)
            if owner and not _same_vendor(owner, vendor):
                if handle not in blocked_handles:
                    blocked_handles.add(handle)
                    issues.append({
                        "line": line_no,
                        "message": "Handlen '%s' tilhører en anden leverandør og blev sprunget over" % handle,
                    })
                continue
        else:
            vendor = _header_value(row, "vendor", "leverandor", "leverandoer", "udbyder") or "Ukendt"
        description = _header_value(row, "description", "beskrivelse", "body_html")
        summary = _header_value(row, "summary", "ai_summary", "kort_beskrivelse")
        categories = split_multi_value(_header_value(row, "categories", "category", "kategori", "kategorier"))
        tags = split_multi_value(_header_value(row, "tags", "emner"))
        product_type = _header_value(row, "product_type", "type") or "Kursus"
        fmt = _header_value(row, "format")
        image_url = _header_value(row, "image_url", "image", "billede")
        price = _header_value(row, "price", "pris")
        location = _header_value(row, "location", "lokation", "sted")
        date_value = _header_value(row, "date", "dato", "tidspunkt", "startdato")

        combined_tags = list(dict.fromkeys(categories + tags + ([fmt] if fmt else [])))
        product = by_handle.setdefault(handle, {
            "id": f"csv-{handle}",
            "handle": handle,
            "title": title,
            "vendor": vendor,
            "product_type": product_type,
            "tags": ", ".join(combined_tags),
            "body_html": description,
            "ai_summary": summary,
            "image": {"src": image_url} if image_url else None,
            "variants": [],
            "_catalog_source": "csv",
        })

        if description and not product.get("body_html"):
            product["body_html"] = description
        if summary and not product.get("ai_summary"):
            product["ai_summary"] = summary
        if image_url and not product.get("image"):
            product["image"] = {"src": image_url}
        if combined_tags:
            existing_tags = split_tags(product.get("tags"))
            product["tags"] = ", ".join(list(dict.fromkeys(existing_tags + combined_tags)))

        product["variants"].append({
            "id": f"csv-{handle}-{len(product['variants']) + 1}",
            "title": " / ".join(part for part in [location, date_value] if part) or title,
            "price": str(parse_price(price) or 0),
            "option1": location,
            "option2": date_value,
        })

    products = list(by_handle.values())
    current_handles = {product["handle"] for product in get_products()}
    created = sum(1 for product in products if product["handle"] not in current_handles)
    updated = len(products) - created
    return {
        "products": products,
        "issues": issues,
        "forced_vendor": str(force_vendor).strip() if force_vendor else None,
        "summary": {
            "created": created,
            "updated": updated,
            "skipped": len(issues),
            "total_rows": max(len(products) + len(issues), 0),
        },
    }


def save_import_draft(parsed, filename="", uploaded_by=""):
    job_id = uuid.uuid4().hex[:12]
    payload = {
        "job_id": job_id,
        "filename": filename,
        "uploaded_by": uploaded_by,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "status": "draft",
        **parsed,
    }
    _write_json(_instance_path(IMPORT_DRAFT_DIR, f"{job_id}.json"), payload)
    return payload


def get_import_draft(job_id):
    return _read_json(_instance_path(IMPORT_DRAFT_DIR, f"{job_id}.json"), None)


def list_import_drafts():
    folder = _instance_path(IMPORT_DRAFT_DIR)
    if not os.path.isdir(folder):
        return []
    drafts = []
    for name in os.listdir(folder):
        if name.endswith(".json"):
            draft = _read_json(os.path.join(folder, name), None)
            if draft:
                drafts.append(draft)
    drafts.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return drafts


@_serialized_catalog_write
def confirm_import_draft(job_id):
    draft = get_import_draft(job_id)
    if not draft:
        return None
    payload = _read_json(_instance_path(IMPORT_PRODUCTS_FILE), {"products": []})
    if draft.get('status') == 'confirmed':
        return draft
    if draft.get('direct_edit'):
        handle = draft['products'][0]['handle']
        current = get_product_any(handle)
        if not current or not _same_vendor(current.get('vendor'),draft.get('forced_vendor')):
            raise ValueError('Kurset tilhører ikke længere denne leverandør.')
        overlay = _read_json(_instance_path(CATALOG_OVERLAY_FILE), {'products': {}})
        applied = (overlay.get('products', {}).get(handle) or {}).get('applied_drafts') or []
        if job_id not in applied:
            update_product(handle,draft['edit_fields'],actor='approved vendor edit',expected_revision=draft.get('expected_revision'),publication_id=job_id)
        draft['status']='confirmed'
        draft['confirmed_at']=datetime.datetime.utcnow().isoformat()+'Z'
        _write_json(_instance_path(IMPORT_DRAFT_DIR,f'{job_id}.json'),draft)
        return draft
    existing = {product.get("handle"): product for product in payload.get("products", []) if product.get("handle")}
    # S-2.7: enforced again at confirm time, because the draft is a file on disk.
    # A vendor-uploaded draft can only (re)write products of THAT vendor or new
    # handles; it can never take over a handle another vendor already owns, and
    # its vendor field is pinned to the uploader regardless of what the file says.
    uploader = str(draft.get("uploaded_by") or "")
    pinned_vendor = draft.get("forced_vendor") if uploader.startswith("vendor:") else None
    owners = _existing_vendor_by_handle() if uploader.startswith("vendor:") else {}
    skipped_handles = []
    for product in draft.get("products", []):
        handle = product.get("handle")
        if not handle:
            continue
        if uploader.startswith("vendor:"):
            if not pinned_vendor:
                skipped_handles.append(handle)
                continue
            owner = owners.get(handle)
            if owner and not _same_vendor(owner, pinned_vendor):
                skipped_handles.append(handle)
                continue
            product = dict(product)
            product["vendor"] = pinned_vendor
        existing[handle] = product
    if skipped_handles:
        draft["skipped_handles"] = skipped_handles
    payload = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "products": list(existing.values()),
    }
    _write_json(_instance_path(IMPORT_PRODUCTS_FILE), payload)
    draft["status"] = "confirmed"
    draft["confirmed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    _write_json(_instance_path(IMPORT_DRAFT_DIR, f"{job_id}.json"), draft)
    clear_catalog_cache()
    _notify_catalog_changed()
    return draft


def delete_import_draft(job_id):
    path = _instance_path(IMPORT_DRAFT_DIR, f"{job_id}.json")
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def create_ai_category_job(created_by=""):
    products = get_products()
    job_id = uuid.uuid4().hex[:12]
    payload = {
        "job_id": job_id,
        "created_by": created_by,
        "created_at": datetime.datetime.utcnow().isoformat() + "Z",
        "status": "draft",
        "total": len(products),
        "processed": 0,
        "handles": [product["handle"] for product in products],
        "results": {},
        "errors": [],
    }
    _write_json(_instance_path(AI_CATEGORY_DRAFT_DIR, f"{job_id}.json"), payload)
    return payload


def get_ai_category_job(job_id):
    return _read_json(_instance_path(AI_CATEGORY_DRAFT_DIR, f"{job_id}.json"), None)


def list_ai_category_jobs():
    folder = _instance_path(AI_CATEGORY_DRAFT_DIR)
    if not os.path.isdir(folder):
        return []
    jobs = []
    for name in os.listdir(folder):
        if name.endswith(".json"):
            job = _read_json(os.path.join(folder, name), None)
            if job:
                jobs.append(job)
    jobs.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return jobs


def _parse_openai_json(raw):
    raw = (raw or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"(\[.*\]|\{.*\})", raw, flags=re.S)
    if match:
        raw = match.group(1)
    return json.loads(raw)


def _category_prompt(batch, allowed_categories):
    lines = []
    for idx, product in enumerate(batch, start=1):
        tags = ", ".join(product.get("tags") or [])
        categories = ", ".join(product.get("categories") or [])
        description = product.get("description_excerpt") or product.get("summary") or ""
        lines.append(
            f"{idx}. handle={product['handle']}\n"
            f"Title: {product['title']}\n"
            f"Vendor: {product['vendor']}\n"
            f"Current categories: {categories}\n"
            f"Tags: {tags}\n"
            f"Description: {description[:450]}"
        )

    return [
        {
            "role": "system",
            "content": (
                "You categorize Danish course catalog products for Futurematch. "
                "Choose 1-3 broad, user-friendly categories per course. Prefer the allowed "
                "categories when they fit, but you may create a concise Danish category if none fits. "
                "Return JSON only as an array of objects: "
                "[{\"handle\":\"...\",\"categories\":[\"...\"],\"reason\":\"short\"}]."
            ),
        },
        {
            "role": "user",
            "content": (
                "Allowed categories:\n"
                + ", ".join(allowed_categories[:80])
                + "\n\nCourses:\n"
                + "\n\n".join(lines)
            ),
        },
    ]


def _call_openai_category_batch(batch, allowed_categories):
    """AI categorisation through the ACTIVE provider (OpenAI or Claude, per the
    admin toggle) instead of a hard-wired OpenAI call."""
    import ai_runtime

    messages = _category_prompt(batch, allowed_categories)
    text = ai_runtime.run_direct_completion(messages, model=ai_runtime.fast_model(), max_tokens=1200)
    parsed = _parse_openai_json(text)
    if isinstance(parsed, dict):
        parsed = parsed.get("results") or []
    return parsed if isinstance(parsed, list) else []


def process_ai_category_batch(job_id, batch_size=8):
    job = get_ai_category_job(job_id)
    if not job:
        return None
    products_by_handle = {product["handle"]: product for product in get_products()}
    pending = [handle for handle in job.get("handles", []) if handle not in job.get("results", {})]
    batch_handles = pending[: max(1, min(int(batch_size or 8), 20))]
    batch = [products_by_handle[handle] for handle in batch_handles if handle in products_by_handle]
    if not batch:
        job["status"] = "complete"
        job["processed"] = len(job.get("results", {}))
        _write_json(_instance_path(AI_CATEGORY_DRAFT_DIR, f"{job_id}.json"), job)
        return job

    allowed_categories = [category["name"] for category in get_categories()]
    try:
        proposals = _call_openai_category_batch(batch, allowed_categories)
        proposals_by_handle = {
            proposal.get("handle"): proposal
            for proposal in proposals
            if isinstance(proposal, dict) and proposal.get("handle")
        }
        for product in batch:
            proposal = proposals_by_handle.get(product["handle"]) or {}
            proposed_categories = [
                str(category).strip()
                for category in proposal.get("categories", [])
                if str(category).strip()
            ][:3]
            if not proposed_categories:
                proposed_categories = product.get("categories") or ["Andet"]
            job["results"][product["handle"]] = {
                "handle": product["handle"],
                "title": product["title"],
                "vendor": product["vendor"],
                "current_categories": product.get("categories") or [],
                "proposed_categories": list(dict.fromkeys(proposed_categories)),
                "reason": proposal.get("reason", ""),
            }
    except Exception as exc:
        job.setdefault("errors", []).append({
            "at": datetime.datetime.utcnow().isoformat() + "Z",
            "handles": batch_handles,
            "message": str(exc),
        })
        for product in batch:
            job["results"][product["handle"]] = {
                "handle": product["handle"],
                "title": product["title"],
                "vendor": product["vendor"],
                "current_categories": product.get("categories") or [],
                "proposed_categories": product.get("categories") or ["Andet"],
                "reason": "Fallback after AI error",
            }

    job["processed"] = len(job.get("results", {}))
    if job["processed"] >= len(job.get("handles", [])):
        job["status"] = "complete"
    job["updated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    _write_json(_instance_path(AI_CATEGORY_DRAFT_DIR, f"{job_id}.json"), job)
    return job


def ai_category_diff(job):
    rows = []
    for result in (job or {}).get("results", {}).values():
        current = result.get("current_categories") or []
        proposed = result.get("proposed_categories") or []
        changed = [c.lower() for c in current] != [c.lower() for c in proposed]
        rows.append({**result, "changed": changed})
    rows.sort(key=lambda row: (not row["changed"], row.get("title", "").lower()))
    changed_count = sum(1 for row in rows if row["changed"])
    return {
        "rows": rows,
        "changed_count": changed_count,
        "unchanged_count": len(rows) - changed_count,
        "processed_count": len(rows),
    }


@_serialized_catalog_write
def confirm_ai_category_job(job_id):
    job = get_ai_category_job(job_id)
    if not job:
        return None
    diff = ai_category_diff(job)
    payload = _read_json(_instance_path(CATEGORY_OVERRIDES_FILE), {"overrides": {}})
    overrides = payload.get("overrides", {}) if isinstance(payload, dict) else {}
    for row in diff["rows"]:
        if row["changed"]:
            overrides[row["handle"]] = row.get("proposed_categories") or []
    payload = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "updated_by_job": job_id,
        "overrides": overrides,
    }
    _write_json(_instance_path(CATEGORY_OVERRIDES_FILE), payload)
    job["status"] = "confirmed"
    job["confirmed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    job["confirmed_changed_count"] = diff["changed_count"]
    _write_json(_instance_path(AI_CATEGORY_DRAFT_DIR, f"{job_id}.json"), job)
    clear_catalog_cache()
    _notify_catalog_changed()
    return job


def delete_ai_category_job(job_id):
    path = _instance_path(AI_CATEGORY_DRAFT_DIR, f"{job_id}.json")
    try:
        os.remove(path)
        return True
    except OSError:
        return False


# ── Admin product management (N-3.1) ───────────────────────────────────────

@_serialized_catalog_write
def _save_overlay_entry(handle, updater):
    payload = _read_json(_instance_path(CATALOG_OVERLAY_FILE), {"products": {}})
    if not isinstance(payload, dict):
        payload = {"products": {}}
    products = payload.setdefault("products", {})
    entry = products.get(handle) or {}
    updater(entry)
    entry["updated_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    products[handle] = entry
    payload["updated_at"] = entry["updated_at"]
    _write_json(_instance_path(CATALOG_OVERLAY_FILE), payload)
    clear_catalog_cache()
    _notify_catalog_changed()
    return entry


def set_product_status(handle, status, actor=""):
    """Publish (active), unpublish (hidden) or archive a product. Returns the
    overlay entry, or None for an unknown handle / status."""
    if status not in PRODUCT_STATUSES or not get_product_any(handle):
        return None

    def _upd(entry):
        entry["status"] = status
        entry["status_by"] = actor or ""
    return _save_overlay_entry(handle, _upd)


def set_products_status(handles, status, actor=""):
    done = 0
    for h in handles or []:
        if set_product_status(h, status, actor):
            done += 1
    return done


@_serialized_catalog_write
def update_product(handle, fields, actor="", expected_revision=None, publication_id=None):
    """Edit admin-editable fields (title, summary, vendor, tags, image_url)."""
    if not get_product_any(handle):
        return None
    clear_catalog_cache()
    if expected_revision and product_revision(get_product_any(handle)) != expected_revision:
        raise ValueError('Kurset er ændret siden du åbnede det. Genindlæs og gennemgå ændringerne.')
    clean = {}
    for key in EDITABLE_FIELDS:
        if key not in fields:
            continue
        val = fields[key]
        if key == "tags":
            val = split_tags(val) if not isinstance(val, list) else [str(t).strip() for t in val if str(t).strip()]
        elif isinstance(val, str):
            val = val.strip()
        clean[key] = val

    def _upd(entry):
        entry.update(clean)
        entry["edited_by"] = actor or ""
        if publication_id:
            entry['applied_drafts'] = list(dict.fromkeys((entry.get('applied_drafts') or []) + [publication_id]))
    return _save_overlay_entry(handle, _upd)


@_serialized_catalog_write
def reset_product_edits(handle):
    payload = _read_json(_instance_path(CATALOG_OVERLAY_FILE), {"products": {}})
    if isinstance(payload, dict) and handle in (payload.get("products") or {}):
        payload["products"].pop(handle, None)
        _write_json(_instance_path(CATALOG_OVERLAY_FILE), payload)
        clear_catalog_cache()
        _notify_catalog_changed()
        return True
    return False


def admin_list_products(q="", status="", vendor="", page=1, per_page=30):
    """Admin product browser: list + search + status filter, paginated."""
    items = get_all_products()
    ql = (q or "").strip().lower()
    if status in PRODUCT_STATUSES:
        items = [p for p in items if p.get("status") == status]
    if vendor:
        items = [p for p in items if p.get("vendor") == vendor]
    if ql:
        items = [p for p in items if ql in p["title"].lower() or ql in p["vendor"].lower()
                 or ql in (p.get("handle") or "").lower()]
    total = len(items)
    per_page = max(5, min(int(per_page or 30), 100))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(int(page or 1), pages))
    start = (page - 1) * per_page
    counts = {s: sum(1 for p in get_all_products() if p.get("status") == s) for s in PRODUCT_STATUSES}
    return {"products": items[start:start + per_page], "total": total, "page": page, "pages": pages,
            "per_page": per_page, "counts": counts}


_STALE_CACHE = {"key": None, "handles": frozenset()}


def stale_handles():
    """Handles whose latest explicit-year session is in the past (catalog_freshness
    rules). Cached per catalog signature + day."""
    key = (_signature(), datetime.date.today().isoformat())
    if _STALE_CACHE["key"] == key:
        return _STALE_CACHE["handles"]
    try:
        import catalog_freshness
        handles = frozenset(c["handle"] for c in catalog_freshness.stale_courses(limit=100000) if c.get("handle"))
    except Exception:
        handles = frozenset()
    _STALE_CACHE["key"], _STALE_CACHE["handles"] = key, handles
    return handles


def exclude_stale(products):
    """Drop stale courses from a list of normalized products (recommendations)."""
    stale = stale_handles()
    return [p for p in products or [] if p.get("handle") not in stale]


def _notify_catalog_changed():
    """Tell the search index the catalog changed (incremental embed, best effort)."""
    try:
        from app1 import rag
        rag.on_catalog_changed()
    except Exception:
        pass


def catalog_stats():
    products = get_products()
    return {
        "products": len(products),
        "categories": len(get_categories(products)),
        "vendors": len(get_vendors(products)),
        "csv_products": sum(1 for product in products if product.get("source") == "csv"),
        "last_loaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def product_revision(product):
    fields = {key:(product or {}).get(key) for key in ('handle','title','summary','vendor','variants','cancellation_terms','status')}
    return hashlib.sha256(json.dumps(fields,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def session_fields_from_form(form):
    """Validate the complete session editor before any live write."""
    from enrollment_service import money
    ids=form.getlist('session_id');dates=form.getlist('session_date');places=form.getlist('session_location')
    prices=form.getlist('session_price');seats=form.getlist('session_seats')
    if len({len(ids),len(dates),len(places),len(prices),len(seats)}) != 1 or len(ids)>100:
        raise ValueError('Holdformularen er ufuldstændig. Genindlæs siden.')
    removed=set(form.getlist('remove_session'));variants=[];seen=set()
    for sid,date,place,price,stock in zip(ids,dates,places,prices,seats):
        if sid in removed:continue
        if not any((date.strip(),place.strip(),price.strip(),stock.strip())):continue
        sid=sid or 'session-'+uuid.uuid4().hex[:24]
        if sid in seen:raise ValueError('To hold har samme id. Genindlæs siden.')
        seen.add(sid)
        from calendar_service import parse_danish_date
        if date.strip() and date.strip().lower() not in ('efter aftale','løbende','on demand') and not parse_danish_date(date):
            raise ValueError('Angiv en gyldig dato eller skriv efter aftale.')
        amount=money(price.strip().replace(' ', '').replace('.', '').replace(',', '.') if ',' in price else price.strip())
        if stock.strip():
            try:quantity=int(stock)
            except ValueError:raise ValueError('Ledige pladser skal være et helt tal.') from None
            if quantity<0:raise ValueError('Ledige pladser må ikke være negativt.')
        else:quantity=None
        variants.append({'id':sid,'price':str(amount),'date':date.strip()[:255],'location':place.strip()[:500],'seats':quantity})
    if not variants:raise ValueError('Tilføj mindst ét hold, eventuelt med dato efter aftale.')
    return {'variants':variants,'cancellation_terms':form.get('cancellation_terms','').strip()[:4000]}


def session_editor_context(product, form=None):
    if form is not None:
        rows=[dict(session_id=sid,date=date,location=place,price=price,seats=seats) for sid,date,place,price,seats in zip(form.getlist('session_id'),form.getlist('session_date'),form.getlist('session_location'),form.getlist('session_price'),form.getlist('session_seats'))]
        return {'session_rows':rows,'revision':form.get('revision',''),'cancellation_terms':form.get('cancellation_terms','')}
    return {'session_rows':list((product or {}).get('variants') or []) + [{'price':None,'seats':None} for _ in range(3)],'revision':product_revision(product),'cancellation_terms':(product or {}).get('cancellation_terms') or ''}
