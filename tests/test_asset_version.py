"""Content-hash cache-busting for static assets (asset_version.py).

Static files are cached for a year, so a changed file only reaches returning
visitors when its URL changes. chat.js was edited twice under a hand-bumped
?v=14 that nobody bumped, and every browser kept running the old script.
Offline: no MySQL, no OpenAI.
"""
import os
import re
import sys
import tempfile
import time
import unittest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)

import asset_version as av  # noqa: E402

_TEMPLATES = os.path.join(_REPO_ROOT, "templates")


class AssetVersionTests(unittest.TestCase):
    def test_version_changes_with_content_and_is_stable_otherwise(self):
        with tempfile.TemporaryDirectory() as static:
            path = os.path.join(static, "app.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("console.log(1)")
            first = av.asset_version(static, "app.js")
            self.assertEqual(first, av.asset_version(static, "app.js"))
            time.sleep(0.01)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("console.log(2)")
            os.utime(path, ns=(time.time_ns(), time.time_ns()))
            self.assertNotEqual(first, av.asset_version(static, "app.js"))

    def test_missing_file_and_traversal_are_safe(self):
        with tempfile.TemporaryDirectory() as static:
            self.assertEqual(av.asset_version(static, "nope.js"), "0")
            self.assertEqual(av.asset_version(static, "../secrets.txt"), "0")
            self.assertEqual(av.asset_version("", "x.js"), "0")

    def test_ai_chat_assets_are_content_versioned_in_templates(self):
        checks = {
            os.path.join("fm", "chat.html"): ("futurematch/assets/chat.js", "futurematch/assets/chat.css"),
            # fm-pages.css sat at a hand-bumped ?v=15 after the HR panel's [hidden] fix, so
            # browsers that had cached the earlier file kept an HR popup that could not be closed.
            "fm_base.html": (
                "futurematch/assets/ai-sidebar.js",
                "futurematch/assets/fm.css",
                "futurematch/assets/fm-pages.css",
                "futurematch/assets/shell.js",
            ),
        }
        for template, assets in checks.items():
            with open(os.path.join(_TEMPLATES, template), encoding="utf-8") as fh:
                src = fh.read()
            for asset in assets:
                self.assertIn(f"?v={{{{ asset_version('{asset}') }}}}", src, f"{template}: {asset}")
                self.assertIsNone(
                    re.search(re.escape(asset) + r"'\) \}\}\?v=\d", src),
                    f"{template} still hand-versions {asset}",
                )

    def test_no_template_hand_versions_a_shared_asset(self):
        pattern = re.compile(r"futurematch/assets/[\w.-]+\.(?:css|js)'\) \}\}\?v=\d")
        offenders = []
        for root, _dirs, files in os.walk(_TEMPLATES):
            for name in files:
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    if pattern.search(fh.read()):
                        offenders.append(os.path.relpath(path, _TEMPLATES))
        self.assertEqual(offenders, [], "use ?v={{ asset_version('...') }} instead of a hand-bumped number")

    def test_app_registers_the_template_global(self):
        os.environ.setdefault("SANDBOX", "1")
        os.environ.setdefault("AI_WARMUP_ON_IMPORT", "0")
        os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
        os.environ.setdefault("MYSQL_USER", "none")
        os.environ.setdefault("MYSQL_PASSWORD", "none")
        os.environ.setdefault("MYSQL_DB", "none")
        os.environ.setdefault("OPENAI_API_KEY", "sk-test")
        from run import create_app
        app = create_app()
        version = app.jinja_env.globals["asset_version"]("futurematch/assets/chat.js")
        self.assertRegex(version, r"^[0-9a-f]{10}$")


if __name__ == "__main__":
    unittest.main()
