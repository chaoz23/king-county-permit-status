import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import route


REPO_ROOT = Path(__file__).resolve().parents[1]
ROUTING_DATA = {
    "last_verified": "2026-07-01",
    "cities_on_mbp": ["bellevue"],
    "cities_own_electrical": ["renton"],
    "city_portals": {
        "bellevue": "https://development.bellevuewa.gov/",
        "renton": "https://permitting.rentonwa.gov/",
        "kent": "https://epermit.kentwa.gov/",
    },
}


class PermitDetectionTests(unittest.TestCase):
    def test_multi_trade_work_returns_each_permit(self):
        permits = route.detect_permits("rewiring kitchen and installing heat pump")

        self.assertEqual(
            {permit["type"] for permit in permits},
            {"electrical", "building", "mechanical"},
        )

    def test_roofing_and_demolition_collapse_to_one_building_permit(self):
        permits = route.detect_permits("demolish porch and reroof house")

        self.assertEqual([permit["type"] for permit in permits], ["building"])

    def test_unknown_work_fails_gracefully(self):
        self.assertEqual(route.detect_permits("paint the bedroom"), [{
            "type": "unknown",
            "description": "Could not determine permit type from description",
            "matched_keyword": None,
        }])


class RouteUserStoryTests(unittest.TestCase):
    def test_city_name_does_not_match_inside_street_word(self):
        self.assertIsNone(route.detect_city("123 Kenton Road", ROUTING_DATA))

    def test_unincorporated_electrical_routes_to_lni(self):
        result = route.route_permit("electrical", None, ROUTING_DATA)

        self.assertEqual(result["handled_by"], "WA State L&I")
        self.assertEqual(result["portal"], route.LNI_PORTAL)

    def test_city_owned_electrical_routes_to_city(self):
        result = route.route_permit("electrical", "renton", ROUTING_DATA)

        self.assertEqual(result["handled_by"], "Renton (city handles electrical)")
        self.assertEqual(result["portal"], "https://permitting.rentonwa.gov/")

    def test_septic_always_routes_to_public_health(self):
        result = route.route_permit("septic", "bellevue", ROUTING_DATA)

        self.assertEqual(result["handled_by"], "King County Public Health")
        self.assertEqual(result["portal"], route.KC_SEPTIC)

    def test_grading_in_city_mentions_city_and_county(self):
        result = route.route_permit("grading", "bellevue", ROUTING_DATA)

        self.assertEqual(result["handled_by"], "Bellevue and/or King County")
        self.assertEqual(result["portal"], "https://development.bellevuewa.gov/")

    def test_mbp_trades_share_one_portal_group(self):
        with (
            patch.object(route, "load_routing_data", return_value=ROUTING_DATA),
            patch.object(route, "check_staleness", return_value=None),
        ):
            result = route.route(
                "123 Main St, Bellevue, WA",
                "kitchen remodel and heat pump",
            )

        self.assertEqual(result["action"], "routed")
        self.assertEqual(result["location"], "Bellevue")
        self.assertEqual(len(result["portals"]), 1)
        self.assertEqual(
            set(result["portals"][0]["permit_types"]),
            {"building", "mechanical"},
        )

    def test_blank_address_is_rejected(self):
        with patch.object(route, "load_routing_data", return_value=ROUTING_DATA):
            result = route.route("   ", "rewire kitchen")

        self.assertEqual(result["action"], "reject")

    def test_old_routing_data_emits_staleness_warning(self):
        old_data = {**ROUTING_DATA, "last_verified": "2000-01-01"}

        warning = route.check_staleness(old_data)

        self.assertIsNotNone(warning)
        self.assertGreater(warning["age_days"], route.STALE_DAYS)
        self.assertIn("refresh.py --apply", warning["action"])

    def test_recent_routing_data_has_no_staleness_warning(self):
        self.assertIsNone(route.check_staleness(ROUTING_DATA))


class RouteCliTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(REPO_ROOT / "route.py"), *args],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_missing_address_exits_two(self):
        completed = self.run_cli()

        self.assertEqual(completed.returncode, 2)
        self.assertIn("Usage:", completed.stdout)

    def test_blank_pipe_address_returns_reject_json(self):
        completed = self.run_cli("--pipe", "", "rewire kitchen")

        self.assertEqual(completed.returncode, 2)
        self.assertEqual(json.loads(completed.stdout)["action"], "reject")

    def test_pipe_success_returns_compact_json(self):
        completed = self.run_cli(
            "--pipe",
            "1817 Morris Ave S, Renton",
            "install heat pump",
        )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(json.loads(completed.stdout)["action"], "routed")
        self.assertNotIn("\n ", completed.stdout)
