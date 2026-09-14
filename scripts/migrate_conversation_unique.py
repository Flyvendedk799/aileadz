"""One-off migration: UNIQUE (username, session_id) on conversation_history.

Why a script and not ensure_tables(): conversation_history holds LONGTEXT
transcripts, so adding a unique key can rebuild/lock the table for a while —
that must never happen inside a gunicorn worker's first request. The app works
without the key (conversation_state.save_turn is revision-checked); the key
closes the last race where two workers insert the same new session at once.

Duplicates are never deleted: all but the most recently updated row of a
(username, session_id) pair get "#dup<id>" appended to their session_id, so
they stay in the user's history and in GDPR export.

Usage (reads MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DB):
    python scripts/migrate_conversation_unique.py --dry-run
    python scripts/migrate_conversation_unique.py
"""
import argparse
import os
import sys

import pymysql

INDEX_NAME = "uk_user_session"


def _connect():
    return pymysql.connect(
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT") or 3306),
        user=os.environ.get("MYSQL_USER", ""),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=os.environ.get("MYSQL_DB", "aileadz"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="report what would change, change nothing")
    args = parser.parse_args(argv)

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'conversation_history' AND INDEX_NAME = %s LIMIT 1",
                (INDEX_NAME,),
            )
            if cur.fetchone():
                print(f"{INDEX_NAME} already exists — nothing to do.")
                return 0

            cur.execute(
                "SELECT username, session_id, COUNT(*) AS n FROM conversation_history "
                "GROUP BY username, session_id HAVING n > 1"
            )
            groups = cur.fetchall()
            renamed = 0
            for group in groups:
                cur.execute(
                    "SELECT id FROM conversation_history WHERE username = %s AND session_id = %s "
                    "ORDER BY updated_at DESC, id DESC",
                    (group["username"], group["session_id"]),
                )
                ids = [row["id"] for row in cur.fetchall()]
                for dup_id in ids[1:]:
                    renamed += 1
                    if not args.dry_run:
                        cur.execute(
                            "UPDATE conversation_history "
                            "SET session_id = CONCAT(LEFT(session_id, 80), '#dup', id) WHERE id = %s",
                            (dup_id,),
                        )
            print(f"{len(groups)} duplicated (username, session_id) pairs, {renamed} rows "
                  f"{'would be' if args.dry_run else 'were'} renamed.")

            if args.dry_run:
                conn.rollback()
                print(f"Dry run: would add UNIQUE KEY {INDEX_NAME} (username, session_id).")
                return 0

            conn.commit()
            print(f"Adding UNIQUE KEY {INDEX_NAME} (may take a while on a large table)…")
            cur.execute(
                f"ALTER TABLE conversation_history ADD UNIQUE KEY {INDEX_NAME} (username, session_id)"
            )
            conn.commit()
            print("Done.")
            return 0
    except Exception as exc:
        conn.rollback()
        print(f"Migration failed, nothing committed after the failing step: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
