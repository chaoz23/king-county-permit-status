#!/usr/bin/env python3
"""Hosted, read-only HTTP front for lookup.py — MCP + REST + OpenAPI (#42).

Zero dependencies (stdlib only), like the rest of the repo. One process serves:

  POST /mcp            MCP Streamable HTTP (JSON-RPC 2.0): initialize,
                       tools/list, tools/call, ping. Stateless; no SSE stream.
  GET  /api/lookup?q=  the same lookup as `lookup.py --pipe`, as JSON
  GET  /openapi.json   OpenAPI 3.1 with typed component schemas
  GET  /llms.txt       plain-text orientation for agents
  GET  /tool.json      the CLI tool definition
  GET  /healthz        liveness + version + source-health summary

Every lookup is live against the municipal sources; results are cached for
KCPS_CACHE_TTL seconds (default 15 min) so repeated agent calls don't re-hit
portals. Per-IP and global rate limits protect the upstream portals, which is
the real scarce resource here.

Run:  python3 server.py            (KCPS_PORT=8080 by default)
Env:  KCPS_PORT, KCPS_HOST, KCPS_CACHE_TTL, KCPS_RATE_PER_MIN,
      KCPS_GLOBAL_RATE_PER_MIN, KCPS_TRUST_PROXY=1 (behind Caddy/nginx)
"""
from __future__ import annotations

import collections
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import lookup  # noqa: E402

VERSION = "1.0.0"
SERVER_NAME = "king-county-permit-status"
TOOL_NAME = lookup.TOOL_SCHEMA["name"]
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_BODY = 64 * 1024

CACHE_TTL = int(os.environ.get("KCPS_CACHE_TTL", "900"))
RATE_PER_MIN = int(os.environ.get("KCPS_RATE_PER_MIN", "30"))
GLOBAL_RATE_PER_MIN = int(os.environ.get("KCPS_GLOBAL_RATE_PER_MIN", "300"))
TRUST_PROXY = os.environ.get("KCPS_TRUST_PROXY", "") == "1"

# The function the server calls; tests patch this.
run_lookup = lookup.lookup


# --- cache + rate limit -----------------------------------------------------

class TTLCache:
    def __init__(self, ttl: int):
        self.ttl, self._d, self._lock = ttl, {}, threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._d.get(key)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1], int(time.time() - hit[0])
            self._d.pop(key, None)
            return None, None

    def put(self, key, value):
        with self._lock:
            if len(self._d) > 5000:
                self._d.clear()
            self._d[key] = (time.time(), value)


class RateLimiter:
    """Sliding one-minute window per key, plus a global window."""
    def __init__(self, per_key: int, global_limit: int):
        self.per_key, self.global_limit = per_key, global_limit
        self._hits = collections.defaultdict(collections.deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            for k in (key, "*"):
                dq = self._hits[k]
                while dq and now - dq[0] > 60:
                    dq.popleft()
            if len(self._hits[key]) >= self.per_key or len(self._hits["*"]) >= self.global_limit:
                return False
            self._hits[key].append(now)
            self._hits["*"].append(now)
            return True


CACHE = TTLCache(CACHE_TTL)
LIMITER = RateLimiter(RATE_PER_MIN, GLOBAL_RATE_PER_MIN)


def cached_lookup(query: str, limit=lookup.DEFAULT_LIMIT, since=None, types=None) -> dict:
    """The full result is cached once per query; shaping (limit/since/types +
    summary) is applied per request, so different views share one fetch."""
    key = " ".join(query.split()).lower()
    hit, age = CACHE.get(key)
    if hit is None:
        hit = run_lookup(query)
        age = 0
        if hit.get("action") != "reject":
            CACHE.put(key, hit)
        cached = False
    else:
        cached = True
    shaped = lookup.shape_result(hit, limit=limit, since=since, types=types)
    return {**shaped, "cached": cached, "cache_age_s": age}


def parse_shaping(args: dict) -> tuple:
    """(limit, since, types) from MCP arguments or REST query params; bad
    values fall back to defaults rather than failing the call."""
    limit = args.get("limit", lookup.DEFAULT_LIMIT)
    try:
        limit = None if limit in (None, "", "all") else max(0, int(limit))
    except (TypeError, ValueError):
        limit = lookup.DEFAULT_LIMIT
    since = args.get("since") or None
    if since and not isinstance(since, str):
        since = None
    types = args.get("types")
    if isinstance(types, str):
        types = [t.strip() for t in types.split(",") if t.strip()]
    elif not isinstance(types, list):
        types = None
    return limit, since, (types or None)


# --- documents --------------------------------------------------------------

def _permit_schema() -> dict:
    return lookup.TOOL_SCHEMA["output_schema"]["properties"]["permits"]["items"]


def openapi() -> dict:
    out = lookup.TOOL_SCHEMA["output_schema"]
    result_props = {k: v for k, v in out["properties"].items() if k != "permits"}
    result_props["permits"] = {"type": "array", "items": {"$ref": "#/components/schemas/Permit"}}
    result_props["cached"] = {"type": "boolean", "description": "The underlying lookup came from the server's short TTL cache (shaping is always fresh)"}
    result_props["cache_age_s"] = {"type": "integer"}
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "King County Permit Status",
            "version": VERSION,
            "description": ("Live building-permit history and status for any King County, WA "
                            "address, parcel or permit number, across 20 city systems plus the "
                            "county and WA L&I. Read-only. Source: "
                            "github.com/chaoz23/king-county-permit-status (MIT)."),
        },
        "servers": [{"url": "/"}],
        "paths": {
            "/api/lookup": {"get": {
                "operationId": "lookupPermits",
                "summary": "Permit history + status for an address, parcel or permit number",
                "parameters": [
                    {"name": "q", "in": "query", "required": True,
                     "schema": lookup.TOOL_SCHEMA["input_schema"]["properties"]["query"]},
                    {"name": "limit", "in": "query", "required": False,
                     "schema": {"type": "integer", "minimum": 0, "default": lookup.DEFAULT_LIMIT},
                     "description": "Max permits returned, newest first; summary always covers the whole matched set"},
                    {"name": "since", "in": "query", "required": False,
                     "schema": {"type": "string", "format": "date"},
                     "description": "Only permits applied/issued on or after YYYY-MM-DD"},
                    {"name": "types", "in": "query", "required": False,
                     "schema": {"type": "string"},
                     "description": "Comma-separated case-insensitive substrings matched against permit type"},
                ],
                "responses": {
                    "200": {"description": "Lookup result", "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/LookupResult"}}}},
                    "400": {"description": "Missing or blank q"},
                    "429": {"description": "Rate limited (per-IP or global)"},
                },
            }},
            "/mcp": {"post": {
                "operationId": "mcp",
                "summary": "MCP Streamable HTTP endpoint (JSON-RPC 2.0); exposes the same lookup as a tool",
                "responses": {"200": {"description": "JSON-RPC response"}},
            }},
            "/healthz": {"get": {"operationId": "health", "summary": "Liveness + last source-health sweep",
                                 "responses": {"200": {"description": "ok"}}}},
        },
        "components": {"schemas": {
            "Permit": _permit_schema(),
            "LookupResult": {"type": "object", "properties": result_props,
                             "required": list(out.get("required", []))},
        }},
    }


def llms_txt(base: str) -> str:
    return f"""# King County Permit Status

Live building-permit history and status for King County, WA — any address,
10-digit parcel number, or permit number. Read-only, no auth, MIT.

- MCP (Streamable HTTP): POST {base}/mcp — tool `{TOOL_NAME}` with one string arg `query`
- REST: GET {base}/api/lookup?q=<address|parcel|permit>
- OpenAPI: {base}/openapi.json · CLI tool definition: {base}/tool.json
- Health: {base}/healthz

What you get: `permits[]` (permit_number, type, status, is_open, dates, address,
jurisdiction, record_url), `jurisdiction` (derived from the city-limits polygon, not the
mailing city), `trust_level`, `next_step` when a city needs a manual portal visit,
`parcel_id` (county-namespaced), `cite_as`.

Coverage: 20 of 39 King County cities live (MyBuildingPermit, Tyler EnerGov/Civic
Access, Accela, eTRAKiT, SmartGov, LAMA, Seattle + Bellevue open data), plus
unincorporated King County and WA L&I electrical. Per-city scorecard and limits:
https://github.com/chaoz23/king-county-permit-status#readme

Please cite results using the `cite_as` field. Rate limit: {RATE_PER_MIN}/min per client.
"""


def health() -> dict:
    out = {"ok": True, "service": SERVER_NAME, "version": VERSION,
           "cache_ttl_s": CACHE_TTL, "rate_per_min": RATE_PER_MIN}
    try:
        with open(os.path.join(ROOT, "source_health.json")) as f:
            sh = json.load(f)
        out["source_health"] = {"checked_at": sh.get("checked_at"), "summary": sh.get("summary")}
    except (OSError, ValueError):
        out["source_health"] = None
    return out


# --- MCP (JSON-RPC 2.0) ------------------------------------------------------

def mcp_tool_definition() -> dict:
    s = lookup.TOOL_SCHEMA
    return {
        "name": TOOL_NAME,
        "title": "King County permit status",
        "description": s["description"],
        "inputSchema": s["input_schema"],
        "outputSchema": s["output_schema"],
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": True},
    }


def _rpc_error(id_, code, message):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def handle_rpc(msg: dict) -> dict | None:
    """One JSON-RPC message → response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc_error(None, -32600, "Invalid Request")
    method, id_, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method and "id" not in msg:          # notification
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": VERSION},
            "instructions": ("Call king_county_permit_status with an address, parcel or permit "
                             "number. Results are live; cite them with the cite_as field. "
                             "If next_step is present, follow it instead of guessing a portal."),
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": [mcp_tool_definition()]}}
    if method == "tools/call":
        name = params.get("name")
        if name != TOOL_NAME:
            return _rpc_error(id_, -32602, f"Unknown tool: {name}")
        query = (params.get("arguments") or {}).get("query")
        if not isinstance(query, str) or not query.strip():
            return {"jsonrpc": "2.0", "id": id_, "result": {
                "isError": True,
                "content": [{"type": "text", "text": "query (string) is required"}]}}
        limit, since, types = parse_shaping(params.get("arguments") or {})
        result = cached_lookup(query, limit, since, types)
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result,
        }}
    return _rpc_error(id_, -32601, f"Method not found: {method}")


# --- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{VERSION}"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):   # no query strings in logs (addresses are PII-ish)
        sys.stderr.write("%s %s %s\n" % (self.address_string(), self.command,
                                         self.path.split("?")[0]))

    def _client(self) -> str:
        if TRUST_PROXY:
            fwd = self.headers.get("X-Forwarded-For")
            if fwd:
                return fwd.split(",")[0].strip()
        return self.client_address[0]

    def _send(self, status: int, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else (
            body.encode() if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode())
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _base(self) -> str:
        proto = self.headers.get("X-Forwarded-Proto", "http") if TRUST_PROXY else "http"
        return f"{proto}://{self.headers.get('Host', 'localhost')}"

    def do_OPTIONS(self):
        self._send(204, b"", extra={
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, Accept, Mcp-Session-Id, MCP-Protocol-Version"})

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = url.path.rstrip("/") or "/"
        if path in ("/", "/healthz"):
            return self._send(200, health())
        if path == "/openapi.json":
            return self._send(200, openapi())
        if path == "/tool.json":
            return self._send(200, lookup.TOOL_SCHEMA)
        if path == "/llms.txt":
            return self._send(200, llms_txt(self._base()), "text/plain; charset=utf-8")
        if path == "/mcp":
            return self._send(405, {"error": "SSE stream not offered; POST JSON-RPC to /mcp"},
                              extra={"Allow": "POST, OPTIONS"})
        if path == "/api/lookup":
            qs = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
            q = qs.get("q", "")
            if not q.strip():
                return self._send(400, {"error": "q is required"})
            if not LIMITER.allow(self._client()):
                return self._send(429, {"error": "rate limited"}, extra={"Retry-After": "60"})
            limit, since, types = parse_shaping(qs)
            result = cached_lookup(q, limit, since, types)
            return self._send(200, result)
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if path != "/mcp":
            return self._send(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(413, {"error": "body too large"})
        raw = self.rfile.read(length) if length else b""
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, _rpc_error(None, -32700, "Parse error"))
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            if not LIMITER.allow(self._client()):
                return self._send(429, _rpc_error(msg.get("id"), -32000, "rate limited"),
                                  extra={"Retry-After": "60"})
        if isinstance(msg, list):                      # batch
            responses = [r for r in (handle_rpc(m) for m in msg) if r is not None]
            return self._send(200, responses) if responses else self._send(202, b"")
        response = handle_rpc(msg)
        if response is None:                           # notification
            return self._send(202, b"")
        return self._send(200, response)


def serve(host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    host = host or os.environ.get("KCPS_HOST", "0.0.0.0")
    port = int(port if port is not None else os.environ.get("KCPS_PORT", "8080"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


if __name__ == "__main__":
    httpd = serve()
    print(f"{SERVER_NAME} {VERSION} listening on {httpd.server_address[0]}:{httpd.server_address[1]} "
          f"(cache {CACHE_TTL}s, {RATE_PER_MIN}/min per client)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
