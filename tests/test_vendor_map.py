import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT)); sys.path.insert(0, str(REPO_ROOT / "scripts"))
import gen_vendor_map  # noqa: E402
import server          # noqa: E402


class VendorMapTests(unittest.TestCase):
    def test_committed_outputs_are_in_sync(self):
        r = subprocess.run([sys.executable, str(REPO_ROOT / "scripts" / "gen_vendor_map.py"), "--check"],
                           cwd=REPO_ROOT, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, f"vendor map stale — run --write\n{r.stderr}")

    def test_one_row_per_city_plus_county_with_valid_status(self):
        m = gen_vendor_map.build()
        self.assertEqual(len(m["rows"]), 40)
        self.assertEqual(sum(m["tally"].values()), 39)
        self.assertTrue(all(r["status"] in ("live", "blocked", "indexed") for r in m["rows"]))
        blocked = [r for r in m["rows"] if r["status"] == "blocked"]
        self.assertTrue(all(r["blocked_reason"] and r["tracking_issue"] for r in blocked))
        kent = next(r for r in m["rows"] if r["key"] == "kent")
        self.assertEqual(kent["status"], "blocked"); self.assertIn("XLSX", kent["mechanism"])
        renton = next(r for r in m["rows"] if r["key"] == "renton")
        self.assertTrue(renton["searched_by_this_tool"]); self.assertEqual(renton["electrical_authority"], "city")
        self.assertEqual(set(renton["routing_by_type"]), set(gen_vendor_map.lookup.PERMIT_TYPES))

    def test_markdown_and_html_render(self):
        m = gen_vendor_map.build()
        md = gen_vendor_map.render_md(m)
        self.assertIn("| Kent | blocked |", md)
        html = server.vendor_map_html(m)
        self.assertIn("<table>", html); self.assertIn("Renton", html); self.assertIn("/vendor-map.json", html)


if __name__ == "__main__":
    unittest.main()
