"""Canonical course quotes and enrolment for screens, AI tools and integrations.

Callers identify a course/session and participants; they never choose a charge.
The lifecycle service owns the transaction, approvals, budget and notifications.
"""

from __future__ import annotations

import datetime
import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def session_key(variant):
    if variant.get("id") not in (None, ""):
        return str(variant["id"])
    identity = [variant.get("date") or "", variant.get("location") or variant.get("city") or "", variant.get("title") or ""]
    return "session-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()[:24]


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError("invalid price")
        return amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("Kursets pris er ikke afklaret. Kontakt udbyderen før bestilling.") from None


def get_course(handle, company_id=None):
    import catalog_service

    if not str(handle).startswith("internal:"):
        return catalog_service.get_product(handle)
    from flask import current_app
    import MySQLdb.cursors

    try:
        course_id = int(str(handle).split(":", 1)[1])
    except (ValueError, IndexError):
        return None
    if not company_id:
        return None
    cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
    try:
        cur.execute("SELECT * FROM company_courses WHERE id = %s AND company_id = %s AND is_active = 1", (course_id, company_id))
        row = cur.fetchone()
        if not row:
            return None
        return {
            "handle": handle,
            "title": row["title"],
            "vendor": "Internt kursus",
            "internal_course_id": course_id,
            "price_min": row.get("price") or 0,
            "description_text": row.get("description") or "",
            "external_url": row.get("external_url") or "",
            "variants": [],
            "location": row.get("location") or "",
            "metadata": {"skills": row.get("skill_tags") or ""},
        }
    finally:
        cur.close()


def quote_course(handle, company_id=None, *, session_id=None, variant_date="", variant_location="", participants=1):
    import catalog_service
    from calendar_service import parse_danish_date

    product = get_course(handle, company_id)
    if not product:
        raise ValueError("Kurset er ikke længere tilgængeligt. Vælg et aktuelt kursus.")
    variants = product.get("variants") or []
    variant = None
    if session_id:
        variant = next((v for v in variants if session_key(v) == str(session_id)), None)
        if not variant:
            raise ValueError("Det valgte hold er ændret eller fjernet. Åbn kurset og vælg et hold igen.")
    elif variant_date or variant_location:
        matches = [
            v
            for v in variants
            if (not variant_date or v.get("date") == variant_date)
            and (not variant_location or (v.get("location") or v.get("city")) == variant_location)
        ]
        if len(matches) != 1:
            raise ValueError("Holdet kunne ikke genfindes entydigt. Vælg dato og sted igen.")
        variant = matches[0]
    elif len(variants) == 1:
        variant = variants[0]
    elif len(variants) > 1:
        raise ValueError("Vælg et bestemt hold med dato og sted før bestilling.")
    variant = variant or {}
    date_text = variant.get("date") or ""
    dated = parse_danish_date(date_text)
    if dated and dated < datetime.date.today():
        raise ValueError("Dette hold er allerede afholdt. Vælg en kommende dato.")
    count = max(1, int(participants))
    if variant.get("seats") is not None and int(variant["seats"]) < count:
        raise ValueError("Der er ikke nok ledige pladser på det valgte hold.")
    base = money(variant.get("price") if variant.get("price") is not None else product.get("price_min"))
    agreement = None
    if not product.get("internal_course_id") and company_id:
        agreement = catalog_service.get_company_discount_map(company_id, strict=True).get((product.get("vendor") or "").lower())
        if agreement and count < int(agreement.get("min_participants") or 1):
            agreement = None
    effective = catalog_service.apply_discount_to_price(float(base), agreement, participants=count) if agreement else None
    price = money(effective if effective is not None else base)
    return {
        "product_handle": handle,
        "product_title": product["title"],
        "vendor": product.get("vendor") or "",
        "session_id": session_key(variant) if variant else None,
        "variant_date": date_text,
        "variant_location": variant.get("location") or variant.get("city") or product.get("location") or "",
        "internal_course_id": product.get("internal_course_id"),
        "external_url": product.get("external_url") or "",
        "price": float(price),
        "list_price": float(base),
        "participants": count,
        "currency": "DKK",
        "agreement_reference": (agreement or {}).get("agreement_reference") or "",
        "booking_required": True,
        "cancellation_terms": product.get("cancellation_terms") or "",
        "quoted_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def create_order(ctx, *, product_handle, product_title="", price=None, variant_date="", variant_location="", extra=None, **kwargs):
    """Compatible call shape, but charge and course details come from current data."""
    import order_service

    extra = dict(extra or {})
    # API callers must identify a real employee rather than create an orphan
    # company order with a display name masquerading as a username.
    if ctx.company_id and ctx.user_id is None and not extra.get("assign_to"):
        cur = order_service._dict_cursor(order_service._get_connection())
        try:
            cur.execute(
                "SELECT cu.user_id,u.username,COALESCE(cu.full_name,u.username) AS name,COALESCE(cu.email,u.email) AS email,cu.department FROM company_users cu JOIN users u ON u.id=cu.user_id WHERE cu.company_id=%s AND cu.status='active' AND LOWER(COALESCE(cu.email,u.email))=LOWER(%s)",
                (ctx.company_id, kwargs.get("user_email") or ""),
            )
            people = list(cur.fetchall() or [])
            if len(people) != 1:
                return {
                    "success": False,
                    "error": "employee_required",
                    "message": "Vælg en aktiv medarbejder i virksomheden før bestilling.",
                }
            extra["assign_to"] = people[0]
        finally:
            cur.close()
    kwargs.pop("status", None)
    try:
        if ctx.company_id and not str(product_handle).startswith("internal:"):
            product = get_course(product_handle, ctx.company_id)
            if product:
                cur = order_service._dict_cursor(order_service._get_connection())
                try:
                    cur.execute(
                        "SELECT is_active FROM company_supplier_preferences WHERE company_id=%s AND vendor_name=%s",
                        (ctx.company_id, product.get("vendor") or ""),
                    )
                    preference = cur.fetchone()
                    if preference and not preference["is_active"]:
                        return {
                            "success": False,
                            "error": "supplier_inactive",
                            "message": "Virksomheden har slået denne udbyder fra. Kontakt HR om et alternativ.",
                        }
                finally:
                    cur.close()

        quote = quote_course(
            product_handle,
            ctx.company_id,
            session_id=extra.get("session_id"),
            variant_date=variant_date,
            variant_location=variant_location,
            participants=extra.get("participant_count", 1),
        )
    except (ValueError, TypeError) as exc:
        return {"success": False, "error": "quote_required", "message": str(exc)}
    except Exception:
        import logging

        logging.getLogger(__name__).exception("Course quote lookup failed")
        return {
            "success": False,
            "error": "quote_unavailable",
            "message": "Kursets pris og vilkår kunne ikke kontrolleres. Prøv igen om lidt.",
        }
    expected = extra.get("expected_price")
    try:
        changed = expected is not None and money(expected) != money(quote["price"])
    except ValueError as exc:
        return {"success": False, "error": "invalid_price", "message": str(exc)}
    if changed:
        return {
            "success": False,
            "error": "price_changed",
            "message": "Prisen er ændret. Gennemse den aktuelle pris og bekræft igen.",
            "quote": quote,
        }
    quote["compliance_requirement_id"] = extra.get("compliance_requirement_id")
    extra["quote"] = quote
    return order_service.create_order(
        ctx,
        product_handle=product_handle,
        product_title=quote["product_title"],
        price=quote["price"],
        variant_date=quote["variant_date"],
        variant_location=quote["variant_location"],
        extra=extra,
        **kwargs,
    )
