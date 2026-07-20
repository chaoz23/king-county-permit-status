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
import re
import sys
import urllib.request
import urllib.parse
import http.cookiejar
from html import unescape as html_unescape
from calendar import monthrange
from datetime import datetime, timedelta

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
    "black diamond": "https://www.ci.blackdiamond.wa.us/",
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
    "redmond": "https://permits.redmond.gov/",
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
# Reusable across agencies via config. Cities that contract permitting to King
# County resolve to the "kingco" agency (their permits live in KC's system).
ACCELA_HOST = "https://aca-prod.accela.com"
ACCELA_PORTALS = {            # city -> Accela agency code
    "woodinville": "WOODINVILLE",
    "black diamond": "kingco",
}
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

        for _ag in sorted(set(ACCELA_PORTALS.values())):
            def t_accela(a=_ag):
                label = ACCELA_AGENCY_LABEL.get(a, a)
                p, er = search_accela(a, input_type, value, label)
                return ([f"{label} (Accela)"], p,
                        [f"{label} Accela: {e}" for e in er])
            thunks.append(t_accela)

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
        elif not city:
            # No city detected — search all jurisdictions
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
            parcel = _geocode_parcel(value)
            if parcel:
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
                }
        elif (city and city in SEPARATE_PORTALS
              and city not in ("seattle", "shoreline")
              and city not in CIVIC_ACCESS_PORTALS
              and city not in ACCELA_PORTALS):
            separate_portal_note = {
                "city": city.title(),
                "portal": SEPARATE_PORTALS[city],
                "note": f"{city.title()} has its own permit system — city-issued permits won't appear here.",
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
    )
    if city_does_electrical and not city_permits_searched:
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
        result["separate_portal"] = separate_portal_note
        result["message"] += f" Note: {separate_portal_note['note']}"

    if errors:
        result["errors"] = errors

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
                    },
                },
            },
            "searched": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Jurisdictions searched (e.g. ['King County', 'Renton (EnerGov)']).",
            },
            "separate_portal": {
                "type": "object",
                "description": "Present when manual follow-up at a city portal is needed, including when a live city search is incomplete. Includes city, portal URL, note.",
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
