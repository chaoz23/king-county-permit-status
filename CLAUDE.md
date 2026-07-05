# King County Permit Status

When the user asks about building permits, permit status, or inspection results for a King County area property, run:

```bash
python3 lookup.py "<address, parcel number, or permit number>"
```

Read the `action` field:
- `found` → show the `permits` list (sorted newest-first), highlight any with status like "Expiration Notice" or "Corrections Required"
- `none` → tell the user no permits were found; if `separate_portal` is present, direct them to that city's portal
- `refine` → connection issue, suggest retrying

The tool auto-detects input type (address vs parcel number vs permit number) and searches King County + the relevant city jurisdiction. For cities not on MyBuildingPermit (Seattle, Renton, Kent, etc.), it flags their separate portal URL.

## Renton EnerGov Integration (live)

Renton permits are searched directly via the Tyler EnerGov API at `permitting.rentonwa.gov`.

**Endpoint:** `POST /api/energov/search/search`
**Required headers:**
```
tenantId: 1
tenantName: RentonWaProd
Tyler-TenantUrl: RentonWaProd
Tyler-Tenant-Culture: en-US
Content-Type: application/json;charset=UTF-8
```

**Criteria body:** Fetch template from `GET /api/energov/search/criteria`, then set:
- `SearchModule: 1, FilterModule: 1` (required — module=0 returns counts only)
- `PageSize: >0` (required — 0 causes 500)
- `Keyword`: permit number or parcel number
- `ExactMatch: true` for permit number; `false` for parcel

**Address search:** Resolve to parcel via KC ArcGIS geocoder first, then search by parcel.

**Test cases confirmed working:**
- `B25000947` → 2 permits (building + electrical sub-permit) at 1817 Morris Ave S
- `1817 Morris Ave S, Renton, WA 98055` → 24 permits via parcel 7222000353

## Bellevue Open Data Integration (live)

Bellevue's former EnerGov hostname is retired. Bellevue permits are searched
through the city's official ArcGIS Open Data layer, which is refreshed daily
and covers 1998 to the present.

**Test cases confirmed working:**
- `919 109th Ave NE, Bellevue, WA` → Bellevue address history
- `6600750000` → Bellevue parcel history
- `23-127651-LP` → exact Bellevue permit
