"""Tests for db_tx.transaction / db_tx.read_cursor.

These use a FAKE connection (no real DB, no Flask app context). The fake records
commit / rollback / close calls so we can assert the managed lifecycle:

* success path  -> commit() exactly once, cursor closed, no rollback
* exception path -> rollback() once, cursor closed, original exception propagates
* read_cursor    -> never commits, cursor always closed
"""

import unittest

from db_tx import read_cursor, transaction


class FakeCursor:
    """Records execution + close; close raising can be toggled for robustness tests."""

    def __init__(self, raise_on_close=False):
        self.closed = False
        self.executed = []
        self._raise_on_close = raise_on_close

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return []

    def close(self):
        self.closed = True
        if self._raise_on_close:
            raise RuntimeError("close blew up")


class FakeConnection:
    """Records commit/rollback and hands out a FakeCursor.

    ``cursor()`` accepts an optional positional DictCursor class (mirroring the
    real shim / driver) and ignores it — we only care about lifecycle calls.
    """

    def __init__(self, cursor=None, raise_on_rollback=False):
        self.commits = 0
        self.rollbacks = 0
        self.pinged = 0
        self._cursor = cursor or FakeCursor()
        self._raise_on_rollback = raise_on_rollback

    def cursor(self, *args, **kwargs):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1
        if self._raise_on_rollback:
            raise RuntimeError("rollback blew up")

    def ping(self, *args, **kwargs):
        self.pinged += 1


class FakeMySQL:
    """Stand-in for the Flask-MySQLdb extension: exposes a .connection."""

    def __init__(self, connection=None):
        self.connection = connection or FakeConnection()


class TransactionTests(unittest.TestCase):
    def test_success_commits_and_closes(self):
        conn = FakeConnection()
        mysql = FakeMySQL(conn)

        with transaction(mysql) as cur:
            cur.execute("INSERT INTO orders (id) VALUES (%s)", (1,))

        self.assertEqual(conn.commits, 1, "clean exit must commit exactly once")
        self.assertEqual(conn.rollbacks, 0, "clean exit must not roll back")
        self.assertTrue(conn._cursor.closed, "cursor must be closed on success")
        self.assertEqual(conn._cursor.executed, [("INSERT INTO orders (id) VALUES (%s)", (1,))])

    def test_yields_the_cursor(self):
        conn = FakeConnection()
        mysql = FakeMySQL(conn)
        with transaction(mysql) as cur:
            self.assertIs(cur, conn._cursor)

    def test_exception_rolls_back_closes_and_propagates(self):
        conn = FakeConnection()
        mysql = FakeMySQL(conn)

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with transaction(mysql) as cur:
                cur.execute("UPDATE x SET y=1", None)
                raise Boom("kaboom")

        self.assertEqual(conn.rollbacks, 1, "exception must roll back once")
        self.assertEqual(conn.commits, 0, "exception must not commit")
        self.assertTrue(conn._cursor.closed, "cursor must be closed even on error")

    def test_rollback_failure_does_not_mask_original_exception(self):
        conn = FakeConnection(raise_on_rollback=True)
        mysql = FakeMySQL(conn)

        class Boom(Exception):
            pass

        # The original Boom must propagate, not the rollback's RuntimeError.
        with self.assertRaises(Boom):
            with transaction(mysql):
                raise Boom("original")
        self.assertEqual(conn.rollbacks, 1)
        self.assertEqual(conn.commits, 0)
        self.assertTrue(conn._cursor.closed)

    def test_close_failure_is_swallowed_on_success(self):
        cur = FakeCursor(raise_on_close=True)
        conn = FakeConnection(cursor=cur)
        mysql = FakeMySQL(conn)

        # A cursor that raises on close must not turn a successful commit into
        # an error for the caller.
        with transaction(mysql):
            pass
        self.assertEqual(conn.commits, 1)
        self.assertTrue(cur.closed)

    def test_no_mysql_raises_runtime_error(self):
        # No explicit mysql and no app context -> clear RuntimeError, not a crash.
        with self.assertRaises(RuntimeError):
            with transaction():
                pass

    def test_refresh_is_called_when_db_compat_available(self):
        # db_compat.refresh_flask_mysql_connection pings the connection. We can
        # observe the ping on our fake to confirm the refresh hook ran.
        conn = FakeConnection()
        mysql = FakeMySQL(conn)
        with transaction(mysql):
            pass
        self.assertGreaterEqual(conn.pinged, 1, "refresh hook should have pinged the connection")


class ReadCursorTests(unittest.TestCase):
    def test_read_never_commits_and_closes(self):
        conn = FakeConnection()
        mysql = FakeMySQL(conn)

        with read_cursor(mysql) as cur:
            cur.execute("SELECT 1", None)
            rows = cur.fetchall()

        self.assertEqual(rows, [])
        self.assertEqual(conn.commits, 0, "read_cursor must never commit")
        self.assertEqual(conn.rollbacks, 0, "read_cursor must not roll back on success")
        self.assertTrue(conn._cursor.closed, "read cursor must be closed")

    def test_read_closes_on_exception_and_propagates(self):
        conn = FakeConnection()
        mysql = FakeMySQL(conn)

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with read_cursor(mysql):
                raise Boom("read failed")

        self.assertEqual(conn.commits, 0)
        self.assertTrue(conn._cursor.closed, "read cursor must close even on error")

    def test_read_yields_the_cursor(self):
        conn = FakeConnection()
        mysql = FakeMySQL(conn)
        with read_cursor(mysql) as cur:
            self.assertIs(cur, conn._cursor)

    def test_no_mysql_raises_runtime_error(self):
        with self.assertRaises(RuntimeError):
            with read_cursor():
                pass


if __name__ == "__main__":
    unittest.main()
