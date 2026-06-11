"""GDPR schema-drift guard (AI quality patch, MC-02).

Every per-user table created by app1/user_profile_db.py MUST be handled by
gdpr_service — either exported AND erased (hard delete) or anonymised in
place. Before this patch the export/erase maps silently omitted
user_memories (the AI's free-form personality/life-context dossier — the
single most sensitive store) plus the 2026-06 profile tables
(user_certifications, user_languages, user_portfolio_links): export handed
the data subject an incomplete dossier and "retten til at blive glemt"
left the AI's notes about them in place.

This test parses the CREATE TABLE statements straight out of
app1/user_profile_db.py source text (read-only — no MySQLdb import, no DB)
and asserts membership in _EXPORT_QUERIES ∪ _DELETE_TABLES ∪
_ANONYMISE_TABLES, so the NEXT profile table cannot silently fall out of
compliance.

Pure string/constant inspection. Offline: no OPENAI_API_KEY, no MySQL.
"""
import os
import re
import sys
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import gdpr_service  # noqa: E402  (module-level is import-safe: json/logging only)

_PROFILE_DB_PATH = os.path.join(REPO_ROOT, "app1", "user_profile_db.py")

# The 2026-06 additions this patch closes the gap for. They are pure-profile
# (no financial/audit value) so they must be hard-deleted, not anonymised.
_NEW_PROFILE_TABLES = (
    "user_certifications",
    "user_languages",
    "user_portfolio_links",
    "user_memories",
)


def _profile_tables():
    """Table names from every CREATE TABLE in app1/user_profile_db.py."""
    with open(_PROFILE_DB_PATH, encoding="utf-8") as fh:
        source = fh.read()
    names = re.findall(
        r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+`?(\w+)`?", source, re.IGNORECASE
    )
    return sorted(set(names))


def _export_tables():
    return {table for table, _sql in gdpr_service._EXPORT_QUERIES}


def _delete_tables():
    return {table for table, _col in gdpr_service._DELETE_TABLES}


def _anonymise_tables():
    return {entry[0] for entry in gdpr_service._ANONYMISE_TABLES}


class TestGdprTableCoverage(unittest.TestCase):
    def test_parser_finds_the_known_schema(self):
        """Sanity: the regex actually sees the profile schema (guards against
        a refactor of user_profile_db.py silently emptying this test)."""
        tables = _profile_tables()
        self.assertGreaterEqual(
            len(tables), 12,
            f"Forventede mindst 12 CREATE TABLE i user_profile_db.py, fandt: {tables}",
        )
        self.assertIn("user_skills", tables)
        self.assertIn("user_memories", tables)

    def test_every_profile_table_is_exported(self):
        """GDPR art. 15/20: eksporten skal dække ALLE per-bruger-tabeller."""
        missing = sorted(set(_profile_tables()) - _export_tables())
        self.assertEqual(
            missing, [],
            "Tabeller oprettet i user_profile_db.py mangler i gdpr_service."
            f"_EXPORT_QUERIES: {missing}",
        )

    def test_every_profile_table_is_erased_or_anonymised(self):
        """GDPR art. 17: hver tabel skal enten hard-deletes eller anonymiseres."""
        covered = _delete_tables() | _anonymise_tables()
        missing = sorted(set(_profile_tables()) - covered)
        self.assertEqual(
            missing, [],
            "Tabeller oprettet i user_profile_db.py mangler i gdpr_service."
            f"_DELETE_TABLES/_ANONYMISE_TABLES: {missing}",
        )

    def test_new_profile_tables_are_hard_deleted(self):
        """user_memories + 2026-06-tabellerne er ren profil (ingen regnskabs-
        eller revisionsværdi) -> hard delete, ikke anonymisering."""
        deletes = _delete_tables()
        for table in _NEW_PROFILE_TABLES:
            self.assertIn(
                table, deletes,
                f"{table} skal hard-deletes ved GDPR-sletning",
            )

    def test_new_profile_tables_are_exported_keyed_on_username(self):
        """Eksport-SQL for de nye tabeller er scoped til præcis én bruger."""
        queries = dict(gdpr_service._EXPORT_QUERIES)
        for table in _NEW_PROFILE_TABLES:
            self.assertIn(table, queries)
            self.assertIn(
                "WHERE username=%s", queries[table],
                f"Eksport af {table} skal være keyed på username",
            )

    def test_delete_tables_use_username_column(self):
        """De nye delete-entries bruger `username` som WHERE-kolonne."""
        deletes = dict(gdpr_service._DELETE_TABLES)
        for table in _NEW_PROFILE_TABLES:
            self.assertEqual(
                deletes.get(table), "username",
                f"{table} skal slettes via WHERE username=%s",
            )

    def test_no_table_is_both_deleted_and_anonymised(self):
        """En tabel må ikke optræde i begge erase-planer (dobbeltbehandling
        ville give et misvisende slette-rapport-output)."""
        overlap = sorted(_delete_tables() & _anonymise_tables())
        self.assertEqual(overlap, [], f"Tabeller i begge planer: {overlap}")


if __name__ == "__main__":
    unittest.main()
