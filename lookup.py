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

from __future__ import annotations

import concurrent.futures
import csv
import io
import json
import random
import re
import sys
import urllib.request
import urllib.parse
import http.cookiejar
from html import unescape as html_unescape
from calendar import monthrange
from datetime import datetime, timedelta, timezone

from city_utils import detect_city_name

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

# Cities with permit portals outside MyBuildingPermit. Most are fallback-only;
# Seattle and Renton also have live source integrations below.
SEPARATE_PORTALS = {
    "algona": "https://www.algonawa.gov/",
    "beaux arts village": "https://beauxarts-wa.gov/",
    "black diamond": "https://www.blackdiamondwa.gov/permits",
    "carnation": "https://www.carnationwa.gov/",
    "clyde hill": "https://www.clydehill.org/",
    "duvall": "https://www.duvallwa.gov/",
    "hunts point": "https://huntspoint-wa.gov/",
    "lake forest park": "https://www.cityoflfp.gov/",
    "medina": "https://www.medina-wa.gov/",
    "pacific": "https://www.pacificwa.gov/",
    "seattle": "https://cosaccela.seattle.gov/portal/",
    "renton": "https://permitting.rentonwa.gov/",
    "kent": "https://www.kentwa.gov/pay-and-apply/apply-for-a-permit/check-your-permit-status",
    "redmond": "https://cityofredmondwa-energovweb.tylerhost.net/apps/selfservice#/home",
    "shoreline": "https://permits.shorelinewa.gov/",
    "tukwila": "https://www.tukwilawa.gov/departments/community-development/",
    "seatac": "https://www.seatacwa.gov/our-city/community-development",
    "woodinville": "https://www.woodinvillewa.gov/",
    "covington": "https://www.covingtonwa.gov/",
    "maple valley": "https://www.maplevalleywa.gov/",
    "enumclaw": "https://www.cityofenumclaw.net/",
    "north bend": "https://www.northbendwa.gov/",
    "skykomish": "https://skykomishwa.gov/",
    "des moines": "https://www.desmoineswa.gov/",
    "normandy park": "https://www.normandyparkwa.gov/",
    "milton": "https://www.cityofmilton.net/",
    "yarrow point": "https://yarrowpointwa.gov/",
}

# Where a human (or a browsing agent) can actually run the search for cities we
# can't query over plain HTTP yet. Captured during portal recon (issues #18,
# #21–#24); falls back to the city's site in SEPARATE_PORTALS. search_by lists
# the inputs the portal's own search form accepts.
MANUAL_PORTALS = {
    # Kent has no public permit search at all (#24): permitstatus.kentwa.gov
    # is an SSO site for your own applications; the public record is monthly
    # XLSX permit logs. Indexing those is a separate project (see the issue).
    "kent": {"vendor": "No public search — monthly XLSX permit logs + applicant SSO",
             "search_url": "https://www.kentwa.gov/pay-and-apply/apply-for-a-permit/permit-logs",
             "search_by": [],
             "hint": ("Kent publishes permits only as monthly XLSX logs at "
                      "https://www.kentwa.gov/pay-and-apply/apply-for-a-permit/permit-logs "
                      "(pick the year folder, open the month, search the sheet for the "
                      "address). Applicants can see their own permit status after signing in "
                      "at https://permitstatus.kentwa.gov/.")},
    "des moines": {"vendor": "PermitTrax Citizens Connect",
                   "search_url": "https://desmoines-wa.permittrax.com/citizen/Home/DESMON_L/PBPW",
                   "search_by": ["address", "permit"]},
    "black diamond": {"vendor": "PermitTrax Citizens Connect",
                      "search_url": "https://www.blackdiamondwa.gov/permits",
                      "search_by": [],
                      "hint": ("Black Diamond issues its own permits through a PermitTrax "
                               "\"Citizen's Connect\" portal linked from "
                               "https://www.blackdiamondwa.gov/permits (account required; "
                               "no public HTTP search). County-level permits for the parcel "
                               "are already included from MyBuildingPermit King County.")},
    "covington": {"vendor": "PermitTrax Citizens Connect",
                  "search_url": "https://covington-wa.permittrax.com/citizen/Home/COVWA_L/PERMIT",
                  "search_by": ["address", "permit"]},
    "milton": {"vendor": "PermitTrax Citizens Connect",
               "search_url": "https://milton_wa.permittrax.com/citizen/Home/MILTON_L/PERMIT",
               "search_by": ["address", "permit"]},
    "enumclaw": {"vendor": "PermitTrax Citizens Connect",
                 "search_url": "https://enumclaw_wa.permittrax.com/",
                 "search_by": ["address", "permit"]},
    "north bend": {"vendor": "PermitTrax Citizens Connect",
                   "search_url": "https://northbend-wa.permittrax.com/",
                   "search_by": ["address", "permit"]},
    # OpenGov PLC: the API is clean JSON:API but every call is gated by
    # Cloudflare Turnstile headers (see #21) — browser only. Address search
    # must match the portal's own location record (house number + street).
    "maple valley": {"vendor": "OpenGov PLC (Turnstile-gated; browser only)",
                     "search_url": "https://maplevalleywa.portal.opengov.com/search",
                     "search_by": ["address", "permit"]},
    "duvall": {"vendor": "OpenGov PLC (Turnstile-gated; browser only)",
               "search_url": "https://duvallwa.portal.opengov.com/search",
               "search_by": ["address", "permit"]},
    "tukwila": {"vendor": "ASP.gov (results require login)",
                "search_url": "https://www.tukwilawa.gov/departments/community-development/",
                "search_by": []},
}


def build_next_step(city: str, reason: str, query: str, input_type: str,
                    portal: str | None = None, electrical: bool = False) -> dict:
    """Structured follow-up for an agent when a city can't be searched here.

    reason: no_feed | electrical_only | parcel_resolution_failed | source_incomplete
    """
    key = (city or "").lower()
    manual = MANUAL_PORTALS.get(key, {})
    search_url = manual.get("search_url") or portal or SEPARATE_PORTALS.get(key)
    search_by = manual.get("search_by", ["address", "permit"])
    step = {
        "kind": "manual_portal_search",
        "reason": reason,
        "city": city.title() if city else None,
        "portal_url": search_url,
        "vendor": manual.get("vendor"),
        "search_by": search_by,
        "query": query,
        "query_type": input_type,
        "covers_electrical": electrical,
        "hint": manual.get("hint") or (
            f"Search {search_url} by {' or '.join(search_by)} for {query!r}."
            if search_by else
            f"{city.title()} results are login-gated; contact the city at {search_url}."),
    }
    return step


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
    parcel = re.sub(r"[\s-]", "", s)
    if re.fullmatch(r"\d{10}", parcel):
        return "parcel", parcel
    # Bellevue-style: 23-127651-LP or 23 127651 LP
    if re.fullmatch(r"\d{2}[-\s]\d{6}[-\s][A-Z]{1,3}", s, re.IGNORECASE):
        return "permit", s
    # Seattle SDCI-style: 6145915-CN, 6001001-EL, 3001271-LU
    if re.fullmatch(r"\d{7}-[A-Z]{2}", s, re.IGNORECASE):
        return "permit", s
    # SeaTac LAMA: 2505-1208-ROW (YYMM-seq-TYPE)
    if re.fullmatch(r"\d{4}-\d{4}-[A-Z]{2,5}", s, re.IGNORECASE):
        return "permit", s
    # MBP-style: ADDC21-0275; EnerGov-style: B25000947, E26000458
    if re.match(r"[A-Z]{1,4}\d{2}[-\d]\d{3,6}$", s, re.IGNORECASE):
        return "permit", s
    # EnerGov Civic Access-style: FDM-2600855, ELEC-2025-08133, FIRE-2022-02703
    # (a letters-dash-digits token; real addresses always contain a space).
    if re.fullmatch(r"[A-Z]{1,6}-\d{3,8}(?:-\d{3,8})?", s, re.IGNORECASE):
        return "permit", s
    # Accela-style: ROW26100, BLD26076, TRE26036 (letters directly followed by
    # 4-8 digits, no space — cannot be a street address).
    if re.fullmatch(r"[A-Z]{2,5}\d{4,8}", s, re.IGNORECASE):
        return "permit", s
    return "address", s


def detect_city(address: str) -> str | None:
    """Try to extract a city name from the address string."""
    cities = (set(JURIS_BY_NAME) - {"king county"}) | set(SEPARATE_PORTALS)
    return detect_city_name(address, cities)


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
LNI_EARLIEST_DATE = datetime(2020, 1, 1)


def _months_before(value: datetime, months: int) -> datetime:
    """Shift a datetime backward by whole calendar months."""
    total_month = value.year * 12 + value.month - 1 - months
    year, month_zero = divmod(total_month, 12)
    month = month_zero + 1
    day = min(value.day, monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def lni_date_windows(now: datetime | None = None) -> list[tuple[str, str]]:
    """Build contiguous 13-month windows covering all available L&I data."""
    cursor = now or datetime.now()
    windows = []
    while cursor >= LNI_EARLIEST_DATE:
        start = max(_months_before(cursor, 13), LNI_EARLIEST_DATE)
        windows.append((start.strftime("%m/%d/%Y"), cursor.strftime("%m/%d/%Y")))
        cursor = start - timedelta(days=1)
    return windows


def _open_lni_session():
    """Open an L&I session and return its ASP.NET form state."""
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    resp = opener.open(urllib.request.Request(
        LNI_URL, headers={"User-Agent": "Mozilla/5.0"}
    ), timeout=15)
    html = resp.read().decode("utf-8", errors="replace")

    vs = re.search(r'id="__VIEWSTATE"[^>]*value="([^"]+)"', html)
    vsg = re.search(r'id="__VIEWSTATEGENERATOR"[^>]*value="([^"]+)"', html)
    ev = re.search(r'id="__EVENTVALIDATION"[^>]*value="([^"]+)"', html)
    if not vs or not ev:
        raise ValueError("permit search form is missing required state tokens")
    return opener, vs.group(1), vsg.group(1) if vsg else "", ev.group(1)


def search_lni(address: str, city: str = "") -> tuple[list[dict], list[str]]:
    """Search WA State L&I for electrical/manufactured-home permits.

    L&I defaults to a 13-month date range and no longer provides records
    purchased before 2020. Returns both permits and any source errors so a
    partial search cannot be mistaken for a complete empty result.
    """
    try:
        opener, cur_vs, cur_vsg, cur_ev = _open_lni_session()
    except Exception as e:
        return [], [f"Could not connect to permit search: {e}"]

    house, street = parse_address(address)
    # L&I docs: "enter only the house number in the site address field"
    site_addr = house if house else address.split(",")[0].strip()

    windows = lni_date_windows()

    all_results = []
    errors = []

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
            # The public ASP.NET session currently expires after several
            # searches. Retry this window once with fresh form state.
            try:
                opener, cur_vs, cur_vsg, cur_ev = _open_lni_session()
                form.update({
                    "__VIEWSTATE": cur_vs,
                    "__VIEWSTATEGENERATOR": cur_vsg,
                    "__EVENTVALIDATION": cur_ev,
                })
                req = urllib.request.Request(
                    LNI_URL,
                    data=urllib.parse.urlencode(form).encode("utf-8"),
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                result = opener.open(req, timeout=30).read().decode(
                    "utf-8", errors="replace"
                )
            except Exception as retry_error:
                errors.append(f"{beg}–{end}: {retry_error}")
                continue

        # Update viewstate for next request
        vs2 = re.search(r'id="__VIEWSTATE"[^>]*value="([^"]+)"', result)
        ev2 = re.search(r'id="__EVENTVALIDATION"[^>]*value="([^"]+)"', result)
        state_missing = not vs2 or not ev2
        if state_missing:
            errors.append(f"{beg}–{end}: response missing required state tokens")
        if vs2:
            cur_vs = vs2.group(1)
        if ev2:
            cur_ev = ev2.group(1)

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

        if state_missing:
            break

    return all_results, errors


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

# --- Jurisdiction from geometry (#39) --------------------------------------
# The mailing city on an address is not the permitting authority: "Kent, WA"
# addresses sit in unincorporated King County, and Milton/Pacific/Auburn
# straddle the Pierce line. So: geocode with King County's four-county locator
# (returns PIN + county for King/Pierce/Snohomish/Kitsap), then point-query the
# county's city-limits polygons for the real jurisdiction.
KC_FOURCOUNTY_GEOCODER_URL = (
    "https://gismaps.kingcounty.gov/arcgis/rest/services"
    "/Address/FourCounty_locator/GeocodeServer/findAddressCandidates"
)
KC_CITY_LIMITS_URL = (
    "https://gismaps.kingcounty.gov/arcgis/rest/services"
    "/Administration/KingCo_AdministrativeAreas/MapServer/2/query"
)
KC_STATE_PLANE_WKID = "2926"
# Polygon NAME → our routing key where they differ.
CITY_LIMITS_NAME_MAP = {"beaux arts": "beaux arts village"}
# Geocoder Addr_type → how much of the address was actually matched.
_MATCH_TYPE = {"PointAddress": "point", "Subaddress": "point",
               "Parcel": "point",          # parcel-address match carries the PIN (Vashon)
               "StreetAddress": "interpolated",
               "StreetName": "street", "Locality": "locality"}


def _arcgis_json(url: str, params: dict, timeout: int = 10) -> dict:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": "Mozilla/5.0"})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def resolve_location(address: str) -> dict | None:
    """Geocode an address and derive its permitting jurisdiction from the
    city-limits polygon it falls in. Returns None when the geocoder has no
    usable candidate (score < 80, or only a locality/street match with no
    point). Never raises."""
    def usable(c):
        # An exact address point is trustworthy at a lower score: the locator
        # docks points for a non-city locality ("Vashon WA" → 75) even when
        # the house number and street matched exactly.
        return (c.get("score", 0) >= 80
                or (c.get("score", 0) >= 70
                    and (c.get("attributes") or {}).get("Addr_type") == "PointAddress"))

    def geocode(q, n):
        data = _arcgis_json(KC_FOURCOUNTY_GEOCODER_URL, {
            "SingleLine": q, "outFields": "*",
            "outSR": KC_STATE_PLANE_WKID, "maxLocations": str(n), "f": "json"})
        return [c for c in data.get("candidates") or [] if usable(c)]

    try:
        cands = geocode(address, 1)
        if not cands and not re.search(r"\b(wa|washington)\b|\b\d{5}\b", address, re.I):
            # The locator needs a region hint. Without a city the same street
            # address can exist in several cities ("220 4th Ave S" is in Kent
            # and Edmonds), so take the top match only if it is unambiguous,
            # preferring King County — this is a King County tool.
            cands = geocode(f"{address}, WA", 5)
            if cands:
                top = cands[0].get("score", 0)
                points = [c for c in cands if c.get("score", 0) == top
                          and (c.get("attributes") or {}).get("Addr_type") == "PointAddress"]
                cities = {((c.get("attributes") or {}).get("City") or "",
                           (c.get("attributes") or {}).get("Subregion") or "") for c in points}
                if len(cities) > 1:
                    king = [c for c in points
                            if (c.get("attributes") or {}).get("Subregion") == "KING"]
                    king_cities = {(c.get("attributes") or {}).get("City") for c in king}
                    cands = king if len(king_cities) == 1 else []
    except Exception:
        return None
    if not cands:
        return None
    c = cands[0]
    a = c.get("attributes") or {}
    match_type = _MATCH_TYPE.get(a.get("Addr_type") or "", "none")
    if match_type in ("locality", "none"):
        return None
    county = (a.get("Subregion") or "").strip().lower() or None
    pin = re.sub(r"\D", "", str(a.get("PIN") or ""))
    house_in = (parse_address(address)[0] or "").strip()
    house_out = str(a.get("AddNum") or "").strip()
    loc = c.get("location") or {}
    out = {
        "matched_address": c.get("address"),
        "score": c.get("score"),
        "match_type": match_type,
        "partial_match": match_type != "point",
        "street_number_snapped": bool(house_in and house_out and house_in != house_out),
        "county": county,
        "parcel_id": f"{county}:{pin}" if county and len(pin) == 10 else None,
        "pin": pin if len(pin) == 10 else None,
        "geocoder_city": (a.get("City") or "").strip().title() or None,
        "jurisdiction": None,
        "jurisdiction_basis": None,
        "unincorporated": None,
    }
    # City-limits polygon → jurisdiction. The layer covers all four counties.
    if loc.get("x") is not None and loc.get("y") is not None:
        try:
            feats = _arcgis_json(KC_CITY_LIMITS_URL, {
                "geometry": f"{loc['x']},{loc['y']}",
                "geometryType": "esriGeometryPoint", "inSR": KC_STATE_PLANE_WKID,
                "spatialRel": "esriSpatialRelIntersects", "outFields": "NAME,UNINC",
                "returnGeometry": "false", "f": "json"}).get("features") or []
        except Exception:
            feats = []
        if feats:
            attrs = feats[0].get("attributes") or {}
            name = (attrs.get("NAME") or "").strip()
            out["unincorporated"] = bool(attrs.get("UNINC"))
            out["jurisdiction"] = name
            out["jurisdiction_basis"] = "city-limits"
    if out["jurisdiction"] is None and out["geocoder_city"]:
        out["jurisdiction"] = out["geocoder_city"]
        out["jurisdiction_basis"] = "geocoder-city"
    return out


def jurisdiction_city_key(loc: dict | None) -> str | None:
    """Our lowercase routing key for a resolved location's city, or None when
    the address is unincorporated or outside the cities we know."""
    if not loc or not loc.get("jurisdiction") or loc.get("unincorporated"):
        return None
    key = " ".join(loc["jurisdiction"].lower().split())
    return CITY_LIMITS_NAME_MAP.get(key, key)


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


# Official Bellevue Open Data permit layer. The city publishes a live snapshot
# of its permitting system from 1998 onward and refreshes it daily.
BELLEVUE_PERMITS_URL = (
    "https://services1.arcgis.com/EYzEZbDhXZjURPbP/arcgis/rest/services"
    "/Bellevue_Permits/FeatureServer/0/query"
)
BELLEVUE_OPEN_DATA = "https://data.bellevuewa.gov/"


def _arcgis_date(raw: int | None) -> str | None:
    if raw is None:
        return None
    return datetime.fromtimestamp(raw / 1000).strftime("%Y-%m-%d")


def _sql_string(value: str) -> str:
    """Quote user text for an ArcGIS standardized SQL string literal."""
    return value.replace("'", "''")


def search_bellevue(input_type: str, value: str) -> list[dict] | str:
    """Search Bellevue's official daily Open Data permit snapshot."""
    if input_type == "permit":
        permit_number = re.sub(r"[-\s]+", " ", value.strip().upper())
        where = f"PERMITNUMBER = '{_sql_string(permit_number)}'"
    elif input_type == "parcel":
        parcel = re.sub(r"\D", "", value)
        where = f"PARCELNUMBER = '{parcel}'"
    else:
        house, street = parse_address(value)
        address = f"{house} {street}".strip().upper()
        if not address:
            return []
        where = f"UPPER(SITEADDRESS) LIKE '{_sql_string(address)}%'"

    fields = ",".join([
        "PERMITNUMBER", "PERMITTYPE", "PERMITTYPEDESCRIPTION",
        "SITEADDRESS", "CITY", "STATE", "ZIPCODE", "PERMITSTATUS",
        "PROJECTNAME", "PROJECTDESCRIPTION", "APPLIEDDATE", "ISSUEDDATE",
        "FINALEDDATE", "EXPIREDATE", "MBPSTATUSSITE",
    ])
    permits = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            "where": where,
            "outFields": fields,
            "returnGeometry": "false",
            "orderByFields": "APPLIEDDATE DESC",
            "resultOffset": offset,
            "resultRecordCount": 1000,
            "f": "json",
        })
        try:
            req = urllib.request.Request(
                BELLEVUE_PERMITS_URL + "?" + params,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            data = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        except Exception as e:
            return f"Error: {e}"

        if data.get("error"):
            return f"Error: {data['error'].get('message', 'Bellevue search failed')}"

        features = data.get("features") or []
        for feature in features:
            raw = feature.get("attributes") or {}
            address = " ".join(filter(None, [
                raw.get("SITEADDRESS"), raw.get("CITY"),
                raw.get("STATE"), raw.get("ZIPCODE"),
            ]))
            permits.append({
                "permit_number": raw.get("PERMITNUMBER") or "",
                "type": raw.get("PERMITTYPEDESCRIPTION") or raw.get("PERMITTYPE") or "",
                "status": raw.get("PERMITSTATUS") or "",
                "description": raw.get("PROJECTDESCRIPTION") or raw.get("PROJECTNAME") or "",
                "address": address,
                "jurisdiction": "Bellevue",
                "applied_date": _arcgis_date(raw.get("APPLIEDDATE")),
                "issued_date": _arcgis_date(raw.get("ISSUEDDATE")),
                "finaled_date": _arcgis_date(raw.get("FINALEDDATE")),
                "expires_date": _arcgis_date(raw.get("EXPIREDATE")),
                "portal": raw.get("MBPSTATUSSITE") or BELLEVUE_OPEN_DATA,
            })

        if not data.get("exceededTransferLimit") or not features:
            break
        offset += len(features)

    return permits


# Seattle publishes four complementary SDCI datasets through its official
# Socrata Open Data API. Their useful permit fields share one common schema.
SEATTLE_OPEN_DATA = "https://data.seattle.gov"
SEATTLE_PERMIT_DATASETS = {
    "Building": "76t5-zqzr",
    "Electrical": "c4tj-daue",
    "Trade": "c87v-5hwh",
    "Land Use": "ht3q-kdvx",
}
SEATTLE_PAGE_SIZE = 1000


def _socrata_string(value: str) -> str:
    """Escape a value for a Socrata SoQL string literal."""
    return value.replace("'", "''")


def _seattle_permit(raw: dict, source: str) -> dict:
    """Normalize one Seattle Open Data record to the shared permit schema."""
    link = raw.get("link")
    if isinstance(link, dict):
        link = link.get("url")
    address = " ".join(filter(None, [
        raw.get("originaladdress1"),
        raw.get("originalcity"),
        raw.get("originalstate"),
        raw.get("originalzip"),
    ]))
    return {
        "permit_number": raw.get("permitnum") or "",
        "type": (
            raw.get("permittypedesc")
            or raw.get("permittype")
            or raw.get("permittypemapped")
            or raw.get("permitclassmapped")
            or source
        ),
        "status": raw.get("statuscurrent") or "",
        "description": raw.get("description") or "",
        "address": address,
        "jurisdiction": "Seattle SDCI",
        "applied_date": _iso_date(raw.get("applieddate")),
        "issued_date": _iso_date(raw.get("issueddate")),
        "finaled_date": _iso_date(raw.get("completeddate")),
        "expires_date": _iso_date(raw.get("expiresdate")),
        "portal": link or SEATTLE_OPEN_DATA,
    }


def search_seattle(input_type: str, value: str) -> tuple[list[dict], list[str]]:
    """Search Seattle's official building, electrical, trade, and land-use data.

    Address searches use the source's normalized site-address prefix. Exact
    permit searches query all four datasets because the suffix identifies the
    permit class but not a source contract we control. Each dataset is isolated
    so partial results remain useful when another source is unavailable.
    """
    if input_type == "permit":
        permit_number = value.strip().upper()
        where = f"upper(permitnum) = '{_socrata_string(permit_number)}'"
    elif input_type == "address":
        house, street = parse_address(value)
        if not house or not street:
            return [], ["Address requires a house number and street name"]
        address = f"{house} {street}".strip().upper()
        where = f"upper(originaladdress1) like '{_socrata_string(address)}%'"
    else:
        return [], []

    permits = []
    errors = []
    for source, dataset_id in SEATTLE_PERMIT_DATASETS.items():
        offset = 0
        while True:
            params = urllib.parse.urlencode({
                "$where": where,
                "$order": "applieddate DESC, permitnum",
                "$limit": SEATTLE_PAGE_SIZE,
                "$offset": offset,
            })
            url = f"{SEATTLE_OPEN_DATA}/resource/{dataset_id}.json?{params}"
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                payload = json.loads(
                    urllib.request.urlopen(req, timeout=30).read().decode()
                )
                if not isinstance(payload, list):
                    message = (
                        payload.get("message", "unexpected response")
                        if isinstance(payload, dict)
                        else "unexpected response"
                    )
                    raise ValueError(message)
            except Exception as error:
                errors.append(f"{source}: {error}")
                break

            permits.extend(_seattle_permit(raw, source) for raw in payload)
            if len(payload) < SEATTLE_PAGE_SIZE:
                break
            offset += len(payload)

    return permits, errors


# Shoreline runs CentralSquare eTRAKiT (ASP.NET WebForms + Telerik RadGrid).
# The public permit search is unauthenticated. Its Export-to-Excel action
# returns every matching row as CSV in a single request, so we avoid paging the
# grid. Status/description are only on the per-permit detail page (a postback,
# not a GET), so those fields come back empty from the search/export.
SHORELINE_ETRAKIT = "https://permits.shorelinewa.gov/eTRAKiT"
SHORELINE_SEARCH_URL = SHORELINE_ETRAKIT + "/Search/permit.aspx"
SHORELINE_SEARCH_BY = {
    "permit": "Permit_Main.PERMIT_NO",
    "parcel": "Permit_Main.SITE_APN",
    "address": "Permit_Main.SITE_ADDR",
}


def _shoreline_date(raw: str | None) -> str | None:
    """Convert eTRAKiT's MM/DD/YYYY date to YYYY-MM-DD."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_shoreline_csv(body: str) -> list[dict]:
    """Normalize the eTRAKiT CSV export into the shared permit schema."""
    rows = list(csv.reader(io.StringIO(body)))
    if not rows:
        return []
    header = [h.strip().upper() for h in rows[0]]
    index = {name: i for i, name in enumerate(header)}

    def col(row: list[str], name: str) -> str:
        i = index.get(name)
        return row[i].strip() if i is not None and i < len(row) else ""

    permits = []
    for row in rows[1:]:
        if not any(cell.strip() for cell in row):
            continue
        permits.append({
            "permit_number": col(row, "PERMIT NUMBER"),
            "type": col(row, "PERMIT TYPE"),
            "status": "",          # not exposed by eTRAKiT search/export
            "description": "",      # detail page only
            "address": col(row, "ADDRESS"),
            "jurisdiction": "Shoreline",
            "applied_date": _shoreline_date(col(row, "APPLIED DATE")),
            "issued_date": _shoreline_date(col(row, "ISSUED DATE")),
            "finaled_date": None,
            "expires_date": None,
            "portal": SHORELINE_ETRAKIT + "/",
        })
    return permits


def search_shoreline(input_type: str, value: str) -> tuple[list[dict], list[str]]:
    """Search Shoreline's public eTRAKiT portal.

    eTRAKiT is ASP.NET WebForms: GET the search page for a fresh __VIEWSTATE,
    then POST the query with the grid's Export-to-Excel action, which returns
    all matching rows as CSV in one request (no pagination). Returns the shared
    (permits, errors) shape.
    """
    search_by = SHORELINE_SEARCH_BY.get(input_type)
    if not search_by:
        return [], []
    if input_type == "parcel":
        term, oper = re.sub(r"\D", "", value), "EQUALS"
    elif input_type == "permit":
        term, oper = value.strip().upper(), "EQUALS"
    else:
        house, street = parse_address(value)
        if not house or not street:
            return [], ["Address requires a house number and street name"]
        term, oper = f"{house} {street}".upper(), "CONTAINS"
    if not term:
        return [], []

    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj))
        page = opener.open(urllib.request.Request(
            SHORELINE_SEARCH_URL, headers={"User-Agent": "Mozilla/5.0"}),
            timeout=30).read().decode("utf-8", "replace")

        def hidden(name: str) -> str:
            match = re.search(
                r'id="%s"[^>]*value="([^"]*)"' % re.escape(name), page)
            return match.group(1) if match else ""

        form = {
            "__EVENTTARGET": "", "__EVENTARGUMENT": "",
            "__VIEWSTATE": hidden("__VIEWSTATE"),
            "__VIEWSTATEGENERATOR": hidden("__VIEWSTATEGENERATOR"),
            "ctl00$cplMain$ddSearchBy": search_by,
            "ctl00$cplMain$ddSearchOper": oper,
            "ctl00$cplMain$txtSearchString": term,
            "ctl00$cplMain$hfActivityMode": "",
            "ctl00$cplMain$btnExportToExcel": "Export to Excel",
        }
        resp = opener.open(urllib.request.Request(
            SHORELINE_SEARCH_URL,
            data=urllib.parse.urlencode(form).encode(),
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": SHORELINE_SEARCH_URL,
            }), timeout=45)
        ctype = resp.headers.get("Content-Type", "")
        body = resp.read().decode("utf-8-sig", "replace")
    except Exception as error:
        return [], [str(error)]

    # A search with no matches re-renders the grid page (HTML) rather than
    # returning a CSV file; that is simply zero results, not an error.
    if "csv" not in ctype.lower():
        return [], []
    return _parse_shoreline_csv(body), []


# Tyler EnerGov "Civic Access" self-service portals. Same search contract as
# the Renton EnerGov integration (tenant headers + a criteria-template body),
# but Tyler-hosted with a per-city tenant, and scoped to the Permit module via
# FilterModule=2. Public/unauthenticated. Add a city by capturing its {host,
# tenant} once and dropping it in here — no new code.
CIVIC_ACCESS_PORTALS = {
    "redmond": {
        "host": "cityofredmondwa-energovweb.tylerhost.net",
        "tenant": "RedmondWA Prod",
    },
}
CIVIC_ACCESS_MAX_PAGES = 8  # 25/page; safety cap for broad street matches


def _civicaccess_date(raw: str | None) -> str | None:
    """ISO datetime -> YYYY-MM-DD. EnerGov placeholder years (<1902) -> None."""
    if not raw or not isinstance(raw, str):
        return None
    iso = raw[:10]
    return None if iso[:4] in ("0001", "1900", "1901") else iso


def _civicaccess_permit(row: dict, city: str, portal_url: str) -> dict:
    """Normalize one Civic Access permit record to the shared schema."""
    address = row.get("Address") or {}
    return {
        "permit_number": row.get("CaseNumber") or "",
        "type": row.get("CaseType") or "",
        "status": row.get("CaseStatus") or "",
        "description": row.get("Description") or "",
        "address": (address.get("FullAddress")
                    or row.get("AddressDisplay") or "").strip(),
        "jurisdiction": city.title(),
        "applied_date": _civicaccess_date(row.get("ApplyDate")),
        "issued_date": _civicaccess_date(row.get("IssueDate")),
        "finaled_date": _civicaccess_date(
            row.get("FinalDate") or row.get("CompleteDate")),
        "expires_date": _civicaccess_date(row.get("ExpireDate")),
        "portal": portal_url,
    }


def search_energov_civicaccess(city: str, input_type: str,
                               value: str) -> tuple[list[dict], list[str]]:
    """Search a Tyler EnerGov Civic Access portal's permit records.

    Mirrors the Renton EnerGov flow: GET the criteria template, then POST a
    keyword search scoped to the Permit module (SearchModule=1, FilterModule=2)
    with the city's tenant headers. ExactMatch keeps address/parcel/permit
    lookups precise. Returns the shared (permits, errors) shape.
    """
    portal = CIVIC_ACCESS_PORTALS.get(city)
    if not portal:
        return [], []
    if input_type == "address":
        house, street = parse_address(value)
        if not house or not street:
            return [], ["Address requires a house number and street name"]
        keyword = f"{house} {street}"
    elif input_type == "parcel":
        keyword = re.sub(r"\D", "", value)
    else:  # permit
        keyword = value.strip()
    if not keyword:
        return [], []

    host = portal["host"]
    base = f"https://{host}/apps/selfservice/api/energov/search"
    portal_url = f"https://{host}/apps/selfservice#/search"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json;charset=UTF-8",
        "tenantId": "1",
        "tenantName": portal["tenant"],
        "Tyler-TenantUrl": portal["tenant"],
        "Tyler-Tenant-Culture": "en-US",
        "Referer": f"https://{host}/apps/selfservice",
    }

    def api(path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path, data=data, headers=headers,
            method="POST" if data else "GET")
        return json.loads(
            urllib.request.urlopen(req, timeout=30).read().decode())

    try:
        template = api("/criteria")["Result"]
    except Exception as error:
        return [], [str(error)]

    def fetch_page(page):
        criteria = dict(template)
        criteria.update({
            "Keyword": keyword, "ExactMatch": True,
            "SearchModule": 1, "FilterModule": 2,  # permit module only
            "PageNumber": page, "PageSize": 25,
            "SortBy": None, "SortAscending": False,
        })
        return api("/search", criteria)["Result"]

    # Fetch page 1 to learn the page count, then pull the rest concurrently —
    # pagination is otherwise the long pole for a property with many records.
    errors = []
    try:
        first = fetch_page(1)
    except Exception as error:
        return [], [str(error)]
    total_pages = first.get("TotalPages") or 1
    last_page = min(total_pages, CIVIC_ACCESS_MAX_PAGES)
    results_by_page = {1: first}
    if last_page > 1:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(7, last_page - 1)) as pool:
            futures = {pool.submit(fetch_page, p): p
                       for p in range(2, last_page + 1)}
            for fut in concurrent.futures.as_completed(futures):
                page = futures[fut]
                try:
                    results_by_page[page] = fut.result()
                except Exception as error:
                    errors.append(str(error))

    permits = []
    for page in range(1, last_page + 1):
        result = results_by_page.get(page)
        if result:
            permits.extend(_civicaccess_permit(r, city, portal_url)
                           for r in (result.get("EntityResults") or []))
    if total_pages > CIVIC_ACCESS_MAX_PAGES:
        errors.append(
            f"showing first {CIVIC_ACCESS_MAX_PAGES * 25} of "
            f"{total_pages * 25}+ matches — narrow the search")
    return permits, errors


# Accela Citizen Access portals (aca-prod.accela.com). The public "global
# search" is a plain GET returning an HTML grid — no session or VIEWSTATE.
# Reusable across agencies via config. The "kingco" agency is King County's
# own system for *unincorporated* addresses — it carries pre-MBP history,
# enforcement and electrical records MBP-KC lacks (Vashon: +8–10 per address).
# It does NOT serve Black Diamond (0 rows for every BD address/permit; #47) —
# Black Diamond runs its own PermitTrax "Citizen's Connect" portal (#18).
ACCELA_HOST = "https://aca-prod.accela.com"
ACCELA_PORTALS = {            # city -> Accela agency code
    "woodinville": "WOODINVILLE",
}
ACCELA_UNINCORPORATED_AGENCY = "kingco"
ACCELA_AGENCY_LABEL = {"WOODINVILLE": "Woodinville", "kingco": "King County"}


def _accela_date(raw: str | None) -> str | None:
    """Accela's MM/DD/YYYY grid date -> YYYY-MM-DD."""
    raw = (raw or "").strip()
    try:
        return datetime.strptime(raw, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _accela_rows(html: str) -> list[list[str]]:
    """Extract permit-grid data rows (cell lists) from a results page.

    Column layout varies per agency, but every data row starts with the record
    Date and the last cell is the Status, so callers map by anchors, not fixed
    positions.
    """
    idx = html.find("gdvPermitList")
    seg = html[idx:idx + 60000] if idx >= 0 else html
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S):
        if not re.search(r"\d{2}/\d{2}/\d{4}", tr):
            continue
        cells = [
            re.sub(r"\s+", " ", html_unescape(re.sub(r"<[^>]+>", " ", c))).strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        ]
        if len(cells) >= 3 and re.match(r"\d{2}/\d{2}/\d{4}", cells[0]):
            rows.append(cells)
    return rows


def _accela_permit(cells: list[str], jurisdiction: str, portal_url: str) -> dict:
    """Normalize one Accela grid row to the shared schema (anchor-based)."""
    number = cells[1] if len(cells) > 1 else ""
    ctype = cells[2] if len(cells) > 2 else ""
    status = cells[-1].strip() if len(cells) > 3 else ""
    address = ""
    for c in cells[3:]:
        if re.search(r"\b[A-Z]{2}\s+\d{5}", c) or ", WA" in c.upper():
            address = re.sub(r"\s+", " ", c).strip()
            break
    description = cells[3].strip() if (
        len(cells) > 4 and cells[3] not in (address, status)) else ""
    return {
        "permit_number": number,
        "type": ctype,
        "status": status,
        "description": description,
        "address": address,
        "jurisdiction": jurisdiction,
        "applied_date": _accela_date(cells[0]),
        "issued_date": None,
        "finaled_date": None,
        "expires_date": None,
        "portal": portal_url,
    }


def search_accela(agency: str, input_type: str, value: str,
                  jurisdiction: str) -> tuple[list[dict], list[str]]:
    """Search an Accela Citizen Access agency via its public global search.

    A single GET to GlobalSearchResults.aspx?QueryText=<term> returns an HTML
    grid — no session/VIEWSTATE. Returns the shared (permits, errors) shape.
    """
    if input_type == "address":
        house, street = parse_address(value)
        if not house or not street:
            return [], ["Address requires a house number and street name"]
        query = f"{house} {street}"
    elif input_type == "parcel":
        query = re.sub(r"\D", "", value)
    else:  # permit
        query = value.strip()
    if not query:
        return [], []

    url = (f"{ACCELA_HOST}/{agency}/Cap/GlobalSearchResults.aspx"
           f"?isNewQuery=yes&QueryText={urllib.parse.quote(query)}")
    portal_url = f"{ACCELA_HOST}/{agency}/Cap/CapHome.aspx?module=DevelopmentServices"
    try:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        body = opener.open(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as error:
        return [], [str(error)]

    rows = _accela_rows(body)
    permits = [_accela_permit(c, jurisdiction, portal_url) for c in rows]
    errors = []
    total = re.search(r"Showing\s+1-\d+\s+of\s+(\d+)", body)
    if rows and total and int(total.group(1)) > len(rows):
        errors.append(f"showing first {len(rows)} matches — narrow the search")
    return permits, errors


# SmartGov public portals (Paladin Data Systems / Granicus GXA). The basic
# search is a full-page POST to /ApplicationPublic/ApplicationSearch/Search with
# a `query` keyword and a client-generated `__submitFormValidator__` token — an
# obfuscated current-timestamp (see _smartgov_token). Results render server-side
# as `search-result-item` cards. Reusable across SmartGov cities via config.
SMARTGOV_PORTALS = {          # city -> smartgovcommunity.com subdomain
    "normandy park": "ci-normandypark-wa",
    "carnation": "ci-carnation-wa",
}
SMARTGOV_FIELDS = "_conv\tquery\tSearch\t_applicationSearchPage\t__submitFormValidator__"


def _smartgov_token() -> str:
    """Reproduce FormSupport.requestVerificationString(): a random char, the
    current epoch-ms, and a random char, with 3 more random a-z chars spliced in
    at a random position. The server decodes the embedded timestamp."""
    def rc():
        return chr(random.randint(97, 122))  # a-z, per randomChar()
    val = rc() + str(int(datetime.now().timestamp() * 1000)) + rc()
    middle = rc() + rc() + rc()
    pos = random.randint(0, 9)
    return val[:pos] + middle + val[pos:]


def _smartgov_date(raw: str | None) -> str | None:
    """SmartGov's M/D/YYYY status date -> YYYY-MM-DD."""
    try:
        return datetime.strptime((raw or "").strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _parse_smartgov(html: str, city: str, base: str) -> list[dict]:
    """Normalize SmartGov result cards to the shared permit schema."""
    permits = []
    for item in re.findall(
            r'<div class="search-result-item">(.*?)</article>', html, re.S):
        title = re.search(
            r"submitAction\(\s*'Detail/([a-f0-9-]+)'\s*\)[^>]*>\s*([^<]+)", item)
        if not title:
            continue
        guid, number = title.group(1), title.group(2).strip()
        texts = []
        for raw in re.findall(r"<div[^>]*>(.*?)</div>", item, re.S):
            txt = re.sub(r"\s+", " ", html_unescape(re.sub(r"<[^>]+>", "", raw))).strip()
            if txt and txt != number and txt not in texts:
                texts.append(txt)
        ptype = texts[0] if texts else ""
        # Status shows as "<Status>, M/D/YYYY" (the status-transition date — the
        # only date in the search view; used as applied_date for sorting).
        status, date = "", None
        for txt in texts:
            m = re.match(r"(.+?),\s*(\d{1,2}/\d{1,2}/\d{4})$", txt)
            if m:
                status, date = m.group(1).strip(), _smartgov_date(m.group(2))
                break
        address = ""
        for i, txt in enumerate(texts):
            if re.search(r",\s*WA\b", txt):
                address = (f"{texts[i - 1]}, {txt}" if i else txt).strip(", ")
                break
        permits.append({
            "permit_number": number,
            "type": ptype,
            "status": status,
            "description": "",
            "address": address,
            "jurisdiction": city.title(),
            "applied_date": date,
            "issued_date": None,
            "finaled_date": None,
            "expires_date": None,
            "portal": f"{base}/ApplicationPublic/ApplicationSearch/Detail/{guid}",
        })
    return permits


def search_smartgov(city: str, input_type: str,
                    value: str) -> tuple[list[dict], list[str]]:
    """Search a SmartGov city's public portal via its keyword search.

    GET the search page for a session cookie + `_conv`, then POST the keyword
    with a freshly generated validator token. Returns (permits, errors).
    """
    host = SMARTGOV_PORTALS.get(city)
    if not host:
        return [], []
    if input_type == "address":
        house, street = parse_address(value)
        if not house or not street:
            return [], ["Address requires a house number and street name"]
        query = f"{house} {street}"
    elif input_type == "parcel":
        query = re.sub(r"\D", "", value)
    else:  # permit
        query = value.strip()
    if not query:
        return [], []

    base = f"https://{host}.smartgovcommunity.com"
    path = "/ApplicationPublic/ApplicationSearch/Search"
    headers = {"User-Agent": "Mozilla/5.0",
               "Accept": "text/html,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.9"}
    try:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        page = opener.open(urllib.request.Request(base + path, headers=headers),
                           timeout=30).read().decode("utf-8", "replace")
        conv = (re.search(r'name="_conv"[^>]*value="([^"]*)"', page)
                or [0, "1"])[1]
        form = {
            "_conv": conv, "query": query, "_applicationSearchPage": "0",
            "__submitFormValidator__": _smartgov_token(),
            "_fields": SMARTGOV_FIELDS,
        }
        req = urllib.request.Request(
            base + path, data=urllib.parse.urlencode(form).encode(),
            headers={**headers,
                     "Content-Type": "application/x-www-form-urlencoded",
                     "Referer": base + path})
        body = opener.open(req, timeout=30).read().decode("utf-8", "replace")
    except Exception as error:
        return [], [str(error)]
    return _parse_smartgov(body, city, base), []


# --- LAMA (SeaTac) --------------------------------------------------------
# ASP.NET WebForms. One full-page POST with the page's VIEWSTATE and
# __EVENTTARGET=ctl00$btnSearch returns result cards; the filter/sort/page-size
# dropdowns can ride along on that same POST. Forward paging (btnFwd) does not
# replay outside the browser's UpdatePanel, so we take the largest page (200)
# and flag truncation. Parcel search is not supported by the portal.
LAMA_PORTALS = {"seatac": "https://lama.seatacwa.gov"}
_CITY_DISPLAY = {"seatac": "SeaTac"}


def display_city(key: str) -> str:
    """Routing key → display name ('seatac' → 'SeaTac', else Title Case)."""
    return _CITY_DISPLAY.get(key.lower(), key.title())
LAMA_PAGE_SIZE = 200
_LAMA_HIDDEN = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")


def _lama_date(raw: str | None) -> str | None:
    """'5/22/2025 1:12:58 PM' or '5/22/2025' → YYYY-MM-DD."""
    if not raw:
        return None
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", raw)
    if not m:
        return None
    mo, d, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def _lama_hidden(page: str) -> dict:
    out = {}
    for k in _LAMA_HIDDEN:
        m = (re.search(r'id="%s"[^>]*value="([^"]*)"' % k, page)
             or re.search(r'value="([^"]*)"[^>]*id="%s"' % k, page))
        if m:
            out[k] = m.group(1)
    return out


def _lama_field(card: str, label: str) -> str:
    m = re.search(r"<strong>\s*%s:?:?\s*</strong>\s*(.*?)\s*</div>" % re.escape(label), card, re.S)
    return html_unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""


def _parse_lama(body: str, city: str, base: str) -> tuple[list[dict], int | None]:
    """Result cards → shared schema. Returns (permits, total_count)."""
    m = re.search(r'id="MainContent_ctl00_countAll" value="(\d+)"', body)
    total = int(m.group(1)) if m else None
    permits = []
    cards = re.split(r"<div class='card shadow-sm os-list-card", body)[1:]
    for card in cards:
        title = re.search(r'<h5 class="card-title[^"]*">(.*?)</h5>', card, re.S)
        if not title:
            continue
        parts = [" ".join(html_unescape(re.sub(r"<[^>]+>", " ", p)).split())
                 for p in re.split(r"""<span class=['"]dot['"]>.*?</span>""", title.group(1))]
        number = next((p.split("#", 1)[1].strip() for p in parts if p.startswith("Permit #")), "")
        if not number:
            continue
        address = parts[0] if parts else ""
        ptype = parts[1] if len(parts) > 1 and not parts[1].startswith("Permit #") else ""
        item = re.search(r"Redirect\.aspx\?module=permits&(?:amp;)?ItemID=(\d+)", card)
        permits.append({
            "permit_number": number,
            "type": ptype or _lama_field(card, "Type"),
            "status": _lama_field(card, "Status"),
            "description": _lama_field(card, "Description"),
            "address": address,
            "jurisdiction": display_city(city),
            "applied_date": _lama_date(_lama_field(card, "Date Filed")),
            "issued_date": None,
            "finaled_date": _lama_date(_lama_field(card, "Final Date")),
            "expires_date": _lama_date(_lama_field(card, "Expires")),
            "portal": (f"{base}/Redirect.aspx?module=permits&ItemID={item.group(1)}&view=true"
                       if item else f"{base}/Search.aspx"),
        })
    return permits, total


def search_lama(city: str, input_type: str, value: str) -> tuple[list[dict], list[str]]:
    """Search a LAMA portal (SeaTac). Address → house + street keyword;
    permit → exact number. Parcel is unsupported (returns nothing, no error)."""
    if input_type == "parcel":
        return [], []
    base = LAMA_PORTALS[city]
    url = f"{base}/Search.aspx"
    if input_type == "address":
        house, street = parse_address(value)
        if not house or not street:
            return [], ["Address requires a house number and street name"]
        term = f"{house} {street}"
    else:
        term = value.strip()
    ua = {"User-Agent": "Mozilla/5.0"}
    try:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        page = opener.open(urllib.request.Request(url, headers=ua), timeout=30).read().decode("utf-8", "replace")
        hidden = _lama_hidden(page)
        if "__VIEWSTATE" not in hidden:
            return [], ["LAMA search page did not expose VIEWSTATE"]
        form = {
            "__EVENTTARGET": "ctl00$btnSearch", "__EVENTARGUMENT": "",
            "ctl00$tbSearch": term,
            "ctl00$MainContent$ctl00$ddFilter": "permits",
            "ctl00$MainContent$ctl00$ddSort": "filed-desc",
            "ctl00$MainContent$ctl00$ddPage": str(LAMA_PAGE_SIZE),
            **hidden,
        }
        req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(),
                                     headers={**ua, "Referer": url,
                                              "Content-Type": "application/x-www-form-urlencoded"})
        body = opener.open(req, timeout=60).read().decode("utf-8", "replace")
    except Exception as error:
        return [], [str(error)]
    permits, total = _parse_lama(body, city, base)
    errors = []
    if total and total > LAMA_PAGE_SIZE:
        errors.append(f"showing first {LAMA_PAGE_SIZE} of {total} matches — narrow the search")
    return permits, errors


CITE_AS = "King County Permit Status (github.com/chaoz23/king-county-permit-status)"

# Status words that mean a permit is no longer active, across every vendor
# vocabulary we normalize (MBP, EnerGov, Accela, SmartGov, Socrata, ArcGIS, L&I).
_CLOSED_STATUS = re.compile(
    r"final|complet|closed|expired|withdrawn|cancel|void|denied|revoked|"
    r"abandon|inactive|rejected", re.I)


def is_open_status(status: str | None, finaled_date: str | None = None) -> bool | None:
    """Normalize a vendor status string to open/closed. None when unknown
    (eTRAKiT exports carry no status)."""
    if finaled_date:
        return False
    if not status or not status.strip():
        return None
    if re.search(r"incomplete|expiration notice", status, re.I):
        return True     # "Application Incomplete" / MBP "Expiration Notice" are still live
    return not _CLOSED_STATUS.search(status)


def record_url(permit: dict) -> str | None:
    """The per-record URL when `portal` points at this permit rather than a
    search page (Seattle, Bellevue, SmartGov detail GUIDs); else None."""
    url = permit.get("portal") or ""
    if not url:
        return None
    # MBP encodes "26 120953 FA" as "26%20120953%20FA" — compare alphanumerics only
    squash = lambda s: re.sub(r"[^a-z0-9]", "", urllib.parse.unquote(s).lower())
    number = squash(permit.get("permit_number") or "")
    if (re.search(r"/Detail/|PermitDetails/|[?&]ItemID=\d+", url)
            or (number and number in squash(url))):
        return url
    return None


def parcel_id(pin: str | None) -> str | None:
    """County-namespaced parcel id (`king:7222000353`), so parcels stay
    unambiguous once Pierce-straddling cities (Milton, Pacific, Auburn) are in
    the mix."""
    digits = re.sub(r"\D", "", pin or "")
    return f"king:{digits}" if len(digits) == 10 else None


def enrich_permit(permit: dict) -> dict:
    """Add the provenance/convenience fields agents act on (additive)."""
    permit["is_open"] = is_open_status(permit.get("status"), permit.get("finaled_date"))
    permit["record_url"] = record_url(permit)
    return permit


def lookup(raw_input: str) -> dict:
    """Core lookup. Returns unified result for human + agent."""
    if not raw_input.strip():
        return {
            "action": "reject",
            "permit_count": 0,
            "searched": [],
            "permits": [],
            "input": raw_input,
            "message": "Query must not be blank.",
        }

    input_type, value = detect_input_type(raw_input)
    city = detect_city(raw_input)
    resolved_parcel = value if input_type == "parcel" else None
    location = None
    jurisdiction_basis = "address-text" if city else None
    if input_type == "address":
        # Polygon beats mailing city (#39): a "Kent, WA" address can be
        # unincorporated King County, and the text city can simply be absent.
        location = resolve_location(value)
        poly_city = jurisdiction_city_key(location)
        known = (set(JURIS_BY_NAME) - {"king county"}) | set(SEPARATE_PORTALS)
        if location and location.get("unincorporated") and location.get("county") == "king":
            city = None                      # unincorporated KC: county sources only
            jurisdiction_basis = "city-limits"
        elif poly_city in known:
            city = poly_city
            jurisdiction_basis = location["jurisdiction_basis"]
        if location and location.get("pin") and location.get("county") == "king":
            resolved_parcel = location["pin"]
    unincorporated_kc = bool(location and location.get("unincorporated")
                             and location.get("county") == "king")

    opener = None
    token = None
    mbp_connection_error = None
    try:
        opener, token = get_session()
    except Exception as e:
        # MyBuildingPermit is only one source. Keep searching independent
        # sources such as EnerGov instead of failing the whole lookup.
        mbp_connection_error = f"Could not connect to MyBuildingPermit: {e}"

    all_permits = []
    searched_jurisdictions = []
    errors = []
    separate_portal_note = None
    if mbp_connection_error:
        errors.append(mbp_connection_error)

    def search_mbp(*args, **kwargs):
        if opener is None or token is None:
            return None
        return search_permits(opener, token, *args, **kwargs)

    if input_type in ("permit", "parcel"):
        # Permit and parcel searches have no city to route on, so they fan out
        # to every source. The sources are independent (each manages its own
        # session; only t_mbp touches the shared MBP opener, and it runs alone),
        # so run them concurrently and aggregate in a fixed order — same results,
        # a fraction of the wall-clock.
        exact = input_type == "permit"

        def t_mbp():
            s, p, er = [], [], []
            if input_type == "permit":
                for jid, jname in JURISDICTIONS.items():
                    r = search_mbp(jid, search_by="PermitNumber",
                                   permit_number=value)
                    if r is None:
                        break
                    if isinstance(r, list) and r:
                        p.extend(r)
                        s.append(jname)
                        break  # permit numbers are unique
                    elif isinstance(r, str):
                        er.append(f"{jname}: {r}")
            else:
                for jid in JURISDICTIONS:
                    r = search_mbp(jid, parcel=value)
                    if r is None:
                        continue
                    jname = JURISDICTIONS.get(jid, jid)
                    s.append(jname)
                    if isinstance(r, list):
                        p.extend(r)
                    elif isinstance(r, str):
                        er.append(f"{jname}: {r}")
            return s, p, er

        def t_energov():
            s, p = [], []
            for portal_key in ENERGOV_PORTALS:
                eg = search_energov(portal_key, value, exact=exact)
                if eg:
                    p.extend(eg)
                    s.append(f"{portal_key.title()} (EnerGov)")
                elif input_type == "permit":
                    s.append(f"{portal_key.title()} (EnerGov — not found)")
                else:
                    s.append(f"{portal_key.title()} (EnerGov)")
            return s, p, []

        def t_bellevue():
            b = search_bellevue(input_type, value)
            if isinstance(b, list):
                return ["Bellevue Open Data"], b, []
            return ["Bellevue Open Data"], [], [f"Bellevue Open Data: {b}"]

        def t_shoreline():
            p, er = search_shoreline(input_type, value)
            return (["Shoreline (eTRAKiT)"], p,
                    [f"Shoreline eTRAKiT: {e}" for e in er])

        thunks = [t_mbp, t_energov, t_bellevue, t_shoreline]

        if input_type == "permit":
            def t_seattle():
                p, er = search_seattle("permit", value)
                return (["Seattle Open Data"], p,
                        [f"Seattle Open Data — {e}" for e in er])
            thunks.append(t_seattle)

        for _ca in CIVIC_ACCESS_PORTALS:
            def t_civic(c=_ca):
                p, er = search_energov_civicaccess(c, input_type, value)
                return ([f"{c.title()} (EnerGov Civic Access)"], p,
                        [f"{c.title()} EnerGov: {e}" for e in er])
            thunks.append(t_civic)

        # Every Accela agency, including KC's unincorporated one: a bare
        # parcel/permit number has no city to route on.
        for _ag in sorted(set(ACCELA_PORTALS.values()) | {ACCELA_UNINCORPORATED_AGENCY}):
            def t_accela(a=_ag):
                label = ACCELA_AGENCY_LABEL.get(a, a)
                p, er = search_accela(a, input_type, value, label)
                return ([f"{label} (Accela)"], p,
                        [f"{label} Accela: {e}" for e in er])
            thunks.append(t_accela)

        for _sg in SMARTGOV_PORTALS:
            def t_smartgov(c=_sg):
                p, er = search_smartgov(c, input_type, value)
                return ([f"{c.title()} (SmartGov)"], p,
                        [f"{c.title()} SmartGov: {e}" for e in er])
            thunks.append(t_smartgov)

        if input_type == "permit":          # LAMA has no parcel search
            for _lm in LAMA_PORTALS:
                def t_lama(c=_lm):
                    p, er = search_lama(c, input_type, value)
                    return ([f"{display_city(c)} (LAMA)"], p,
                            [f"{display_city(c)} LAMA: {e}" for e in er])
                thunks.append(t_lama)

        def _safe(fn):
            try:
                return fn()
            except Exception as exc:  # one source failing must not sink the rest
                return [], [], [str(exc)]

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(10, len(thunks))) as pool:
            for s, p, er in pool.map(_safe, thunks):
                searched_jurisdictions.extend(s)
                all_permits.extend(p)
                errors.extend(er)

    else:  # address
        house, street = parse_address(value)
        # Search King County + city jurisdiction
        juris_to_search = ["20"]
        if city and city in JURIS_BY_NAME:
            juris_to_search.append(JURIS_BY_NAME[city])
        elif not city and not unincorporated_kc:
            # No city detected and no polygon answer — search all jurisdictions
            juris_to_search = list(JURISDICTIONS.keys())

        for jid in juris_to_search:
            results = search_mbp(jid, house=house, street=street)
            if results is None:
                continue
            jname = JURISDICTIONS.get(jid, jid)
            searched_jurisdictions.append(jname)
            if isinstance(results, list):
                all_permits.extend(results)
            elif isinstance(results, str) and "too many" in results.lower():
                errors.append(f"{jname}: {results}")

        # EnerGov cities: resolve parcel, then search by parcel
        if city and city in ENERGOV_PORTALS:
            parcel = resolved_parcel or _geocode_parcel(value)
            if parcel:
                resolved_parcel = parcel
                eg = search_energov(city, parcel, exact=False)
                all_permits.extend(eg)
                searched_jurisdictions.append(f"{city.title()} (EnerGov, parcel {parcel})")
            else:
                searched_jurisdictions.append(f"{city.title()} (EnerGov — parcel lookup failed)")
                separate_portal_note = {
                    "city": city.title(),
                    "portal": ENERGOV_PORTALS[city]["url"],
                    "note": (
                        f"Could not resolve this address to a parcel for the "
                        f"{city.title()} search — check the city portal directly."
                    ),
                    "reason": "parcel_resolution_failed",
                }
        elif (city and city in SEPARATE_PORTALS
              and city not in ("seattle", "shoreline")
              and city not in CIVIC_ACCESS_PORTALS
              and city not in ACCELA_PORTALS
              and city not in SMARTGOV_PORTALS
              and city not in LAMA_PORTALS):
            separate_portal_note = {
                "city": city.title(),
                "portal": SEPARATE_PORTALS[city],
                "note": f"{city.title()} has its own permit system — city-issued permits won't appear here.",
                "reason": "no_feed",
            }

    # Bellevue's former EnerGov hostname is retired. Its official Open Data
    # layer is current, daily refreshed. (Permit/parcel handled above.)
    if input_type == "address" and city in (None, "bellevue"):
        bellevue = search_bellevue(input_type, value)
        searched_jurisdictions.append("Bellevue Open Data")
        if isinstance(bellevue, list):
            all_permits.extend(bellevue)
        else:
            errors.append(f"Bellevue Open Data: {bellevue}")

    # Seattle's official Open Data. (Permit searches handled above.)
    if input_type == "address" and city == "seattle":
        seattle_permits, seattle_errors = search_seattle(input_type, value)
        all_permits.extend(seattle_permits)
        searched_jurisdictions.append("Seattle Open Data")
        errors.extend(f"Seattle Open Data — {error}" for error in seattle_errors)
        if city == "seattle" and seattle_errors:
            separate_portal_note = {
                "city": "Seattle",
                "portal": SEPARATE_PORTALS["seattle"],
                "note": (
                    "Some Seattle Open Data searches were incomplete — "
                    "check the Seattle Services Portal directly."
                ),
                "electrical": True,
                "reason": "source_incomplete",
            }

    # Shoreline eTRAKiT (CentralSquare) — building, mechanical/plumbing, and
    # land-use permits. Queried for permit/parcel searches (no city context) and
    # for Shoreline addresses. Electrical is issued by WA L&I (Shoreline does not
    # run its own program), so it is covered by the L&I layer below.
    if input_type == "address" and city == "shoreline":
        shoreline_permits, shoreline_errors = search_shoreline(input_type, value)
        all_permits.extend(shoreline_permits)
        searched_jurisdictions.append("Shoreline (eTRAKiT)")
        errors.extend(f"Shoreline eTRAKiT: {error}" for error in shoreline_errors)

    # Tyler EnerGov Civic Access portals (Redmond, ...). Full permit history
    # including electrical. Queried for permit/parcel searches (no city context)
    # and for a matching city address.
    for ca_city in CIVIC_ACCESS_PORTALS:
        if input_type == "address" and city == ca_city:
            ca_permits, ca_errors = search_energov_civicaccess(
                ca_city, input_type, value)
            all_permits.extend(ca_permits)
            searched_jurisdictions.append(f"{ca_city.title()} (EnerGov Civic Access)")
            errors.extend(f"{ca_city.title()} EnerGov: {error}" for error in ca_errors)

    # Accela Citizen Access (Woodinville; Black Diamond via King County's agency).
    # A matching city address searches its agency; permit/parcel searches (no
    # city context) query each unique Accela agency once.
    if input_type == "address" and city in ACCELA_PORTALS:
        ac_permits, ac_errors = search_accela(
            ACCELA_PORTALS[city], input_type, value, city.title())
        all_permits.extend(ac_permits)
        searched_jurisdictions.append(f"{city.title()} (Accela)")
        errors.extend(f"{city.title()} Accela: {error}" for error in ac_errors)
    elif input_type == "address" and (unincorporated_kc or not city):
        # Unincorporated King County (polygon-confirmed), or no city at all
        # (already fanning out everywhere): KC's own Accela agency on top of
        # MBP-KC — it holds the older and non-building county records.
        ac_permits, ac_errors = search_accela(
            ACCELA_UNINCORPORATED_AGENCY, input_type, value, "King County")
        all_permits.extend(ac_permits)
        searched_jurisdictions.append("King County (Accela)")
        errors.extend(f"King County Accela: {error}" for error in ac_errors)

    # SmartGov (Paladin/GXA) — Normandy Park, Carnation.
    if input_type == "address" and city in SMARTGOV_PORTALS:
        sg_permits, sg_errors = search_smartgov(city, input_type, value)
        all_permits.extend(sg_permits)
        searched_jurisdictions.append(f"{city.title()} (SmartGov)")
        errors.extend(f"{city.title()} SmartGov: {error}" for error in sg_errors)

    # LAMA — SeaTac. Full history including SeaTac's self-run electrical.
    if input_type == "address" and city in LAMA_PORTALS:
        lm_permits, lm_errors = search_lama(city, input_type, value)
        all_permits.extend(lm_permits)
        searched_jurisdictions.append(f"{display_city(city)} (LAMA)")
        errors.extend(f"{display_city(city)} LAMA: {error}" for error in lm_errors)

    # Layer 3: WA State L&I electrical permits (address searches only)
    # Skip L&I if the city handles its own electrical
    lni_permits = []
    city_does_electrical = city and city.lower() in CITIES_OWN_ELECTRICAL
    if input_type == "address":
        if city_does_electrical:
            searched_jurisdictions.append(f"WA State L&I — skipped ({city.title()} handles its own electrical)")
        else:
            house, street = parse_address(value)
            lni_permits, lni_errors = search_lni(f"{house} {street}", city or "")
            errors.extend(f"WA State L&I: {error}" for error in lni_errors)
            searched_jurisdictions.append("WA State L&I (electrical, 2020+)")

    # If the city does its own electrical and we can't search it, flag it
    city_permits_searched = (
        city in JURIS_BY_NAME
        or city in ENERGOV_PORTALS
        or city == "seattle"
        or city in CIVIC_ACCESS_PORTALS
        or city in ACCELA_PORTALS
        or city in SMARTGOV_PORTALS
        or city in LAMA_PORTALS
    )
    if city_does_electrical and not city_permits_searched:
        portal = SEPARATE_PORTALS.get(city.lower())
        electrical_note = {
            "city": city.title(),
            "portal": portal,
            "note": f"{city.title()} handles its own electrical permits — check their portal, not L&I.",
            "reason": "electrical_only",
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
    for p in unique:
        enrich_permit(p)

    if unique:
        result = {
            "action": "found",
            "permit_count": len(unique),
            "searched": searched_jurisdictions,
            "permits": unique,
            "input": raw_input,
            "message": f"Found {len(unique)} permit(s) across {', '.join(set(searched_jurisdictions))}.",
        }
        if errors:
            result["message"] += " Some source searches were incomplete."
    else:
        suggestions = ["Try searching by street name without the city"]
        if errors:
            suggestions.append(f"Some searches had issues: {'; '.join(errors)}")
        action = "refine" if errors else "none"
        message = (
            f"Search incomplete: {'; '.join(errors)}"
            if errors
            else f"No permits found in {', '.join(set(searched_jurisdictions))}."
        )
        result = {
            "action": action,
            "permit_count": 0,
            "searched": searched_jurisdictions,
            "permits": [],
            "input": raw_input,
            "message": message,
            "suggestions": suggestions,
        }

    if separate_portal_note:
        reason = separate_portal_note.pop("reason", "no_feed")
        result["separate_portal"] = separate_portal_note
        result["message"] += f" Note: {separate_portal_note['note']}"
        # Machine-actionable twin of the prose note (#41)
        result["next_step"] = build_next_step(
            separate_portal_note["city"], reason, value, input_type,
            portal=separate_portal_note.get("portal"),
            electrical=bool(separate_portal_note.get("electrical")))

    if errors:
        result["errors"] = errors

    # Provenance envelope: every record above came from a live source query at
    # fetched_at. trust_level tells an agent how complete that picture is.
    if not searched_jurisdictions:
        trust = "fallback"      # nothing searchable; separate_portal is the lead
    elif errors or separate_portal_note:
        trust = "partial"
    else:
        trust = "live"
    result["trust_level"] = trust
    result["fetched_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    result["parcel_id"] = ((location or {}).get("parcel_id")
                           or parcel_id(resolved_parcel))
    result["jurisdiction"] = {
        "city": city.title() if city else None,
        "basis": jurisdiction_basis,
        "county": (location or {}).get("county"),
        "unincorporated": (location or {}).get("unincorporated"),
    }
    if location:
        result["address_resolution"] = {
            k: location[k] for k in ("matched_address", "score", "match_type",
                                     "partial_match", "street_number_snapped")}
    result["cite_as"] = (f"{CITE_AS}, queried {result['fetched_at'][:10]}"
                         + (f" via {', '.join(dict.fromkeys(searched_jurisdictions))}"
                            if searched_jurisdictions else ""))

    return result


EXIT_CODES = {"found": 0, "none": 1, "refine": 1, "reject": 2}


TOOL_SCHEMA = {
    "name": "king_county_permit_status",
    "description": (
        "Look up building permit history and status for any King County, WA property. "
        "Accepts a street address, 10-digit parcel number, or permit number. "
        "Searches MyBuildingPermit.com (14 cities + King County), Bellevue and "
        "Seattle Open Data, Renton EnerGov (live API), and WA State L&I "
        "(electrical permits). "
        "Returns all matching permits sorted newest-first. "
        "Use for due diligence, permit tracking, or verifying contractor pull history."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "One of: street address ('1817 Morris Ave S, Renton WA 98055'), "
                    "10-digit parcel number (plain '7222000353' or formatted "
                    "'722200-0353'), "
                    "or permit number ('B25000947', 'ADDC21-0275', "
                    "'23-127651-LP', '6145915-CN'). "
                    "Input type is auto-detected."
                ),
            }
        },
        "required": ["query"],
    },
    "output_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["found", "none", "refine", "reject"],
                "description": (
                    "found — permits[] is populated; "
                    "none — no permits found; "
                    "refine — connection issue, retry; "
                    "reject — bad input"
                ),
            },
            "permit_count": {"type": "integer"},
            "permits": {
                "type": "array",
                "description": "Permit records sorted newest applied_date first.",
                "items": {
                    "type": "object",
                    "properties": {
                        "permit_number": {"type": "string"},
                        "type": {"type": "string", "description": "Permit category (e.g. 'Residential Electrical Permit')"},
                        "status": {"type": "string", "description": "e.g. Issued, Complete, On Hold, Withdrawn, Expired"},
                        "description": {"type": "string", "description": "Scope of work"},
                        "address": {"type": "string"},
                        "jurisdiction": {"type": "string"},
                        "applied_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                        "issued_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                        "finaled_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                        "expires_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                        "portal": {"type": ["string", "null"], "description": "Source permit or portal URL when available"},
                        "record_url": {"type": ["string", "null"], "description": "URL of this specific permit record when the source exposes one; null when portal is only a search page"},
                        "is_open": {"type": ["boolean", "null"], "description": "Normalized across vendor status vocabularies: false when finaled/closed/expired/withdrawn/etc., null when the source exposes no status"},
                    },
                },
            },
            "trust_level": {
                "type": "string",
                "enum": ["live", "partial", "fallback"],
                "description": "live — every applicable source answered; partial — some source errored or a city portal needs manual follow-up; fallback — nothing searchable, use separate_portal",
            },
            "fetched_at": {"type": "string", "description": "UTC ISO-8601 timestamp of this query; records are live, not cached"},
            "parcel_id": {"type": ["string", "null"], "description": "County-namespaced parcel id, e.g. 'king:7222000353' or 'pierce:5985002900', when the query was or resolved to a parcel"},
            "jurisdiction": {
                "type": "object",
                "description": "Which authority the query was routed to and why. basis: city-limits (geocoded point inside the city's polygon — authoritative), geocoder-city (locator's city, outside King County's polygon layer), address-text (the mailing city in the query), null (none). unincorporated=true means county permits only.",
                "properties": {
                    "city": {"type": ["string", "null"]},
                    "basis": {"type": ["string", "null"], "enum": ["city-limits", "geocoder-city", "address-text", None]},
                    "county": {"type": ["string", "null"]},
                    "unincorporated": {"type": ["boolean", "null"]},
                },
            },
            "address_resolution": {
                "type": "object",
                "description": "How well the geocoder matched an address query. match_type: point (exact address point), interpolated (number placed along the street; no parcel), street (street only). partial_match is true for anything but point; street_number_snapped when the matched house number differs from the one given.",
                "properties": {
                    "matched_address": {"type": ["string", "null"]},
                    "score": {"type": "number"},
                    "match_type": {"type": "string", "enum": ["point", "interpolated", "street"]},
                    "partial_match": {"type": "boolean"},
                    "street_number_snapped": {"type": "boolean"},
                },
            },
            "cite_as": {"type": "string", "description": "One-line attribution string for generated text"},
            "searched": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Jurisdictions searched (e.g. ['King County', 'Renton (EnerGov)']).",
            },
            "separate_portal": {
                "type": "object",
                "description": "Present when manual follow-up at a city portal is needed, including when a live city search is incomplete. Includes city, portal URL, note.",
            },
            "next_step": {
                "type": "object",
                "description": "Structured twin of separate_portal: the exact follow-up an agent can take. kind=manual_portal_search; reason in no_feed|electrical_only|parcel_resolution_failed|source_incomplete; portal_url (the portal's search page when known); vendor; search_by (inputs that page accepts, empty when login-gated); query/query_type to re-use; covers_electrical; hint.",
                "properties": {
                    "kind": {"type": "string", "enum": ["manual_portal_search"]},
                    "reason": {"type": "string", "enum": ["no_feed", "electrical_only", "parcel_resolution_failed", "source_incomplete"]},
                    "city": {"type": ["string", "null"]},
                    "portal_url": {"type": ["string", "null"]},
                    "vendor": {"type": ["string", "null"]},
                    "search_by": {"type": "array", "items": {"type": "string", "enum": ["address", "permit", "parcel"]}},
                    "query": {"type": "string"},
                    "query_type": {"type": "string", "enum": ["address", "parcel", "permit"]},
                    "covers_electrical": {"type": "boolean"},
                    "hint": {"type": "string"},
                },
            },
            "errors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Source errors when a search is incomplete; may accompany permits from sources that succeeded.",
            },
            "message": {"type": "string"},
        },
        "required": ["action", "message"],
    },
    "invocation": {
        "command": "python3 lookup.py --pipe \"{query}\"",
        "exit_codes": {
            "0": "action=found — permits[] is populated",
            "1": "action=none or refine — no permits or connection issue",
            "2": "action=reject — bad input",
        },
    },
}


def print_usage():
    print("Usage: lookup.py [--pipe] [--schema] <address|parcel|permit_number>")
    print('  lookup.py "27927 E Main St"              # by address')
    print('  lookup.py "7222000353"                    # by parcel number')
    print('  lookup.py "ADDC21-0275"                   # by permit number')
    print('  lookup.py --pipe "27927 E Main St"        # agent mode')
    print('  lookup.py --schema                        # print tool definition')


def main():
    args = sys.argv[1:]

    if "-h" in args or "--help" in args:
        print_usage()
        sys.exit(0)

    pipe_mode = "--pipe" in args
    schema_mode = "--schema" in args
    args = [a for a in args if a not in ("--pipe", "--schema")]

    if schema_mode:
        print(json.dumps(TOOL_SCHEMA, indent=2))
        sys.exit(0)

    if not args:
        print_usage()
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
