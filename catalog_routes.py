from urllib.parse import urlencode

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
import db_compat  # noqa: F401
import MySQLdb.cursors

import catalog_service as catalog
from auth_decorators import login_required


catalog_bp = Blueprint("catalog", __name__, template_folder="templates")


def _query_filters(extra=None):
    # Category is multi-select: read every "category" param. A single forced
    # category (category_detail page) arrives via extra={"category": slug}.
    categories = [c.strip() for c in request.args.getlist("category") if c.strip()]
    filters = {
        "q": request.args.get("q", "").strip(),
        "categories": categories,
        "vendor": request.args.get("vendor", "").strip(),
        "format": request.args.get("format", "").strip(),
        "location": request.args.get("location", "").strip(),
        "price_min": request.args.get("price_min", "").strip(),
        "price_max": request.args.get("price_max", "").strip(),
        "sort": request.args.get("sort", "relevance").strip() or "relevance",
    }
    if extra:
        extra = dict(extra)
        forced_category = extra.pop("category", None)
        if forced_category:
            filters["categories"] = [forced_category]
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


def _company_discount_for(vendor_name):
    """Resolve the active negotiated agreement for a single vendor for the
    logged-in company. Returns None for anonymous / non-company users or when no
    valid agreement exists (so templates fall back to the list price)."""
    company_id = session.get("company_id")
    if not company_id or not vendor_name:
        return None
    try:
        discount_map = catalog.get_company_discount_map(company_id)
        return discount_map.get((vendor_name or "").lower())
    except Exception as exc:
        current_app.logger.warning("Company discount lookup failed: %s", exc)
        return None


def _list_base_and_pairs(endpoint, view_args):
    """(base_url, current_pairs): the live query string as (key, value) pairs,
    preserving repeated keys (multi-category) and dropping the page cursor and
    empty values. Used to build nav/remove URLs that keep every other filter."""
    base = url_for(endpoint, **(view_args or {}))
    pairs = [(k, v) for k, v in request.args.items(multi=True) if k != "page" and v]
    return base, pairs


def _build_url(base, pairs):
    qs = urlencode(pairs)
    return base + (("?" + qs) if qs else "")


def _active_filters(filters, options, endpoint, view_args):
    """Removable chips for the applied filters. Each chip's ``remove_url`` drops
    just that one value (one category at a time for the multi-select) and keeps
    the rest. Slugs resolve to display names."""
    base, pairs = _list_base_and_pairs(endpoint, view_args)
    cat_name = {c["slug"]: c["name"] for c in (options.get("categories") or [])}
    ven_name = {v["slug"]: v["name"] for v in (options.get("vendors") or [])}
    chips = []

    def remove_key(key):
        return _build_url(base, [(k, v) for (k, v) in pairs if k != key])

    def remove_pair(key, value):
        out, dropped = [], False
        for (k, v) in pairs:
            if not dropped and k == key and v == value:
                dropped = True
                continue
            out.append((k, v))
        return _build_url(base, out)

    for slug in (filters.get("categories") or []):
        chips.append({"label": "Kategori", "display": cat_name.get(slug, slug),
                      "remove_url": remove_pair("category", slug)})
    if filters.get("q"):
        chips.append({"label": "Søgning", "display": filters["q"], "remove_url": remove_key("q")})
    if filters.get("vendor"):
        chips.append({"label": "Leverandør", "display": ven_name.get(filters["vendor"], filters["vendor"]),
                      "remove_url": remove_key("vendor")})
    if filters.get("format"):
        chips.append({"label": "Format", "display": filters["format"], "remove_url": remove_key("format")})
    if filters.get("location"):
        chips.append({"label": "Sted", "display": filters["location"], "remove_url": remove_key("location")})
    if filters.get("price_min"):
        chips.append({"label": "Min. pris", "display": "fra {} kr".format(filters["price_min"]),
                      "remove_url": remove_key("price_min")})
    if filters.get("price_max"):
        chips.append({"label": "Maks. pris", "display": "op til {} kr".format(filters["price_max"]),
                      "remove_url": remove_key("price_max")})
    return chips


def _render_catalog_list(template, list_endpoint=None, **context):
    filters = context.pop("filters", _query_filters())
    result = catalog.search_products(
        filters=filters,
        page=_page(),
        per_page=24,
        company_id=session.get("company_id"),
    )
    options = catalog.get_filter_options()
    # nav/remove URLs are built against `list_endpoint` (the canonical page) so
    # the AJAX results-fragment endpoint still emits /catalog links, not
    # /catalog/results links.
    endpoint = list_endpoint or request.endpoint
    view_args = request.view_args or {}
    base, pairs = _list_base_and_pairs(endpoint, view_args)

    def nav_url(page):
        return _build_url(base, pairs + [("page", str(page))])

    pagination_urls = {"prev": nav_url(max(result["page"] - 1, 1)), "next": nav_url(result["page"] + 1)}
    return render_template(
        template,
        products=result["products"],
        pagination=result,
        pagination_urls=pagination_urls,
        filters=filters,
        filter_options=options,
        active_filters=_active_filters(filters, options, endpoint, view_args),
        clear_url=base,
        **context,
    )


@catalog_bp.route("/catalog")
def catalog_index():
    return _render_catalog_list(
        "fm/catalog.html",
        page_title="Kursuskatalog",
        page_subtitle="Sog, filtrer og ga direkte til kurser uden at bruge AI-assistenten.",
    )


@catalog_bp.route("/catalog/results")
def catalog_results():
    """Results-only fragment for live (debounced AJAX) search + filtering on the
    catalog page. Renders just the results partial; its pagination/chip links
    point at the canonical /catalog so they work with or without JavaScript."""
    return _render_catalog_list(
        "fm/_catalog_results.html",
        list_endpoint="catalog.catalog_index",
    )


@catalog_bp.route("/products/<handle>")
def product_detail(handle):
    product = catalog.get_product(handle)
    if not product:
        return render_template("catalog/not_found.html", handle=handle), 404
    supplier_state = _supplier_state(product["vendor"])
    # Compute-on-read negotiated price for logged-in company users; no session
    # company -> product is returned unchanged (list price).
    agreement = _company_discount_for(product["vendor"])
    if agreement:
        product = catalog.decorate_product_with_discount(product, agreement)
    related_products = catalog.decorate_products_with_discounts(
        catalog.get_related_products(product),
        company_id=session.get("company_id"),
    )
    return render_template(
        "fm/product_detail.html",
        product=product,
        related_products=related_products,
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

    # Apply the negotiated supplier discount AT CAPTURE so the order is charged
    # at the agreed price, not the list price. Re-resolve the agreement at order
    # time (company-scoped); fall back to the list price when none applies.
    agreement = _company_discount_for(product["vendor"])
    if agreement:
        discounted = catalog.apply_discount_to_price(price, agreement)
        if discounted is not None:
            price = discounted

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
