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

## TODO: Crack Renton EnerGov API

Renton's permit portal (`permitting.rentonwa.gov`) is a Tyler EnerGov Citizen Self Service
Angular SPA. The REST API pattern hasn't been cracked yet — `/api/resource/Permit/keyvalues/`
accepts requests but only serves dropdown data, not search results. The actual search goes
through an Angular service that we need to observe via browser DevTools.

**To unblock:**
1. Add `permitting.rentonwa.gov` to the Chrome extension's allowed domains
   (Claude in Chrome MCP → extension settings → domain allowlist)
2. Navigate to the portal, search for test permit `B25000947`, and capture the XHR
3. Replicate the API call in Python

**Test cases:**
- Permit number: `B25000947` (Renton building permit)
- Address: `1817 Morris Ave S, Renton, WA 98055` (should have 2 electrical permits via Renton)

**What we know so far:**
- Portal URL: `https://permitting.rentonwa.gov/`
- webApiBaseUrl: `/api`
- Framework: Tyler EnerGov CSS (Angular 1.x), hosted by Tyler (cdn.forge.tylertech.com)
- `/api/resource/Permit/keyvalues/{id}` returns `Success:true, Result:null` for permit numbers
- No `/api/Cap/Search` or similar endpoint found — all return 404
- `rentonwa.gov` blocks curl/WebFetch (403) but the portal subdomain responds fine
