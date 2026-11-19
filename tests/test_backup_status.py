"""T0-7: the status page notices a missing, stale or failed backup."""
from datetime import datetime, timezone

import backup_status

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _backup(root, stamp, db_bytes=1234):
    d = root / stamp
    d.mkdir()
    (d / "MANIFEST").write_text("created_utc=%s\ndb_bytes=%d\n" % (stamp, db_bytes))


def test_unconfigured(monkeypatch):
    monkeypatch.delenv("BACKUP_DIR", raising=False)
    assert backup_status.status(NOW)["state"] == "unconfigured"


def test_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    assert backup_status.status(NOW)["state"] == "missing"
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "nope"))
    assert backup_status.status(NOW)["state"] == "missing"


def test_fresh_then_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    _backup(tmp_path, "20261119T013000Z")
    _backup(tmp_path, "20261120T013000Z", db_bytes=99)
    (tmp_path / "20261121T000000Z").mkdir()          # unfinished: no manifest
    s = backup_status.status(NOW)
    assert s["state"] == "ok" and s["stamp"] == "20261120T013000Z" and s["db_bytes"] == 99 and s["count"] == 2
    later = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    assert backup_status.status(later)["state"] == "stale"


def test_failure_after_newest_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    _backup(tmp_path, "20261120T013000Z")
    (tmp_path / "LAST_FAILURE").write_text("2026-11-19T01:30:05Z")   # older: ignored
    assert backup_status.status(NOW)["state"] == "ok"
    (tmp_path / "LAST_FAILURE").write_text("2026-11-20T11:00:00Z")
    assert backup_status.status(NOW)["state"] == "failed"
