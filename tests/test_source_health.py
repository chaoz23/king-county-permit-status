import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import gen_scorecard  # noqa: E402
import source_health  # noqa: E402


class ClassifyTests(unittest.TestCase):
    def test_outcome_taxonomy(self):
        cases = [
            ((5, []), "ok"),
            ((0, []), "empty"),
            ((None, []), "unknown"),
            ((0, ["HTTP Error 403: Forbidden"]), "blocked"),
            ((0, ["HTTP Error 404: Not Found"]), "missing"),
            ((0, ["HTTP Error 502: Bad Gateway"]), "server_error"),
            ((0, ["<urlopen error [Errno 8] nodename nor servname provided>"]), "domain_dead"),
            ((0, ["The read operation timed out"]), "unreachable"),
            ((0, ["[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure"]), "unreachable"),
            ((0, ["Search returned too many results"]), "error"),
        ]
        for (records, errors), expected in cases:
            outcome, _ = source_health.classify(records, errors)
            self.assertEqual(outcome, expected, (records, errors))

    def test_records_with_non_fatal_error_is_still_ok(self):
        outcome, detail = source_health.classify(12, ["Bellevue: too many results"])
        self.assertEqual(outcome, "ok")
        self.assertIn("non-fatal", detail)

    def test_every_probe_has_a_label(self):
        for key, (label, fn) in source_health.PROBES.items():
            self.assertTrue(label, key)
            self.assertTrue(callable(fn), key)
        # one MBP probe per jurisdiction
        import lookup
        for name in lookup.JURISDICTIONS.values():
            self.assertIn(f"mbp:{name.lower()}", source_health.PROBES)


class ScorecardHealthTests(unittest.TestCase):
    HEALTH = {"checked_at": "2026-10-08T12:00:00+00:00", "sources": {
        "renton": {"outcome": "ok"},
        "bellevue": {"outcome": "ok"},
        "mbp:bellevue": {"outcome": "empty"},
        "accela:kingco": {"outcome": "blocked"},
        "mbp:kent": {"outcome": "ok"},
    }}

    def test_cell_is_worst_outcome_across_backing_probes(self):
        self.assertEqual(gen_scorecard.health_cell("renton", False, True, self.HEALTH), "✅")
        # Bellevue is backed by Open Data (ok) and MBP (empty) → empty wins
        self.assertEqual(gen_scorecard.health_cell("bellevue", True, True, self.HEALTH), "⚪")
        # unincorporated KC row is backed by MBP-KC + KC Accela; Accela blocked wins
        self.assertEqual(gen_scorecard.health_cell("king county", True, True, self.HEALTH), "⛔")
        # Black Diamond has no live source any more (#47)
        self.assertEqual(gen_scorecard.health_cell("black diamond", False, False, self.HEALTH), "—")

    def test_no_probe_means_dash(self):
        self.assertEqual(gen_scorecard.health_cell("kent", False, False, self.HEALTH), "—")
        self.assertEqual(gen_scorecard.health_cell("renton", False, True, {}), "—")

    def test_unlisted_outcome_renders_down(self):
        h = {"sources": {"renton": {"outcome": "domain_dead"}}}
        self.assertEqual(gen_scorecard.health_cell("renton", False, True, h), "❌")


if __name__ == "__main__":
    unittest.main()
