"""Per-request memo on ``flask.g`` for rows that several layers read in one request.

Contract: ``memo(key, loader)`` runs ``loader()`` once per request and returns the
same value for later calls with the same key. It memoises only during GET/HEAD
requests (a write request may change the row it later re-reads) and passes straight
through outside a request context, so it can only change speed, never correctness.

``company_row(company_id)`` is the one place the current company row is read
(``SELECT * FROM companies``). Branding, the feature-flag lookup and the HR
company context all use it, so a page that needs the row in four places queries it
once. It returns a copy: callers may add keys without leaking them to each other.
"""
from flask import current_app, g, has_request_context, request

_ATTR = "_request_memo"


def memo(key, loader):
    if not has_request_context() or request.method not in ("GET", "HEAD"):
        return loader()
    store = getattr(g, _ATTR, None)
    if store is None:
        store = {}
        setattr(g, _ATTR, store)
    if key not in store:
        store[key] = loader()
    return store[key]


def _copy(value):
    return dict(value) if isinstance(value, dict) else value


def company_row(company_id):
    """The ``companies`` row for ``company_id`` (a dict), or None. One query per request."""
    if not company_id:
        return None

    def load():
        import MySQLdb.cursors

        cur = current_app.mysql.connection.cursor(MySQLdb.cursors.DictCursor)
        try:
            cur.execute("SELECT * FROM companies WHERE id = %s", (company_id,))
            return cur.fetchone()
        finally:
            cur.close()

    return _copy(memo(("company_row", company_id), load))


def prime(key, value):
    """Seed the memo with a value another layer already loaded (GET/HEAD only)."""
    if not has_request_context() or request.method not in ("GET", "HEAD"):
        return
    store = getattr(g, _ATTR, None)
    if store is None:
        store = {}
        setattr(g, _ATTR, store)
    store.setdefault(key, value)


def prime_company_row(row):
    """A caller that already holds the company's row (the HR context joins it with the
    membership) hands it over, so branding and the feature lookup do not read it again."""
    if row and row.get("id"):
        prime(("company_row", row["id"]), dict(row))
