#!/usr/bin/env python3
"""Dump the live schema so it can be reviewed as the Alembic baseline (N-3.4).

Run it on the VPS (or through the SSH tunnel) against the PRODUCTION database:

    MYSQL_HOST=... MYSQL_USER=... MYSQL_PASSWORD=... MYSQL_DB=... \
        python scripts/dump_schema_baseline.py > docs/schema_baseline.sql

It prints one ``SHOW CREATE TABLE`` per table (no data) and a comparison against
the tables the code owns (``schema_registry.expected_tables``), so drift between
production and the code is visible before you ``alembic stamp b001_part_b_lifecycle``.
Read-only; it never writes to the database.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    import pymysql

    conn = pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ["MYSQL_DB"],
        charset="utf8mb4",
    )
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    tables = sorted(r[0] for r in cur.fetchall())
    print("-- Schema baseline dump (no data). Tables: %d" % len(tables))
    for t in tables:
        cur.execute("SHOW CREATE TABLE `%s`" % t)
        print("\n-- %s" % t)
        print(cur.fetchone()[1] + ";")

    try:
        from schema_registry import expected_tables

        expected = expected_tables()
        missing = sorted(set(expected) - set(tables))
        extra = sorted(set(tables) - set(expected))
        print("\n-- ---- drift report ----")
        print("-- tables the code owns but production lacks: %s" % (missing or "none"))
        print("-- tables in production the code does not define: %s" % (extra or "none"))
    except Exception as exc:  # the dump above is the important part
        print("-- drift report unavailable: %s" % exc)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
