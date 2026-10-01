"""Admin product browser + search-index controls (N-3.1). Platform admin only.

* ``GET  /admin/catalog/products``                 list, search, filter by status
* ``GET|POST /admin/catalog/products/<handle>/edit``
* ``POST /admin/catalog/products/<handle>/status`` publish / unpublish / archive
* ``POST /admin/catalog/products/status``          bulk (used by the freshness page)
* ``POST /admin/catalog/reindex``                  "Genopbyg indeks" (+ embed what is missing)
* ``GET  /admin/catalog/index-status``             status as JSON
* ``POST /admin/catalog/sync``                     run the Shopify sync now
"""

from __future__ import annotations

import logging

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

import catalog_service as catalog
from auth_decorators import require_role

logger = logging.getLogger(__name__)

catalog_admin_bp = Blueprint("catalog_admin", __name__, url_prefix="/admin/catalog")


@catalog_admin_bp.route("/products")
@require_role("admin")
def products():
    result = catalog.admin_list_products(
        q=request.args.get("q", ""), status=request.args.get("status", ""),
        vendor=request.args.get("vendor", ""), page=request.args.get("page", 1, type=int))
    return render_template("fm/admin_catalog_products.html", result=result,
                           q=request.args.get("q", ""), status=request.args.get("status", ""),
                           stale=catalog.stale_handles())


@catalog_admin_bp.route("/products/<handle>/edit", methods=["GET", "POST"])
@require_role("admin")
def edit_product(handle):
    product = catalog.get_product_any(handle)
    if not product:
        flash("Kurset blev ikke fundet.", "warning")
        return redirect(url_for("catalog_admin.products"))
    if request.method == "POST":
        if request.form.get("reset"):
            catalog.reset_product_edits(handle)
            flash("Ændringerne er fjernet. Kurset viser nu kildedata.", "success")
        else:
            catalog.update_product(handle, {
                "title": request.form.get("title", ""), "summary": request.form.get("summary", ""),
                "vendor": request.form.get("vendor", ""), "tags": request.form.get("tags", ""),
                "image_url": request.form.get("image_url", ""),
            }, actor=session.get("user", ""))
            flash("Kurset er opdateret.", "success")
        return redirect(url_for("catalog_admin.edit_product", handle=handle))
    return render_template("fm/admin_catalog_product_edit.html", product=product)


@catalog_admin_bp.route("/products/<handle>/status", methods=["POST"])
@require_role("admin")
def product_status(handle):
    status = request.form.get("status", "")
    if catalog.set_product_status(handle, status, actor=session.get("user", "")):
        flash({"active": "Kurset er publiceret igen.", "hidden": "Kurset er skjult fra kataloget og AI'en.",
               "archived": "Kurset er arkiveret."}.get(status, "Status opdateret."), "success")
    else:
        flash("Status kunne ikke ændres.", "danger")
    return redirect(request.referrer or url_for("catalog_admin.products"))


@catalog_admin_bp.route("/products/status", methods=["POST"])
@require_role("admin")
def bulk_status():
    handles = request.form.getlist("handles")
    status = request.form.get("status", "")
    if not handles:
        flash("Vælg mindst ét kursus.", "warning")
    else:
        done = catalog.set_products_status(handles, status, actor=session.get("user", ""))
        flash(f"{done} kursus(er) opdateret.", "success" if done else "danger")
    return redirect(request.referrer or url_for("catalog_admin.products"))


@catalog_admin_bp.route("/reindex", methods=["POST"])
@require_role("admin")
def reindex():
    try:
        from app1 import rag
        status = rag.rebuild_index(embed=True)
        er = status.get("embed_result") or {}
        msg = "Indekset er genopbygget: %d kurser" % status["products"]
        if er.get("embedded"):
            msg += ", %d nye embeddings" % er["embedded"]
        if er.get("reason") == "no_api_key":
            msg += ". Embeddings springes over, fordi OPENAI_API_KEY mangler (søgning bruger stadig nøgleord)."
        flash(msg + ".", "success")
    except Exception as e:
        logger.warning("reindex failed: %s", e)
        flash("Indekset kunne ikke genopbygges: %s" % e, "danger")
    return redirect(request.referrer or url_for("admin_dashboard.admin_catalog"))


@catalog_admin_bp.route("/index-status")
@require_role("admin")
def index_status():
    from app1 import rag
    return jsonify(rag.index_status())


@catalog_admin_bp.route("/sync", methods=["POST"])
@require_role("admin")
def sync_now():
    import shopify_sync
    result = shopify_sync.sync()
    if result.get("synced"):
        flash("Shopify-synkronisering færdig: %d kurser." % result["synced"], "success")
    elif result.get("skipped"):
        flash("Synkronisering er ikke sat op: %s." % result["skipped"], "warning")
    else:
        flash("Synkronisering fejlede: %s" % result.get("error"), "danger")
    return redirect(request.referrer or url_for("admin_dashboard.admin_catalog"))
