#!/usr/bin/env python3
"""King County permit status lookup.

Search building permits by address, parcel number, or permit number across
three layers that can all apply to the same property:
  1. City jurisdiction (if on MyBuildingPermit portal) — building, mechanical
  2. King County — septic, critical areas, grading
  3. WA State L&I — electrical, manufactured/mobile home

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
    "renton": "https://permitting.rentonwa.gov/",
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
    "des moines": "https://www.desmoineswa.gov/",
    "normandy park": "https://www.normandyparkwa.gov/",
    "milton": "https://www.cityofmilton.net/",
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
    # MBP-style: ADDC21-0275; EnerGov-style: B25000947, E26000458
    if re.match(r"[A-Z]{1,4}\d{2}[-\d]\d{3,6}$", s, re.IGNORECASE):
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


# Cities that handle their own electrical permits (NOT through L&I).
# Source: https://www.lni.wa.gov/licensing-permits/electrical/electrical-permits-fees-and-inspections/city-electrical-permits-inspections
CITIES_OWN_ELECTRICAL = {
    "aberdeen", "bellingham", "bellevue", "burien", "des moines", "everett",
    "federal way", "kirkland", "lacey", "lynnwood", "marysville",
    "mercer island", "milton", "mountlake terrace", "normandy park",
    "olympia", "port angeles", "redmond", "renton", "sammamish", "seatac",
    "seattle", "spokane", "tukwila", "vancouver",
}

LNI_URL = "https://secure.lni.wa.gov/epispub/frmPermitSearchMain.aspx"


def search_lni(address: str, city: str = "") -> list[dict]:
    """Search WA State L&I for electrical/manufactured-home permits.

    L&I limits date range to 13 months, so we search the most recent
    windows. Records before 2019 are not available online.
    """
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    try:
        resp = opener.open(urllib.request.Request(
            LNI_URL, headers={"User-Agent": "Mozilla/5.0"}
        ), timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return []

    vs = re.search(r'id="__VIEWSTATE"[^>]*value="([^"]+)"', html)
    vsg = re.search(r'id="__VIEWSTATEGENERATOR"[^>]*value="([^"]+)"', html)
    ev = re.search(r'id="__EVENTVALIDATION"[^>]*value="([^"]+)"', html)
    if not vs or not ev:
        return []

    house, street = parse_address(address)
    # L&I docs: "enter only the house number in the site address field"
    site_addr = house if house else address.split(",")[0].strip()

    # Search last 3 years in 13-month windows
    from datetime import timedelta
    now = datetime.now()
    windows = []
    cursor = now
    for _ in range(3):
        end = cursor
        start = cursor - timedelta(days=395)
        if start < datetime(2019, 1, 1):
            start = datetime(2019, 1, 1)
        windows.append((start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y")))
        cursor = start - timedelta(days=1)
        if cursor < datetime(2019, 1, 1):
            break

    all_results = []
    cur_vs, cur_ev = vs.group(1), ev.group(1)
    cur_vsg = vsg.group(1) if vsg else ""

    for beg, end in windows:
        form = {
            "__VIEWSTATE": cur_vs,
            "__VIEWSTATEGENERATOR": cur_vsg,
            "__EVENTVALIDATION": cur_ev,
            "__LASTFOCUS": "",
            "rdoPermitType": "0",
            "tbxPermitNumber": "",
            "tbxBegDate": beg,
            "tbxEndDate": end,
            "tbxContractorId": "", "tbxBusinessName": "", "tbxLastName": "",
            "tbxFirstName": "", "tbxUBI": "", "tbxSiteOwner": "",
            "tbxSiteLastName": "", "tbxSiteFirstName": "",
            "tbxSiteAddr1": site_addr,
            "tbxSiteCity": city,
            "lstSiteCounty": "17",
            "rdoCityLimits": "1",
            "btnSearch": "Search",
            "URL": "",
        }
        data = urllib.parse.urlencode(form).encode("utf-8")
        try:
            req = urllib.request.Request(LNI_URL, data=data, headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/x-www-form-urlencoded",
            })
            resp2 = opener.open(req, timeout=30)
            result = resp2.read().decode("utf-8", errors="replace")
        except Exception:
            continue

        # Update viewstate for next request
        vs2 = re.search(r'id="__VIEWSTATE"[^>]*value="([^"]+)"', result)
        ev2 = re.search(r'id="__EVENTVALIDATION"[^>]*value="([^"]+)"', result)
        if vs2:
            cur_vs = vs2.group(1)
        if ev2:
            cur_ev = ev2.group(1)

        if len(result) < 15000:
            continue

        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", result, re.DOTALL)
        for row in rows:
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.DOTALL)
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
            if len(cells) >= 10 and cells[0] and cells[0] != "Permit Number":
                all_results.append({
                    "permit_number": cells[0],
                    "type": "WA State L&I Electrical",
                    "status": cells[8],
                    "description": cells[9],
                    "address": cells[5],
                    "jurisdiction": f"WA State L&I ({cells[7]})",
                    "applied_date": parse_lni_date(cells[1]),
                    "issued_date": None,
                    "finaled_date": None,
                    "expires_date": None,
                    "site_owner": cells[4],
                    "site_city": cells[6],
                })

    return all_results


def parse_lni_date(raw: str) -> str | None:
    """Parse L&I date (M/D/YYYY) to YYYY-MM-DD."""
    if not raw or raw == "&nbsp;":
        return None
    try:
        return datetime.strptime(raw.strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


# Cities with Tyler EnerGov portals that we can query directly.
ENERGOV_PORTALS = {
    "renton": {
        "url": "https://permitting.rentonwa.gov",
        "tenant_id": "1",
        "tenant_name": "RentonWaProd",
        "tenant_url": "RentonWaProd",
    },
}

# KC ArcGIS geocoder used for address → parcel when searching EnerGov cities
KC_GEOCODER_URL = (
    "https://gismaps.kingcounty.gov/arcgis/rest/services"
    "/Address/KingCo_ParcelAddress_locator/GeocodeServer/findAddressCandidates"
)


def _geocode_parcel(address: str) -> str | None:
    """Look up King County parcel number for an address via ArcGIS geocoder."""
    try:
        params = urllib.parse.urlencode({
            "SingleLine": address,
            "outFields": "*",
            "outSR": "4326",
            "maxLocations": "3",
            "f": "json",
        })
        url = KC_GEOCODER_URL + "?" + params
        resp = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=10)
        data = json.loads(resp.read().decode())
        for c in data.get("candidates", []):
            if c.get("score", 0) < 80:
                continue
            attrs = c.get("attributes") or {}
            pn = attrs.get("PIN") or attrs.get("ParcelNumber") or ""
            pn = re.sub(r"[\s\-]", "", str(pn))
            if re.fullmatch(r"\d{10}", pn):
                return pn
    except Exception:
        pass
    return None


def search_energov(portal_key: str, keyword: str, exact: bool = False) -> list[dict]:
    """Search a Tyler EnerGov Citizen Self Service portal.

    Returns normalized permit dicts or empty list on failure.
    Tenant headers discovered from JS bundle interceptor.
    """
    cfg = ENERGOV_PORTALS[portal_key]
    base = cfg["url"]

    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json",
        "tenantId": cfg["tenant_id"],
        "tenantName": cfg["tenant_name"],
        "Tyler-TenantUrl": cfg["tenant_url"],
        "Tyler-Tenant-Culture": "en-US",
        "Content-Type": "application/json;charset=UTF-8",
    }

    try:
        opener.open(urllib.request.Request(base + "/", headers={"User-Agent": "Mozilla/5.0"}), timeout=10)
        raw = json.loads(opener.open(urllib.request.Request(
            base + "/api/energov/search/criteria", headers=headers), timeout=10).read().decode())["Result"]
    except Exception:
        return []

    raw["Keyword"] = keyword
    raw["ExactMatch"] = exact
    raw["SearchModule"] = 1
    raw["FilterModule"] = 1
    raw["PageSize"] = 25
    raw["PageNumber"] = 1
    raw["PermitCriteria"]["PageSize"] = 25
    raw["PermitCriteria"]["PageNumber"] = 1

    try:
        resp = opener.open(urllib.request.Request(
            base + "/api/energov/search/search",
            data=json.dumps(raw).encode(), headers=headers), timeout=30)
        result = json.loads(resp.read().decode())
    except Exception:
        return []

    if not result.get("Success"):
        return []

    permits = []
    for e in (result.get("Result") or {}).get("EntityResults") or []:
        addr = e.get("AddressDisplay") or ((e.get("Address") or {}).get("FullAddress") or "")
        permits.append({
            "permit_number": e.get("CaseNumber", ""),
            "type": e.get("CaseType", ""),
            "status": e.get("CaseStatus", ""),
            "description": e.get("Description") or e.get("ProjectName") or "",
            "address": addr.strip(),
            "jurisdiction": portal_key.title(),
            "applied_date": _iso_date(e.get("ApplyDate")),
            "issued_date": _iso_date(e.get("IssueDate")),
            "finaled_date": _iso_date(e.get("FinalDate")),
            "expires_date": _iso_date(e.get("ExpireDate")),
            "portal": base,
        })
    return permits


def _iso_date(raw: str | None) -> str | None:
    """Convert ISO datetime string to YYYY-MM-DD."""
    if not raw:
        return None
    return raw[:10] if len(raw) >= 10 else raw


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
        # Search MyBuildingPermit jurisdictions first
        for jid, jname in JURISDICTIONS.items():
            results = search_permits(opener, token, jid,
                                     search_by="PermitNumber", permit_number=value)
            if isinstance(results, list) and results:
                all_permits.extend(results)
                searched_jurisdictions.append(jname)
                break  # permit numbers are unique
            elif isinstance(results, str):
                errors.append(f"{jname}: {results}")

        # Also check EnerGov portals (permit numbers are unique to their portal)
        for portal_key, pcfg in ENERGOV_PORTALS.items():
            eg = search_energov(portal_key, value, exact=True)
            if eg:
                all_permits.extend(eg)
                searched_jurisdictions.append(f"{portal_key.title()} (EnerGov)")
            else:
                searched_jurisdictions.append(f"{portal_key.title()} (EnerGov — not found)")

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

        # EnerGov cities: search by parcel number as keyword
        if city and city in ENERGOV_PORTALS:
            eg = search_energov(city, value, exact=False)
            all_permits.extend(eg)
            searched_jurisdictions.append(f"{city.title()} (EnerGov)")
        elif city and city in SEPARATE_PORTALS:
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

        # EnerGov cities: resolve parcel, then search by parcel
        if city and city in ENERGOV_PORTALS:
            parcel = _geocode_parcel(value)
            if parcel:
                eg = search_energov(city, parcel, exact=False)
                all_permits.extend(eg)
                searched_jurisdictions.append(f"{city.title()} (EnerGov, parcel {parcel})")
            else:
                searched_jurisdictions.append(f"{city.title()} (EnerGov — parcel lookup failed)")
        elif city and city in SEPARATE_PORTALS:
            separate_portal_note = {
                "city": city.title(),
                "portal": SEPARATE_PORTALS[city],
                "note": f"{city.title()} has its own permit system — city-issued permits won't appear here.",
            }

    # Layer 3: WA State L&I electrical permits (address searches only)
    # Skip L&I if the city handles its own electrical
    lni_permits = []
    city_does_electrical = city and city.lower() in CITIES_OWN_ELECTRICAL
    if input_type == "address":
        if city_does_electrical:
            searched_jurisdictions.append(f"WA State L&I — skipped ({city.title()} handles its own electrical)")
        else:
            house, street = parse_address(value)
            lni_permits = search_lni(f"{house} {street}", city or "")
            searched_jurisdictions.append("WA State L&I (electrical, 2019+)")

    # If the city does its own electrical and we can't search it, flag it
    if city_does_electrical and city not in ENERGOV_PORTALS:
        portal = SEPARATE_PORTALS.get(city.lower())
        electrical_note = {
            "city": city.title(),
            "portal": portal,
            "note": f"{city.title()} handles its own electrical permits — check their portal, not L&I.",
        }
        if separate_portal_note:
            separate_portal_note["note"] += f" {city.title()} also handles electrical permits."
            separate_portal_note["electrical"] = True
        else:
            separate_portal_note = electrical_note

    # Deduplicate by permit number
    # all_permits may contain raw MBP dicts (PermitNumber) or pre-normalized EnerGov dicts (permit_number)
    seen = set()
    unique = []
    for p in all_permits:
        if "permit_number" in p:  # already normalized (EnerGov)
            pn = p["permit_number"]
            normalized = p
        else:  # raw MBP dict
            pn = p.get("PermitNumber", "")
            normalized = format_permit(p)
        if pn not in seen:
            seen.add(pn)
            unique.append(normalized)
    for p in lni_permits:
        pn = p.get("permit_number", "")
        if pn not in seen:
            seen.add(pn)
            unique.append(p)

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
