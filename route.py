#!/usr/bin/env python3
"""King County permit router.

Given an address and a description of the work, tells you which permit(s)
you likely need and which portal(s) to use. No API calls — pure routing
logic based on jurisdiction rules.

Usage:
  python3 route.py "1817 Morris Ave S, Renton" "installing a heat pump"
  python3 route.py --pipe "123 Main St" "rewiring kitchen and adding outlets"
"""

import json
import os
import re
import sys
from datetime import datetime

from city_utils import detect_city_name

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STALE_DAYS = 90

# --- Permit type detection from work description ---

PERMIT_PATTERNS = {
    "electrical": {
        "keywords": [
            "electric", "wiring", "rewire", "outlet", "circuit", "panel",
            "breaker", "solar", "photovoltaic", "pv", "ev charger",
            "charging station", "generator", "transfer switch", "subpanel",
            "lighting", "light fixture", "ceiling fan", "240v", "200 amp",
            "service upgrade", "meter", "knob and tube",
        ],
        "description": "Electrical permit",
    },
    "building": {
        "keywords": [
            "addition", "remodel", "renovate", "renovation", "deck",
            "porch", "garage", "carport", "adu", "accessory dwelling",
            "dadu", "mother-in-law", "in-law", "convert", "conversion",
            "demolition", "demolish", "tear down", "new construction",
            "build", "construct", "foundation", "framing", "structural",
            "load-bearing", "wall", "window", "door", "siding",
            "bathroom", "kitchen", "basement", "finish basement",
            "square foot", "sqft", "sq ft", "shed", "pergola",
        ],
        "description": "Building permit",
    },
    "mechanical": {
        "keywords": [
            "hvac", "heat pump", "mini split", "furnace", "boiler",
            "air condition", "ac unit", "ductwork", "duct", "ventilat",
            "exhaust", "fireplace", "wood stove", "pellet stove",
            "gas line", "gas piping", "mechanical",
        ],
        "description": "Mechanical permit",
    },
    "plumbing": {
        "keywords": [
            "plumb", "water heater", "tankless", "repipe", "sewer",
            "drain", "toilet", "sink", "bathtub", "shower",
            "water line", "backflow", "irrigation", "sprinkler system",
            "gas water", "fixture",
        ],
        "description": "Plumbing permit",
    },
    "roofing": {
        "keywords": [
            "roof", "reroof", "re-roof", "shingle", "metal roof",
            "flat roof", "roofing",
        ],
        "description": "Roofing permit (usually building permit)",
        "maps_to": "building",
    },
    "grading": {
        "keywords": [
            "grad", "excavat", "clear", "fill", "retaining wall",
            "landslide", "erosion", "drainage", "stormwater",
            "critical area", "wetland", "steep slope", "stream",
        ],
        "description": "Grading/site development permit",
    },
    "septic": {
        "keywords": [
            "septic", "on-site sewage", "drainfield", "cesspool",
        ],
        "description": "Septic/on-site sewage permit",
    },
    "fire": {
        "keywords": [
            "sprinkler", "fire alarm", "fire suppression", "hood",
            "commercial kitchen", "ansul", "fire system",
        ],
        "description": "Fire permit",
    },
    "demolition": {
        "keywords": [
            "demolition", "demolish", "tear down", "raze",
        ],
        "description": "Demolition permit",
        "maps_to": "building",
    },
    "fence": {
        "keywords": [
            "fence", "fencing", "gate",
        ],
        "description": "Fence (may be exempt under 6ft; check with jurisdiction)",
    },
}

# --- Jurisdiction routing (loaded from routing_data.json) ---

MBP_SEARCH = "https://permitsearch.mybuildingpermit.com/"
KC_PORTAL = "https://aca-prod.accela.com/KINGCO/Cap/CapHome.aspx?module=Building"
LNI_PORTAL = "https://secure.lni.wa.gov/epispub/frmPermitSearchMain.aspx"
KC_SEPTIC = "https://kingcounty.gov/en/dept/dph/health-safety/environmental-health/septic-systems"


def load_routing_data() -> dict:
    data_path = os.path.join(SCRIPT_DIR, "routing_data.json")
    with open(data_path) as f:
        return json.load(f)


def check_staleness(data: dict) -> dict | None:
    """Return a warning if routing data is stale."""
    last = data.get("last_verified", "2000-01-01")
    age_days = (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days
    if age_days > STALE_DAYS:
        return {
            "warning": f"Routing data is {age_days} days old (last verified {last}). Jurisdiction assignments may have changed.",
            "age_days": age_days,
            "last_verified": last,
            "action": "Run: python3 refresh.py --apply",
        }
    return None


def detect_city(address: str, data: dict) -> str | None:
    all_cities = (
        set(data.get("king_county_cities", []))
        | set(data.get("city_portals", {}).keys())
        | set(data.get("cities_on_mbp", []))
        | set(data.get("cities_own_electrical", []))
    )
    return detect_city_name(address, all_cities)


def detect_permits(work: str) -> list[dict]:
    """Match work description to likely permit types."""
    work_lower = work.lower()
    matched = []
    seen_types = set()

    for ptype, info in PERMIT_PATTERNS.items():
        for kw in info["keywords"]:
            if kw in work_lower:
                canonical = info.get("maps_to", ptype)
                if canonical not in seen_types:
                    seen_types.add(canonical)
                    matched.append({
                        "type": canonical,
                        "description": info["description"],
                        "matched_keyword": kw,
                    })
                if ptype != canonical and ptype not in seen_types:
                    seen_types.add(ptype)
                break

    if not matched:
        matched.append({
            "type": "unknown",
            "description": "Could not determine permit type from description",
            "matched_keyword": None,
        })

    return matched


def route_permit(permit_type: str, city: str | None, data: dict) -> dict:
    """Determine which portal handles this permit type for this location."""
    city_lower = city.lower() if city else None
    is_unincorporated = city_lower is None
    cities_own_elec = set(data.get("cities_own_electrical", []))
    cities_on_mbp = set(data.get("cities_on_mbp", []))
    city_portals = data.get("city_portals", {})

    if permit_type == "electrical":
        if city_lower and city_lower in cities_own_elec:
            portal = city_portals.get(city_lower)
            return {
                "handled_by": f"{city.title()} (city handles electrical)",
                "portal": portal,
                "note": f"{city.title()} does its own electrical permits, not L&I.",
            }
        else:
            return {
                "handled_by": "WA State L&I",
                "portal": LNI_PORTAL,
                "note": "Electrical permits in this area go through state Labor & Industries.",
            }

    elif permit_type == "septic":
        return {
            "handled_by": "King County Public Health",
            "portal": KC_SEPTIC,
            "note": "Septic permits are always King County Public Health, regardless of city.",
        }

    elif permit_type == "grading":
        return {
            "handled_by": "King County DPER" if is_unincorporated else f"{city.title()} and/or King County",
            "portal": KC_PORTAL if is_unincorporated else city_portals.get(city_lower, KC_PORTAL),
            "note": "Grading in critical areas may require King County review even within city limits.",
        }

    else:
        if is_unincorporated:
            return {
                "handled_by": "King County DPER",
                "portal": KC_PORTAL,
                "note": "Unincorporated King County — permits through KC DPER.",
            }
        elif city_lower in cities_on_mbp:
            return {
                "handled_by": f"{city.title()} (via MyBuildingPermit)",
                "portal": MBP_SEARCH,
                "note": f"{city.title()} uses the MyBuildingPermit.com portal.",
            }
        else:
            portal = city_portals.get(city_lower)
            return {
                "handled_by": f"{city.title()}",
                "portal": portal,
                "note": f"{city.title()} has its own permit portal.",
            }


def route(address: str, work: str) -> dict:
    """Main routing function."""
    if not address.strip():
        return {
            "action": "reject",
            "address": address,
            "work_description": work,
            "permits": [],
            "portals": [],
            "message": "Address must not be blank.",
        }

    data = load_routing_data()
    staleness = check_staleness(data)
    city = detect_city(address, data)
    permits_needed = detect_permits(work)

    routes = []
    for permit in permits_needed:
        routing = route_permit(permit["type"], city, data)
        routes.append({
            **permit,
            **routing,
        })

    # Group by portal for a cleaner summary
    by_portal = {}
    for r in routes:
        portal = r.get("portal") or "unknown"
        if portal not in by_portal:
            by_portal[portal] = {
                "handled_by": r["handled_by"],
                "portal": portal,
                "permit_types": [],
            }
        by_portal[portal]["permit_types"].append(r["type"])

    location = city.title() if city else "Unincorporated King County"

    # Build human-readable summary
    lines = [f"For work at {address} ({location}):"]
    for r in routes:
        lines.append(f"  {r['description']:35s} → {r['handled_by']}")
    summary = "\n".join(lines)

    result = {
        "action": "routed",
        "address": address,
        "location": location,
        "work_description": work,
        "permits": routes,
        "portals": list(by_portal.values()),
        "message": summary,
        "data_verified": data.get("last_verified"),
    }

    if staleness:
        result["staleness_warning"] = staleness
        result["message"] += f"\n\n⚠ {staleness['warning']}"

    return result


def main():
    args = sys.argv[1:]
    pipe_mode = "--pipe" in args
    args = [a for a in args if a != "--pipe"]

    if len(args) < 1:
        print('Usage: route.py [--pipe] "<address>" ["work description"]')
        print('  route.py "1817 Morris Ave S, Renton" "installing a heat pump"')
        print('  route.py "123 Main St" "rewiring kitchen and adding deck"')
        print('  route.py "1234 Rural Rd, Fall City" "new septic system"')
        sys.exit(2)

    address = args[0]
    work = args[1] if len(args) > 1 else ""

    result = route(address, work)

    if pipe_mode:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(json.dumps(result, indent=2))

    if result["action"] == "reject":
        sys.exit(2)


if __name__ == "__main__":
    main()
