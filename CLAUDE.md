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
