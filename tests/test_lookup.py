import unittest
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
        ):
            result = lookup.lookup("7222000353")

        search_permits.assert_not_called()
        self.assertEqual(result["action"], "refine")
        self.assertIn("Could not connect to MyBuildingPermit", result["message"])
        self.assertEqual(result["searched"], ["Renton (EnerGov)"])

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
        ):
            result = lookup.lookup("7222000353")

        self.assertEqual(result["permit_count"], 1)
        self.assertEqual(result["permits"][0]["permit_number"], "B25000947")


if __name__ == "__main__":
    unittest.main()
