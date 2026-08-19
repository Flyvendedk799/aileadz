"""WSGI entrypoint for ServerHoster (and any gunicorn/uWSGI host).

Run with:  gunicorn wsgi:application --bind 0.0.0.0:$PORT

All configuration comes from the process environment (MYSQL_* or a mysql://
DATABASE_URL, SECRET_KEY, OPENAI_API_KEY, AI_*, MAIL_*, ...). A `.env` next to
this file is loaded first, without overriding variables the host already set —
ServerHoster injects env vars directly, so no .env is needed there.
See docs/runbooks/DEPLOY.md for the full variable list.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_HERE, ".env"))
except ImportError:  # python-dotenv is in requirements.txt; stay bootable without it
    pass

from run import create_app  # noqa: E402

application = create_app()


class CanonicalHost:
    """301 `www.<host>` -> `<host>` (path + query kept).

    The site lived at www.futurematch.dk on PythonAnywhere; ServerHoster routes
    both www and the apex to this process, so the app itself picks the canonical
    apex (one host for session cookies, SEO and absolute links).
    Set CANONICAL_WWW_REDIRECT=0 to disable.
    """

    def __init__(self, app):
        self.app = app
        self.enabled = os.environ.get("CANONICAL_WWW_REDIRECT", "1") not in ("0", "false", "no")

    def __call__(self, environ, start_response):
        host = (environ.get("HTTP_HOST") or "").lower()
        if self.enabled and host.startswith("www."):
            from werkzeug.wsgi import get_current_url
            target = get_current_url(environ).replace("://" + host, "://" + host[4:], 1)
            start_response("301 Moved Permanently", [("Location", target), ("Content-Length", "0")])
            return [b""]
        return self.app(environ, start_response)


# The app runs on localhost behind Cloudflare's tunnel, which terminates TLS and
# forwards X-Forwarded-Proto/For/Host. Trust exactly one proxy hop so
# url_for(_external=True) (vendor password links, login links) builds https://
# URLs on the public host and request.remote_addr is the real client IP.
# ProxyFix runs first so CanonicalHost sees the real scheme/host.
try:
    from werkzeug.middleware.proxy_fix import ProxyFix
    application.wsgi_app = ProxyFix(
        CanonicalHost(application.wsgi_app), x_for=1, x_proto=1, x_host=1, x_port=1
    )
except Exception:  # pragma: no cover - never let the proxy shim break boot
    pass
