"""Content-hash cache-busting for static assets.

Static files are served with a one-year ``immutable`` Cache-Control (run.py,
security_headers.py), so the only thing that makes a browser fetch a changed
file is a changed URL. The old convention — hand-bump ``?v=N`` in the template
on every edit — failed silently: chat.js was changed twice without a bump, and
every returning visitor kept running the old script (which restored the last
conversation instead of opening a new chat).

Templates use ``?v={{ asset_version('futurematch/assets/chat.js') }}``; the
version is a short hash of the file's content, so an edit busts the cache on
its own and an unchanged file keeps its cached copy across deploys.
"""
import hashlib
import os
from functools import lru_cache


@lru_cache(maxsize=512)
def _digest(path, mtime_ns, size):
    # mtime/size are part of the cache key only, so an edited file is re-hashed.
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:10]


def asset_version(static_folder, filename):
    """Short content hash for a file under ``static_folder``; "0" if unreadable."""
    if not static_folder or not filename:
        return "0"
    root = os.path.normpath(static_folder)
    path = os.path.normpath(os.path.join(root, filename))
    if not path.startswith(root + os.sep):
        return "0"
    try:
        stat = os.stat(path)
        return _digest(path, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return "0"


def register_asset_version(app):
    """Expose ``asset_version(filename)`` to every Jinja template."""
    app.jinja_env.globals["asset_version"] = lambda filename: asset_version(app.static_folder, filename)
