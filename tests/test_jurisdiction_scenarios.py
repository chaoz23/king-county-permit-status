import unittest
from unittest.mock import patch

import lookup
import route


def mbp_permit(number="B-100", jurisdiction="Bellevue"):
    return {
        "PermitNumber": number,
        "PermitType": "Building Permit",
        "PermitStatus": "Issued",
        "PermitDescription": "Addition",
        "Address": "123 Main St",
        "Jurisdiction": jurisdiction,
        "AppliedDate": None,
        "IssuedDate": None,
        "FinaledDate": None,
        "ApplicationExpDate": None,
    }


def normalized_permit(number="B-100", jurisdiction="Bellevue"):
    return {
        "permit_number": number,
        "type": "Building Permit",
        "status": "Issued",
        "description": "Addition",
        "address": "123 Main St",
        "jurisdiction": jurisdiction,
        "applied_date": None,
        "issued_date": None,
        "finaled_date": None,
        "expires_date": None,
        "portal": "https://example.test/permit",
    }


class LocalityPriorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = route.load_routing_data()

    def assert_city(self, address, expected):
        self.assertEqual(lookup.detect_city(address), expected)
        self.assertEqual(route.detect_city(address, self.data), expected)

    def test_kent_kangley_street_does_not_override_covington(self):
        self.assert_city("17000 SE Kent Kangley Rd, Covington, WA", "covington")

    def test_renton_avenue_does_not_override_seattle(self):
        self.assert_city("9000 Renton Ave S, Seattle, WA", "seattle")

    def test_pacific_highway_does_not_override_federal_way(self):
        self.assert_city("32000 Pacific Hwy S, Federal Way, WA", "federal way")

    def test_loose_address_without_commas_still_detects_city(self):
        self.assert_city("123 Main St Maple Valley WA 98038", "maple valley")

    def test_spelled_out_state_is_accepted_in_locality_component(self):
        self.assert_city("123 Main St, Bellevue Washington 98004", "bellevue")


class PopulationPriorityScenarioTests(unittest.TestCase):
    def test_seattle_groups_building_and_electrical_at_city_portal(self):
        result = route.route(
            "900 5th Ave, Seattle, WA",
            "remodel kitchen and rewire electrical panel",
        )

        self.assertEqual(result["location"], "Seattle")
        self.assertEqual(len(result["portals"]), 1)
        self.assertEqual(
            set(result["portals"][0]["permit_types"]),
            {"building", "electrical"},
        )
        self.assertEqual(result["portals"][0]["portal"], lookup.SEPARATE_PORTALS["seattle"])

    def test_seattle_lookup_skips_lni_after_complete_city_search(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(lookup, "search_seattle", return_value=([], [])),
            patch.object(lookup, "search_lni") as search_lni,
        ):
            result = lookup.lookup("900 5th Ave, Seattle, WA")

        search_lni.assert_not_called()
        self.assertNotIn("separate_portal", result)
        self.assertIn("Seattle Open Data", result["searched"])
        self.assertIn(
            "WA State L&I — skipped (Seattle handles its own electrical)",
            result["searched"],
        )

    def test_seattle_partial_failure_returns_actionable_fallback(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(
                lookup,
                "search_seattle",
                return_value=([], ["Electrical: offline"]),
            ),
        ):
            result = lookup.lookup("900 5th Ave, Seattle, WA")

        self.assertEqual(result["action"], "refine")
        self.assertEqual(result["separate_portal"]["city"], "Seattle")
        self.assertTrue(result["separate_portal"]["electrical"])
        self.assertIn("Seattle Open Data — Electrical: offline", result["errors"])

    def test_unincorporated_work_routes_to_county_and_lni(self):
        result = route.route(
            "123 Rural Rd, Fall City, WA",
            "build addition and rewire electrical panel",
        )

        self.assertEqual(result["location"], "Unincorporated King County")
        self.assertEqual(
            {permit["handled_by"] for permit in result["permits"]},
            {"King County DPER", "WA State L&I"},
        )

    def test_unincorporated_lookup_searches_all_mbp_sources_and_lni(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]) as search_mbp,
            patch.object(lookup, "search_bellevue", return_value=[]),
            patch.object(lookup, "search_lni", return_value=([], [])) as search_lni,
        ):
            result = lookup.lookup("123 Rural Rd, Fall City, WA")

        self.assertEqual(search_mbp.call_count, len(lookup.JURISDICTIONS))
        search_lni.assert_called_once()
        self.assertNotIn("separate_portal", result)

    def test_bellevue_deduplicates_mbp_and_open_data_results(self):
        raw = mbp_permit()

        def search_mbp(_opener, _token, jurisdiction, **_kwargs):
            return [raw] if jurisdiction == lookup.JURIS_BY_NAME["bellevue"] else []

        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", side_effect=search_mbp),
            patch.object(lookup, "search_bellevue", return_value=[normalized_permit()]),
            patch.object(lookup, "search_lni") as search_lni,
        ):
            result = lookup.lookup("123 Main St, Bellevue, WA")

        search_lni.assert_not_called()
        self.assertEqual(result["action"], "found")
        self.assertEqual(result["permit_count"], 1)
        self.assertIn("Bellevue Open Data", result["searched"])

    def test_bellevue_partial_source_failure_preserves_mbp_result(self):
        def search_mbp(_opener, _token, jurisdiction, **_kwargs):
            return [mbp_permit()] if jurisdiction == lookup.JURIS_BY_NAME["bellevue"] else []

        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", side_effect=search_mbp),
            patch.object(lookup, "search_bellevue", return_value="Error: offline"),
        ):
            result = lookup.lookup("123 Main St, Bellevue, WA")

        self.assertEqual(result["action"], "found")
        self.assertEqual(result["permit_count"], 1)
        self.assertIn("Bellevue Open Data: Error: offline", result["errors"])
        self.assertIn("incomplete", result["message"])

    def test_kent_combines_city_building_portal_with_lni_electrical(self):
        result = route.route(
            "123 Main St, Kent, WA",
            "build addition and rewire electrical panel",
        )

        portals = {entry["portal"] for entry in result["portals"]}
        self.assertEqual(
            portals,
            {lookup.SEPARATE_PORTALS["kent"], route.LNI_PORTAL},
        )

    def test_kent_lookup_keeps_city_fallback_and_searches_lni(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(lookup, "search_lni", return_value=([], [])) as search_lni,
        ):
            result = lookup.lookup("123 Main St, Kent, WA")

        search_lni.assert_called_once()
        self.assertEqual(result["separate_portal"]["city"], "Kent")
        self.assertEqual(
            result["separate_portal"]["portal"],
            "https://www.kentwa.gov/pay-and-apply/apply-for-a-permit/check-your-permit-status",
        )
        self.assertIn("WA State L&I (electrical, 2020+)", result["searched"])

    def test_renton_address_resolves_to_energov_and_skips_lni(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(lookup, "_geocode_parcel", return_value="7222000353"),
            patch.object(
                lookup,
                "search_energov",
                return_value=[normalized_permit("R-100", "Renton")],
            ) as search_energov,
            patch.object(lookup, "search_lni") as search_lni,
        ):
            result = lookup.lookup("1817 Morris Ave S, Renton, WA")

        search_energov.assert_called_once_with("renton", "7222000353", exact=False)
        search_lni.assert_not_called()
        self.assertEqual(result["action"], "found")
        self.assertEqual(result["permits"][0]["jurisdiction"], "Renton")

    def test_renton_failed_geocode_returns_direct_portal_fallback(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(lookup, "_geocode_parcel", return_value=None),
            patch.object(lookup, "search_energov") as search_energov,
            patch.object(lookup, "search_lni") as search_lni,
        ):
            result = lookup.lookup("1817 Morris Ave S, Renton, WA")

        search_energov.assert_not_called()
        search_lni.assert_not_called()
        self.assertEqual(result["action"], "none")
        self.assertEqual(result["separate_portal"]["city"], "Renton")
        self.assertEqual(
            result["separate_portal"]["portal"],
            lookup.ENERGOV_PORTALS["renton"]["url"],
        )
        self.assertIn("parcel", result["separate_portal"]["note"])


if __name__ == "__main__":
    unittest.main()
