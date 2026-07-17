#!/usr/bin/env python3
"""Re-verify permit routing data against authoritative sources.

Checks:
  1. L&I city electrical list — which cities do their own electrical
  2. MyBuildingPermit jurisdictions — which cities are on the portal
  3. Portal URLs — which ones still respond

Usage:
  python3 refresh.py          # check + report what changed
  python3 refresh.py --apply  # check + update routing_data.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(SCRIPT_DIR, "routing_data.json")

# lookup.py holds the JurisId map + MBP client used for backend proof-of-life.
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
try:
    import lookup
except Exception:  # pragma: no cover - lookup should always import
    lookup = None

LNI_URL = "https://www.lni.wa.gov/licensing-permits/electrical/electrical-permits-fees-and-inspections/city-electrical-permits-inspections"
MBP_URL = "https://permitsearch.mybuildingpermit.com/"
PORTAL_CHECK_LIMIT = 10

# Common street tokens for probing whether an MBP jurisdiction is still served.
# A populated jurisdiction returns records or a "too many results" cap on at
# least one of these; a removed one returns empty lists for all.
MBP_PROBE_STREETS = ("1st Ave", "Main St", "2nd Ave", "Park", "Ave")


def load_data() -> dict:
    with open(DATA_PATH) as f:
        return json.load(f)


def fetch_lni_cities() -> set[str]:
    """Scrape the L&I page for cities that do their own electrical."""
    try:
        req = urllib.request.Request(LNI_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARN: Could not fetch L&I page: {e}")
        return set()

    cities = set()
    noise = {"permit", "inspect", "contact", "click", "visit", "map", "office",
             "more", "detailed", "location", "http", "here", "back", "home",
             "page", "information", "about", "find", "search", "view", "list",
             "county", "pud", "utility", "district", "power"}

    items = re.findall(r"<li[^>]*>([^<]+)</li>", html)
    for item in items:
        name = item.strip().lower()
        words = set(name.split())
        if len(name) < 30 and not words & noise and len(words) <= 3:
            name = re.sub(r"\s*power$", "", name).strip()
            if name and len(name) >= 4:
                cities.add(name)

    links = re.findall(r'<a[^>]*>([^<]{4,25})</a>', html)
    for link in links:
        name = link.strip().lower()
        words = set(name.split())
        if name and not words & noise and len(words) <= 3 and len(name) >= 4:
            cities.add(name)

    return cities


def fetch_mbp_jurisdictions() -> set[str]:
    """Scrape MyBuildingPermit for jurisdictions in the dropdown."""
    try:
        req = urllib.request.Request(MBP_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  WARN: Could not fetch MBP page: {e}")
        return set()

    match = re.search(r'id="ddlJurisdictions"(.*?)</select>', html, re.DOTALL)
    if not match:
        return set()

    opts = re.findall(r'value="\d+"[^>]*>([^<]+)</option>', match.group(1))
    cities = set()
    for name in opts:
        name = name.strip().lower()
        if name and name != "--select one--" and name != "king county":
            cities.add(name)
    return cities


def mbp_backend_alive(city: str) -> bool | None:
    """Probe the MBP backend by JurisId for proof of life.

    MyBuildingPermit's public dropdown is a UI surface only: it has dropped
    jurisdictions (e.g. Newcastle, Mill Creek) that the backend still serves by
    JurisId. Returns True if the backend returns records for the city, False if
    it returns no data across every probe street, or None if it cannot be
    checked (no JurisId, import/session failure).
    """
    if lookup is None:
        return None
    jid = lookup.JURIS_BY_NAME.get(city.lower())
    if not jid:
        return None
    try:
        opener, token = lookup.get_session()
    except Exception:
        return None
    saw_empty = False
    for street in MBP_PROBE_STREETS:
        try:
            result = lookup.search_permits(opener, token, jid, street=street)
        except Exception:
            continue
        if isinstance(result, str):
            if "too many" in result.lower():
                return True          # cap hit — the jurisdiction is populated
            continue                 # e.g. "valid jurisdiction" — not proof
        if isinstance(result, list):
            if result:
                return True          # real records came back
            saw_empty = True
    return False if saw_empty else None


def check_url(url: str) -> tuple[bool, str]:
    """Check if a URL responds. Follows redirects. Returns (ok, status_note)."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            final_url = resp.url
            if final_url != url:
                return True, f"redirects → {final_url[:60]}"
            return True, "OK"
    except urllib.error.HTTPError as e:
        # Auth, method, and throttling responses still prove the portal exists.
        # Missing/bad-request and server errors indicate an unusable URL.
        return e.code in {401, 403, 405, 429}, f"HTTP {e.code}"
    except Exception as e:
        return False, f"DEAD ({e})"


def prioritized_portals(data: dict) -> list[tuple[str, str]]:
    """Return portal checks ordered by population priority, then data order."""
    portals = data["city_portals"]
    priority = data.get("portal_check_priority", [])
    ordered = []
    seen = set()
    for city in list(priority) + list(portals):
        if city in portals and city not in seen:
            ordered.append((city, portals[city]))
            seen.add(city)
        if len(ordered) == PORTAL_CHECK_LIMIT:
            break
    return ordered


def main():
    apply_changes = "--apply" in sys.argv

    data = load_data()
    last = data["last_verified"]
    days_old = (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days
    print(f"Routing data last verified: {last} ({days_old} days ago)")
    print()

    changes = {}

    # 1. Check L&I city electrical list
    print("Checking L&I city electrical list...")
    lni_cities = fetch_lni_cities()
    current_electrical = set(data["cities_own_electrical"])
    if lni_cities:
        added = lni_cities - current_electrical
        removed = current_electrical - lni_cities
        if added:
            print(f"  NEW cities doing own electrical: {sorted(added)}")
            changes["cities_own_electrical_added"] = sorted(added)
        if removed:
            print(f"  Cities NO LONGER on L&I list: {sorted(removed)}")
            changes["cities_own_electrical_removed"] = sorted(removed)
        if not added and not removed:
            print(f"  No changes ({len(lni_cities)} cities)")
    else:
        print("  Could not verify (fetch failed)")

    # 2. Check MyBuildingPermit jurisdictions.
    # The public dropdown is UI-only. MBP has dropped jurisdictions from it that
    # the backend still serves by JurisId, so a dropdown absence is NOT proof a
    # city left MBP. Confirm every drop against the backend, and never auto-drop
    # a city: a silent coverage loss is far worse than a stale entry, which
    # surfaces loudly as lookup errors and gets caught on the next run.
    print("\nChecking MyBuildingPermit jurisdictions...")
    mbp_cities = fetch_mbp_jurisdictions()
    verification_failed = not lni_cities or not mbp_cities
    current_mbp = set(data["cities_on_mbp"])
    mbp_added = set()
    if mbp_cities:
        mbp_added = mbp_cities - current_mbp
        dropped = current_mbp - mbp_cities
        if mbp_added:
            print(f"  NEW cities on MBP: {sorted(mbp_added)}")
            changes["mbp_added"] = sorted(mbp_added)
        retained, review = [], []
        for city in sorted(dropped):
            alive = mbp_backend_alive(city)
            if alive is True:
                retained.append(city)
                print(f"  Dropped from dropdown but backend still serves — "
                      f"RETAINED: {city}")
            else:
                review.append(city)
                verdict = "no records found" if alive is False else "unverified"
                print(f"  Dropped from dropdown, {verdict} — "
                      f"NEEDS MANUAL REVIEW (kept): {city}")
        if retained:
            changes["mbp_retained_ui_drift"] = retained
        if review:
            changes["mbp_needs_review"] = review
        if not mbp_added and not dropped:
            print(f"  No changes ({len(mbp_cities)} cities)")
    else:
        print("  Could not verify (fetch failed)")

    # 3. Spot-check the highest-population portal URLs first.
    print("\nSpot-checking portal URLs...")
    sample = prioritized_portals(data)
    dead = []
    for city, url in sample:
        ok, note = check_url(url)
        print(f"  {city:20s} {url:50s} {note}")
        if not ok:
            dead.append(city)
    if dead:
        changes["dead_urls"] = dead
        verification_failed = True

    # Summary
    print()
    if apply_changes and verification_failed:
        print("Verification incomplete; routing data was not updated.")
        return

    if changes:
        print(f"CHANGES DETECTED: {json.dumps(changes, indent=2)}")
        if apply_changes:
            if lni_cities:
                data["cities_own_electrical"] = sorted(lni_cities)
            if mbp_cities:
                # Add newly-listed cities; retain every current entry. Drops are
                # never applied automatically — they are surfaced above for
                # manual review so a UI-only change can't silently cut coverage.
                data["cities_on_mbp"] = sorted(current_mbp | mbp_added)
            data["last_verified"] = datetime.now().strftime("%Y-%m-%d")
            with open(DATA_PATH, "w") as f:
                json.dump(data, f, indent=2)
            print(f"\nUpdated {DATA_PATH}")
        else:
            print("\nRun with --apply to update routing_data.json")
    else:
        if apply_changes:
            data["last_verified"] = datetime.now().strftime("%Y-%m-%d")
            with open(DATA_PATH, "w") as f:
                json.dump(data, f, indent=2)
            print(f"No changes. Updated last_verified to {data['last_verified']}")
        else:
            print("No changes detected. Data looks current.")


if __name__ == "__main__":
    main()
