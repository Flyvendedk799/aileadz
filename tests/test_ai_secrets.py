"""Tests for admin-managed, encrypted-at-rest provider API keys.

The critical assertions here are negative ones: that a stored key is ciphertext
and not the plaintext, that no code path returns a key value to a caller that
renders it, and that a write is REFUSED rather than downgraded to plaintext when
encryption is unavailable.

Storage is exercised through a small in-memory fake of the MySQL cursor API so
the encrypt -> store -> read -> decrypt round trip is real rather than mocked
out at the boundary.
"""
import base64
import os
import unittest
from unittest.mock import patch

import db_compat  # noqa: F401  (installs the MySQLdb shim)
from flask import Flask

import ai_provider
import ai_secrets

_KEY_A = base64.urlsafe_b64encode(b"A" * 32).decode()
_KEY_B = base64.urlsafe_b64encode(b"B" * 32).decode()

_OPENAI = "OPENAI_API_KEY"
_ANTHROPIC = "ANTHROPIC_API_KEY"


# --- in-memory MySQL fake ------------------------------------------------------

class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._rows = []

    def execute(self, sql, params=()):
        flat = " ".join(sql.split()).upper()
        if flat.startswith("CREATE TABLE"):
            return
        if flat.startswith("INSERT INTO AI_SECRETS"):
            name, value, hint, updated_by = params
            self.store[name] = {
                "secret_name": name, "secret_value": value, "hint": hint,
                "updated_by": updated_by, "updated_at": "2026-01-01 00:00:00",
            }
            return
        if flat.startswith("DELETE FROM AI_SECRETS"):
            self.store.pop(params[0], None)
            return
        if flat.startswith("SELECT SECRET_NAME"):
            self._rows = [dict(row) for row in self.store.values()]
            return
        self._rows = []

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, store):
        self.store = store
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, *a, **k):
        return _FakeCursor(self.store)

    def ping(self, *a, **k):
        return True

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakeMySQL:
    def __init__(self):
        self.store = {}
        self.connection = _FakeConnection(self.store)


class _SecretsTestCase(unittest.TestCase):
    """Fresh app context, fresh fake DB, and no leaked module/env state."""

    def setUp(self):
        self.mysql = _FakeMySQL()
        self.app = Flask(__name__)
        self.app.config["SECRET_KEY"] = "unit-test-secret"
        self.app.mysql = self.mysql
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.addCleanup(self.ctx.pop)

        ai_secrets._CACHE = {}
        ai_secrets._ENV_ORIGINAL.clear()
        ai_secrets.invalidate_cache()
        ai_secrets._TABLE_READY.clear()
        self.addCleanup(ai_secrets._ENV_ORIGINAL.clear)
        self.addCleanup(ai_secrets.invalidate_cache)
        self.addCleanup(setattr, ai_secrets, "_CACHE", {})
        ai_provider.invalidate_settings_cache()
        ai_provider._SNAPSHOT = {}
        self.addCleanup(ai_provider.invalidate_settings_cache)

        # Keys are exported to os.environ for legacy consumers; keep the test
        # process clean either way.
        self._env = patch.dict(os.environ, {"AI_SECRET_KEY": _KEY_A}, clear=False)
        self._env.start()
        self.addCleanup(self._env.stop)
        for name in (_OPENAI, _ANTHROPIC):
            os.environ.pop(name, None)


# --- storage round trip --------------------------------------------------------

class StorageTests(_SecretsTestCase):
    def test_stored_value_is_ciphertext_not_the_key(self):
        ok, err = ai_secrets.set_secret(self.mysql, _OPENAI, "sk-super-secret-123", "admin")
        self.assertTrue(ok, err)
        stored = self.mysql.store[_OPENAI]["secret_value"]
        self.assertNotIn("sk-super-secret-123", stored)
        self.assertTrue(stored.startswith("fernet$"))

    def test_round_trip_returns_the_original_key(self):
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-round-trip", "admin")
        ai_secrets.invalidate_cache()
        self.assertEqual(ai_secrets.get_secret(_OPENAI), "sk-round-trip")

    def test_database_value_wins_over_environment(self):
        os.environ[_OPENAI] = "sk-from-env"
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-from-db", "admin")
        ai_secrets.invalidate_cache()
        self.assertEqual(ai_secrets.get_secret(_OPENAI), "sk-from-db")

    def test_environment_is_the_fallback_when_nothing_is_stored(self):
        os.environ[_OPENAI] = "sk-from-env"
        self.assertEqual(ai_secrets.get_secret(_OPENAI), "sk-from-env")

    def test_clear_removes_the_stored_key(self):
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-temp", "admin")
        ok, err = ai_secrets.clear_secret(self.mysql, _OPENAI, "admin")
        self.assertTrue(ok, err)
        self.assertNotIn(_OPENAI, self.mysql.store)

    def test_clearing_a_key_actually_stops_it_working(self):
        """Removal must take effect now, not after a process restart: the
        environ mirror that makes legacy callers work has to be undone too."""
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-compromised", "admin")
        self.assertEqual(os.environ.get(_OPENAI), "sk-compromised")

        ai_secrets.clear_secret(self.mysql, _OPENAI, "admin")
        self.assertEqual(ai_secrets.get_secret(_OPENAI), "")
        self.assertIsNone(os.environ.get(_OPENAI))

    def test_clearing_restores_a_pre_existing_environment_key(self):
        os.environ[_OPENAI] = "sk-from-env"
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-from-db", "admin")
        self.assertEqual(ai_secrets.get_secret(_OPENAI), "sk-from-db")

        ai_secrets.clear_secret(self.mysql, _OPENAI, "admin")
        # Falls back to what the host environment configured, not to nothing.
        self.assertEqual(ai_secrets.get_secret(_OPENAI), "sk-from-env")
        self.assertEqual(os.environ.get(_OPENAI), "sk-from-env")

    def test_a_row_removed_by_another_worker_is_un_exported_on_refresh(self):
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-worker-a", "admin")
        # Simulate a different gunicorn worker deleting the row.
        self.mysql.store.pop(_OPENAI)
        ai_secrets.invalidate_cache()
        self.assertEqual(ai_secrets.get_secret(_OPENAI), "")

    def test_unknown_names_and_blank_values_are_rejected(self):
        self.assertFalse(ai_secrets.set_secret(self.mysql, "SECRET_KEY", "x", "admin")[0])
        self.assertFalse(ai_secrets.set_secret(self.mysql, "DATABASE_URL", "x", "admin")[0])
        self.assertFalse(ai_secrets.set_secret(self.mysql, _OPENAI, "   ", "admin")[0])
        self.assertFalse(self.mysql.store)

    def test_legacy_consumers_see_the_key_via_environ(self):
        """app1 / catalog_service / insights_engine read OPENAI_API_KEY directly."""
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-exported", "admin")
        self.assertEqual(os.environ.get(_OPENAI), "sk-exported")


# --- refusal instead of plaintext ---------------------------------------------

class EncryptionRequiredTests(_SecretsTestCase):
    def test_write_is_refused_when_no_key_material_exists(self):
        with patch.object(ai_secrets, "_get_fernet", return_value=None):
            ok, err = ai_secrets.set_secret(self.mysql, _OPENAI, "sk-should-not-persist", "admin")
        self.assertFalse(ok)
        self.assertIn("kryptering", err.lower())
        # The decisive assertion: nothing was written at all.
        self.assertFalse(self.mysql.store)

    def test_write_is_refused_when_cryptography_is_missing(self):
        with patch.object(ai_secrets, "_FERNET_AVAILABLE", False):
            self.assertFalse(ai_secrets.encryption_available())
            ok, _err = ai_secrets.set_secret(self.mysql, _OPENAI, "sk-nope", "admin")
        self.assertFalse(ok)
        self.assertFalse(self.mysql.store)

    def test_a_row_encrypted_under_a_rotated_key_is_not_used(self):
        # Realistic ordering: the host environment configures a key at boot, an
        # admin later overrides it from the UI, then the encryption key rotates.
        os.environ[_OPENAI] = "sk-from-env"
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-old-key", "admin")
        self.assertEqual(ai_secrets.get_secret(_OPENAI), "sk-old-key")

        ai_secrets.invalidate_cache()
        ai_secrets._CACHE = {}
        with patch.dict(os.environ, {"AI_SECRET_KEY": _KEY_B}):
            # Undecryptable: fall back to the environment, never hand out the
            # ciphertext, and flag the row so an operator can see why.
            self.assertEqual(ai_secrets.get_secret(_OPENAI), "sk-from-env")
            status = {s["name"]: s for s in ai_secrets.secret_status()}[_OPENAI]
            self.assertTrue(status["unreadable"])
            self.assertEqual(status["source"], "miljøvariabel")

    def test_an_unmarked_row_is_never_returned(self):
        """A value written outside this module is ignored, not handed out."""
        self.mysql.store[_OPENAI] = {
            "secret_name": _OPENAI, "secret_value": "sk-plaintext-somehow",
            "hint": "ehow", "updated_by": "?", "updated_at": None,
        }
        ai_secrets.invalidate_cache()
        self.assertEqual(ai_secrets.get_secret(_OPENAI), "")


# --- nothing leaks -------------------------------------------------------------

class NoLeakTests(_SecretsTestCase):
    def test_secret_status_never_contains_the_value(self):
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-do-not-render-me", "admin")
        ai_secrets.invalidate_cache()
        rendered = repr(ai_secrets.secret_status())
        self.assertNotIn("sk-do-not-render-me", rendered)
        self.assertNotIn("fernet$", rendered)

    def test_secret_status_reports_presence_and_a_short_hint_only(self):
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-abcdefgh1234", "admin")
        ai_secrets.invalidate_cache()
        status = {s["name"]: s for s in ai_secrets.secret_status()}[_OPENAI]
        self.assertTrue(status["configured"])
        self.assertEqual(status["source"], "database")
        self.assertEqual(status["hint"], "1234")
        self.assertEqual(status["updated_by"], "admin")

    def test_audit_details_are_scrubbed_of_secret_values(self):
        import admin_dashboard

        scrubbed = admin_dashboard._scrub_secret_values({
            "settings": {"AI_PROVIDER": "anthropic"},
            "OPENAI_API_KEY": "sk-leaked",
            "nested": {"ANTHROPIC_API_KEY": "sk-also-leaked"},
        })
        self.assertEqual(scrubbed["OPENAI_API_KEY"], "<redacted>")
        self.assertEqual(scrubbed["nested"]["ANTHROPIC_API_KEY"], "<redacted>")
        # Non-secret settings survive so the audit row stays useful.
        self.assertEqual(scrubbed["settings"]["AI_PROVIDER"], "anthropic")

    def test_managed_settings_still_cannot_hold_a_secret(self):
        self.assertNotIn(_OPENAI, ai_provider.MANAGED_KEYS)
        self.assertNotIn(_ANTHROPIC, ai_provider.MANAGED_KEYS)


# --- client wiring -------------------------------------------------------------

class ClientWiringTests(_SecretsTestCase):
    def test_openai_client_picks_up_a_database_key_and_rotates(self):
        import ai_runtime

        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-first", "admin")
        ai_secrets.invalidate_cache()
        first = ai_runtime._openai_client()
        self.assertEqual(first.api_key, "sk-first")

        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-second", "admin")
        ai_secrets.invalidate_cache()
        second = ai_runtime._openai_client()
        # Rotation must build a NEW client, not serve the cached one.
        self.assertEqual(second.api_key, "sk-second")
        self.assertIsNot(first, second)

    def test_anthropic_client_picks_up_a_database_key(self):
        import ai_provider_anthropic

        ai_secrets.set_secret(self.mysql, _ANTHROPIC, "sk-ant-db", "admin")
        ai_secrets.invalidate_cache()
        self.assertEqual(ai_provider_anthropic.client().api_key, "sk-ant-db")

    def test_readiness_reflects_a_database_key(self):
        self.assertFalse(ai_provider.openai_configured())
        ai_secrets.set_secret(self.mysql, _OPENAI, "sk-ready", "admin")
        ai_secrets.invalidate_cache()
        self.assertTrue(ai_provider.openai_configured())

    def test_connection_test_reports_a_missing_key_without_calling_out(self):
        ok, message = ai_secrets.test_provider_key("anthropic")
        self.assertFalse(ok)
        self.assertIn("ANTHROPIC_API_KEY", message)


if __name__ == "__main__":
    unittest.main()
