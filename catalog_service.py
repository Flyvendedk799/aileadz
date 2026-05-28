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

GENERIC_PRODUCT_TYPES = {"", "kursus"}

_CACHE = {
    "signature": None,
    "raw": None,
    "products": None,
    "by_handle": None,
    "categories": None,
    "vendors": None,
}


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


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def clear_catalog_cache():
    for key in _CACHE:
        _CACHE[key] = None


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
    return {
        "id": variant.get("id") if isinstance(variant, dict) else None,
        "title": title,
        "price": price,
        "price_label": format_price(price),
        "location": location,
        "city": extract_city_name(location),
        "date": date,
    }


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
    paths = [
        _data_path(SOURCE_FILE),
        _data_path(AUGMENTED_FILE),
        _instance_path(CATEGORY_OVERRIDES_FILE),
        _instance_path(IMPORT_PRODUCTS_FILE),
    ]
    return tuple((path, _mtime(path)) for path in paths)


def load_raw_products():
    signature = _signature()
    if _CACHE["raw"] is not None and _CACHE["signature"] == signature:
        return _CACHE["raw"]

    source_products = _read_json(_data_path(SOURCE_FILE), [])
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
    _CACHE["raw"] = raw_products
    _CACHE["signature"] = signature
    _CACHE["products"] = None
    _CACHE["by_handle"] = None
    _CACHE["categories"] = None
    _CACHE["vendors"] = None
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
        "source": raw_product.get("_catalog_source") or "shopify_json",
        "raw": raw_product,
    }


def get_products():
    signature = _signature()
    if _CACHE["products"] is not None and _CACHE["signature"] == signature:
        return _CACHE["products"]
    overrides = _load_category_overrides()
    products = [normalize_product(product, overrides=overrides) for product in load_raw_products()]
    products.sort(key=lambda p: (p["title"].lower(), p["vendor"].lower()))
    _CACHE["products"] = products
    _CACHE["by_handle"] = {p["handle"]: p for p in products}
    return products


def warm_catalog():
    """Eager-load normalized catalog products for chat tools."""
    return get_products()


def get_product(handle):
    if _CACHE["by_handle"] is None:
        get_products()
    return _CACHE["by_handle"].get(handle)


def get_categories(products=None):
    products = products or get_products()
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
    return categories


def get_category(slug):
    for category in get_categories():
        if category["slug"] == slug:
            return category
    return None


def _load_vendor_profiles():
    payload = _read_json(_data_path(VENDOR_PROFILES_FILE), {})
    return payload if isinstance(payload, dict) else {}


def get_vendors(products=None):
    products = products or get_products()
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
    return vendors


def get_vendor(slug):
    for vendor in get_vendors():
        if vendor["slug"] == slug:
            return vendor
    return None


def get_filter_options(products=None):
    products = products or get_products()
    formats = sorted({p["format"] for p in products if p.get("format")})
    locations = Counter(loc for p in products for loc in p["locations"])
    return {
        "categories": get_categories(products),
        "vendors": get_vendors(products),
        "formats": formats,
        "locations": [{"name": name, "count": count} for name, count in locations.most_common(30)],
    }


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


def search_products(filters=None, page=1, per_page=24):
    filters = filters or {}
    q = (filters.get("q") or "").strip().lower()
    category_slug = filters.get("category") or ""
    vendor_slug = filters.get("vendor") or ""
    fmt = (filters.get("format") or "").strip().lower()
    location = (filters.get("location") or "").strip().lower()
    sort = filters.get("sort") or "relevance"
    price_min = parse_price(filters.get("price_min"))
    price_max = parse_price(filters.get("price_max"))

    matched = []
    for product in get_products():
        if category_slug and category_slug not in product["category_slugs"]:
            continue
        if vendor_slug and product["vendor_slug"] != vendor_slug:
            continue
        if fmt and product["format"].lower() != fmt:
            continue
        if location and not any(location in loc.lower() for loc in product["locations"]):
            continue
        if price_min is not None and (product["price_min"] is None or product["price_min"] < price_min):
            continue
        if price_max is not None and (product["price_min"] is None or product["price_min"] > price_max):
            continue
        text = product_search_text(product)
        if q and q not in text:
            tokens = [token for token in re.findall(r"[a-zæøå0-9]+", q) if len(token) > 1]
            if tokens and not all(token in text for token in tokens):
                continue
        matched.append(product)

    def relevance_key(product):
        if not q:
            return (0, product["title"].lower())
        title = product["title"].lower()
        vendor = product["vendor"].lower()
        text = product_search_text(product)
        score = 0
        if q in title:
            score += 6
        if q in vendor:
            score += 3
        score += sum(1 for token in q.split() if token and token in text)
        return (-score, product["title"].lower())

    if sort == "price_asc":
        matched.sort(key=lambda p: (p["price_min"] is None, p["price_min"] or 0, p["title"].lower()))
    elif sort == "price_desc":
        matched.sort(key=lambda p: (p["price_min"] is None, -(p["price_min"] or 0), p["title"].lower()))
    elif sort == "vendor":
        matched.sort(key=lambda p: (p["vendor"].lower(), p["title"].lower()))
    else:
        matched.sort(key=relevance_key)

    total = len(matched)
    page = max(int(page or 1), 1)
    per_page = max(min(int(per_page or 24), 60), 1)
    start = (page - 1) * per_page
    end = start + per_page
    total_pages = max((total + per_page - 1) // per_page, 1)
    return {
        "products": matched[start:end],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "has_prev": page > 1,
        "has_next": page < total_pages,
    }


def get_related_products(product, limit=4):
    if not product:
        return []
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
    return [item[2] for item in scores[:limit]]


def build_product_url(handle):
    return f"/products/{handle}"


def build_ask_ai_url(product):
    return f"/app1?product_handle={product['handle']}&product_title={product['title']}"


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


def parse_catalog_csv(file_storage):
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

    for line_no, row in enumerate(reader, start=2):
        title = _header_value(row, "title", "titel", "course_title", "kursus")
        if not title:
            issues.append({"line": line_no, "message": "Mangler titel"})
            continue
        handle = _csv_handle(title, _header_value(row, "handle", "slug"))
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


def confirm_import_draft(job_id):
    draft = get_import_draft(job_id)
    if not draft:
        return None
    payload = _read_json(_instance_path(IMPORT_PRODUCTS_FILE), {"products": []})
    existing = {product.get("handle"): product for product in payload.get("products", []) if product.get("handle")}
    for product in draft.get("products", []):
        if product.get("handle"):
            existing[product["handle"]] = product
    payload = {
        "updated_at": datetime.datetime.utcnow().isoformat() + "Z",
        "products": list(existing.values()),
    }
    _write_json(_instance_path(IMPORT_PRODUCTS_FILE), payload)
    draft["status"] = "confirmed"
    draft["confirmed_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    _write_json(_instance_path(IMPORT_DRAFT_DIR, f"{job_id}.json"), draft)
    clear_catalog_cache()
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


def _call_openai_category_batch(batch, allowed_categories):
    import openai

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

    response = openai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
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
        ],
        temperature=0.1,
        max_tokens=1200,
    )
    parsed = _parse_openai_json(response.choices[0].message.content)
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
    return job


def delete_ai_category_job(job_id):
    path = _instance_path(AI_CATEGORY_DRAFT_DIR, f"{job_id}.json")
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def catalog_stats():
    products = get_products()
    return {
        "products": len(products),
        "categories": len(get_categories(products)),
        "vendors": len(get_vendors(products)),
        "csv_products": sum(1 for product in products if product.get("source") == "csv"),
        "last_loaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
