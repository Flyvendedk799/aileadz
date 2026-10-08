"""Behavioral regression tests for the actual browser chat transports (offline)."""
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node.js is needed for browser transport tests")
def test_chat_stream_behaviors():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "--test", "tests/js/chat_stream.test.cjs"],
        cwd=root, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
