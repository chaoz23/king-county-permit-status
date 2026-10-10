import json
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import sys
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
import server  # noqa: E402


FAKE = {
    "action": "found", "permit_count": 1, "searched": ["Renton (EnerGov)"],
    "permits": [{"permit_number": "B25000947", "type": "Building", "status": "Issued",
                 "description": "", "address": "1817 Morris Ave S", "jurisdiction": "Renton",
                 "applied_date": "2025-01-01", "issued_date": None, "finaled_date": None,
                 "expires_date": None, "portal": None, "record_url": None, "is_open": True}],
    "input": "1817 Morris Ave S, Renton WA", "message": "Found 1 permit(s).",
    "trust_level": "live", "fetched_at": "2026-10-09T00:00:00+00:00",
    "parcel_id": "king:7222000353", "cite_as": "King County Permit Status",
    "jurisdiction": {"city": "Renton", "basis": "city-limits", "county": "king", "unincorporated": False},
}


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.calls = []

        def fake_lookup(q):
            cls.calls.append(q)
            return dict(FAKE, input=q) if q.strip() else {"action": "reject", "message": "blank"}

        cls._patch = patch.object(server, "run_lookup", side_effect=fake_lookup)
        cls._patch.start()
        # urlopen caches the first opener it builds for the whole process; other
        # test modules patch build_opener, so make sure we talk to a real one.
        urllib.request.install_opener(urllib.request.build_opener())
        server.CACHE = server.TTLCache(300)
        server.LIMITER = server.RateLimiter(per_key=5, global_limit=100)
        cls.httpd = server.serve("127.0.0.1", 0)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls._patch.stop()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.headers, r.read()

    def rpc(self, body):
        req = urllib.request.Request(
            self.base + "/mcp", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)

    def test_healthz_and_docs(self):
        status, _, body = self.get("/healthz")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        status, _, body = self.get("/openapi.json")
        spec = json.loads(body)
        self.assertEqual(spec["openapi"], "3.1.0")
        self.assertIn("Permit", spec["components"]["schemas"])
        self.assertIn("LookupResult", spec["components"]["schemas"])
        self.assertIn("is_open", spec["components"]["schemas"]["Permit"]["properties"])
        status, headers, body = self.get("/llms.txt")
        self.assertTrue(headers["Content-Type"].startswith("text/plain"))
        self.assertIn("/mcp", body.decode())
        status, _, body = self.get("/tool.json")
        self.assertEqual(json.loads(body)["name"], "king_county_permit_status")

    def test_rest_lookup_and_cache(self):
        n = len(self.calls)
        status, _, body = self.get("/api/lookup?q=1817+Morris+Ave+S,+Renton+WA")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual(d["permit_count"], 1)
        self.assertFalse(d["cached"])
        status, _, body = self.get("/api/lookup?q=1817+morris+ave+s,+renton+wa")   # same, normalized
        self.assertTrue(json.loads(body)["cached"])
        self.assertEqual(len(self.calls), n + 1)

    def test_rest_requires_q(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/lookup")
        self.assertEqual(cm.exception.code, 400)

    def test_mcp_handshake_list_call(self):
        status, r = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "0"}}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", r["result"]["capabilities"])
        status, r = self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(status, 202)
        status, r = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool = r["result"]["tools"][0]
        self.assertEqual(tool["name"], "king_county_permit_status")
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertIn("query", tool["inputSchema"]["properties"])
        status, r = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "king_county_permit_status",
                                         "arguments": {"query": "7222000353"}}})
        self.assertFalse(r["result"]["isError"])
        self.assertEqual(r["result"]["structuredContent"]["permit_count"], 1)
        self.assertEqual(json.loads(r["result"]["content"][0]["text"])["parcel_id"], "king:7222000353")

    def test_mcp_errors(self):
        _, r = self.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(r["error"]["code"], -32602)
        _, r = self.rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                         "params": {"name": "king_county_permit_status", "arguments": {}}})
        self.assertTrue(r["result"]["isError"])
        _, r = self.rpc({"jsonrpc": "2.0", "id": 6, "method": "resources/list"})
        self.assertEqual(r["error"]["code"], -32601)
        req = urllib.request.Request(self.base + "/mcp", data=b"{not json",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 400)
        self.assertEqual(json.loads(cm.exception.read())["error"]["code"], -32700)

    def test_mcp_get_is_405(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/mcp")
        self.assertEqual(cm.exception.code, 405)

    def test_rate_limit(self):
        server.LIMITER = server.RateLimiter(per_key=2, global_limit=100)
        try:
            self.get("/api/lookup?q=a1+x+st")
            self.get("/api/lookup?q=a2+x+st")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get("/api/lookup?q=a3+x+st")
            self.assertEqual(cm.exception.code, 429)
            self.assertEqual(cm.exception.headers["Retry-After"], "60")
            # docs are never rate limited
            self.assertEqual(self.get("/healthz")[0], 200)
        finally:
            server.LIMITER = server.RateLimiter(per_key=5, global_limit=100)

    def test_cors_preflight(self):
        req = urllib.request.Request(self.base + "/mcp", method="OPTIONS")
        with urllib.request.urlopen(req, timeout=5) as r:
            self.assertEqual(r.status, 204)
            self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")


class DocumentTests(unittest.TestCase):
    def test_openapi_result_schema_mirrors_tool_schema(self):
        spec = server.openapi()
        props = spec["components"]["schemas"]["LookupResult"]["properties"]
        for k in ("action", "trust_level", "next_step", "jurisdiction", "cite_as", "cached"):
            self.assertIn(k, props)

    def test_server_json_points_at_mcp(self):
        d = json.loads((REPO_ROOT / "server.json").read_text())
        self.assertEqual(d["remotes"][0]["type"], "streamable-http")
        self.assertTrue(d["remotes"][0]["url"].endswith("/mcp"))


if __name__ == "__main__":
    unittest.main()


class ShapingParamTests(unittest.TestCase):
    def test_parse_shaping_from_rest_and_mcp(self):
        self.assertEqual(server.parse_shaping({}), (50, None, None))
        self.assertEqual(server.parse_shaping({"limit": "5", "since": "2020-01-01", "types": "elec, mech"}),
                         (5, "2020-01-01", ["elec", "mech"]))
        self.assertEqual(server.parse_shaping({"limit": "all"}), (None, None, None))
        self.assertEqual(server.parse_shaping({"limit": "x", "types": ["Electrical"]}), (50, None, ["Electrical"]))
        self.assertEqual(server.parse_shaping({"limit": -3}), (0, None, None))

    def test_cached_full_result_shaped_per_request(self):
        many = [{"permit_number": f"P{i}", "type": "Electrical", "status": "Issued", "is_open": True,
                 "applied_date": f"20{i:02d}-01-01", "jurisdiction": "Renton"} for i in range(10, 70)]
        calls = []
        def fake(q):
            calls.append(q); return {"action": "found", "permits": many, "message": "m", "permit_count": 60}
        server.CACHE = server.TTLCache(300)
        with patch.object(server, "run_lookup", side_effect=fake):
            a = server.cached_lookup("addr", 5, None, None)
            b = server.cached_lookup("addr", None, "2050-01-01", None)
        self.assertEqual(len(calls), 1)                     # one fetch, two shapes
        self.assertEqual((len(a["permits"]), a["summary"]["total"], a["cached"]), (5, 60, False))
        self.assertTrue(b["cached"])
        self.assertTrue(all(p["applied_date"] >= "2050-01-01" for p in b["permits"]))
        self.assertEqual(b["summary"]["matched"], len(b["permits"]))
