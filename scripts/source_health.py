#!/usr/bin/env python3
"""Probe every live permit source with a known-good query and record its health.

refresh.py checks that routing *data* is still right (which city is on which
system). This checks that each *adapter* still answers — the thing that
actually drifts week to week. Each source gets one cheap real query that is
known to return records; the outcome is classified the same way for every
vendor so the scorecard can show it:

  ok            records came back
  empty         the source answered but returned no rows for a known-good query
                (parser drift or the record set moved — look at it)
  blocked       HTTP 403 — a bot filter refused us; usually works in a browser
  missing       HTTP 404 — the endpoint moved
  server_error  HTTP 5xx
  unreachable   timeout / TLS failure / connection refused
  domain_dead   DNS failure
  error         any other adapter error
  unknown       could not be checked (e.g. no session)

Usage:
  python3 scripts/source_health.py            # probe + print
  python3 scripts/source_health.py --write    # also write source_health.json
  python3 scripts/source_health.py --strict   # exit 1 if anything is not ok/empty

Weekly run: .github/workflows/source-health.yml (commits the JSON + README).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import lookup   # noqa: E402
import refresh  # noqa: E402

HEALTH_PATH = os.path.join(ROOT, "source_health.json")

OUTCOMES = ("ok", "empty", "blocked", "missing", "server_error",
            "unreachable", "domain_dead", "error", "unknown")


def classify(records: int | None, errors: list[str]) -> tuple[str, str]:
    """Map an adapter result to (outcome, detail). Adapters swallow transport
    failures into error strings, so the HTTP/transport class is parsed from
    the text rather than from an exception type."""
    text = "; ".join(e for e in errors if e)
    if text:
        t = text.lower()
        if re.search(r"\b403\b|forbidden", t):
            return "blocked", text[:160]
        if re.search(r"\b404\b|not found", t):
            return "missing", text[:160]
        if re.search(r"\b5\d\d\b|internal server error|bad gateway|service unavailable", t):
            return "server_error", text[:160]
        if re.search(r"nodename nor servname|name or service not known|getaddrinfo|no address associated", t):
            return "domain_dead", text[:160]
        if re.search(r"timed out|timeout|ssl|handshake|connection refused|unreachable|connection reset|remote end closed", t):
            return "unreachable", text[:160]
        if records:
            return "ok", f"{records} records; non-fatal: {text[:120]}"
        return "error", text[:160]
    if records is None:
        return "unknown", "could not be checked"
    if records > 0:
        return "ok", f"{records} records"
    return "empty", "0 records for a known-good query"


# --- probes --------------------------------------------------------------
# Each returns (records, errors). Queries are the same real ones qc_smoke.py
# regresses against, so a drop to `empty` is a real signal.

def _pair(result) -> tuple[int | None, list[str], list[dict]]:
    """Normalize the three adapter return shapes: list | str | (list, errors)
    → (count, errors, rows). Rows feed the status-vocabulary census (#52)."""
    if isinstance(result, tuple):
        rows, errs = result
        return len(rows), list(errs), list(rows)
    if isinstance(result, str):
        return None, [result], []
    return len(result), [], list(result)


def probe_mbp(city: str):
    alive = refresh.mbp_backend_alive(city)
    if alive is None:
        return None, [], []
    return (1 if alive else 0), [], []


PROBES = {
    "renton": ("Renton (EnerGov)",
               lambda: _pair(lookup.search_energov("renton", "7222000353"))),
    "seattle": ("Seattle (SDCI Open Data)",
                lambda: _pair(lookup.search_seattle("address", "400 Broad St, Seattle WA 98109"))),
    "bellevue": ("Bellevue (Open Data)",
                 lambda: _pair(lookup.search_bellevue("address", "919 109th Ave NE, Bellevue WA"))),
    "shoreline": ("Shoreline (eTRAKiT)",
                  lambda: _pair(lookup.search_shoreline("address", "15332 Aurora Ave N, Shoreline WA"))),
    "redmond": ("Redmond (EnerGov Civic Access)",
                lambda: _pair(lookup.search_energov_civicaccess("redmond", "address", "16080 NE 85th St, Redmond WA 98052"))),
    "accela:WOODINVILLE": ("Woodinville (Accela)",
                           lambda: _pair(lookup.search_accela("WOODINVILLE", "address", "13206 NE 201st Ct, Woodinville WA", "Woodinville"))),
    # Vashon (unincorporated KC) — Black Diamond addresses return 0 rows from
    # the kingco agency (see the issue filed from the first sweep), so probe
    # the agency with an address it demonstrably serves.
    "accela:kingco": ("King County (Accela)",
                      lambda: _pair(lookup.search_accela("kingco", "address", "17630 Vashon Hwy SW, Vashon WA", "King County"))),
    # City halls — a house number is required and these have permit history.
    "smartgov:normandy park": ("Normandy Park (SmartGov)",
                               lambda: _pair(lookup.search_smartgov("normandy park", "address", "801 SW 174th St, Normandy Park WA"))),
    "smartgov:carnation": ("Carnation (SmartGov)",
                           lambda: _pair(lookup.search_smartgov("carnation", "address", "4621 Tolt Ave, Carnation WA"))),
    "lama:seatac": ("SeaTac (LAMA)",
                    lambda: _pair(lookup.search_lama("seatac", "address", "18740 International Blvd, SeaTac WA"))),
    "assessor": ("King County Assessor (permit index)",
                 lambda: _pair(lookup.search_assessor("7759800010"))),   # Kent hotel: 7 rows
    "lni": ("WA State L&I (electrical)",
            lambda: _pair(lookup.search_lni("15332 Aurora Ave N", "shoreline"))),
}
for _jid, _name in lookup.JURISDICTIONS.items():
    PROBES[f"mbp:{_name.lower()}"] = (f"{_name} (MyBuildingPermit)",
                                      (lambda c=_name: probe_mbp(c)))


def run_probe(key: str) -> tuple[str, dict]:
    label, fn = PROBES[key]
    t0 = time.time()
    try:
        records, errors, rows = fn()
    except Exception as exc:  # a probe must never sink the sweep
        records, errors, rows = None, [f"{type(exc).__name__}: {exc}"], []
    outcome, detail = classify(records, errors)
    # Status-vocabulary census (#52): every distinct status string this source
    # returned, with how is_open classifies it, so unknown words are visible.
    statuses = {}
    for r in rows:
        st = (r.get("status") or "").strip()
        if st:
            statuses.setdefault(st, {"n": 0, "is_open": lookup.is_open_status(st)})["n"] += 1
    return key, {"label": label, "outcome": outcome, "records": records,
                 "detail": detail, "elapsed_ms": int((time.time() - t0) * 1000),
                 "statuses": dict(sorted(statuses.items(), key=lambda kv: -kv[1]["n"]))}


def sweep() -> dict:
    keys = list(PROBES)
    # MBP probes each open a session; keep them on one worker so we don't
    # hammer the portal. Everything else fans out.
    mbp_keys = [k for k in keys if k.startswith("mbp:")]
    other = [k for k in keys if not k.startswith("mbp:")]
    results: dict[str, dict] = {}

    def mbp_chain():
        return [run_probe(k) for k in mbp_keys]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(run_probe, k) for k in other]
        chain = pool.submit(mbp_chain)
        for f in futs:
            k, r = f.result()
            results[k] = r
        for k, r in chain.result():
            results[k] = r

    previous = load_previous()
    for k, r in results.items():
        prev = (previous.get("sources") or {}).get(k, {}).get("outcome")
        if prev and prev != r["outcome"]:
            r["changed_from"] = prev
    summary = {o: sum(1 for r in results.values() if r["outcome"] == o) for o in OUTCOMES}
    vocab = {}
    for k, r in results.items():
        for st, info in r.get("statuses", {}).items():
            vocab.setdefault(st, {"is_open": info["is_open"], "sources": []})["sources"].append(k)
    return {
        "status_vocabulary": dict(sorted(vocab.items())),
        "checked_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "summary": {k: v for k, v in summary.items() if v},
        "sources": {k: results[k] for k in keys},
    }


def load_previous() -> dict:
    try:
        with open(HEALTH_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


ICON = {"ok": "✅", "empty": "⚪", "blocked": "⛔", "missing": "❌", "server_error": "❌",
        "unreachable": "❌", "domain_dead": "❌", "error": "❌", "unknown": "❔"}


def main() -> int:
    report = sweep()
    print(f"Source health @ {report['checked_at']}  {report['summary']}")
    for k, r in report["sources"].items():
        change = f"  (was {r['changed_from']})" if r.get("changed_from") else ""
        print(f"  {ICON[r['outcome']]} {r['label']:34} {r['outcome']:12} "
              f"{r['elapsed_ms']:>6}ms  {r['detail'][:70]}{change}")
    if "--qc" in sys.argv:
        # Fold the weekly end-to-end QC run (scripts/qc_smoke.py --json) in, so
        # routing-level regressions sit next to adapter liveness (#55).
        qc_path = sys.argv[sys.argv.index("--qc") + 1]
        try:
            with open(qc_path) as f:
                qc = json.load(f)
            report["qc"] = {k: qc.get(k) for k in ("passed", "failed", "errored", "slow")}
            report["qc"]["failing"] = [c["case"] for c in qc.get("cases", [])
                                       if c.get("outcome") != "pass"]
            print(f"QC smoke: {report['qc']['passed']} passed · {report['qc']['failed']} failed · "
                  f"{report['qc']['errored']} errored  failing={report['qc']['failing']}")
        except (OSError, ValueError) as exc:
            report["qc"] = {"error": f"no QC summary: {exc}"}
            print(f"QC smoke: no summary ({exc})")
    if "--scorecard-qc" in sys.argv:
        # Per-city scorecard conformance (scripts/qc_scorecard.py --json): does
        # every README row still behave as claimed?
        path = sys.argv[sys.argv.index("--scorecard-qc") + 1]
        try:
            with open(path) as f:
                sc = json.load(f)
            report["scorecard_qc"] = {
                "tally": sc.get("tally"),
                "failing": [c["city"] for c in sc.get("cities", []) if c.get("outcome") == "FAIL"],
                "warning": [c["city"] for c in sc.get("cities", []) if c.get("outcome") == "WARN"],
            }
            print(f"Scorecard QC: {report['scorecard_qc']['tally']} failing={report['scorecard_qc']['failing']}")
        except (OSError, ValueError) as exc:
            report["scorecard_qc"] = {"error": f"no scorecard QC summary: {exc}"}
    if "--write" in sys.argv:
        with open(HEALTH_PATH, "w") as f:
            json.dump(report, f, indent=1, ensure_ascii=False)
            f.write("\n")
        print(f"wrote {HEALTH_PATH}")
    if "--strict" in sys.argv:
        bad = [k for k, r in report["sources"].items() if r["outcome"] not in ("ok", "empty")]
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
