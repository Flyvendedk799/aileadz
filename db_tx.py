"""Explicit transaction / read-cursor context managers for the Futurematch DB layer.

Why this module exists
----------------------
The codebase historically relied on ~150 bare ``except:`` blocks and ~126
scattered manual ``commit()`` calls. A common failure mode was an INSERT that
ran but whose ``commit()`` was skipped (or swallowed by a bare except), so the
row silently vanished on connection teardown ("order inserted but never
persisted"). These helpers centralise the commit/rollback/close lifecycle so a
call site only has to describe its SQL, not the bookkeeping.

The ``with mysql.connection as tx`` form does **not** work through this
codebase's PyMySQL shim (``run.py`` wraps the real connection in
``_PyMySQLConnection``, which does not implement ``__enter__`` /``__exit__``),
so this module provides explicit ``@contextmanager`` helpers instead.

Design constraints (production + sandbox safe):
* Importing this module must never require an app context and must never crash
  ``create_app()`` — every external import is lazy and guarded.
* The connection is resolved from the passed ``mysql`` argument, falling back to
  ``flask.current_app.mysql`` only when needed.
* The cursor uses DictCursor when the class can be resolved (mirroring how
  callers do ``connection.cursor(MySQLdb.cursors.DictCursor)`` and how the shim
  exposes ``cursor()``). The connection is already configured with
  ``MYSQL_CURSORCLASS='DictCursor'`` so rows are read by COLUMN NAME either way.
"""

from contextlib import contextmanager

__all__ = ["transaction", "read_cursor"]


def _resolve_mysql(mysql):
    """Return the ``mysql`` extension object.

    Falls back to ``flask.current_app.mysql`` when no explicit object is passed.
    Imports are deferred so this module can be imported without Flask installed
    or without an active application context.
    """
    if mysql is not None:
        return mysql
    try:
        from flask import current_app
    except Exception:
        return None
    try:
        return getattr(current_app, "mysql", None)
    except Exception:
        # Accessing current_app outside an app context raises; degrade quietly.
        return None


def _refresh_connection(mysql):
    """Heal a stale request connection before we use it.

    Mirrors what every other DB-touching module does before opening a cursor.
    Guarded so a missing/over-eager ``db_compat`` never breaks the transaction.
    """
    if mysql is None:
        return
    try:
        from db_compat import refresh_flask_mysql_connection
    except Exception:
        return
    try:
        refresh_flask_mysql_connection(mysql)
    except Exception:
        # refresh is best-effort; if it fails we still try to use the
        # connection (it may be perfectly healthy). A real failure surfaces
        # when we open the cursor / run SQL below.
        pass


def _dict_cursor_class():
    """Resolve a DictCursor class, preferring MySQLdb, then PyMySQL.

    Returns ``None`` when neither driver is importable, in which case callers
    fall back to a bare ``cursor()`` (the connection's default cursorclass is
    already DictCursor in this app).
    """
    try:
        import MySQLdb.cursors  # type: ignore

        return MySQLdb.cursors.DictCursor
    except Exception:
        pass
    try:
        import pymysql.cursors  # type: ignore

        return pymysql.cursors.DictCursor
    except Exception:
        pass
    return None


def _open_cursor(connection):
    """Open a DictCursor on ``connection``, mirroring the run.py shim.

    The shim's ``cursor()`` accepts the DictCursor class positionally (and also
    a ``dictionary=True`` kwarg). We pass the class positionally because that is
    what every existing call site does. If the class cannot be resolved or the
    positional call is rejected, we degrade to a bare ``cursor()`` — the
    connection is already configured with ``MYSQL_CURSORCLASS='DictCursor'`` so
    rows are still keyed by column name.
    """
    cursor_class = _dict_cursor_class()
    if cursor_class is not None:
        try:
            return connection.cursor(cursor_class)
        except TypeError:
            # Some cursor() implementations don't accept a positional class.
            try:
                return connection.cursor(dictionary=True)
            except TypeError:
                pass
    return connection.cursor()


@contextmanager
def transaction(mysql=None):
    """Yield a DictCursor inside a managed transaction.

    On a clean exit the connection is committed. If the body raises, the
    connection is rolled back and the exception re-raised. The cursor is always
    closed in ``finally``.

    Usage::

        from db_tx import transaction

        with transaction() as cur:               # uses current_app.mysql
            cur.execute("INSERT INTO orders (...) VALUES (...)", params)
        # committed here, only if no exception was raised

        with transaction(mysql) as cur:          # explicit extension object
            ...
    """
    mysql = _resolve_mysql(mysql)
    if mysql is None:
        raise RuntimeError(
            "db_tx.transaction(): no MySQL extension available "
            "(pass mysql= or call within an app context with current_app.mysql)."
        )

    _refresh_connection(mysql)
    connection = mysql.connection
    cursor = _open_cursor(connection)
    try:
        yield cursor
    except Exception:
        # Roll back, but never let a rollback failure mask the original error.
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    else:
        connection.commit()
    finally:
        try:
            cursor.close()
        except Exception:
            pass


@contextmanager
def read_cursor(mysql=None):
    """Yield a read-only DictCursor.

    Identical lifecycle to :func:`transaction` except it never commits (and does
    not roll back on success — there is nothing to persist). The cursor is
    always closed in ``finally``. If the body raises we still close the cursor
    and re-raise; a SELECT-only path has nothing to roll back, but we leave any
    rollback decision to the caller's enclosing transaction, if any.

    Usage::

        from db_tx import read_cursor

        with read_cursor() as cur:
            cur.execute("SELECT id, name FROM users WHERE company_id=%s", (cid,))
            rows = cur.fetchall()   # rows keyed by COLUMN NAME (DictCursor)
    """
    mysql = _resolve_mysql(mysql)
    if mysql is None:
        raise RuntimeError(
            "db_tx.read_cursor(): no MySQL extension available "
            "(pass mysql= or call within an app context with current_app.mysql)."
        )

    _refresh_connection(mysql)
    connection = mysql.connection
    cursor = _open_cursor(connection)
    try:
        yield cursor
    finally:
        try:
            cursor.close()
        except Exception:
            pass
