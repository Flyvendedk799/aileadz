"""
Vendor (leverandør) AI tools — aggregate, anonymized analytics for a course
supplier inside the isolated vendor portal.

Hard scoping rules (see SHARED CONTRACT + vendor_portal.py / vendor_auth.py):
  * Every tool is scoped to a SINGLE vendor, identified by ``vendor_name``
    (session['vendor_name']). A vendor never sees another vendor's private data.
  * Buyer identities are NEVER exposed. course_orders rows carry company_id /
    user_id / username, but a vendor tool MUST aggregate those away and never
    return which company or which employee bought a course. We select only
    aggregate counts / sums and apply k-anonymity (kanon.py) to any breakdown
    so a low-volume slice can never single out one buyer.
  * Competitor (other vendor) names ARE allowed in get_comparable_courses,
    because the catalog itself is public — but only catalog facts (price,
    duration, difficulty, format), never any buyer/order data tied to a
    competitor.

Boot-safety / robustness:
  * This module is import-safe: heavy / optional deps (catalog_service, kanon,
    MySQLdb) are imported lazily and guarded so importing it can never crash
    create_app().
  * Every DB call is wrapped; on any failure a tool returns a compact Danish
    error dict instead of raising. A tool result is ALWAYS a JSON-serialisable
    dict the model can use directly.

Public surface:
  * VENDOR_TOOLS            — OpenAI tool schema list (offered on /vendor/ask).
  * execute_vendor_tool(name, args, vendor_name) -> dict
  * VENDOR_TOOL_TRIGGER_KEYWORDS — {tool_name: [keywords]} for the Register phase.

These tools are registered ONLY within the vendor portal path (vendor-surface);
they are deliberately NOT added to the employee/HR tool registry.
"""

import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Guarded lazy helpers (module import stays side-effect free + boot-safe)
# ---------------------------------------------------------------------------
def _catalog_products():
    """Return the normalized live catalog product list, or [] on any failure."""
    try:
        import catalog_service
        return catalog_service.get_products() or []
    except Exception as exc:  # pragma: no cover - boot safety
        logger.warning("vendor_tools: catalog_service unavailable: %s", exc)
        return []


def _kanon():
    """Return the kanon module (k-anonymity helpers) or None."""
    try:
        import kanon
        return kanon
    except Exception as exc:  # pragma: no cover - boot safety
        logger.warning("vendor_tools: kanon unavailable: %s", exc)
        return None


def _dict_cursor():
    """Return a DictCursor on the live request connection, or None.

    Heals a possibly-stale connection via db_compat (mirrors the rest of the
    app). Never raises — callers treat None as "DB unavailable".
    """
    try:
        from flask import current_app
        mysql = getattr(current_app, "mysql", None)
        if mysql is None:
            return None
        try:
            from db_compat import refresh_flask_mysql_connection
            refresh_flask_mysql_connection(mysql)
        except Exception:
            pass
        conn = mysql.connection
        try:
            import MySQLdb.cursors
            return conn.cursor(MySQLdb.cursors.DictCursor)
        except Exception:
            # DictCursor is the app-wide default, so a bare cursor() is fine.
            return conn.cursor()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("vendor_tools: kunne ikke åbne database-cursor: %s", exc)
        return None


def _k_default():
    k = _kanon()
    try:
        return int(getattr(k, "K_DEFAULT", 5)) if k else 5
    except Exception:
        return 5


def _err(msg):
    """Uniform compact Danish error dict."""
    return {"error": msg}


def _norm_name(value):
    return (value or "").strip().lower()


def _vendor_products(vendor_name):
    """The catalog products that belong to THIS vendor (case-insensitive).

    Scoped strictly by product 'vendor' == session vendor_name. Returns the
    normalized catalog dicts (handle/title/vendor/price_min/.../metadata).
    """
    wanted = _norm_name(vendor_name)
    if not wanted:
        return []
    out = []
    for p in _catalog_products():
        try:
            if _norm_name(p.get("vendor")) == wanted:
                out.append(p)
        except Exception:
            continue
    return out


def _price_of(product):
    """Single representative price for a normalized product (min, else max)."""
    for key in ("price_min", "price_max"):
        v = product.get(key)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def _duration_of(product):
    md = product.get("metadata") or {}
    v = md.get("duration_days")
    try:
        v = int(v)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _difficulty_of(product):
    md = product.get("metadata") or {}
    return (md.get("difficulty") or "").strip().lower() or None


def _primary_topic_of(product):
    md = product.get("metadata") or {}
    return (md.get("primary_topic") or "").strip() or None


def _categories_of(product):
    cats = product.get("categories") or []
    return [c for c in cats if c]


# ---------------------------------------------------------------------------
# Tool 1: vendor_performance_summary
# ---------------------------------------------------------------------------
def vendor_performance_summary(args, vendor_name):
    """Aggregate order performance for THIS vendor's catalog.

    Resolves the vendor's product handles from the public catalog, then reads
    course_orders for ONLY those handles. Returns aggregate-only metrics:
    total orders, orders in the last 30/90 days, a completion rate, and the
    vendor's top courses by order volume. No buyer (company/employee) identity
    is ever read or returned — we never select company_id/user_id/username.
    """
    args = args or {}
    if not vendor_name:
        return _err("Ingen leverandør fundet i sessionen.")

    products = _vendor_products(vendor_name)
    # handle -> title (own catalog only)
    handle_title = {}
    for p in products:
        h = p.get("handle")
        if h:
            handle_title[h] = p.get("title") or h

    if not handle_title:
        return {
            "vendor": vendor_name,
            "course_count": 0,
            "total_orders": 0,
            "orders_30d": 0,
            "orders_90d": 0,
            "completion_rate_pct": 0.0,
            "top_courses": [],
            "note": "Ingen kurser fundet for denne leverandør i kataloget endnu.",
        }

    cur = _dict_cursor()
    if cur is None:
        return _err("Databasen er midlertidigt utilgængelig. Prøv igen senere.")

    handles = list(handle_title.keys())
    placeholders = ",".join(["%s"] * len(handles))

    try:
        # Aggregate totals + recency windows. NOTE: we deliberately select only
        # aggregate counts — never company_id / user_id / username.
        cur.execute(
            f"""
            SELECT
                COUNT(*) AS total_orders,
                SUM(CASE WHEN created_at >= DATE_SUB(NOW(), INTERVAL 30 DAY) THEN 1 ELSE 0 END) AS orders_30d,
                SUM(CASE WHEN created_at >= DATE_SUB(NOW(), INTERVAL 90 DAY) THEN 1 ELSE 0 END) AS orders_90d,
                SUM(CASE WHEN status = 'completed' OR completion_status = 'completed' THEN 1 ELSE 0 END) AS completed_orders,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled_orders
            FROM course_orders
            WHERE product_handle IN ({placeholders})
            """,
            tuple(handles),
        )
        totals = cur.fetchone() or {}

        # Per-course volume (top courses). Grouped by the vendor's OWN handle —
        # this is the vendor's own catalog, so course identity is fine to show.
        cur.execute(
            f"""
            SELECT product_handle,
                   COUNT(*) AS orders,
                   SUM(CASE WHEN status = 'completed' OR completion_status = 'completed' THEN 1 ELSE 0 END) AS completed
            FROM course_orders
            WHERE product_handle IN ({placeholders})
            GROUP BY product_handle
            ORDER BY orders DESC
            LIMIT 10
            """,
            tuple(handles),
        )
        per_course = cur.fetchall() or []
    except Exception as exc:
        logger.warning("vendor_tools: vendor_performance_summary query failed: %s", exc)
        return _err("Kunne ikke hente leverandørens salgstal lige nu.")
    finally:
        try:
            cur.close()
        except Exception:
            pass

    def _i(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    total_orders = _i(totals.get("total_orders"))
    completed_orders = _i(totals.get("completed_orders"))
    completion_rate = round((completed_orders / total_orders) * 100, 1) if total_orders else 0.0

    top_courses = []
    for r in per_course:
        h = r.get("product_handle")
        orders = _i(r.get("orders"))
        comp = _i(r.get("completed"))
        top_courses.append({
            "handle": h,
            "title": handle_title.get(h, h),
            "orders": orders,
            "completed": comp,
            "completion_rate_pct": round((comp / orders) * 100, 1) if orders else 0.0,
        })

    return {
        "vendor": vendor_name,
        "course_count": len(handle_title),
        "total_orders": total_orders,
        "orders_30d": _i(totals.get("orders_30d")),
        "orders_90d": _i(totals.get("orders_90d")),
        "completed_orders": completed_orders,
        "cancelled_orders": _i(totals.get("cancelled_orders")),
        "completion_rate_pct": completion_rate,
        "top_courses": top_courses,
        "anonymitet": "Tal er aggregeret. Køberidentitet (virksomhed/medarbejder) vises aldrig.",
    }


# ---------------------------------------------------------------------------
# Tool 2: get_demand_by_category
# ---------------------------------------------------------------------------
def get_demand_by_category(args, vendor_name):
    """Market demand by category/topic across the WHOLE platform, anonymized.

    Two demand signals, both AGGREGATED across all companies and k-anonymized so
    a vendor sees the market — never a specific buyer:
      * Realised demand: course_orders grouped by the catalog category of the
        ordered course (which company ordered is NOT read).
      * Search demand: chatbot_interactions search terms (query_text), grouped
        by their detected category.

    k-anonymity: a category row is only surfaced if its volume is >= k (kanon.
    K_DEFAULT). Low-volume slices are dropped so they cannot single out a buyer.
    """
    args = args or {}
    if not vendor_name:
        return _err("Ingen leverandør fundet i sessionen.")

    period_days = args.get("period_days", 90)
    try:
        period_days = max(1, min(365, int(period_days)))
    except (TypeError, ValueError):
        period_days = 90

    k = _k_default()
    kanon = _kanon()

    # Build handle -> categories map from the public catalog (all vendors), so we
    # can map ordered handles to categories WITHOUT touching buyer data.
    handle_categories = {}
    for p in _catalog_products():
        h = p.get("handle")
        if h:
            handle_categories[h] = _categories_of(p) or ["Ukategoriseret"]

    cur = _dict_cursor()
    if cur is None:
        return _err("Databasen er midlertidigt utilgængelig. Prøv igen senere.")

    order_category_counts = {}
    search_category_counts = {}

    try:
        # --- Realised demand from orders (platform-wide, no company_id read) ---
        cur.execute(
            """
            SELECT product_handle, COUNT(*) AS orders
            FROM course_orders
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
              AND product_handle IS NOT NULL AND product_handle != ''
            GROUP BY product_handle
            """,
            (period_days,),
        )
        for r in (cur.fetchall() or []):
            h = r.get("product_handle")
            try:
                n = int(r.get("orders") or 0)
            except (TypeError, ValueError):
                n = 0
            for cat in handle_categories.get(h, ["Ukategoriseret"]):
                order_category_counts[cat] = order_category_counts.get(cat, 0) + n

        # --- Search demand from chatbot interactions (platform-wide) -----------
        # We use the stored 'category' classification when present; query_text is
        # only counted at the aggregate category level, never echoed verbatim, so
        # no free-text that could identify a buyer is returned.
        cur.execute(
            """
            SELECT COALESCE(NULLIF(TRIM(category), ''), 'Ukategoriseret') AS category,
                   COUNT(*) AS searches
            FROM chatbot_interactions
            WHERE created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)
            GROUP BY category
            """,
            (period_days,),
        )
        for r in (cur.fetchall() or []):
            cat = (r.get("category") or "Ukategoriseret").strip() or "Ukategoriseret"
            try:
                n = int(r.get("searches") or 0)
            except (TypeError, ValueError):
                n = 0
            search_category_counts[cat] = search_category_counts.get(cat, 0) + n
    except Exception as exc:
        logger.warning("vendor_tools: get_demand_by_category query failed: %s", exc)
        return _err("Kunne ikke hente markedsefterspørgslen lige nu.")
    finally:
        try:
            cur.close()
        except Exception:
            pass

    def _kanon_rows(counts, count_key):
        rows = [{"category": c, count_key: n} for c, n in counts.items()]
        rows.sort(key=lambda r: r[count_key], reverse=True)
        if kanon is not None:
            try:
                kept, _note = kanon.suppress_small_groups(rows, count_key=count_key, k=k, merge=False)
                return kept
            except Exception:
                pass
        # Fallback k-anon: drop rows below k ourselves.
        return [r for r in rows if r.get(count_key, 0) >= k]

    order_rows = _kanon_rows(order_category_counts, "orders")
    search_rows = _kanon_rows(search_category_counts, "searches")

    # Which of the demanded categories does THIS vendor already cover? Helps the
    # vendor spot gaps (demand they don't yet serve). Own catalog only.
    own_categories = set()
    for p in _vendor_products(vendor_name):
        for c in _categories_of(p):
            own_categories.add(_norm_name(c))

    top_demand_categories = sorted(
        {r["category"] for r in order_rows} | {r["category"] for r in search_rows}
    )
    gaps = [c for c in top_demand_categories if _norm_name(c) not in own_categories]

    note_da = (
        kanon.anon_note(k) if (kanon and hasattr(kanon, "anon_note"))
        else f"Grupper under k={k} er skjult af hensyn til anonymitet"
    )

    return {
        "vendor": vendor_name,
        "period_days": period_days,
        "k_anonymity": k,
        "demand_by_orders": order_rows[:15],
        "demand_by_searches": search_rows[:15],
        "categories_you_dont_cover": gaps[:15],
        "anonymitet": note_da + ". Tal er aggregeret på tværs af platformen; ingen køber kan identificeres.",
    }


# ---------------------------------------------------------------------------
# Tool 3: get_comparable_courses
# ---------------------------------------------------------------------------
def get_comparable_courses(args, vendor_name):
    """Compare ONE of this vendor's courses to similar catalog courses.

    Similarity is computed from PUBLIC catalog facts only (shared category /
    primary_topic), and the comparison shows price, duration, difficulty and
    format. Competitor vendor names are included (the catalog is public), but
    NO buyer/order data is read for any vendor.
    """
    args = args or {}
    if not vendor_name:
        return _err("Ingen leverandør fundet i sessionen.")

    handle = (args.get("handle") or "").strip()
    if not handle:
        return _err("Angiv et kursus-handle fra dit eget katalog.")

    own = _vendor_products(vendor_name)
    target = None
    for p in own:
        if (p.get("handle") or "") == handle:
            target = p
            break
    if target is None:
        # Scope guard: a vendor may only compare its OWN course as the anchor.
        return _err("Kurset blev ikke fundet i dit katalog. Du kan kun sammenligne dine egne kurser.")

    t_topic = _norm_name(_primary_topic_of(target))
    t_cats = {_norm_name(c) for c in _categories_of(target)}
    t_type = _norm_name(target.get("product_type"))

    # Score every OTHER course on shared category / topic / product_type.
    comparables = []
    for p in _catalog_products():
        h = p.get("handle")
        if not h or h == handle:
            continue
        p_topic = _norm_name(_primary_topic_of(p))
        p_cats = {_norm_name(c) for c in _categories_of(p)}
        p_type = _norm_name(p.get("product_type"))

        score = 0
        if t_topic and p_topic and t_topic == p_topic:
            score += 3
        shared_cats = t_cats & p_cats
        score += 2 * len(shared_cats)
        if t_type and p_type and t_type == p_type:
            score += 1
        if score <= 0:
            continue

        comparables.append((score, p))

    comparables.sort(key=lambda sp: sp[0], reverse=True)

    limit = args.get("limit", 8)
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 8

    def _summ(p, include_score=None):
        d = {
            "handle": p.get("handle"),
            "title": p.get("title"),
            "vendor": p.get("vendor"),
            "price": _price_of(p),
            "duration_days": _duration_of(p),
            "difficulty": _difficulty_of(p),
            "format": (p.get("format") or None),
            "primary_topic": _primary_topic_of(p),
            "categories": _categories_of(p),
        }
        if include_score is not None:
            d["match_score"] = include_score
        return d

    peer_list = [_summ(p, include_score=score) for score, p in comparables[:limit]]

    # Market position summary vs. peers (catalog facts only — no buyer data).
    own_price = _price_of(target)
    own_duration = _duration_of(target)
    peer_prices = [c["price"] for c in peer_list if isinstance(c["price"], (int, float))]
    peer_durations = [c["duration_days"] for c in peer_list if isinstance(c["duration_days"], (int, float))]

    position = {}
    if own_price is not None and peer_prices:
        avg = sum(peer_prices) / len(peer_prices)
        position["price"] = {
            "yours": own_price,
            "peer_avg": round(avg, 2),
            "vs_peer_avg_pct": round(((own_price - avg) / avg) * 100, 1) if avg else None,
            "cheaper_than_peers": own_price < avg,
        }
    if own_duration is not None and peer_durations:
        avg_d = sum(peer_durations) / len(peer_durations)
        position["duration_days"] = {
            "yours": own_duration,
            "peer_avg": round(avg_d, 1),
            "shorter_than_peers": own_duration < avg_d,
        }

    if not peer_list:
        return {
            "vendor": vendor_name,
            "course": _summ(target),
            "comparables": [],
            "market_position": {},
            "note": "Ingen sammenlignelige kurser fundet i kataloget.",
        }

    return {
        "vendor": vendor_name,
        "course": _summ(target),
        "comparables": peer_list,
        "market_position": position,
        "anonymitet": "Sammenligningen bygger kun på offentlige katalogdata. Ingen købsdata indgår.",
    }


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI function-calling) — offered on /vendor/ask only.
# ---------------------------------------------------------------------------
VENDOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "vendor_performance_summary",
            "description": (
                "Hent leverandørens egne, aggregerede salgstal: samlet antal "
                "ordrer på leverandørens kurser, ordrer de seneste 30/90 dage, "
                "gennemførelsesrate og topkurser efter ordrevolumen. "
                "Returnerer KUN aggregerede tal — aldrig hvilken virksomhed "
                "eller medarbejder der har købt."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_demand_by_category",
            "description": (
                "Vis markedsefterspørgsel pr. kategori/emne på tværs af hele "
                "platformen (anonymiseret og aggregeret): hvilke kategorier der "
                "bestilles og søges mest. Kun kategorier med tilstrækkelig "
                "volumen vises (k-anonymitet), så ingen køber kan identificeres. "
                "Viser også kategorier med efterspørgsel som leverandøren endnu "
                "ikke dækker."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "period_days": {
                        "type": "integer",
                        "description": "Tilbageblik i dage (1-365). Standard 90.",
                        "default": 90,
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_comparable_courses",
            "description": (
                "Sammenlign ét af leverandørens egne kurser (angiv handle) med "
                "lignende kurser i kataloget på pris, varighed, sværhedsgrad og "
                "format. Konkurrenters navne må vises (kataloget er offentligt), "
                "men ingen købs- eller ordredata indgår."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "handle": {
                        "type": "string",
                        "description": "Handle på leverandørens eget kursus, der skal sammenlignes.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maks antal sammenlignelige kurser (1-20). Standard 8.",
                        "default": 8,
                    },
                },
                "required": ["handle"],
            },
        },
    },
]


# Trigger keywords per tool (consumed by the vendor-path Register phase).
VENDOR_TOOL_TRIGGER_KEYWORDS = {
    "vendor_performance_summary": [
        "salg", "ordrer", "ordre", "performance", "tal", "omsætning",
        "hvor mange", "gennemførelse", "completion", "topkurser", "trend",
        "sælger", "mine kurser",
    ],
    "get_demand_by_category": [
        "efterspørgsel", "demand", "marked", "market", "kategori", "kategorier",
        "emne", "trend", "populær", "populært", "søgning", "hvad efterspørges",
        "hul", "gap", "muligheder",
    ],
    "get_comparable_courses": [
        "sammenlign", "sammenligning", "konkurrent", "konkurrenter", "compare",
        "pris", "varighed", "sværhedsgrad", "difficulty", "format",
        "lignende kurser", "benchmark", "position",
    ],
}


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
_VENDOR_TOOL_ROUTER = {
    "vendor_performance_summary": vendor_performance_summary,
    "get_demand_by_category": get_demand_by_category,
    "get_comparable_courses": get_comparable_courses,
}


def execute_vendor_tool(name, args, vendor_name):
    """Execute a vendor tool by name, scoped to ``vendor_name``.

    Returns a compact JSON-serialisable dict (never raises). ``args`` is an
    already-parsed dict; ``vendor_name`` is session['vendor_name'] — the caller
    (vendor_portal) is responsible for passing the SESSION vendor so a vendor can
    never query another vendor's data.
    """
    if not vendor_name:
        return _err("Ingen leverandør fundet i sessionen.")

    fn = _VENDOR_TOOL_ROUTER.get(name)
    if fn is None:
        return _err(f"Ukendt leverandør-funktion: {name}")

    if not isinstance(args, dict):
        args = {}

    try:
        result = fn(args, vendor_name)
        if not isinstance(result, dict):
            return _err("Uventet svar fra værktøjet.")
        return result
    except Exception as exc:
        import traceback
        logger.warning("vendor_tools: %s failed: %s\n%s", name, exc, traceback.format_exc())
        return _err(f"Intern fejl i {name}.")


__all__ = [
    "VENDOR_TOOLS",
    "VENDOR_TOOL_TRIGGER_KEYWORDS",
    "execute_vendor_tool",
    "vendor_performance_summary",
    "get_demand_by_category",
    "get_comparable_courses",
]
