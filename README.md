# King County Permit Status

Look up building permit status for any King County, WA address. Searches across jurisdictions since county-level permits (septic, critical areas, grading) and city-level permits (building, mechanical) can both apply to a single address.

```bash
python3 lookup.py "27927 E Main St"           # by address
python3 lookup.py "7222000353"                  # by parcel number
python3 lookup.py "ADDC21-0275"                 # by permit number
python3 lookup.py --pipe "27927 E Main St"      # agent pipeline mode
```

## What you get

```json
{
  "action": "found",
  "permit_count": 5,
  "searched": ["King County"],
  "permits": [
    {
      "permit_number": "ADDC24-0217",
      "type": "Building/Residential Building/Addition-Improvement",
      "status": "Permit Expiration Notice",
      "description": "CONSTRUCT 780 SF ADDITION...",
      "address": "27927 E MAIN ST",
      "jurisdiction": "King County",
      "applied_date": "2024-04-05",
      "issued_date": "2024-06-24",
      "finaled_date": null,
      "expires_date": null
    }
  ]
}
```

## Multi-jurisdiction coverage

The tool searches the [MyBuildingPermit.com](https://permitsearch.mybuildingpermit.com/) portal which covers 15 jurisdictions:

Auburn, Bellevue, Bothell, Burien, Edmonds, Federal Way, Issaquah, Kenmore, **King County** (unincorporated), Kirkland, Mercer Island, Mill Creek, Newcastle, Sammamish, Snoqualmie

**Cities with separate portals** (not searched, but flagged with a link):

Seattle, Renton, Kent, Redmond, Shoreline, Tukwila, Covington, Maple Valley, Enumclaw, Woodinville, and others

When you search an address in a city with a separate portal, the tool:
1. Searches King County anyway (catches county-level permits)
2. Flags that the city has its own system with a link to their portal

## Agent pipeline contract

| Field | Description |
|---|---|
| `action` | `found` (permits returned), `none` (no matches), `refine` (connection issue) |
| `permit_count` | Number of permits found |
| `permits` | Array of permit records sorted newest-first |
| `searched` | Which jurisdictions were searched |
| `separate_portal` | If the city has its own system: city name + portal URL |

| Exit code | Meaning |
|---|---|
| 0 | Permits found |
| 1 | No permits or search issue |
| 2 | Bad input |

## Requirements

- Python 3.10+ (stdlib only)
- Network access to `permitsearch.mybuildingpermit.com`

## License

MIT
