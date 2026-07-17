import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GEN = REPO_ROOT / "scripts" / "gen_scorecard.py"
README = REPO_ROOT / "README.md"


class ScorecardTests(unittest.TestCase):
    def test_readme_scorecard_is_in_sync(self):
        """README coverage scorecard must match routing_data.json.

        If this fails, run: python3 scripts/gen_scorecard.py --write
        """
        result = subprocess.run(
            [sys.executable, str(GEN), "--check"],
            cwd=REPO_ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(
            result.returncode, 0,
            f"README scorecard is stale — regenerate it.\n{result.stderr}",
        )

    def test_scorecard_has_a_row_per_king_county_city(self):
        import json
        data = json.loads((REPO_ROOT / "routing_data.json").read_text())
        block = subprocess.run(
            [sys.executable, str(GEN), "--print"],
            cwd=REPO_ROOT, text=True, capture_output=True, check=False,
        ).stdout
        for city in data["king_county_cities"]:
            # SeaTac is the one display-name override; check a stable token.
            token = "SeaTac" if city.lower() == "seatac" else city.split()[0].capitalize()
            self.assertIn(token, block, f"{city} missing from scorecard")


if __name__ == "__main__":
    unittest.main()
