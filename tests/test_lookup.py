import json
import subprocess
import sys
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import call, patch

import lookup


REPO_ROOT = Path(__file__).resolve().parents[1]


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

    def test_formatted_parcel_reaches_sources_as_digits(self):
        opener = object()
        with (
            patch.dict(lookup.JURISDICTIONS, {"20": "King County"}, clear=True),
            patch.object(lookup, "get_session", return_value=(opener, "token")),
            patch.object(lookup, "search_permits", return_value=[]) as search_permits,
            patch.object(lookup, "search_energov", return_value=[]),
            patch.object(lookup, "search_bellevue", return_value=[]),
        ):
            lookup.lookup("722200-0353")

        search_permits.assert_called_once_with(
            opener, "token", "20", parcel="7222000353"
        )

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

    def test_bellevue_api_error_is_returned(self):
        class Response:
            def read(self):
                return json.dumps({
                    "error": {"message": "service unavailable"},
                }).encode()

        with patch.object(
            lookup.urllib.request,
            "urlopen",
            return_value=Response(),
        ):
            result = lookup.search_bellevue("parcel", "6600750000")

        self.assertEqual(result, "Error: service unavailable")


class LniSourceTests(unittest.TestCase):
    def test_date_windows_cover_every_day_back_to_2020(self):
        windows = lookup.lni_date_windows(datetime(2026, 7, 5))

        self.assertEqual(windows[0], ("06/05/2025", "07/05/2026"))
        self.assertEqual(windows[-1][0], "01/01/2020")
        for newer, older in zip(windows, windows[1:]):
            newer_start = datetime.strptime(newer[0], "%m/%d/%Y")
            older_end = datetime.strptime(older[1], "%m/%d/%Y")
            self.assertEqual(older_end + timedelta(days=1), newer_start)

    def test_connection_failure_is_reported(self):
        class Opener:
            def open(self, request, timeout):
                raise OSError("offline")

        with patch.object(lookup.urllib.request, "build_opener", return_value=Opener()):
            permits, errors = lookup.search_lni("123 Main St", "Auburn")

        self.assertEqual(permits, [])
        self.assertEqual(errors, ["Could not connect to permit search: offline"])

    def test_malformed_window_response_stops_with_error(self):
        responses = iter([
            '<input id="__VIEWSTATE" value="vs">'
            '<input id="__VIEWSTATEGENERATOR" value="vsg">'
            '<input id="__EVENTVALIDATION" value="ev">',
            "<html>unexpected response</html>",
        ])

        class Response:
            def __init__(self, text):
                self.text = text

            def read(self):
                return self.text.encode()

        class Opener:
            def open(self, request, timeout):
                return Response(next(responses))

        with (
            patch.object(lookup.urllib.request, "build_opener", return_value=Opener()),
            patch.object(lookup, "lni_date_windows", return_value=[
                ("06/05/2025", "07/05/2026"),
            ]),
        ):
            permits, errors = lookup.search_lni("123 Main St", "Auburn")

        self.assertEqual(permits, [])
        self.assertEqual(errors, [
            "06/05/2025–07/05/2026: response missing required state tokens",
        ])

    def test_failed_window_retries_with_fresh_session(self):
        initial_html = (
            '<input id="__VIEWSTATE" value="vs">'
            '<input id="__VIEWSTATEGENERATOR" value="vsg">'
            '<input id="__EVENTVALIDATION" value="ev">'
        )
        result_html = (
            '<input id="__VIEWSTATE" value="vs2">'
            '<input id="__EVENTVALIDATION" value="ev2">'
        )

        class Response:
            def __init__(self, text):
                self.text = text

            def read(self):
                return self.text.encode()

        class ExpiredOpener:
            calls = 0

            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1:
                    return Response(initial_html)
                raise OSError("session expired")

        class FreshOpener:
            calls = 0

            def open(self, request, timeout):
                self.calls += 1
                return Response(initial_html if self.calls == 1 else result_html)

        openers = iter([ExpiredOpener(), FreshOpener()])
        with (
            patch.object(
                lookup.urllib.request,
                "build_opener",
                side_effect=lambda *args: next(openers),
            ) as build_opener,
            patch.object(lookup, "lni_date_windows", return_value=[
                ("06/05/2025", "07/05/2026"),
            ]),
        ):
            permits, errors = lookup.search_lni("123 Main St", "Auburn")

        self.assertEqual(permits, [])
        self.assertEqual(errors, [])
        self.assertEqual(build_opener.call_count, 2)

    def test_lookup_marks_total_lni_failure_incomplete(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(lookup, "search_lni", return_value=([], ["2020 window failed"])),
        ):
            result = lookup.lookup("123 Main St, Auburn, WA")

        self.assertEqual(result["action"], "refine")
        self.assertEqual(result["errors"], [
            "WA State L&I: 2020 window failed",
        ])
        self.assertIn("WA State L&I (electrical, 2020+)", result["searched"])

    def test_lookup_reports_partial_lni_failure_with_results(self):
        permit = {
            "permit_number": "LNI-1",
            "type": "WA State L&I Electrical",
            "status": "Active",
            "description": "Service",
            "address": "123 Main St",
            "jurisdiction": "WA State L&I (KING)",
            "applied_date": "2026-01-01",
            "issued_date": None,
            "finaled_date": None,
            "expires_date": None,
        }
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(lookup, "search_lni", return_value=([permit], ["old window failed"])),
        ):
            result = lookup.lookup("123 Main St, Auburn, WA")

        self.assertEqual(result["action"], "found")
        self.assertEqual(result["permit_count"], 1)
        self.assertEqual(result["errors"], [
            "WA State L&I: old window failed",
        ])
        self.assertIn("Some source searches were incomplete", result["message"])


class InputContractTests(unittest.TestCase):
    def test_blank_query_is_rejected_without_network(self):
        with patch.object(lookup, "get_session") as get_session:
            result = lookup.lookup("   ")

        get_session.assert_not_called()
        self.assertEqual(result["action"], "reject")
        self.assertEqual(result["permits"], [])

    def test_formatted_parcel_is_normalized(self):
        self.assertEqual(
            lookup.detect_input_type("722200-0353"),
            ("parcel", "7222000353"),
        )

    def test_city_name_does_not_match_inside_street_word(self):
        self.assertIsNone(lookup.detect_city("123 Kenton Road"))

    def test_city_name_matches_as_address_component(self):
        self.assertEqual(
            lookup.detect_city("123 Main St, Maple Valley, WA"),
            "maple valley",
        )


class LookupCliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "lookup.py"), *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_missing_query_exits_two(self):
        completed = self.run_cli()

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Usage:", completed.stdout)

    def test_blank_pipe_query_returns_reject_json(self):
        completed = self.run_cli("--pipe", "")

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stdout)["action"], "reject")

    def test_schema_output_matches_tool_json(self):
        completed = self.run_cli("--schema")

        self.assertEqual(completed.returncode, 0)
        with open(REPO_ROOT / "tool.json") as f:
            expected = json.load(f)
        self.assertEqual(json.loads(completed.stdout), expected)


class SourceUtilityTests(unittest.TestCase):
    def test_dotnet_date_parser_handles_valid_and_invalid_values(self):
        self.assertEqual(lookup.parse_date("/Date(1782975600000)/"), "2026-07-02")
        self.assertIsNone(lookup.parse_date("not a date"))
        self.assertIsNone(lookup.parse_date(None))

    def test_lni_date_parser_handles_valid_and_invalid_values(self):
        self.assertEqual(lookup.parse_lni_date("7/2/2026"), "2026-07-02")
        self.assertIsNone(lookup.parse_lni_date("&nbsp;"))
        self.assertIsNone(lookup.parse_lni_date("bogus"))

    def test_parse_address_splits_house_and_street(self):
        self.assertEqual(
            lookup.parse_address("1817 Morris Ave S, Renton, WA"),
            ("1817", "Morris Ave S"),
        )
        self.assertEqual(lookup.parse_address("Main Street"), ("", "Main Street"))

    def test_geocoder_skips_low_scores_and_normalizes_pin(self):
        payload = {
            "candidates": [
                {"score": 70, "attributes": {"PIN": "1111111111"}},
                {"score": 95, "attributes": {"PIN": "722200-0353"}},
            ],
        }

        class Response:
            def read(self):
                return json.dumps(payload).encode()

        with patch.object(
            lookup.urllib.request,
            "urlopen",
            return_value=Response(),
        ):
            parcel = lookup._geocode_parcel("1817 Morris Ave S, Renton")

        self.assertEqual(parcel, "7222000353")

    def test_mbp_error_payload_is_preserved(self):
        class Response:
            def read(self):
                return json.dumps({
                    "success": False,
                    "ErrorMessage": "Too many results",
                }).encode()

        class Opener:
            def open(self, request, timeout):
                return Response()

        result = lookup.search_permits(Opener(), "token", "20")

        self.assertEqual(result, "Too many results")

    def test_mbp_transport_failure_is_returned(self):
        class Opener:
            def open(self, request, timeout):
                raise OSError("offline")

        result = lookup.search_permits(Opener(), "token", "20")

        self.assertEqual(result, "Error: offline")

    def test_energov_transport_failure_returns_empty_list(self):
        class Opener:
            def open(self, request, timeout):
                raise OSError("offline")

        with patch.object(
            lookup.urllib.request,
            "build_opener",
            return_value=Opener(),
        ):
            result = lookup.search_energov("renton", "B25000947", exact=True)

        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
