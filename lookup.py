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
from calendar import monthrange
from datetime import datetime, timedelta

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
    # Bellevue-style: 23-127651-LP or 23 127651 LP
    if re.fullmatch(r"\d{2}[-\s]\d{6}[-\s][A-Z]{1,3}", s, re.IGNORECASE):
        return "permit", s
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


def lookup(raw_input: str) -> dict:
    """Core lookup. Returns unified result for human + agent."""
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

    if input_type == "permit":
        # Search MyBuildingPermit jurisdictions first
        for jid, jname in JURISDICTIONS.items():
            results = search_mbp(jid, search_by="PermitNumber",
                                 permit_number=value)
            if results is None:
                break
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
        # A bare parcel number has no city text to route on. Query every
        # MyBuildingPermit jurisdiction so city permits are not missed.
        for jid in JURISDICTIONS:
            results = search_mbp(jid, parcel=value)
            if results is None:
                continue
            jname = JURISDICTIONS.get(jid, jid)
            searched_jurisdictions.append(jname)
            if isinstance(results, list):
                all_permits.extend(results)
            elif isinstance(results, str):
                errors.append(f"{jname}: {results}")

        # A bare parcel number has no city text to route on. Query every
        # configured EnerGov portal so supported city permits are not missed.
        for portal_key in ENERGOV_PORTALS:
            eg = search_energov(portal_key, value, exact=False)
            all_permits.extend(eg)
            searched_jurisdictions.append(f"{portal_key.title()} (EnerGov)")

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
        elif city and city in SEPARATE_PORTALS:
            separate_portal_note = {
                "city": city.title(),
                "portal": SEPARATE_PORTALS[city],
                "note": f"{city.title()} has its own permit system — city-issued permits won't appear here.",
            }

    # Bellevue's former EnerGov hostname is retired. Its official Open Data
    # layer is current, daily refreshed, and supports all three input types.
    if input_type in ("permit", "parcel") or city in (None, "bellevue"):
        bellevue = search_bellevue(input_type, value)
        searched_jurisdictions.append("Bellevue Open Data")
        if isinstance(bellevue, list):
            all_permits.extend(bellevue)
        else:
            errors.append(f"Bellevue Open Data: {bellevue}")

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
    city_permits_searched = city in JURIS_BY_NAME or city in ENERGOV_PORTALS
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
        "Searches MyBuildingPermit.com (14 cities + King County), Bellevue Open Data, "
        "Renton EnerGov (live API), and WA State L&I (electrical permits). "
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
                    "10-digit parcel number ('7222000353'), "
                    "or permit number ('B25000947', 'ADDC21-0275', '23-127651-LP'). "
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
                "description": "Present when the city has its own portal not yet searchable. Includes city, portal URL, note.",
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


def main():
    args = sys.argv[1:]
    pipe_mode = "--pipe" in args
    schema_mode = "--schema" in args
    args = [a for a in args if a not in ("--pipe", "--schema")]

    if schema_mode:
        print(json.dumps(TOOL_SCHEMA, indent=2))
        sys.exit(0)

    if not args:
        print("Usage: lookup.py [--pipe] [--schema] <address|parcel|permit_number>")
        print('  lookup.py "27927 E Main St"              # by address')
        print('  lookup.py "7222000353"                    # by parcel number')
        print('  lookup.py "ADDC21-0275"                   # by permit number')
        print('  lookup.py --pipe "27927 E Main St"        # agent mode')
        print('  lookup.py --schema                        # print tool definition')
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
