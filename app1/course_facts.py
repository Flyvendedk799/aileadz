"""Canonical course/session facts shared by tools, prose and cards.

One normalized record so inventory, city, date and price never disagree between
the chat answer and the Futurematch course cards (QA L03/L04/L10).
"""
from __future__ import annotations

from typing import Any, Optional


def availability_from_seats(seats: Optional[int], *, source: str = "unknown") -> dict:
    """Map a normalized seat count (None = untracked) to an explicit availability fact."""
    if seats is None:
        return {
            "status": "unknown",
            "count": None,
            "label_da": "Tilgængelighed ikke oplyst",
            "source": source or "untracked",
        }
    try:
        n = int(seats)
    except (TypeError, ValueError):
        return {
            "status": "unknown",
            "count": None,
            "label_da": "Tilgængelighed ikke oplyst",
            "source": "invalid",
        }
    if n <= 0:
        return {
            "status": "sold_out",
            "count": 0,
            "label_da": "0 pladser",
            "source": source or "tracked",
        }
    return {
        "status": "known",
        "count": n,
        "label_da": ("Ledig" if n > 3 else f"{n} pladser"),
        "source": source or "tracked",
    }


def variant_availability(variant: dict | None) -> dict:
    """Availability for one raw or normalized variant.

    Prefer already-normalized ``seats`` (from catalog_service.normalize_variant);
    otherwise re-derive using the same tracked-inventory rules.
    """
    v = variant if isinstance(variant, dict) else {}
    if "seats" in v and not (
        "inventory_management" in v or "inventory_quantity" in v or "available" in v
    ):
        # Already normalized compact variant.
        src = "normalized"
        return availability_from_seats(v.get("seats"), source=src)

    tracked = bool(str(v.get("inventory_management") or "").strip())
    if tracked:
        try:
            seats = int(v.get("inventory_quantity"))
        except (TypeError, ValueError):
            return availability_from_seats(None, source="invalid")
        if seats <= 0 and str(v.get("inventory_policy") or "").lower() == "continue":
            return availability_from_seats(None, source="oversell_policy")
        return availability_from_seats(seats, source="tracked_inventory")
    if v.get("available") is False:
        return availability_from_seats(0, source="available_flag")
    if v.get("seats") is not None:
        try:
            return availability_from_seats(int(v.get("seats")), source="seats_field")
        except (TypeError, ValueError):
            return availability_from_seats(None, source="invalid")
    # Untracked raw 0 / missing → unknown (never invent 99 or "Ledig").
    return availability_from_seats(None, source="untracked")


def session_fact(variant: dict, *, product: dict | None = None) -> dict:
    """One session (date + city + price + availability) from a variant."""
    from catalog_service import normalize_variant, extract_city_name, format_price, parse_price

    raw = variant if isinstance(variant, dict) else {}
    # If already normalized (has city + seats key without inventory_*), reuse.
    if "city" in raw and "price_label" in raw:
        nv = raw
    else:
        fallback = None
        if product and (product.get("variants") or []):
            fallback = (product.get("variants") or [{}])[0].get("price")
        nv = normalize_variant(raw, fallback_price=fallback)

    avail = availability_from_seats(nv.get("seats"), source="normalized")
    city = (nv.get("city") or extract_city_name(nv.get("location") or "") or "").strip()
    price = nv.get("price")
    if price is None:
        price = parse_price(raw.get("price"))
    return {
        "session_id": nv.get("session_id") or nv.get("id"),
        "date": (nv.get("date") or "").strip() or None,
        "location": (nv.get("location") or "").strip() or None,
        "city": city or None,
        "price": price,
        "price_label": nv.get("price_label") or format_price(price),
        "availability": avail,
        "seats": avail["count"] if avail["status"] != "unknown" else None,
    }


def course_fact_bundle(product: dict, *, location_filter: str = "", exact_city: bool = False) -> dict:
    """Canonical course + matching-session bundle for tools and cards."""
    p = product if isinstance(product, dict) else {}
    variants = [v for v in (p.get("variants") or []) if isinstance(v, dict)]
    sessions = [session_fact(v, product=p) for v in variants]

    loc_q = (location_filter or "").strip().lower()
    matching, nearby = [], []
    if loc_q:
        for s in sessions:
            city = (s.get("city") or "").lower()
            loc = (s.get("location") or "").lower()
            hay = f"{city} {loc}"
            if loc_q in hay or (city and city == loc_q):
                matching.append(s)
            elif not exact_city:
                nearby.append(s)
    else:
        matching = list(sessions)

    return {
        "title": p.get("title") or "",
        "handle": p.get("handle") or "",
        "vendor": p.get("vendor") or "",
        "sessions": sessions,
        "matching_sessions": matching,
        "nearby_sessions": nearby if loc_q and not exact_city else [],
        "location_filter": location_filter or None,
        "exact_city": bool(exact_city),
        "has_exact_city_match": bool(matching) if loc_q else None,
    }


def card_variant_payload(variant: dict, product: dict | None = None) -> dict:
    """Shape expected by Futurematch courseCard() for one session row."""
    fact = session_fact(variant, product=product)
    avail = fact["availability"]
    seats_value: Any
    if avail["status"] == "unknown":
        seats_value = None
    else:
        seats_value = avail["count"]
    return {
        "date": fact["date"] or "Efter aftale",
        "loc": fact["city"] or fact["location"] or ((product or {}).get("location") if product else None) or "Online",
        "seats": seats_value,
        "availability": avail["status"],
        "availability_label": avail["label_da"],
    }
