# King County Permit Status

Look up building permit history and status for any King County, WA property. Searches across jurisdictions — county-level permits (septic, critical areas, grading) and city-level permits (building, mechanical, electrical) can all apply to the same address.

```bash
python3 lookup.py "27927 E Main St"           # by address
python3 lookup.py "7222000353"                  # by parcel number
python3 lookup.py "B25000947"                   # by permit number
python3 lookup.py "23-127651-LP"                # Bellevue permit number
python3 lookup.py "6145915-CN"                  # Seattle SDCI permit number
python3 lookup.py --pipe "27927 E Main St"      # agent pipeline mode
python3 lookup.py --schema                      # print tool definition
```

Parcel-number searches query every supported MyBuildingPermit and live city
portal because a bare parcel number does not identify its city jurisdiction.

## What you get

```json
{
  "action": "found",
  "permit_count": 2,
  "searched": ["Renton (EnerGov)"],
  "permits": [
    {
      "permit_number": "E26000458",
      "type": "Residential Electrical Permit",
      "status": "Issued",
      "description": "Electrical Work (Garage & Garage Exterior): ...",
      "address": "1817 Morris Ave S Renton WA 98055",
      "jurisdiction": "Renton",
      "applied_date": "2026-01-28",
      "issued_date": "2026-01-28",
      "finaled_date": null,
      "expires_date": "2027-07-27",
      "portal": "https://permitting.rentonwa.gov"
    }
  ]
}
```

| Field | Description |
|---|---|
| `action` | `found` (permits returned), `none` (no matches), `refine` (connection issue) |
| `permit_count` | Number of unique permits found |
| `permits` | Array sorted newest `applied_date` first |
| `searched` | Which jurisdictions were searched |
| `separate_portal` | If the city has an unsupported portal: city name + URL |
| `errors` | Source errors when a search is incomplete; may accompany permits from successful sources |

Per permit: `permit_number`, `type`, `status`, `description`, `address`, `jurisdiction`, `applied_date`, `issued_date`, `finaled_date`, `expires_date`, `portal`.

## Multi-jurisdiction coverage

| Source | Cities / scope |
|---|---|
| [MyBuildingPermit.com](https://permitsearch.mybuildingpermit.com/) | Auburn, Bellevue, Bothell, Burien, Edmonds, Federal Way, Issaquah, Kenmore, **King County** (unincorporated), Kirkland, Mercer Island, Mill Creek, Newcastle, Sammamish, Snoqualmie |
| Bellevue Open Data (live ArcGIS API) | Daily Bellevue permit snapshot (1998+) for address, parcel, and permit-number searches |
| Seattle Open Data (live Socrata APIs) | Building, electrical, trade, and land-use permits for Seattle address and permit-number searches |
| Renton EnerGov (live API) | All Renton permit types including electrical |
| WA State L&I | Electrical permits for cities not handling their own (2020+) |

All 39 incorporated King County cities are recognized. Cities outside the searchable sources are flagged with their municipal portal URL rather than being treated as unincorporated King County.

For comma-separated addresses, the locality component takes precedence over
city names that also appear in streets (for example, `Kent Kangley Rd,
Covington`). If a Renton address cannot be resolved to a parcel for its live
EnerGov search, the result retains Renton's portal as an actionable fallback.
Seattle address and exact permit-number searches query all four official SDCI
Open Data datasets; parcel-only Seattle lookup is not yet supported.

## Exit codes

| Code | Action | Meaning |
|---|---|---|
| 0 | `found` | `permits[]` is populated |
| 1 | `none` / `refine` | No permits or connection issue |
| 2 | `reject` | Bad input |

## For agents

`tool.json` at the repo root contains the full tool definition in Anthropic/OpenAI tool-call format. An agent can load it directly or fetch the live version:

```bash
python3 lookup.py --schema
```

```json
{
  "name": "king_county_permit_status",
  "description": "Look up building permit history and status for any King County, WA property...",
  "input_schema": {
    "type": "object",
    "properties": {
      "query": {
        "type": "string",
        "description": "Street address, 10-digit parcel number, or permit number..."
      }
    },
    "required": ["query"]
  },
  "output_schema": { ... },
  "invocation": {
    "command": "python3 lookup.py --pipe \"{query}\"",
    "exit_codes": {
      "0": "action=found — permits[] is populated",
      "1": "action=none or refine — no permits or connection issue",
      "2": "action=reject — bad input"
    }
  }
}
```

**Pipeline pattern:**
```bash
# Get all permits at an address, extract open ones
python3 lookup.py --pipe "1817 Morris Ave S, Renton WA" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
open_statuses = {'Issued', 'In Review', 'On Hold', 'Pending'}
open_permits = [p for p in d.get('permits', []) if p['status'] in open_statuses]
print(json.dumps(open_permits, indent=2))
"
```

**Related tools in this series:**
- [`king-county-address-to-parcel-number`](https://github.com/chaoz23/king-county-address-to-parcel-number) — resolve an address to its parcel number
- [`wa-contractor-license`](https://github.com/chaoz23/wa-contractor-license) — verify WA contractor license status and violations
- [`king-county-property-tax-appeal`](https://github.com/chaoz23/king-county-property-tax-appeal) — build a filing-ready tax appeal packet

## Requirements

- Python 3.10+ (stdlib only, no dependencies)
- Network access to `permitsearch.mybuildingpermit.com`, `services1.arcgis.com`, `permitting.rentonwa.gov`, `secure.lni.wa.gov`

## Development

Run the network-free regression suite with:

```bash
python3 -m unittest discover -s tests -v
```

## License

MIT
