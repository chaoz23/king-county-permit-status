import json
import unittest
import urllib.parse
from unittest.mock import call, patch

import lookup


def energov_permit(number="B25000947"):
    return {
        "permit_number": number,
        "type": "Building Permit",
        "status": "Issued",
        "description": "Garage",
        "address": "1817 Morris Ave S Renton WA 98055",
        "jurisdiction": "Renton",
        "applied_date": "2025-01-01",
        "issued_date": "2025-01-02",
        "finaled_date": None,
        "expires_date": None,
        "portal": "https://permitting.rentonwa.gov",
    }


class ParcelRoutingTests(unittest.TestCase):
    def test_parcel_queries_every_mbp_jurisdiction(self):
        jurisdictions = {"20": "King County", "1": "Bellevue"}
        opener = object()
        with (
            patch.dict(lookup.JURISDICTIONS, jurisdictions, clear=True),
            patch.object(lookup, "get_session", return_value=(opener, "token")),
            patch.object(lookup, "search_permits", return_value=[]) as search_permits,
            patch.object(lookup, "search_energov", return_value=[]),
            patch.object(lookup, "search_bellevue", return_value=[]),
        ):
            result = lookup.lookup("6600750005")

        self.assertEqual(search_permits.call_args_list, [
            call(opener, "token", "20", parcel="6600750005"),
            call(opener, "token", "1", parcel="6600750005"),
        ])
        self.assertEqual(result["searched"], [
            "King County", "Bellevue", "Renton (EnerGov)",
            "Bellevue Open Data",
        ])

    def test_parcel_queries_every_energov_portal(self):
        portals = {
            "renton": {"url": "https://renton.example"},
            "bellevue": {"url": "https://bellevue.example"},
        }
        with (
            patch.dict(lookup.ENERGOV_PORTALS, portals, clear=True),
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(lookup, "search_energov", return_value=[energov_permit()]) as search_energov,
            patch.object(lookup, "search_bellevue", return_value=[]),
        ):
            result = lookup.lookup("7222000353")

        self.assertEqual(search_energov.call_args_list, [
            call("renton", "7222000353", exact=False),
            call("bellevue", "7222000353", exact=False),
        ])
        self.assertEqual(result["action"], "found")
        self.assertEqual(result["permit_count"], 1)
        self.assertEqual(result["permits"][0]["jurisdiction"], "Renton")
        self.assertIn("Renton (EnerGov)", result["searched"])

    def test_mbp_failure_does_not_block_energov(self):
        with (
            patch.object(lookup, "get_session", side_effect=OSError("offline")),
            patch.object(lookup, "search_permits") as search_permits,
            patch.object(lookup, "search_energov", return_value=[energov_permit()]),
            patch.object(lookup, "search_bellevue", return_value=[]),
        ):
            result = lookup.lookup("7222000353")

        search_permits.assert_not_called()
        self.assertEqual(result["action"], "found")
        self.assertEqual(result["permit_count"], 1)

    def test_total_source_failure_requests_retry(self):
        with (
            patch.object(lookup, "get_session", side_effect=OSError("offline")),
            patch.object(lookup, "search_permits") as search_permits,
            patch.object(lookup, "search_energov", return_value=[]),
            patch.object(lookup, "search_bellevue", return_value=[]),
        ):
            result = lookup.lookup("7222000353")

        search_permits.assert_not_called()
        self.assertEqual(result["action"], "refine")
        self.assertIn("Could not connect to MyBuildingPermit", result["message"])
        self.assertEqual(result["searched"], [
            "Renton (EnerGov)", "Bellevue Open Data",
        ])

    def test_duplicate_permits_from_sources_are_collapsed(self):
        mbp_permit = {
            "PermitNumber": "B25000947",
            "PermitType": "Building Permit",
            "PermitStatus": "Issued",
            "PermitDescription": "Garage",
            "Address": "1817 Morris Ave S",
            "Jurisdiction": "King County",
            "AppliedDate": None,
            "IssuedDate": None,
            "FinaledDate": None,
            "ApplicationExpDate": None,
        }
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[mbp_permit]),
            patch.object(lookup, "search_energov", return_value=[energov_permit()]),
            patch.object(lookup, "search_bellevue", return_value=[]),
        ):
            result = lookup.lookup("7222000353")

        self.assertEqual(result["permit_count"], 1)
        self.assertEqual(result["permits"][0]["permit_number"], "B25000947")


class CityRoutingTests(unittest.TestCase):
    def test_mbp_city_does_not_emit_separate_electrical_warning(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(lookup, "search_bellevue", return_value=[]),
        ):
            result = lookup.lookup("919 109th Ave NE, Bellevue, WA")

        self.assertNotIn("separate_portal", result)
        self.assertIn("Bellevue", result["searched"])
        self.assertIn(
            "WA State L&I — skipped (Bellevue handles its own electrical)",
            result["searched"],
        )

    def test_unsupported_electrical_city_keeps_portal_warning(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
        ):
            result = lookup.lookup("600 4th Ave, Seattle, WA")

        self.assertEqual(result["separate_portal"]["city"], "Seattle")
        self.assertTrue(result["separate_portal"]["electrical"])


class BellevueSourceTests(unittest.TestCase):
    def test_bellevue_permit_number_is_detected(self):
        self.assertEqual(
            lookup.detect_input_type("23-127651-LP"),
            ("permit", "23-127651-LP"),
        )

    def test_bellevue_arcgis_result_is_normalized(self):
        payload = {
            "features": [{
                "attributes": {
                    "PERMITNUMBER": "26 115098 BF",
                    "PERMITTYPE": "BF",
                    "PERMITTYPEDESCRIPTION": "Electrical Permit",
                    "SITEADDRESS": "919 109th Ave NE",
                    "CITY": "Bellevue",
                    "STATE": "WA",
                    "ZIPCODE": "98004",
                    "PERMITSTATUS": "Issued",
                    "PROJECTNAME": "Tenant work",
                    "PROJECTDESCRIPTION": "Two altered branch circuits",
                    "APPLIEDDATE": 1782975600000,
                    "ISSUEDDATE": 1782975600000,
                    "FINALEDDATE": None,
                    "EXPIREDATE": None,
                    "MBPSTATUSSITE": "https://example.test/permit",
                },
            }],
            "exceededTransferLimit": False,
        }
        seen = {}

        class Response:
            def read(self):
                return json.dumps(payload).encode()

        def urlopen(request, timeout):
            seen["query"] = urllib.parse.parse_qs(
                urllib.parse.urlparse(request.full_url).query
            )
            self.assertEqual(timeout, 30)
            return Response()

        with patch.object(lookup.urllib.request, "urlopen", side_effect=urlopen):
            result = lookup.search_bellevue("parcel", "6600750000")

        self.assertEqual(seen["query"]["where"], ["PARCELNUMBER = '6600750000'"])
        self.assertEqual(result, [{
            "permit_number": "26 115098 BF",
            "type": "Electrical Permit",
            "status": "Issued",
            "description": "Two altered branch circuits",
            "address": "919 109th Ave NE Bellevue WA 98004",
            "jurisdiction": "Bellevue",
            "applied_date": "2026-07-02",
            "issued_date": "2026-07-02",
            "finaled_date": None,
            "expires_date": None,
            "portal": "https://example.test/permit",
        }])


if __name__ == "__main__":
    unittest.main()
