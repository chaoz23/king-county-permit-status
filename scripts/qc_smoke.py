#!/usr/bin/env python3
"""Live QC smoke run: exercise lookup() against real + fake queries.

Hits real portals — NOT part of the network-free unit suite. Run manually:

    python3 scripts/qc_smoke.py

Each case asserts a loose contract (action + a jurisdiction/portal signal),
so it catches crashes, regressions, and misroutes without being brittle about
exact permit counts.
"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lookup  # noqa: E402

# (label, query, check) — check(result) -> (ok, note)
def found_in(juris):
    def c(r):
        js = {p["jurisdiction"] for p in r["permits"]}
        return (r["action"] == "found" and any(juris.lower() in j.lower() for j in js),
                f"action={r['action']} count={r['permit_count']} juris={sorted(js)[:4]}")
    return c

def action_is(*actions):
    def c(r):
        return (r["action"] in actions, f"action={r['action']} count={r['permit_count']}")
    return c

def fallback_for(city):
    def c(r):
        sp = r.get("separate_portal") or {}
        return (sp.get("city", "").lower() == city.lower(),
                f"separate_portal={sp.get('city')} action={r['action']}")
    return c

CASES = [
    # --- REAL addresses, one per live source (regression) ---
    ("real/Renton",      "1817 Morris Ave S, Renton WA 98055",  found_in("Renton")),
    ("real/Seattle",     "400 Broad St, Seattle WA 98109",      found_in("Seattle")),
    ("real/Bellevue",    "919 109th Ave NE, Bellevue WA",       found_in("Bellevue")),
    ("real/Shoreline",   "15332 Aurora Ave N, Shoreline WA",    found_in("Shoreline")),
    ("real/Redmond",     "16080 NE 85th St, Redmond WA 98052",  found_in("Redmond")),
    ("real/Woodinville", "13206 NE 201st Ct, Woodinville WA",   found_in("Woodinville")),
    ("real/BlackDiamond","33230 293rd Ave SE, Black Diamond WA",found_in("King County")),
    ("real/Auburn(MBP)", "25 W Main St, Auburn WA 98001",       found_in("Auburn")),
    # --- REAL parcels / permit numbers ---
    ("real/parcel-Renton",  "7222000353",  found_in("Renton")),
    ("real/parcel-Redmond", "0225059115",  action_is("found")),
    ("real/permit-Renton",  "B25000947",   found_in("Renton")),
    ("real/permit-Woodv",   "ROW26100",    action_is("found", "none")),  # Accela permit# may redirect
    # --- FAKE addresses (robustness: none, no crash) ---
    ("fake/Renton",     "99999 Nowhere St, Renton WA 98055",   action_is("none")),
    ("fake/Seattle",    "88888 Imaginary Ave, Seattle WA",     action_is("none")),
    ("fake/Redmond",    "77777 Madeup Blvd, Redmond WA 98052", action_is("none")),
    ("fake/Woodv",      "66666 Fake Ct, Woodinville WA",       action_is("none")),
    ("fake/nocity",     "55555 Ghost Rd",                      action_is("none")),
    ("fake/permit",     "ZZZ99-99999",                         action_is("none")),
    # --- FALLBACK cities (expect a portal note) ---
    ("fallback/Kent",       "220 4th Ave S, Kent WA 98032",      fallback_for("Kent")),
    ("fallback/DesMoines",  "21630 11th Ave S, Des Moines WA",   fallback_for("Des Moines")),
    ("fallback/Covington",  "16720 SE 271st St, Covington WA",   fallback_for("Covington")),
    # --- EDGE cases ---
    ("edge/blank",      "",           action_is("reject")),
    ("edge/gibberish",  "asdf qwer",  action_is("none", "reject", "refine")),
]


def main():
    print(f"QC smoke: {len(CASES)} live cases\n" + "=" * 66)
    passed = failed = errored = 0
    slow = []
    for label, query, check in CASES:
        t0 = time.time()
        try:
            result = lookup.lookup(query)
            ok, note = check(result)
        except Exception:
            errored += 1
            print(f"  ERROR {label:22} {query[:32]!r}\n{traceback.format_exc()}")
            continue
        dt = time.time() - t0
        if dt > 8:
            slow.append((label, round(dt, 1)))
        mark = "PASS" if ok else "FAIL"
        if ok:
            passed += 1
        else:
            failed += 1
        print(f"  {mark} {label:22} {dt:5.1f}s  {note}")
    print("=" * 66)
    print(f"  {passed} passed · {failed} failed · {errored} errored")
    if slow:
        print("  slow (>8s):", slow)
    sys.exit(1 if (failed or errored) else 0)


if __name__ == "__main__":
    main()
