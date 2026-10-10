#!/usr/bin/env python3
"""Scorecard conformance QC: prove every README scorecard row live.

For each city in qc_addresses.json the expected behaviour is *derived* from
the same classification that renders the README (scripts/gen_scorecard.py),
then checked against a real lookup:

  jurisdiction  the address must resolve (polygon) to the city it stands for
  🟢 Live       ≥1 permit from the city's own live source (not the Assessor)
                and the source's label in `searched`
  🟠 Indexed    the Assessor index was searched and `next_step` names the
                city's portal; Assessor rows are reported (0 is a WARN, since
                a public building may have nothing reported for valuation)
  Electrical    ➖ L&I  → "WA State L&I" searched
                ✅       → L&I skipped because the city self-runs electrical
                ⚠️ gap  → next_step.covers_electrical is true
  Health        not checked here (that's source_health.py)

Usage:
  python3 scripts/qc_scorecard.py              # all cities (~4 min, live)
  python3 scripts/qc_scorecard.py kent seatac  # a subset
  python3 scripts/qc_scorecard.py --json out.json
Exit 1 if any city FAILs. WARNs don't fail.
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import lookup           # noqa: E402
import gen_scorecard    # noqa: E402

ADDRESSES = os.path.join(ROOT, "qc_addresses.json")
# Live-source labels that prove a city's own feed answered. MBP cities answer
# under their plain name in `searched`; dedicated ones under their label.
DEDICATED_LABEL = {
    "renton": "Renton (EnerGov", "seattle": "Seattle Open Data", "bellevue": "Bellevue Open Data",
    "shoreline": "Shoreline (eTRAKiT)", "redmond": "Redmond (EnerGov Civic Access)",
    "woodinville": "Woodinville (Accela)", "normandy park": "Normandy Park (SmartGov)",
    "carnation": "Carnation (SmartGov)", "seatac": "SeaTac (LAMA)",
    "king county": "King County (Accela)",
}


def is_index_row(p: dict) -> bool:
    return "blue.kingcounty.com" in (p.get("portal") or "")


def check_city(city: str, address: str, row: dict) -> dict:
    """Run one lookup and compare with the scorecard row. Returns a report."""
    t0 = time.time()
    r = lookup.lookup(address)
    dt = round(time.time() - t0, 1)
    fails, warns = [], []
    name = row["city"]
    key = city.lower()
    searched = " | ".join(r.get("searched", []))
    j = (r.get("jurisdiction") or {})
    permits = r.get("permits", [])

    # 1. the address really is in this city (or unincorporated KC)
    if key == "king county":
        if not j.get("unincorporated"):
            fails.append(f"address did not resolve as unincorporated KC (got {j})")
    elif (j.get("city") or "").lower() != key:
        fails.append(f"address resolved to {j.get('city')!r} via {j.get('basis')}, not {name}")

    own = [p for p in permits if not is_index_row(p)
           and (p.get("jurisdiction") or "").lower().split()[:1] == [key.split()[0]]]
    idx = [p for p in permits if is_index_row(p)]

    # 2. status row
    if row["status"].endswith("Live"):
        label = DEDICATED_LABEL.get(key, name)          # MBP cities: plain city name
        if label not in searched:
            fails.append(f"live source {label!r} not in searched: [{searched}]")
        if not own:
            fails.append(f"no live records from {name}'s own source (permits={len(permits)}, index={len(idx)})")
    else:  # Indexed
        if j.get("county") and j.get("county") != "king":
            warns.append(f"{j['county'].title()}-side parcel: the KC Assessor index cannot cover it")
        elif lookup.ASSESSOR_LABEL not in searched:
            fails.append("Assessor index not searched")
        ns = r.get("next_step") or {}
        if not ns:
            fails.append("indexed city has no next_step (portal pointer)")
        elif (ns.get("city") or "").lower() != key:
            fails.append(f"next_step points at {ns.get('city')!r}")
        if not idx and j.get("county") == "king":
            warns.append("0 Assessor rows for this address (try a residential/commercial address)")
        if own:
            warns.append(f"{len(own)} non-index records from {name} — is a live source available?")

    # 3. electrical column
    e = row["electrical"]
    if e == "lni" and "WA State L&I (electrical" not in searched:
        fails.append("L&I not searched although electrical column says L&I")
    if e == "yes" and "L&I — skipped" not in searched:
        fails.append("city self-runs electrical but L&I was not skipped")
    if e == "gap":
        ns = r.get("next_step") or {}
        if not ns.get("covers_electrical"):
            fails.append("electrical gap not flagged in next_step.covers_electrical")

    return {"city": name, "address": address, "status": row["status"], "electrical": e,
            "action": r.get("action"), "permits": len(permits), "own": len(own), "index": len(idx),
            "trust": r.get("trust_level"), "elapsed_s": dt,
            "outcome": "FAIL" if fails else ("WARN" if warns else "PASS"),
            "fails": fails, "warns": warns}


def main() -> int:
    argv = sys.argv[1:]
    out_path = None
    if "--json" in argv:
        i = argv.index("--json")
        out_path = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    args = [a.lower() for a in argv if not a.startswith("--")]
    addresses = json.load(open(ADDRESSES))
    addresses.pop("_comment", None)
    d, kc, mbp, own_elec = gen_scorecard.load()
    rows = {c.lower(): gen_scorecard.classify(c, mbp, own_elec) for c in kc}
    rows["king county"] = {"city": "King County (unincorp.)", "status": "🟢 Live",
                           "source": "MBP-KC + KC Accela", "electrical": "lni"}
    wanted = [c for c in addresses if not args or c in args]
    print(f"Scorecard QC: {len(wanted)} cities (live)\n" + "=" * 78)
    reports = []
    for city in wanted:
        rep = check_city(city, addresses[city], rows[city])
        reports.append(rep)
        print(f"  {rep['outcome']:4} {rep['city']:24} {rep['status']:11} elec={rep['electrical']:4} "
              f"permits={rep['permits']:3} own={rep['own']:3} idx={rep['index']:3} {rep['elapsed_s']:5.1f}s")
        for f in rep["fails"]:
            print(f"         ✗ {f}")
        for w in rep["warns"]:
            print(f"         ~ {w}")
    tally = {k: sum(1 for r in reports if r["outcome"] == k) for k in ("PASS", "WARN", "FAIL")}
    print("=" * 78 + f"\n  {tally}")
    if out_path:
        with open(out_path, "w") as f:
            json.dump({"tally": tally, "cities": reports}, f, indent=1)
        print(f"  wrote {out_path}")
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
