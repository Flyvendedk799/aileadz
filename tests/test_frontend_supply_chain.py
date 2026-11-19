"""No page may load a script from another host without an exact version and an
integrity hash; Chart.js, marked and QRCode.js are served from the app itself."""
import hashlib
import base64
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "static", "futurematch", "vendor")


def _templates():
    for base in ("templates", os.path.join("app1", "templates")):
        for root, _dirs, files in os.walk(os.path.join(ROOT, base)):
            for name in files:
                if name.endswith(".html"):
                    yield os.path.join(root, name)


def test_every_external_script_is_pinned_and_has_integrity():
    offenders = []
    for path in _templates():
        text = open(path, encoding="utf-8", errors="ignore").read()
        for tag in re.findall(r"<script\b[^>]*\bsrc=[\"']https?://[^>]*>", text):
            src = re.search(r"src=[\"']([^\"']+)", tag).group(1)
            pinned = re.search(r"@\d+\.\d+\.\d+|/r\d+/|/\d+\.\d+\.\d+/", src)
            if "integrity=" not in tag or "crossorigin" not in tag or not pinned:
                offenders.append("%s: %s" % (os.path.relpath(path, ROOT), src))
    assert offenders == [], offenders


def test_vendored_files_match_the_hashes_in_the_readme():
    readme = open(os.path.join(VENDOR, "README.md"), encoding="utf-8").read()
    listed = set(re.findall(r"\| `([A-Za-z0-9+/=]{64})` \|", readme))
    assert len(listed) == 3
    found = set()
    for root, _dirs, files in os.walk(VENDOR):
        for name in files:
            if name.endswith(".js"):
                data = open(os.path.join(root, name), "rb").read()
                found.add(base64.b64encode(hashlib.sha384(data).digest()).decode())
    assert found == listed
