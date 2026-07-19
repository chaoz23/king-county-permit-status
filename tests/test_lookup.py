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
            patch.object(lookup, "search_shoreline", return_value=([], [])),
            patch.object(lookup, "search_energov_civicaccess", return_value=([], [])),
            patch.object(lookup, "search_accela", return_value=([], [])),
        ):
            result = lookup.lookup("6600750005")

        self.assertEqual(search_permits.call_args_list, [
            call(opener, "token", "20", parcel="6600750005"),
            call(opener, "token", "1", parcel="6600750005"),
        ])
        self.assertEqual(result["searched"], [
            "King County", "Bellevue", "Renton (EnerGov)",
            "Bellevue Open Data", "Shoreline (eTRAKiT)",
            "Redmond (EnerGov Civic Access)",
            "Woodinville (Accela)", "King County (Accela)",
        ])

    def test_formatted_parcel_reaches_sources_as_digits(self):
        opener = object()
        with (
            patch.dict(lookup.JURISDICTIONS, {"20": "King County"}, clear=True),
            patch.object(lookup, "get_session", return_value=(opener, "token")),
            patch.object(lookup, "search_permits", return_value=[]) as search_permits,
            patch.object(lookup, "search_energov", return_value=[]),
            patch.object(lookup, "search_bellevue", return_value=[]),
            patch.object(lookup, "search_shoreline", return_value=([], [])),
            patch.object(lookup, "search_energov_civicaccess", return_value=([], [])),
            patch.object(lookup, "search_accela", return_value=([], [])),
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
            patch.object(lookup, "search_shoreline", return_value=([], [])),
            patch.object(lookup, "search_energov_civicaccess", return_value=([], [])),
            patch.object(lookup, "search_accela", return_value=([], [])),
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
            patch.object(lookup, "search_shoreline", return_value=([], [])),
            patch.object(lookup, "search_energov_civicaccess", return_value=([], [])),
            patch.object(lookup, "search_accela", return_value=([], [])),
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
            patch.object(lookup, "search_shoreline", return_value=([], [])),
            patch.object(lookup, "search_energov_civicaccess", return_value=([], [])),
            patch.object(lookup, "search_accela", return_value=([], [])),
        ):
            result = lookup.lookup("7222000353")

        search_permits.assert_not_called()
        self.assertEqual(result["action"], "refine")
        self.assertIn("Could not connect to MyBuildingPermit", result["message"])
        self.assertEqual(result["searched"], [
            "Renton (EnerGov)", "Bellevue Open Data", "Shoreline (eTRAKiT)",
            "Redmond (EnerGov Civic Access)",
            "Woodinville (Accela)", "King County (Accela)",
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
            patch.object(lookup, "search_shoreline", return_value=([], [])),
            patch.object(lookup, "search_energov_civicaccess", return_value=([], [])),
            patch.object(lookup, "search_accela", return_value=([], [])),
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
            patch.object(lookup, "search_shoreline", return_value=([], [])),
            patch.object(lookup, "search_energov_civicaccess", return_value=([], [])),
            patch.object(lookup, "search_accela", return_value=([], [])),
        ):
            result = lookup.lookup("919 109th Ave NE, Bellevue, WA")

        self.assertNotIn("separate_portal", result)
        self.assertIn("Bellevue", result["searched"])
        self.assertIn(
            "WA State L&I — skipped (Bellevue handles its own electrical)",
            result["searched"],
        )

    def test_failed_seattle_search_keeps_portal_warning(self):
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(
                lookup,
                "search_seattle",
                return_value=([], ["Building: offline"]),
            ),
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


class SeattleSourceTests(unittest.TestCase):
    def test_seattle_permit_number_is_detected(self):
        self.assertEqual(
            lookup.detect_input_type("6145915-CN"),
            ("permit", "6145915-CN"),
        )

    def test_address_query_is_normalized_to_shared_schema(self):
        payload = [{
            "permitnum": "6145915-CN",
            "permittypedesc": "Addition/Alteration",
            "statuscurrent": "Closed",
            "description": "Restaurant tenant improvement",
            "originaladdress1": "600 4TH AVE",
            "originalcity": "SEATTLE",
            "originalstate": "WA",
            "originalzip": "98104",
            "applieddate": "2024-01-02T00:00:00.000",
            "issueddate": "2024-02-03",
            "completeddate": "2025-03-04",
            "expiresdate": "2026-04-05",
            "link": {"url": "https://example.test/6145915-CN"},
        }]
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

        with (
            patch.dict(
                lookup.SEATTLE_PERMIT_DATASETS,
                {"Building": "building-id"},
                clear=True,
            ),
            patch.object(lookup.urllib.request, "urlopen", side_effect=urlopen),
        ):
            permits, errors = lookup.search_seattle(
                "address",
                "600 4th Ave, Seattle, WA",
            )

        self.assertEqual(
            seen["query"]["$where"],
            ["upper(originaladdress1) like '600 4TH AVE%'"],
        )
        self.assertEqual(errors, [])
        self.assertEqual(permits, [{
            "permit_number": "6145915-CN",
            "type": "Addition/Alteration",
            "status": "Closed",
            "description": "Restaurant tenant improvement",
            "address": "600 4TH AVE SEATTLE WA 98104",
            "jurisdiction": "Seattle SDCI",
            "applied_date": "2024-01-02",
            "issued_date": "2024-02-03",
            "finaled_date": "2025-03-04",
            "expires_date": "2026-04-05",
            "portal": "https://example.test/6145915-CN",
        }])

    def test_exact_permit_query_searches_each_dataset(self):
        seen = []

        class Response:
            def read(self):
                return b"[]"

        def urlopen(request, timeout):
            seen.append(request.full_url)
            return Response()

        datasets = {"Building": "building-id", "Trade": "trade-id"}
        with (
            patch.dict(lookup.SEATTLE_PERMIT_DATASETS, datasets, clear=True),
            patch.object(lookup.urllib.request, "urlopen", side_effect=urlopen),
        ):
            permits, errors = lookup.search_seattle("permit", "6145915-cn")

        self.assertEqual(permits, [])
        self.assertEqual(errors, [])
        self.assertEqual(len(seen), 2)
        for url in seen:
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            self.assertEqual(
                query["$where"],
                ["upper(permitnum) = '6145915-CN'"],
            )

    def test_incomplete_address_is_rejected_without_network(self):
        with patch.object(lookup.urllib.request, "urlopen") as urlopen:
            permits, errors = lookup.search_seattle("address", "Seattle, WA")

        urlopen.assert_not_called()
        self.assertEqual(permits, [])
        self.assertEqual(
            errors,
            ["Address requires a house number and street name"],
        )

    def test_electrical_record_uses_string_link_and_mapped_type(self):
        result = lookup._seattle_permit({
            "permitnum": "6001001-EL",
            "permittypemapped": "Electrical",
            "link": "https://example.test/6001001-EL",
        }, "Electrical")

        self.assertEqual(result["type"], "Electrical")
        self.assertEqual(result["portal"], "https://example.test/6001001-EL")

    def test_pagination_fetches_every_page(self):
        rows = [
            {"permitnum": "1-CN"},
            {"permitnum": "2-CN"},
            {"permitnum": "3-CN"},
        ]
        offsets = []

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode()

        def urlopen(request, timeout):
            query = urllib.parse.parse_qs(
                urllib.parse.urlparse(request.full_url).query
            )
            offset = int(query["$offset"][0])
            offsets.append(offset)
            return Response(rows[offset:offset + 2])

        with (
            patch.dict(
                lookup.SEATTLE_PERMIT_DATASETS,
                {"Building": "building-id"},
                clear=True,
            ),
            patch.object(lookup, "SEATTLE_PAGE_SIZE", 2),
            patch.object(lookup.urllib.request, "urlopen", side_effect=urlopen),
        ):
            permits, errors = lookup.search_seattle("permit", "6145915-CN")

        self.assertEqual(errors, [])
        self.assertEqual(offsets, [0, 2])
        self.assertEqual(
            [permit["permit_number"] for permit in permits],
            ["1-CN", "2-CN", "3-CN"],
        )

    def test_one_dataset_failure_preserves_other_results(self):
        class Response:
            def read(self):
                return json.dumps([{"permitnum": "6145915-CN"}]).encode()

        def urlopen(request, timeout):
            if "electrical-id" in request.full_url:
                raise OSError("offline")
            return Response()

        with (
            patch.dict(
                lookup.SEATTLE_PERMIT_DATASETS,
                {"Building": "building-id", "Electrical": "electrical-id"},
                clear=True,
            ),
            patch.object(lookup.urllib.request, "urlopen", side_effect=urlopen),
        ):
            permits, errors = lookup.search_seattle("permit", "6145915-CN")

        self.assertEqual(len(permits), 1)
        self.assertEqual(errors, ["Electrical: offline"])

    def test_api_error_payload_is_reported(self):
        class Response:
            def read(self):
                return json.dumps({"message": "query timed out"}).encode()

        with (
            patch.dict(
                lookup.SEATTLE_PERMIT_DATASETS,
                {"Building": "building-id"},
                clear=True,
            ),
            patch.object(lookup.urllib.request, "urlopen", return_value=Response()),
        ):
            permits, errors = lookup.search_seattle("permit", "6145915-CN")

        self.assertEqual(permits, [])
        self.assertEqual(errors, ["Building: query timed out"])

    def test_lookup_deduplicates_seattle_results_and_surfaces_partial_error(self):
        permit = {
            "permit_number": "6145915-CN",
            "type": "Building",
            "status": "Closed",
            "description": "Tenant improvement",
            "address": "600 4TH AVE SEATTLE WA 98104",
            "jurisdiction": "Seattle SDCI",
            "applied_date": "2024-01-02",
            "issued_date": None,
            "finaled_date": None,
            "expires_date": None,
            "portal": "https://example.test/permit",
        }
        with (
            patch.object(lookup, "get_session", return_value=(object(), "token")),
            patch.object(lookup, "search_permits", return_value=[]),
            patch.object(lookup, "search_energov", return_value=[]),
            patch.object(lookup, "search_bellevue", return_value=[]),
            patch.object(lookup, "search_shoreline", return_value=([], [])),
            patch.object(lookup, "search_energov_civicaccess", return_value=([], [])),
            patch.object(lookup, "search_accela", return_value=([], [])),
            patch.object(
                lookup,
                "search_seattle",
                return_value=([permit, permit], ["Trade: offline"]),
            ),
        ):
            result = lookup.lookup("6145915-CN")

        self.assertEqual(result["action"], "found")
        self.assertEqual(result["permit_count"], 1)
        self.assertIn("Seattle Open Data — Trade: offline", result["errors"])


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

    def test_help_flag_prints_usage_and_exits_zero(self):
        for flag in ("-h", "--help"):
            completed = self.run_cli(flag)
            self.assertEqual(completed.returncode, 0, flag)
            self.assertIn("Usage:", completed.stdout)

    def test_help_flag_is_not_treated_as_query(self):
        completed = self.run_cli("--pipe", "--help")

        self.assertEqual(completed.returncode, 0)
        self.assertIn("Usage:", completed.stdout)
        self.assertNotIn('"action"', completed.stdout)

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


class ShorelineSearchTests(unittest.TestCase):
    SEARCH_PAGE = (
        '<input id="__VIEWSTATE" value="vs">'
        '<input id="__VIEWSTATEGENERATOR" value="vsg">'
    )
    CSV = (
        '﻿"PERMIT NUMBER","APPLIED DATE","ISSUED DATE","Permit Type",'
        '"PARCEL","ADDRESS","OWNER NAME","APPLICANT NAME","CONTRACTOR NAME",'
        '"RECORDID"\r\n'
        '"101014","01/26/2001","02/02/2001","FIRE SYSTEM","1826049268",'
        '"15332 AURORA AVE N","SAFEWAY INC","SAFEWAY","","CONV:1"\r\n'
        '"","","","","","","","","",""\r\n'  # blank row is skipped
    )

    def _opener(self, page, post_body, ctype="text/csv; charset=utf-8"):
        responses = iter([(page, ""), (post_body, ctype)])

        class Headers:
            def __init__(self, ct):
                self.ct = ct

            def get(self, key, default=""):
                return self.ct if key == "Content-Type" else default

        class Response:
            def __init__(self, text, ct):
                self.text = text
                self.headers = Headers(ct)

            def read(self):
                return self.text.encode("utf-8")

        class Opener:
            def open(self, request, timeout):
                text, ct = next(responses)
                return Response(text, ct)

        return Opener()

    def test_address_search_parses_csv_export(self):
        opener = self._opener(self.SEARCH_PAGE, self.CSV)
        with patch.object(
            lookup.urllib.request, "build_opener", return_value=opener
        ):
            permits, errors = lookup.search_shoreline(
                "address", "15332 Aurora Ave N, Shoreline WA")

        self.assertEqual(errors, [])
        self.assertEqual(len(permits), 1)  # header + 1 data + blank(skipped)
        p = permits[0]
        self.assertEqual(p["permit_number"], "101014")
        self.assertEqual(p["type"], "FIRE SYSTEM")
        self.assertEqual(p["applied_date"], "2001-01-26")
        self.assertEqual(p["issued_date"], "2001-02-02")
        self.assertEqual(p["address"], "15332 AURORA AVE N")
        self.assertEqual(p["jurisdiction"], "Shoreline")
        self.assertEqual(p["status"], "")

    def test_no_match_returns_empty_without_error(self):
        # eTRAKiT re-renders the HTML grid (not CSV) when there are no matches.
        opener = self._opener(
            self.SEARCH_PAGE, "<html>no records</html>", ctype="text/html")
        with patch.object(
            lookup.urllib.request, "build_opener", return_value=opener
        ):
            permits, errors = lookup.search_shoreline("permit", "NOPE")

        self.assertEqual((permits, errors), ([], []))

    def test_connection_failure_is_reported(self):
        class Opener:
            def open(self, request, timeout):
                raise OSError("offline")

        with patch.object(
            lookup.urllib.request, "build_opener", return_value=Opener()
        ):
            permits, errors = lookup.search_shoreline("parcel", "1826049268")

        self.assertEqual(permits, [])
        self.assertEqual(errors, ["offline"])

    def test_address_without_house_number_is_rejected_without_network(self):
        with patch.object(lookup.urllib.request, "build_opener") as build_opener:
            permits, errors = lookup.search_shoreline("address", "Aurora Ave")
        build_opener.assert_not_called()
        self.assertEqual(permits, [])
        self.assertIn("house number", errors[0])

    def test_search_axis_and_date_mapping(self):
        self.assertEqual(
            lookup.SHORELINE_SEARCH_BY,
            {"permit": "Permit_Main.PERMIT_NO",
             "parcel": "Permit_Main.SITE_APN",
             "address": "Permit_Main.SITE_ADDR"})
        self.assertEqual(lookup._shoreline_date("03/09/2001"), "2001-03-09")
        self.assertIsNone(lookup._shoreline_date(""))
        self.assertIsNone(lookup._shoreline_date("not a date"))

    def test_lookup_routes_shoreline_address_and_suppresses_fallback_note(self):
        permit = {
            "permit_number": "B1", "type": "BUILDING", "status": "",
            "description": "", "address": "1 MAIN ST",
            "jurisdiction": "Shoreline", "applied_date": "2020-01-01",
            "issued_date": None, "finaled_date": None, "expires_date": None,
            "portal": "https://permits.shorelinewa.gov/eTRAKiT/",
        }
        with (
            patch.object(lookup, "get_session", side_effect=OSError("offline")),
            patch.object(lookup, "search_shoreline",
                         return_value=([permit], [])) as search,
            patch.object(lookup, "search_lni", return_value=([], [])),
        ):
            result = lookup.lookup("15332 Aurora Ave N, Shoreline WA")

        search.assert_called_once()
        self.assertEqual(search.call_args[0][0], "address")
        self.assertNotIn("separate_portal", result)  # Shoreline now covered
        self.assertIn("Shoreline", str(result["searched"]))
        self.assertTrue(
            any(p["jurisdiction"] == "Shoreline" for p in result["permits"]))


class CivicAccessSearchTests(unittest.TestCase):
    CRITERIA = json.dumps({"Result": {
        "Keyword": "", "ExactMatch": False, "SearchModule": 1,
        "FilterModule": 0, "PermitCriteria": {}, "PlanCriteria": {},
    }})

    def _row(self, number="ELEC-2025-08133"):
        return {
            "CaseNumber": number, "CaseType": "Electrical - Multi-Family",
            "CaseStatus": "Finaled", "Description": "panel upgrade",
            "ModuleName": 2, "MainParcel": "0225059115",
            "Address": {"FullAddress": "16080 NE 85TH ST REDMOND WA 98052"},
            "ApplyDate": "2025-09-01T00:00:00", "IssueDate": "2025-10-01T00:00:00",
            "FinalDate": "2025-10-31T00:00:00", "ExpireDate": None,
        }

    def _urlopen(self, search_payload):
        """Return a urlopen stub: GET /criteria, then POST /search."""
        def urlopen(request, timeout):
            url = request.full_url
            body = self.CRITERIA if url.endswith("/criteria") else search_payload

            class Resp:
                def read(self):
                    return body.encode()
            return Resp()
        return urlopen

    def test_permit_search_normalizes_to_schema(self):
        payload = json.dumps({"Result": {
            "EntityResults": [self._row()], "TotalPages": 1}})
        with patch.object(lookup.urllib.request, "urlopen",
                          side_effect=self._urlopen(payload)):
            permits, errors = lookup.search_energov_civicaccess(
                "redmond", "permit", "ELEC-2025-08133")
        self.assertEqual(errors, [])
        self.assertEqual(len(permits), 1)
        p = permits[0]
        self.assertEqual(p["permit_number"], "ELEC-2025-08133")
        self.assertEqual(p["status"], "Finaled")
        self.assertEqual(p["jurisdiction"], "Redmond")
        self.assertEqual(p["applied_date"], "2025-09-01")
        self.assertEqual(p["finaled_date"], "2025-10-31")
        self.assertEqual(p["address"], "16080 NE 85TH ST REDMOND WA 98052")

    def test_placeholder_date_becomes_null(self):
        row = self._row()
        row["ApplyDate"] = "1901-01-01T00:00:00"
        payload = json.dumps({"Result": {"EntityResults": [row], "TotalPages": 1}})
        with patch.object(lookup.urllib.request, "urlopen",
                          side_effect=self._urlopen(payload)):
            permits, _ = lookup.search_energov_civicaccess(
                "redmond", "permit", "X")
        self.assertIsNone(permits[0]["applied_date"])

    def test_unknown_city_is_noop_without_network(self):
        with patch.object(lookup.urllib.request, "urlopen") as urlopen:
            permits, errors = lookup.search_energov_civicaccess(
                "nowhere", "permit", "X")
        urlopen.assert_not_called()
        self.assertEqual((permits, errors), ([], []))

    def test_address_without_house_number_is_rejected_without_network(self):
        with patch.object(lookup.urllib.request, "urlopen") as urlopen:
            permits, errors = lookup.search_energov_civicaccess(
                "redmond", "address", "NE 85th St")
        urlopen.assert_not_called()
        self.assertIn("house number", errors[0])

    def test_civic_access_permit_formats_detect_as_permit(self):
        for number in ("FDM-2600855", "ELEC-2025-08133", "FIRE-2022-02703"):
            self.assertEqual(lookup.detect_input_type(number)[0], "permit", number)


class AccelaSearchTests(unittest.TestCase):
    def _page(self, rows_html):
        return (
            '<table id="ctl00_PlaceHolderMain_CapView_gdvPermitList">'
            '<tr><th>Date</th><th>Record Number</th></tr>'
            + rows_html + '</table>Showing 1-1 of 1')

    # Woodinville layout: Date, Record#, Type, Project, Address, Status
    WOODINVILLE = ('<tr>'
                   '<td>06/18/2026</td><td>ROW26100</td><td>Right of Way Permit</td>'
                   '<td>Comcast ROW</td><td>13206 NE 201ST CT WOODINVILLE WA 98072</td>'
                   '<td>Issued</td></tr>')
    # kingco layout: Date, Record#, Type, Module, empty, Address, dup, Status
    KINGCO = ('<tr>'
              '<td>07/15/2026</td><td>MECH26-1265</td><td>Building Mechanical Residential</td>'
              '<td>Building</td><td></td><td>33230 293RD AVE SE, BLACK DIAMOND, WA 98010</td>'
              '<td>33230 293RD AVE SE dup</td><td>Permit Issued</td></tr>')

    def _opener(self, page):
        class Resp:
            def read(self):
                return page.encode()

        class Opener:
            def open(self, request, timeout):
                return Resp()
        return Opener()

    def test_woodinville_layout_parses(self):
        with patch.object(lookup.urllib.request, "build_opener",
                          return_value=self._opener(self._page(self.WOODINVILLE))):
            permits, errors = lookup.search_accela(
                "WOODINVILLE", "address", "13206 NE 201st Ct", "Woodinville")
        self.assertEqual(errors, [])
        p = permits[0]
        self.assertEqual(p["permit_number"], "ROW26100")
        self.assertEqual(p["type"], "Right of Way Permit")
        self.assertEqual(p["status"], "Issued")
        self.assertEqual(p["applied_date"], "2026-06-18")
        self.assertEqual(p["jurisdiction"], "Woodinville")
        self.assertIn("13206 NE 201ST CT", p["address"])

    def test_kingco_layout_status_is_last_cell(self):
        with patch.object(lookup.urllib.request, "build_opener",
                          return_value=self._opener(self._page(self.KINGCO))):
            permits, _ = lookup.search_accela(
                "kingco", "address", "33230 293rd Ave SE", "Black Diamond")
        p = permits[0]
        self.assertEqual(p["permit_number"], "MECH26-1265")
        self.assertEqual(p["status"], "Permit Issued")   # last cell, not the dup address
        self.assertIn("BLACK DIAMOND", p["address"].upper())
        self.assertEqual(p["jurisdiction"], "Black Diamond")

    def test_address_without_house_number_is_rejected_without_network(self):
        with patch.object(lookup.urllib.request, "build_opener") as bo:
            permits, errors = lookup.search_accela(
                "WOODINVILLE", "address", "NE 201st Ct", "Woodinville")
        bo.assert_not_called()
        self.assertIn("house number", errors[0])

    def test_connection_failure_is_reported(self):
        class Opener:
            def open(self, request, timeout):
                raise OSError("offline")
        with patch.object(lookup.urllib.request, "build_opener",
                          return_value=Opener()):
            permits, errors = lookup.search_accela(
                "WOODINVILLE", "parcel", "1234567890", "Woodinville")
        self.assertEqual(permits, [])
        self.assertEqual(errors, ["offline"])

    def test_agency_config_shapes(self):
        self.assertEqual(lookup.ACCELA_PORTALS["woodinville"], "WOODINVILLE")
        self.assertEqual(lookup.ACCELA_PORTALS["black diamond"], "kingco")


if __name__ == "__main__":
    unittest.main()
