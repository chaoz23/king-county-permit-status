#!/usr/bin/env python3
"""King County permit status lookup.

Search building permits by address, parcel number, or permit number across
King County jurisdictions. Searches both the city jurisdiction (if on the
MyBuildingPermit portal) AND King County (for county-level permits like
septic, critical areas, grading) since both can apply to a single address.

Two modes:
  Human:  python3 lookup.py "27927 E Main St"
  Agent:  python3 lookup.py --pipe "27927 E Main St"

Exit codes:
  0 = permits found (action=found)
  1 = no permits / search issue (action=none/refine)
  2 = bad input (action=reject)
"""

import json
import re
import sys
import urllib.request
import urllib.parse
import http.cookiejar
from datetime import datetime

SEARCH_URL = "https://permitsearch.mybuildingpermit.com/SearchPermits/GetSearchResults"
BASE_URL = "https://permitsearch.mybuildingpermit.com/"

JURISDICTIONS = {
    "24": "Auburn", "1": "Bellevue", "2": "Bothell", "11": "Burien",
    "23": "Edmonds", "25": "Federal Way", "3": "Issaquah", "4": "Kenmore",
    "20": "King County", "5": "Kirkland", "6": "Mercer Island",
    "13": "Mill Creek", "19": "Newcastle", "7": "Sammamish",
    "9": "Snoqualmie",
}
JURIS_BY_NAME = {v.lower(): k for k, v in JURISDICTIONS.items()}

# Cities NOT on MyBuildingPermit — have their own permit portals
SEPARATE_PORTALS = {
    "seattle": "https://cosaccela.seattle.gov/portal/",
    "renton": "https://permits.rentonwa.gov/",
    "kent": "https://epermit.kentwa.gov/",
    "redmond": "https://permits.redmond.gov/",
    "shoreline": "https://permits.shorelinewa.gov/",
    "tukwila": "https://www.tukwilawa.gov/departments/community-development/",
    "seatac": "https://www.seatacwa.gov/our-city/community-development",
    "woodinville": "https://www.woodinvillewa.gov/",
    "covington": "https://www.covingtonwa.gov/",
    "maple valley": "https://www.maplevalleywa.gov/",
    "enumclaw": "https://www.cityofenumclaw.net/",
    "north bend": "https://www.northbendwa.gov/",
    "black diamond": "https://www.ci.blackdiamond.wa.us/",
}


def parse_date(ms_date: str | None) -> str | None:
    """Parse .NET /Date(milliseconds)/ to YYYY-MM-DD."""
    if not ms_date:
        return None
    m = re.search(r"/Date\((\d+)\)/", str(ms_date))
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000).strftime("%Y-%m-%d")


def detect_input_type(raw: str) -> tuple[str, str]:
    """Detect if input is a permit number, parcel number, or address."""
    s = raw.strip()
    if re.fullmatch(r"\d{10}", s):
        return "parcel", s
    if re.match(r"[A-Z]{3,4}\d{2}-\d{3,4}", s, re.IGNORECASE):
        return "permit", s
    return "address", s


def detect_city(address: str) -> str | None:
    """Try to extract a city name from the address string."""
    addr_lower = address.lower().replace(",", " ")
    for city in list(JURIS_BY_NAME.keys()) + list(SEPARATE_PORTALS.keys()):
        if city in addr_lower:
            return city
    return None


def get_session():
    """Get a session with anti-forgery token."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    resp = opener.open(urllib.request.Request(
        BASE_URL, headers={"User-Agent": "Mozilla/5.0"}
    ), timeout=15)
    html = resp.read().decode("utf-8", errors="replace")
    token = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html).group(1)
    return opener, token


def search_permits(opener, token, juris_id, search_by="Location",
                   street="", house="", parcel="", permit_number="") -> list[dict] | str:
    """Search permits in a single jurisdiction. Returns list or error string."""
    form = {
        "__RequestVerificationToken": token,
        "SearchBy": search_by,
        "JurisId": juris_id,
        "PermitNumber": permit_number,
        "ProjectName": "",
        "HouseBldgNum": house,
        "StreetName": street,
        "ParcelNum": parcel,
        "ContractorCompany": "",
        "ContractorLicNum": "",
        "ApplicantLastName": "",
        "FromDate": "",
        "ToDate": "",
    }
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(SEARCH_URL, data=data, headers={
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
    })
    try:
        resp = opener.open(req, timeout=30)
        result = json.loads(resp.read().decode("utf-8", errors="replace"))
        if isinstance(result, dict) and not result.get("success", True):
            return result.get("ErrorMessage", "Too many results — narrow your search")
        return result if isinstance(result, list) else []
    except Exception as e:
        return f"Error: {e}"


def format_permit(raw: dict) -> dict:
    """Normalize a raw permit record into a clean output dict."""
    return {
        "permit_number": raw.get("PermitNumber", ""),
        "type": raw.get("PermitType", ""),
        "status": raw.get("PermitStatus", ""),
        "description": raw.get("PermitDescription", ""),
        "address": (raw.get("Address") or "").strip(),
        "jurisdiction": raw.get("Jurisdiction", ""),
        "applied_date": parse_date(raw.get("AppliedDate")),
        "issued_date": parse_date(raw.get("IssuedDate")),
        "finaled_date": parse_date(raw.get("FinaledDate")),
        "expires_date": parse_date(raw.get("ApplicationExpDate")),
    }


def parse_address(address: str) -> tuple[str, str]:
    """Split an address into house number and street name."""
    m = re.match(r"(\d+)\s+(.+)", address.strip())
    if m:
        return m.group(1), m.group(2).split(",")[0].strip()
    return "", address.split(",")[0].strip()


def lookup(raw_input: str) -> dict:
    """Core lookup. Returns unified result for human + agent."""
    input_type, value = detect_input_type(raw_input)
    city = detect_city(raw_input)

    try:
        opener, token = get_session()
    except Exception as e:
        return {
            "action": "refine",
            "message": f"Could not connect to permit search: {e}",
            "permits": [], "input": raw_input,
        }

    all_permits = []
    searched_jurisdictions = []
    errors = []
    separate_portal_note = None

    if input_type == "permit":
        # Search all jurisdictions for the permit number
        for jid, jname in JURISDICTIONS.items():
            results = search_permits(opener, token, jid,
                                     search_by="PermitNumber", permit_number=value)
            if isinstance(results, list) and results:
                all_permits.extend(results)
                searched_jurisdictions.append(jname)
                break  # permit numbers are unique
            elif isinstance(results, str):
                errors.append(f"{jname}: {results}")

    elif input_type == "parcel":
        # Search King County + city jurisdiction if known
        juris_to_search = ["20"]  # always search KC
        if city and city in JURIS_BY_NAME:
            juris_to_search.append(JURIS_BY_NAME[city])
        for jid in juris_to_search:
            results = search_permits(opener, token, jid, parcel=value)
            jname = JURISDICTIONS.get(jid, jid)
            searched_jurisdictions.append(jname)
            if isinstance(results, list):
                all_permits.extend(results)
            elif isinstance(results, str):
                errors.append(f"{jname}: {results}")
        if city and city in SEPARATE_PORTALS:
            separate_portal_note = {
                "city": city.title(),
                "portal": SEPARATE_PORTALS[city],
                "note": f"{city.title()} has its own permit system — city-issued permits won't appear here.",
            }

    else:  # address
        house, street = parse_address(value)
        # Search King County + city jurisdiction
        juris_to_search = ["20"]
        if city and city in JURIS_BY_NAME:
            juris_to_search.append(JURIS_BY_NAME[city])
        elif not city:
            # No city detected — search all jurisdictions
            juris_to_search = list(JURISDICTIONS.keys())

        for jid in juris_to_search:
            results = search_permits(opener, token, jid,
                                     house=house, street=street)
            jname = JURISDICTIONS.get(jid, jid)
            searched_jurisdictions.append(jname)
            if isinstance(results, list):
                all_permits.extend(results)
            elif isinstance(results, str) and "too many" in results.lower():
                errors.append(f"{jname}: {results}")

        if city and city in SEPARATE_PORTALS:
            separate_portal_note = {
                "city": city.title(),
                "portal": SEPARATE_PORTALS[city],
                "note": f"{city.title()} has its own permit system — city-issued permits won't appear here.",
            }

    # Deduplicate by permit number
    seen = set()
    unique = []
    for p in all_permits:
        pn = p.get("PermitNumber", "")
        if pn not in seen:
            seen.add(pn)
            unique.append(format_permit(p))

    # Sort by applied date (newest first)
    unique.sort(key=lambda p: p["applied_date"] or "", reverse=True)

    if unique:
        result = {
            "action": "found",
            "permit_count": len(unique),
            "searched": searched_jurisdictions,
            "permits": unique,
            "input": raw_input,
            "message": f"Found {len(unique)} permit(s) across {', '.join(set(searched_jurisdictions))}.",
        }
    else:
        suggestions = ["Try searching by street name without the city"]
        if errors:
            suggestions.append(f"Some searches had issues: {'; '.join(errors)}")
        result = {
            "action": "none",
            "permit_count": 0,
            "searched": searched_jurisdictions,
            "permits": [],
            "input": raw_input,
            "message": f"No permits found in {', '.join(set(searched_jurisdictions))}.",
            "suggestions": suggestions,
        }

    if separate_portal_note:
        result["separate_portal"] = separate_portal_note
        result["message"] += f" Note: {separate_portal_note['note']}"

    if errors and not unique:
        result["errors"] = errors

    return result


EXIT_CODES = {"found": 0, "none": 1, "refine": 1, "reject": 2}


def main():
    args = sys.argv[1:]
    pipe_mode = "--pipe" in args
    args = [a for a in args if a != "--pipe"]

    if not args:
        print("Usage: lookup.py [--pipe] <address|parcel|permit_number>")
        print('  lookup.py "27927 E Main St"              # by address')
        print('  lookup.py "7222000353"                    # by parcel number')
        print('  lookup.py "ADDC21-0275"                   # by permit number')
        print('  lookup.py --pipe "27927 E Main St"        # agent mode')
        sys.exit(2)

    query = " ".join(args)
    result = lookup(query)

    if pipe_mode:
        print(json.dumps(result, separators=(",", ":")))
    else:
        print(json.dumps(result, indent=2))

    sys.exit(EXIT_CODES.get(result["action"], 1))


if __name__ == "__main__":
    main()
