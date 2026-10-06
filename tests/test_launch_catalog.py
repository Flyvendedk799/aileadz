"""Publication, stable session identity and branding have explicit preview boundaries."""

import json
import unittest
from unittest import mock
from werkzeug.datastructures import MultiDict
from tests.test_catalog_one_source import TempCatalog, _raw
import catalog_service as catalog
import branding_service
from calendar_service import parse_danish_date
import datetime


class CatalogLaunchTests(unittest.TestCase):
    def draft(self, job="vendor-change"):
        product = catalog.get_product("a")
        draft = {
            "job_id": job,
            "direct_edit": True,
            "status": "pending",
            "products": [{"handle": "a"}],
            "forced_vendor": "Udbyder A",
            "expected_revision": catalog.product_revision(product),
            "edit_fields": {
                "title": "Ny titel",
                "variants": [{"id": "stable-1", "date": "2099-06-01", "location": "Odense", "price": "1200", "seats": 4}],
            },
        }
        catalog._write_json(catalog._instance_path(catalog.IMPORT_DRAFT_DIR, job + ".json"), draft)
        return draft

    def test_vendor_draft_is_not_live_until_approved_and_keeps_session_identity(self):
        with TempCatalog([_raw("a", "Original")]):
            self.draft()
            self.assertEqual(catalog.get_product("a")["title"], "Original")
            catalog.confirm_import_draft("vendor-change")
            live = catalog.get_product("a")
            self.assertEqual(live["title"], "Ny titel")
            self.assertEqual(live["variants"][0]["session_id"], "stable-1")
            self.assertEqual(float(live["variants"][0]["price"]), 1200)
            self.assertEqual(catalog.confirm_import_draft("vendor-change")["status"], "confirmed")

    def test_stale_vendor_edit_does_not_overwrite_newer_admin_changes(self):
        with TempCatalog([_raw("a", "Original")]):
            self.draft()
            catalog.update_product("a", {"title": "Admin rettelse"})
            with self.assertRaises(ValueError):
                catalog.confirm_import_draft("vendor-change")
            self.assertEqual(catalog.get_product("a")["title"], "Admin rettelse")

    def test_crash_after_live_write_recovers_without_reapplying_or_conflicting(self):
        with TempCatalog([_raw("a", "Original")]):
            self.draft()
            real = catalog._write_json

            def crash(path, data):
                if path.endswith("vendor-change.json") and data.get("status") == "confirmed":
                    raise OSError("simulated power loss")
                return real(path, data)

            with mock.patch.object(catalog, "_write_json", side_effect=crash):
                with self.assertRaises(OSError):
                    catalog.confirm_import_draft("vendor-change")
            self.assertEqual(catalog.get_product("a")["title"], "Ny titel")
            self.assertEqual(catalog.confirm_import_draft("vendor-change")["status"], "confirmed")

    def test_session_form_rejects_malformed_dates_and_retains_existing_id(self):
        form = MultiDict(
            [
                ("session_id", "fixed"),
                ("session_date", "2099-01-01"),
                ("session_location", "København"),
                ("session_price", "1.250,50"),
                ("session_seats", "4"),
            ]
        )
        fields = catalog.session_fields_from_form(form)
        self.assertEqual(fields["variants"][0]["id"], "fixed")
        self.assertEqual(fields["variants"][0]["price"], "1250.50")
        form["session_date"] = "næste tilfældige dag"
        with self.assertRaises(ValueError):
            catalog.session_fields_from_form(form)

    def test_danish_date_range_uses_explicit_year_instead_of_second_day(self):
        self.assertEqual(parse_danish_date("15.-16. september 2099"), datetime.date(2099, 9, 15))
        self.assertEqual(parse_danish_date("10.–11. marts 2098"), datetime.date(2098, 3, 10))

    def test_branding_draft_never_changes_default_live_reader(self):
        row = {
            "company_name": "Firma",
            "primary_color": "#111111",
            "branding_status": "draft",
            "branding_draft": json.dumps({"primary_color": "#ff0000", "company_display_name": "Udgives senere"}),
        }
        self.assertEqual(branding_service._row_to_branding(row)["primary_color"], "#111111")
        self.assertEqual(branding_service._row_to_branding(row)["company_name"], "Firma")
        self.assertEqual(branding_service._row_to_branding(row, preview=True)["primary_color"], "#ff0000")
