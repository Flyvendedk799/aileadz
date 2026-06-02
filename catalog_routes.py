from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
import db_compat  # noqa: F401
import MySQLdb.cursors

import catalog_service as catalog
from auth_decorators import login_required


catalog_bp = Blueprint("catalog", __name__, template_folder="templates")


def _query_filters(extra=None):
    filters = {
        "q": request.args.get("q", "").strip(),
        "category": request.args.get("category", "").strip(),
        "vendor": request.args.get("vendor", "").strip(),
        "format": request.args.get("format", "").strip(),
        "location": request.args.get("location", "").strip(),
        "price_min": request.args.get("price_min", "").strip(),
        "price_max": request.args.get("price_max", "").strip(),
        "sort": request.args.get("sort", "relevance").strip() or "relevance",
    }
    if extra:
        filters.update(extra)
    return filters


def _page():
    try:
        return max(int(request.args.get("page", 1)), 1)
    except (TypeError, ValueError):
        return 1


def _supplier_state(vendor_name):
    """Return company-specific supplier state for logged-in users; public users default to active."""
    company_id = session.get("company_id")
    if not company_id or not vendor_name:
        return {"known": False, "is_active": True, "notes": ""}
    try:
        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        cur.execute(
            """
            SELECT is_active, notes
            FROM company_supplier_preferences
            WHERE company_id = %s AND vendor_name = %s
            """,
            (company_id, vendor_name),
        )
        row = cur.fetchone()
        cur.close()
        if row:
            return {
                "known": True,
                "is_active": bool(row.get("is_active")),
                "notes": row.get("notes") or "",
            }
    except Exception as exc:
        current_app.logger.warning("Supplier preference lookup failed: %s", exc)
    return {"known": False, "is_active": True, "notes": ""}


def _render_catalog_list(template, **context):
    filters = context.pop("filters", _query_filters())
    result = catalog.search_products(filters=filters, page=_page(), per_page=24)
    prev_args = request.args.to_dict()
    prev_args["page"] = max(result["page"] - 1, 1)
    next_args = request.args.to_dict()
    next_args["page"] = result["page"] + 1
    pagination_urls = {
        "prev": url_for(request.endpoint, **(request.view_args or {}), **prev_args),
        "next": url_for(request.endpoint, **(request.view_args or {}), **next_args),
    }
    return render_template(
        template,
        products=result["products"],
        pagination=result,
        pagination_urls=pagination_urls,
        filters=filters,
        filter_options=catalog.get_filter_options(),
        **context,
    )


@catalog_bp.route("/catalog")
def catalog_index():
    return _render_catalog_list(
        "fm/catalog.html",
        page_title="Kursuskatalog",
        page_subtitle="Sog, filtrer og ga direkte til kurser uden at bruge AI-assistenten.",
    )


@catalog_bp.route("/products/<handle>")
def product_detail(handle):
    product = catalog.get_product(handle)
    if not product:
        return render_template("catalog/not_found.html", handle=handle), 404
    supplier_state = _supplier_state(product["vendor"])
    return render_template(
        "fm/product_detail.html",
        product=product,
        related_products=catalog.get_related_products(product),
        supplier_state=supplier_state,
    )


@catalog_bp.route("/products/<handle>/request", methods=["POST"])
@login_required
def request_product(handle):
    product = catalog.get_product(handle)
    if not product:
        flash("Kurset blev ikke fundet.", "danger")
        return redirect(url_for("catalog.catalog_index"))

    supplier_state = _supplier_state(product["vendor"])
    if not supplier_state.get("is_active", True):
        flash("Denne leverandor er deaktiveret for din virksomhed.", "warning")
        return redirect(url_for("catalog.product_detail", handle=handle))

    name = request.form.get("name", "").strip() or session.get("user", "")
    email = request.form.get("email", "").strip()
    phone = request.form.get("phone", "").strip()
    notes = request.form.get("notes", "").strip()
    if not name or not email:
        flash("Navn og email er pakraevet for at anmode om tilmelding.", "danger")
        return redirect(url_for("catalog.product_detail", handle=handle))

    try:
        variant_index = int(request.form.get("variant_index", 0))
    except (TypeError, ValueError):
        variant_index = 0
    variants = product.get("variants") or []
    variant = variants[variant_index] if 0 <= variant_index < len(variants) else {}
    price = variant.get("price") if variant.get("price") is not None else product.get("price_min") or 0

    product_data = {
        "handle": product["handle"],
        "title": product["title"],
        "vendor": product["vendor"],
        "product_type": product["product_type"],
        "price": str(price or 0),
    }
    variant_info = {
        "date": variant.get("date", ""),
        "location": variant.get("location") or variant.get("city") or "",
        "notes": notes,
    }
    user_info = {"name": name, "email": email, "phone": phone}

    try:
        from app1.order_handler import order_handler

        result = order_handler.create_order(product_data, user_info, variant_info)
    except Exception as exc:
        current_app.logger.error("Catalog order request failed: %s", exc)
        result = {"success": False, "error": str(exc)}

    if not result.get("success"):
        flash("Tilmeldingsanmodningen kunne ikke oprettes. Prov igen eller kontakt support.", "danger")
        return redirect(url_for("catalog.product_detail", handle=handle))

    order = result.get("order", {})
    order_id = order.get("order_id") or result.get("order_id", "")
    flash("Tilmeldingsanmodning oprettet. Vi har gemt den i Futurematch.", "success")
    return redirect(url_for("catalog.product_detail", handle=handle, order=order_id[:8]))


@catalog_bp.route("/categories")
def category_index():
    return render_template("fm/categories.html", categories=catalog.get_categories())


@catalog_bp.route("/categories/<slug>")
def category_detail(slug):
    category = catalog.get_category(slug)
    if not category:
        return render_template("catalog/not_found.html", handle=slug, kind="category"), 404
    filters = _query_filters({"category": slug})
    return _render_catalog_list(
        "fm/category_detail.html",
        filters=filters,
        category=category,
        page_title=category["name"],
        page_subtitle=f"{category['count']} kurser i kategorien.",
    )


@catalog_bp.route("/vendors")
def vendor_index():
    return render_template("fm/vendors.html", vendors=catalog.get_vendors())


@catalog_bp.route("/vendors/<slug>")
def vendor_detail(slug):
    vendor = catalog.get_vendor(slug)
    if not vendor:
        return render_template("catalog/not_found.html", handle=slug, kind="vendor"), 404
    filters = _query_filters({"vendor": slug})
    return _render_catalog_list(
        "fm/vendor_detail.html",
        filters=filters,
        vendor=vendor,
        page_title=vendor["name"],
        page_subtitle=f"{vendor['course_count']} kurser fra leverandoren.",
    )
