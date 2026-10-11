#!/usr/bin/env python3
"""Generate the King County permit-vendor map (#27): which permitting system
each of the 39 cities (+ unincorporated King County) runs, how it can be
queried, whether this tool does, and where each permit type lives.

Single source of truth is the adapter config in lookup.py plus
routing_data.json — the same tables the lookup searches — so the map cannot
drift from the tool. Two outputs, both fully generated:

    vendor_map.json      machine-readable (served at /vendor-map.json)
    docs/vendor-map.md   human-readable (rendered at /vendor-map)

    python3 scripts/gen_vendor_map.py --check    # fail if either is stale
    python3 scripts/gen_vendor_map.py --write    # rewrite both
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)
import lookup           # noqa: E402
import gen_scorecard    # noqa: E402

JSON_PATH = os.path.join(ROOT, "vendor_map.json")
MD_PATH = os.path.join(ROOT, "docs", "vendor-map.md")
REPO = "https://github.com/chaoz23/king-county-permit-status"

# How each vendor class is actually queried (the crystallized recipes), and
# why the blocked ones are blocked. vendor_class comes from lookup.city_system.
MECHANISM = {
    "mbp": ("form POST (parcel-first; applied-date windows beat the result cap); People search for contractors",
            "live"),
    "mbp+arcgis": ("MyBuildingPermit form POST + Bellevue ArcGIS FeatureServer REST (daily snapshot, 1998+)", "live"),
    "mbp+accela": ("MyBuildingPermit KC jurisdiction + KC Accela global search (older history, enforcement)", "live"),
    "energov": ("Tyler EnerGov JSON API — criteria template + tenant headers, search by parcel", "live"),
    "civicaccess": ("Tyler EnerGov Civic Access JSON API — tenant headers, FilterModule=2, 100/page", "live"),
    "accela": ("Accela Citizen Access GlobalSearchResults GET → HTML grid; pager replayed via postback", "live"),
    "etrakit": ("eTRAKiT VIEWSTATE POST → CSV export; detail page per permit for status (time-boxed)", "live"),
    "smartgov": ("SmartGov form POST with time-based validator token; SearchPage paginator", "live"),
    "lama": ("LAMA (ASP.NET WebForms) VIEWSTATE POST, filter+sort+200/page in one request", "live"),
    "socrata": ("Seattle Open Data Socrata REST (4 datasets); parcel queries via the Assessor site address", "live"),
    "blocked": (None, "blocked"),
    "none": ("No public permit portal; history via the King County Assessor parcel index", "indexed"),
}
# Blocked-vendor specifics, by the vendor label in MANUAL_PORTALS.
BLOCKED = {
    "PermitTrax Citizens Connect": ("Blazor Server over SignalR — the search never issues an HTTP request an adapter could replay", f"{REPO}/issues/18"),
    "OpenGov PLC (Turnstile-gated; browser only)": ("Clean JSON:API, but every call needs Cloudflare Turnstile proof-of-possession headers (403 without)", f"{REPO}/issues/21"),
    "ASP.gov (results require login)": ("Permit results are behind an account login", f"{REPO}/issues/23"),
    "No public search — monthly XLSX permit logs + applicant SSO": ("No search at all: monthly XLSX/PDF logs on kentwa.gov plus an applicant-only SSO portal", f"{REPO}/issues/24"),
}
RECIPES_DOC = f"{REPO}/blob/main/docs/EPIC-coverage-and-hardening.md"


def build() -> dict:
    data, kc, mbp, own_elec = gen_scorecard.load()
    health = gen_scorecard.load_health()
    rows = []
    for city in sorted(kc + ["king county"], key=str.lower):
        key = city.lower()
        unincorp = key == "king county"
        sysd = lookup.city_system(key, unincorp)
        mech, status = MECHANISM[sysd["vendor_class"]]
        blocked = BLOCKED.get(sysd["system"]) if sysd["vendor_class"] == "blocked" else None
        routing = lookup.permit_type_routing(key, unincorp)
        depth = gen_scorecard.depth_counts(key, health)
        rows.append({
            "city": "King County (unincorporated)" if unincorp else gen_scorecard.display_name(city),
            "key": key,
            "status": status,                      # live | blocked | indexed
            "system": sysd["system"],
            "vendor_class": sysd["vendor_class"],
            "portal": sysd["portal"],
            "mechanism": mech or blocked[0],
            "searched_by_this_tool": sysd["live"],
            "blocked_reason": blocked[0] if blocked else None,
            "tracking_issue": blocked[1] if blocked else None,
            "electrical_authority": ("city" if key in own_elec and not unincorp else "WA L&I"),
            "electrical_searched": routing["electrical"]["live"],
            "history_index": "King County Assessor parcel index",
            "records_at_qc_address": {"live": depth[0], "index": depth[1]} if depth else None,
            "routing_by_type": {t: {"system": v["system"], "live": v["live"]} for t, v in routing.items()},
        })
    tally = {s: sum(1 for r in rows if r["status"] == s and r["key"] != "king county") for s in ("live", "blocked", "indexed")}
    return {
        "title": "King County permit-vendor map",
        "generated_from": ["lookup.py adapter config", "routing_data.json", "source_health.json (record depth)"],
        "routing_data_verified": data.get("last_verified"),
        "cities": len(kc),
        "tally": tally,
        "vendors": sorted({r["system"] for r in rows}),
        "rows": rows,
        "recipes": RECIPES_DOC,
        "tool": REPO,
    }


def render_md(m: dict) -> str:
    out = [f"# {m['title']}", "",
           f"Generated by `scripts/gen_vendor_map.py` from the lookup tool's own adapter config — "
           f"the same tables it searches — so this cannot drift from what the tool does. "
           f"{m['cities']} cities: **{m['tally']['live']} live**, {m['tally']['blocked']} blocked, "
           f"{m['tally']['indexed']} with no portal (history via the Assessor index). "
           f"Routing data verified {m['routing_data_verified']}. Machine-readable: `vendor_map.json`.", "",
           "**Status** — live: this tool searches the city's system · blocked: a public portal exists but cannot be "
           "queried cheaply (reason + tracking issue given) · indexed: no portal; the King County Assessor's "
           "parcel index supplies issued-permit history for every parcel regardless.", "",
           "| City | Status | System | How it is queried / why not | Electrical | Records @ QC address (live / index) | Portal |",
           "|---|---|---|---|---|---|---|"]
    for r in m["rows"]:
        how = r["mechanism"] + (f" — [{r['tracking_issue'].rsplit('/', 1)[1]}]({r['tracking_issue']})" if r["tracking_issue"] else "")
        elec = ("city (searched)" if r["electrical_authority"] == "city" and r["electrical_searched"] else
                "city (**gap** — not searched)" if r["electrical_authority"] == "city" else "WA L&I (searched)")
        d = r["records_at_qc_address"]
        depth = f"{d['live']} / {d['index']}" if d else "—"
        portal = f"[portal]({r['portal']})" if r["portal"] else "—"
        out.append(f"| {r['city']} | {r['status']} | {r['system']} | {how} | {elec} | {depth} | {portal} |")
    out += ["", "## Where each permit type lives", "",
            "Every city follows the same shape: building, mechanical/plumbing, land-use and engineering/ROW "
            "permits live in the city's system above; **electrical** is WA L&I unless the city self-runs it "
            "(then it is in the city's system — and a gap if that system is blocked); **septic** is always "
            "King County Public Health; **fire** is the city fire marshal (King County Fire Marshal when "
            "unincorporated); **grading** in critical areas may also need King County review inside city "
            "limits. The lookup returns this per address as `routing_by_type`.", "",
            "## Vendors and how they are queried", ""]
    for cls, (mech, status) in MECHANISM.items():
        if mech:
            cities = [r["city"] for r in m["rows"] if r["vendor_class"] == cls]
            out.append(f"- **{cls}** ({status}): {mech}. Cities: {', '.join(cities) or '—'}.")
    for label, (why, issue) in BLOCKED.items():
        cities = [r["city"] for r in m["rows"] if r["system"] == label]
        out.append(f"- **{label}** (blocked): {why}. Cities: {', '.join(cities) or '—'}. Tracking: {issue}")
    out += ["", f"Per-vendor request recipes and recon notes: {m['recipes']}. Tool: {m['tool']}.", ""]
    return "\n".join(out)


def main():
    m = build()
    js = json.dumps(m, indent=1, ensure_ascii=False) + "\n"
    md = render_md(m)
    mode = sys.argv[1] if len(sys.argv) > 1 else "--print"
    if mode == "--print":
        print(md)
        return
    cur_js = open(JSON_PATH).read() if os.path.exists(JSON_PATH) else ""
    cur_md = open(MD_PATH).read() if os.path.exists(MD_PATH) else ""
    if mode == "--check":
        sys.exit(0 if (cur_js == js and cur_md == md) else "vendor map is stale — run --write")
    if mode == "--write":
        open(JSON_PATH, "w").write(js)
        open(MD_PATH, "w").write(md)
        print(f"wrote {JSON_PATH} and {MD_PATH}")


if __name__ == "__main__":
    main()
