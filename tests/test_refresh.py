import io
import sys
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import mock_open, patch

import refresh


class Response:
    def __init__(self, text="", url="https://example.test/"):
        self.text = text
        self.url = url

    def read(self):
        return self.text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RefreshParserTests(unittest.TestCase):
    def test_lni_city_parser_filters_navigation_noise(self):
        html = """
        <ul>
          <li>Bellevue</li><li>Seattle</li><li>Permit information</li>
          <li>Grant County Power</li>
        </ul>
        <a>Bellingham</a><a>Click here</a>
        """
        with patch.object(
            refresh.urllib.request,
            "urlopen",
            return_value=Response(html),
        ):
            cities = refresh.fetch_lni_cities()

        self.assertEqual(cities, {"bellevue", "seattle", "bellingham"})

    def test_mbp_parser_excludes_placeholder_and_county(self):
        html = """
        <select id="ddlJurisdictions">
          <option value="0">--Select One--</option>
          <option value="1">Bellevue</option>
          <option value="20">King County</option>
          <option value="25">Federal Way</option>
        </select>
        """
        with patch.object(
            refresh.urllib.request,
            "urlopen",
            return_value=Response(html),
        ):
            cities = refresh.fetch_mbp_jurisdictions()

        self.assertEqual(cities, {"bellevue", "federal way"})

    def test_fetch_failure_returns_empty_set_with_warning(self):
        output = io.StringIO()
        with (
            patch.object(
                refresh.urllib.request,
                "urlopen",
                side_effect=OSError("offline"),
            ),
            redirect_stdout(output),
        ):
            cities = refresh.fetch_mbp_jurisdictions()

        self.assertEqual(cities, set())
        self.assertIn("WARN", output.getvalue())


class PortalCheckTests(unittest.TestCase):
    def test_redirect_is_reported_as_healthy(self):
        with patch.object(
            refresh.urllib.request,
            "urlopen",
            return_value=Response(url="https://example.test/new"),
        ):
            ok, note = refresh.check_url("https://example.test/old")

        self.assertTrue(ok)
        self.assertIn("redirects", note)

    def test_server_error_is_unhealthy(self):
        error = urllib.error.HTTPError(
            "https://example.test", 503, "Unavailable", None, None
        )
        with patch.object(refresh.urllib.request, "urlopen", side_effect=error):
            ok, note = refresh.check_url("https://example.test")

        self.assertFalse(ok)
        self.assertEqual(note, "HTTP 503")

    def test_not_found_is_unhealthy(self):
        error = urllib.error.HTTPError(
            "https://example.test", 404, "Not Found", None, None
        )
        with patch.object(refresh.urllib.request, "urlopen", side_effect=error):
            ok, note = refresh.check_url("https://example.test")

        self.assertFalse(ok)
        self.assertEqual(note, "HTTP 404")

    def test_forbidden_response_still_proves_portal_exists(self):
        error = urllib.error.HTTPError(
            "https://example.test", 403, "Forbidden", None, None
        )
        with patch.object(refresh.urllib.request, "urlopen", side_effect=error):
            ok, note = refresh.check_url("https://example.test")

        self.assertTrue(ok)
        self.assertEqual(note, "HTTP 403")


class RefreshApplySafetyTests(unittest.TestCase):
    def test_apply_does_not_stamp_failed_source_checks_as_verified(self):
        data = {
            "last_verified": "2026-06-23",
            "cities_own_electrical": ["bellevue"],
            "cities_on_mbp": ["bellevue"],
            "city_portals": {"bellevue": "https://example.test"},
        }
        output = io.StringIO()
        file_open = mock_open()
        with (
            patch.object(sys, "argv", ["refresh.py", "--apply"]),
            patch.object(refresh, "load_data", return_value=data),
            patch.object(refresh, "fetch_lni_cities", return_value=set()),
            patch.object(refresh, "fetch_mbp_jurisdictions", return_value=set()),
            patch.object(refresh, "check_url", return_value=(True, "OK")),
            patch("builtins.open", file_open),
            redirect_stdout(output),
        ):
            refresh.main()

        file_open.assert_not_called()
        self.assertIn("not updated", output.getvalue().lower())

    def test_apply_writes_verified_source_changes(self):
        data = {
            "last_verified": "2026-06-23",
            "cities_own_electrical": ["bellevue"],
            "cities_on_mbp": ["bellevue"],
            "city_portals": {"bellevue": "https://example.test"},
        }
        output = io.StringIO()
        file_open = mock_open()
        with (
            patch.object(sys, "argv", ["refresh.py", "--apply"]),
            patch.object(refresh, "load_data", return_value=data),
            patch.object(
                refresh,
                "fetch_lni_cities",
                return_value={"bellevue", "renton"},
            ),
            patch.object(
                refresh,
                "fetch_mbp_jurisdictions",
                return_value={"bellevue"},
            ),
            patch.object(refresh, "check_url", return_value=(True, "OK")),
            patch("builtins.open", file_open),
            redirect_stdout(output),
        ):
            refresh.main()

        file_open.assert_called_once_with(refresh.DATA_PATH, "w")
        self.assertEqual(data["cities_own_electrical"], ["bellevue", "renton"])
        self.assertNotEqual(data["last_verified"], "2026-06-23")
        self.assertIn("Updated", output.getvalue())
