# Kolmo MCP server — what we can learn (2026-10-08)

**Subject:** `Kolmo-Construction/kolmo-mcp-server` (MIT) and the live server at `https://www.kolmo.io/mcp` (36 tools, no auth), plus `kolmo.io/api/public/openapi.json` and `kolmo.io/permits/data-quality`.

**License check:** MIT — fine to study and borrow patterns. But the repo is a *shell*: 9 files, a stdio→Streamable-HTTP proxy (`proxy.mjs`), a health check, registry manifests, and `tools.json` = `[]`. The server implementation (rules engine, parcel resolver, source monitor) is **not published**. Everything below comes from tool schemas and live responses, not source code. Nothing is copied; these are design observations.

**Who does what:** Kolmo answers *"what permits does this project need and what do the rules say"* (1,723 rules, 475 authoritative, 82 jurisdictions across King/Pierce/Snohomish). We answer *"what's actually on file at this address and what's its status"* (live records from 20/39 King County cities, 6 vendor systems). Live probe confirms the complement: their `priorPermits` returned **0 records for Bellevue, Shoreline, Redmond, Woodinville and Kent** and 11 for Seattle (vs. our 346 / 66 / 32 / 11 / fallback and 129). Their `get_neighbor_permit_activity` supports Seattle only (Socrata). They do read Renton EnerGov (`B26003961 "STRADER SFR REMODEL"` came back for 1817 Morris Ave S) but nothing else in our vendor set.

---

## 1. Address → parcel

| | Kolmo `lookup_parcel_by_address` | Ours (`king-county-address-to-parcel-number`, `_geocode_parcel`) |
|---|---|---|
| Parcel ID | `"king:7222000353"` — **county-namespaced** | bare 10-digit PIN |
| Resolution metadata | `addressResolution{locationType:"CACHED", partialMatch, streetNumberSnapped}` | none — caller can't tell exact vs snapped match |
| Jurisdiction basis | `jurisdictionBasis{method:"city-limits", source: WSDOT CityLimits}` | mailing-city string from the geocoder |
| Cache | 30-day, `fetchedAt`, `forceRefresh` arg | none |
| Extras | zoning + setbacks (each with `sourceUrl`), 14 overlay checks with per-source `overlayStatus` + `overlaysComplete`, appraisal, building footprint (`source:"overture"`) | PIN only |

**Adopt:** (a) namespace the PIN (`king:` prefix) now that Milton/Pacific/Auburn straddle Pierce — zero cost, prevents a future collision; (b) surface `partialMatch` / `streetNumberSnapped` so an agent knows when the geocoder guessed; (c) derive jurisdiction from a **city-limits polygon** (WSDOT or King County GIS) instead of the mailing city — mailing city is wrong for unincorporated KC and for addresses like Milton-in-King vs Milton-in-Pierce.

## 2. Response data model — provenance everywhere

Patterns we lack and should copy into the shared permit schema / `--pipe` envelope:

- **Per-fact `sourceUrl` + `lastVerifiedAt`.** Every rule and every parcel attribute carries the URL it came from. We return `portal` (a name) but not the record URL — and we have it for Civic Access, Accela and SmartGov (detail GUIDs/record numbers). Add `record_url` to the schema (closes most of #29's "find the documents" first step too).
- **Response-level `trustLevel`** and `fetchedAt`. Ours implicitly says "live"; make it explicit per source (`live`, `cached`, `fallback`).
- **`attribution.citeAs`.** A one-line citation string the agent can paste. Trivial, and it's how a tool earns a mention in generated text.
- **`verdict{verdict, headline, detail, fired, cleared, open, questions[{fact, prompt, kind, unit}]}`.** Their rules engine returns *the questions it still needs answered* as structured follow-ups. Our analogue: when a city is fallback-only, return a structured `next_step{portal_url, search_by, hint}` instead of a prose note — agents act on structure, not notes.
- **`priorPermits[].isOpen` / `recordKind`.** A boolean "open" derived from status is more useful to agents than our raw vendor status strings (which differ per vendor). Add a normalized `is_open` alongside `status`.
- **Separate jurisdiction slugs for split cities:** `milton-king` / `milton-pierce`. We treat Milton as one city; our scorecard should at least note the split.

## 3. Data stores & freshness — their "data-quality" page is our `refresh.py`, grown up

Kolmo monitors **834 official source URLs** (plus 320 retired ones kept as history), re-fetches **every Monday**, hashes *text content only* (raw-HTML hashing flagged 66% of pages as changed), and classifies each as `ok / missing(404) / blocked(403) / unreachable / serverError / domainDead`. 403 is explicitly *not* treated as dead (the page works in a browser). An AI classifier (Gemini Flash, instructed to be conservative) flags factual conflicts; a human confirms before anything is published as stale. `get_permit_data_freshness` exposes `sourceHealth` per jurisdiction with `problemUrls[]` and a `staleOver180Days` list.

Live sample: Renton 13/14 ok (the one miss is a 404'd ArcGIS MapServer), Covington 1/11 ok — 10 `municipal.codes` pages 403 their fetcher. That is exactly the Redmond/Kent TLS-block class we hit.

**Adopt:**
- Extend `refresh.py` into a **source-health probe** with the same outcome taxonomy, run weekly in CI, writing `source_health.json` → scorecard shows per-city health, not just "routed." (Our MBP dropdown-drift fix was the first instance of this; generalize it.)
- Hash the **CSV/JSON payload shape**, not the HTML, when detecting portal drift.
- Record `last_verified` per *source*, not per repo.

## 4. Jurisdiction catalog

Their King County list = 40 slugs; ours = 39. Differences: they add `king-county-unincorporated`, `wa-state` (L&I baseline) and `milton-king`; they **omit Bothell** (we have it, on MBP). Every one of their 40 is `verified:true` — but "verified" means *rules verified*, not *records reachable*. Our scorecard distinguishes live/partial/fallback; keep that, it's more honest for our use case. Add `king-county-unincorporated` as an explicit row (we already serve it via MBP KC) and a `wa-lni` pseudo-row for state electrical.

## 5. Access patterns & distribution

- **One remote server, multiple protocols:** MCP (Streamable HTTP), REST (`GET /api/public/permits/{city}/{address}/{projectType}`), OpenAPI 3.1, A2A agent card (`/.well-known/agent-card.json`), `llms.txt`, and **`.md` suffix content negotiation** on web pages. Registry listings (Glama, Docker MCP registry) point at the hosted URL; the GitHub repo exists mainly to be *listable*.
- **Takeaway for us:** our `tool.json` + `--pipe` is CLI-first; nobody can discover or call it without cloning. The cheapest distribution step is a **hosted read-only MCP endpoint** (we already own `secondlandings.com` infra) listed in the same registries — that is probably the single biggest reason their server gets found and ours has zero external users. The 30-day parcel cache is what makes hosting affordable.
- Their public OpenAPI has **zero component schemas** (responses untyped) — ours should ship typed schemas; it's a differentiator.

## 6. What we have that they don't (keep leading here)

Live multi-vendor record search (EnerGov, Civic Access, Accela, eTRAKiT, SmartGov, MBP) with normalized status; the city→vendor map (#27); the electrical-authority model; honest live/partial/fallback scoring. Their rules + our records is the whole homeowner question — hence the outreach draft.

---

## Proposed follow-ups (to file under #25)

1. **Schema:** add `record_url`, `is_open`, `source_verified_at`, `trust_level`, `cite_as`; namespace PIN as `king:<PIN>`. (small, non-breaking: additive fields)
2. **Jurisdiction from city-limits polygon** (WSDOT/KC GIS) instead of mailing city; expose `partial_match`/`snapped`. (medium)
3. **Weekly source-health probe** in CI with Kolmo's outcome taxonomy → scorecard health column. (medium; generalizes the MBP drift fix)
4. **Structured `next_step` for fallback cities** replacing prose notes. (small)
5. **Hosted read-only MCP + OpenAPI + registry listings.** (large; the distribution gap)
6. **Scorecard rows:** `king-county-unincorporated`, Milton King/Pierce split note. (tiny)
