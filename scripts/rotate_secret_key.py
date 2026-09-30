#!/usr/bin/env python3
"""Re-encrypt everything that is encrypted with a key DERIVED from SECRET_KEY.

Rotating SECRET_KEY (S-1.4) invalidates every session cookie (expected) but also
orphans three kinds of stored secrets that derive their Fernet key from it:

  * ai_secrets.secret_value          -> AI provider API keys   (unless AI_SECRET_KEY is set)
  * company_sso_configs.config       -> SSO client_secret      (unless SSO_FERNET_KEY is set)
  * user_2fa.secret_enc              -> TOTP secrets           (unless TWOFA_FERNET_KEY is set)

Run it ONCE, with both keys, BEFORE starting the app on the new key:

    OLD_SECRET_KEY=<old> NEW_SECRET_KEY=<new> python scripts/rotate_secret_key.py          # dry run
    OLD_SECRET_KEY=<old> NEW_SECRET_KEY=<new> python scripts/rotate_secret_key.py --apply  # do it

Database access comes from the usual MYSQL_* / DATABASE_URL environment (the same
variables the app reads). A dedicated key env var (AI_SECRET_KEY etc.) means that
family does not depend on SECRET_KEY and is skipped. The script never prints a
secret, only counts.
"""
import base64
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def derive(secret, kind):
    """Fernet for ``kind`` derived from ``secret`` exactly like the app does."""
    from cryptography.fernet import Fernet
    material = ("2fa|" + secret) if kind == "2fa" else secret
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest()))


def reencrypt(blob, old, new, prefix=""):
    """Return ``blob`` re-encrypted from ``old`` to ``new`` Fernet, or None when it
    is not decryptable with ``old`` (already rotated / a different key family)."""
    from cryptography.fernet import InvalidToken
    if prefix:
        if not blob or not blob.startswith(prefix):
            return None
        token = blob[len(prefix):]
    else:
        token = blob
    try:
        plain = old.decrypt(token.encode("utf-8"))
    except InvalidToken:
        return None
    return prefix + new.encrypt(plain).decode("utf-8")


def rotate(conn, old_key, new_key, apply=False, environ=None):
    """Returns ``{family: {'seen': n, 'rotated': n, 'skipped': n}}``."""
    environ = os.environ if environ is None else environ
    report = {}
    cur = conn.cursor()

    def family(name, dedicated_env):
        if environ.get(dedicated_env):
            report[name] = {"skipped_reason": "%s is set; independent of SECRET_KEY" % dedicated_env}
            return False
        report[name] = {"seen": 0, "rotated": 0, "unreadable": 0}
        return True

    if family("ai_secrets", "AI_SECRET_KEY"):
        old, new = derive(old_key, "derived"), derive(new_key, "derived")
        cur.execute("SELECT secret_name, secret_value FROM ai_secrets")
        for row in cur.fetchall() or []:
            name, value = (row["secret_name"], row["secret_value"]) if isinstance(row, dict) else row
            report["ai_secrets"]["seen"] += 1
            out = reencrypt(value, old, new, "fernet$")
            if out is None:
                report["ai_secrets"]["unreadable"] += 1
                continue
            if apply:
                cur.execute("UPDATE ai_secrets SET secret_value = %s WHERE secret_name = %s", (out, name))
            report["ai_secrets"]["rotated"] += 1

    if family("sso_client_secrets", "SSO_FERNET_KEY"):
        old, new = derive(old_key, "derived"), derive(new_key, "derived")
        cur.execute("SELECT id, config FROM company_sso_configs")
        for row in cur.fetchall() or []:
            cid, cfg = (row["id"], row["config"]) if isinstance(row, dict) else row
            try:
                data = json.loads(cfg) if isinstance(cfg, (str, bytes)) else (cfg or {})
            except Exception:
                continue
            value = data.get("client_secret") if isinstance(data, dict) else None
            if not value:
                continue
            report["sso_client_secrets"]["seen"] += 1
            out = reencrypt(value, old, new, "fernet$")
            if out is None:
                report["sso_client_secrets"]["unreadable"] += 1
                continue
            data["client_secret"] = out
            if apply:
                cur.execute("UPDATE company_sso_configs SET config = %s WHERE id = %s", (json.dumps(data), cid))
            report["sso_client_secrets"]["rotated"] += 1

    if family("totp_secrets", "TWOFA_FERNET_KEY"):
        old, new = derive(old_key, "2fa"), derive(new_key, "2fa")
        try:
            cur.execute("SELECT user_id, secret_enc FROM user_2fa")
            rows = cur.fetchall() or []
        except Exception:
            rows = []
        for row in rows:
            uid, blob = (row["user_id"], row["secret_enc"]) if isinstance(row, dict) else row
            report["totp_secrets"]["seen"] += 1
            out = reencrypt(blob, old, new)
            if out is None:
                report["totp_secrets"]["unreadable"] += 1
                continue
            if apply:
                cur.execute("UPDATE user_2fa SET secret_enc = %s WHERE user_id = %s", (out, uid))
            report["totp_secrets"]["rotated"] += 1

    if apply:
        conn.commit()
    else:
        conn.rollback()
    cur.close()
    return report


def main(argv):
    old_key, new_key = os.environ.get("OLD_SECRET_KEY"), os.environ.get("NEW_SECRET_KEY")
    if not old_key or not new_key or old_key == new_key:
        print("Set OLD_SECRET_KEY and NEW_SECRET_KEY (different values).", file=sys.stderr)
        return 2
    apply = "--apply" in argv
    os.environ["SECRET_KEY"] = new_key      # the app factory insists on a real key
    from run import create_app
    app = create_app()
    with app.app_context():
        report = rotate(app.mysql.connection, old_key, new_key, apply=apply)
    print(("APPLIED" if apply else "DRY RUN") + ": " + json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
