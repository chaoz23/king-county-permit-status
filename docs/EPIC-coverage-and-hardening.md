# Epic: Close the "any King County property" gap — coverage + hardening

**Status:** inventory / not started
**Size target:** 20–50 PRs (current inventory: **~40**)
**Thesis:** The product's *aspirational truth* is "give me any King County address/parcel/permit and I return the complete permit history." The *current truth* is "complete where the jurisdiction offers an open API (Renton, Seattle, Bellevue, 12 MyBuildingPermit cities); a correct signpost everywhere else." This epic closes that delta: expand real coverage across the 25 fallback-only cities, harden the data we already return, and bring the interface/distribution up to the standard of the sibling tools.

Grounded in live runs against every source on 2026-07-15 (all "live" claims verified working).

---

## Coverage baseline (measured)

| Bucket | Count | Cities |
|---|---:|---|
| **Live — MyBuildingPermit** | 12 | Auburn, Bellevue, Bothell, Burien, Federal Way, Issaquah, Kenmore, Kirkland, Mercer Island, Newcastle, Sammamish, Snoqualmie |
| **Live — dedicated integration** | 3 | Renton (EnerGov), Seattle (Socrata), Bellevue (ArcGIS Open Data) |
| **Fallback-only (frontier)** | 25 | Algona, Beaux Arts Village, Black Diamond, Carnation, Clyde Hill, Covington, Des Moines, Duvall, Enumclaw, Hunts Point, Kent, Lake Forest Park, Maple Valley, Medina, Milton, Normandy Park, North Bend, Pacific, Redmond, SeaTac, Shoreline, Skykomish, Tukwila, Woodinville, Yarrow Point |

Fallback tiering (to prioritize integration effort):
- **Tier 1 — real volume + dedicated permit portal (crack these):** Redmond (`permits.redmond.gov`), Shoreline (`permits.shorelinewa.gov`), Kent (login-walled status page), Covington, Maple Valley, Des Moines, SeaTac, Tukwila, Enumclaw, North Bend, Duvall, Woodinville, Black Diamond, Carnation.
- **Tier 2 — tiny towns, homepage-only portal, near-zero permit volume:** Algona, Beaux Arts Village, Clyde Hill, Hunts Point, Medina, Milton, Normandy Park, Pacific, Skykomish, Yarrow Point, Lake Forest Park. Fallback link is the right answer; only improve the note.

### Recon log — 2026-07-15 (partial; A1 completes this)

Fingerprinted the dedicated-portal cities directly. **Key finding: the frontier is multi-vendor**, which breaks the original assumption that one generalized EnerGov client unlocks it. Each vendor family = its own adapter.

| City | Permit system | Evidence | Adapter |
|---|---|---|---|
| Renton *(live)* | **Tyler EnerGov** | root fingerprint confirmed | done |
| Maple Valley | **Tyler EnerGov** (likely) | `tyler` marker on permits page | EnerGov (reuse) |
| Shoreline | **TRAKiT** (CentralSquare) | `trakit` in root; ASP.NET/IIS | **new — TRAKiT** |
| Redmond | unknown — blocks plain curl (`000`) | needs browser-based recon | TBD |
| Kent | unknown — login-walled status page | — | TBD |
| Covington, Des Moines, SeaTac, Tukwila, Enumclaw, North Bend, Woodinville | not yet fingerprinted | permit-portal link behind site nav/JS | **A1 recon** |

Implication: Workstream A is **adapter-count-driven, not city-count-driven.** Cheap "config-entry" cities only exist within a vendor family we already support. Real recon (A1) must land before A3+ counts are trustworthy — the frontier estimate below is a **range**.

---

## Critical needs (P0 — data integrity of what we already ship)

These are bugs/gaps in *current* coverage, not expansion. Do first.

- [ ] **B1 · MyBuildingPermit "too many results" overflow.** Broad address searches (e.g. `919 109th Ave NE, Bellevue`) make MBP return the string `"Search returned too many results. Please refine…"` — surfaced as an error while other sources still populate, so the result looks complete but silently drops MBP permits. Fix: date-window or paginate the MBP query like L&I does, or narrow by house-number. `[M]`
- [ ] **B2 · Cross-jurisdiction dedup collision.** `lookup()` dedups on `permit_number` alone (lookup.py:857). Two cities can issue the same permit number (e.g. `B25000947`), so a real permit from city B is dropped as a "duplicate" of city A. Fix: dedup key = `(jurisdiction, permit_number)`. `[S]`
- [x] **H1 · Coverage scorecard (DONE 2026-07-15).** Added a per-city × permit-type scorecard to the README, auto-generated from `routing_data.json` via `scripts/gen_scorecard.py` and guarded by `tests/test_scorecard.py` (fails if README drifts). Baseline: **14/39 live · 19/39 partial (L&I electrical only) · 6/39 fallback**. This is now the tracking instrument for Workstream A — every city integration flips a row and regenerates. Still TODO: reword the headline "any property" prose above the scorecard.
- [x] **B7 · Confirm the electrical gaps (DONE 2026-07-15).** Verified against live MBP feeds: MyBuildingPermit **does** carry a city's own electrical history (Federal Way `Electrical`; Mercer Island `ELECTRICAL`/`LOW VOLTAGE ELECTRIC`; Kirkland `ENR`=Electrical Non-Residential, `ELV`/`ELE`; Burien `Electric`; Sammamish `ELECTRICAL - NON RESIDENTIAL`). The original ⚠️ was a false inference from "owns electrical → L&I skipped." Fixed the scorecard logic: **5 cities flipped ⚠️→✅.** The true electrical gap is now exactly the 6 fallback cities that self-run electrical and aren't on MBP: **Des Moines, Milton, Normandy Park, Redmond, SeaTac, Tukwila** — their electrical closes only when the city itself is integrated (Workstream A). No separate electrical work needed.
- [ ] **H2 · `action`-value doc inconsistency.** The "What you get" table lists `found/none/refine` but omits `reject`; exit-code table includes it. Reconcile. `[S]`

---

## Workstream A — Coverage frontier (vendor-adapter driven)

The bulk of the epic. Renton proved the pattern (crack the muni permit system's API, normalize into the shared schema). But recon shows the frontier spans **multiple vendors** — a city is cheap only if it runs a vendor we already have an adapter for. Structure: build one adapter per vendor family, then add same-vendor cities as config entries.

**Blocking gate:**
- [ ] **A1 · Recon spike (blocks all counts below).** Follow each Tier-1 city's real permit-portal link (browser, not curl — several block plain requests / are JS SPAs) and record vendor + whether a search API is reachable. Output: city→vendor→reachable matrix. Until this lands, A3+ counts are estimates. `[M]`

  **Prior-art check (done 2026-07-15):** No pre-made public table maps KC city → permit system exists (checked Reddit, permit-expediter guides, and the WA Commerce *2024 Digital Permitting* report — the latter is a vendor cost/landscape paper, not a jurisdiction map). The mapping must be assembled by fingerprinting, but the search **bounds the vendor universe** in WA to: Accela, Tyler EnerGov, CentralSquare E-TRAKiT, SmartGov, OpenGov, Clariti, Cityworks, Amanda/Granicus. Confirmed so far: Renton = EnerGov · Shoreline = TRAKiT · Maple Valley = Tyler/EnerGov · **King County unincorporated = Accela** (`aca-prod.accela.com/kingco`; its data.kingcounty.gov Open Data entry is a non-queryable catalog pointer, not a SoQL source). So A1 is a bounded fingerprint-per-city job (~8 candidate systems), not an open-ended mystery.
  - **Data-drift lead:** the live MBP jurisdiction dropdown lists 13 (Auburn, Bellevue, Bothell, Burien, Edmonds, Federal Way, Issaquah, Kenmore, King County, Kirkland, Mercer Island, Sammamish, Snoqualmie) — **Newcastle is absent** though `routing_data.json` marks it MBP-live. Verify whether Newcastle left MBP; if so the scorecard row is stale. (Belongs to Workstream D / `refresh.py`.)

**Vendor adapters (one PR each; each unlocks N same-vendor cities as follow-on config):**
- [ ] **A2 · Generalize the EnerGov client.** Refactor `search_energov` + `ENERGOV_PORTALS` from Renton-specific to config-driven multi-tenant. Confirmed unlocks: Maple Valley (+ any EnerGov city A1 finds). `[M]`
- [ ] **A3 · TRAKiT adapter (new).** CentralSquare TRAKiT / eTRAKiT public search. Confirmed needed for **Shoreline**; likely shared by other WA cities. `[L]`
- [ ] **A4 · Accela adapter (new)** — build if A1 finds Accela Citizen Access cities. `[L]`
- [ ] **A5 · CityView / SmartGov / other adapter (new)** — build if A1 finds one of these; else drop. `[L]`

**City config entries (cheap — only for a supported vendor; exact list set by A1):**
- [ ] **A6 · Maple Valley** (EnerGov config) `[S]`
- [ ] **A7 · Redmond** — browser-recon vendor first (curl-blocked), then config or new adapter. `[M]`
- [ ] **A8 · Kent** — attempt the API behind the login-walled status page; if truly unbeatable, upgrade the fallback note to name the exact search page. `[M]`
- [ ] **A9 · Covington** `[S–M]`
- [ ] **A10 · Des Moines** `[S–M]`
- [ ] **A11 · SeaTac** `[S–M]`
- [ ] **A12 · Tukwila** `[S–M]`
- [ ] **A13 · Enumclaw** `[S–M]`
- [ ] **A14 · North Bend / Duvall / Carnation / Black Diamond** (Snoqualmie-valley cluster; batch if same vendor) `[M]`
- [ ] **A15 · Woodinville** `[S–M]`

**Fallback quality:**
- [ ] **A16 · Tier-2 fallback quality pass.** Batch the ~11 homepage-only towns: verify each portal URL, add "call city hall / low permit volume" note, ensure none are mis-flagged as unincorporated KC. `[S]`

> Frontier PR count is a **range (12–18)** pending A1: fewer if cities cluster onto 1–2 vendors we adapt once, more if each runs a different system. This uncertainty is the single biggest driver of total epic size.

---

## Workstream B — Data quality & correctness

(B1, B2 listed as P0 above.)

- [ ] **B3 · Seattle parcel search.** Currently address + permit-number only; parcel queries silently miss Seattle. Add parcel path to the Socrata queries. `[M]`
- [ ] **B4 · Consolidate date parsers.** Four separate parsers (`parse_date`, `parse_lni_date`, `_arcgis_date`, `_iso_date`) — unify into one hardened util with a shared test table. `[S]`
- [ ] **B5 · Bellevue address-match precision.** ArcGIS uses `UPPER(SITEADDRESS) LIKE '<addr>%'` — audit for prefix false-positives (`12 Main` matching `120 Main`) and unit/suffix false-negatives. `[S]`
- [ ] **B6 · Unified status vocabulary.** Each source emits its own status strings; CLAUDE.md asks agents to highlight "Expiration Notice"/"Corrections Required" that only some sources use. Map to a normalized status enum while keeping the raw value. `[M]`

---

## Workstream C — Performance & reliability

- [ ] **C1 · Parallelize source queries.** Parcel/no-city address lookups hit 12 MBP jurisdictions + every EnerGov portal **serially**. Thread-pool the independent sources — biggest latency win in the tool. `[M]`
- [ ] **C2 · Uniform retry/backoff.** Only L&I retries (once). Wrap all outbound calls in a shared retry-with-backoff helper for transient 5xx/timeouts. `[M]`
- [ ] **C3 · Standardize timeouts.** Currently a mix of 10/15/30s literals; centralize and make env-overridable (`KCPS_TIMEOUT`). `[S]`
- [ ] **C4 · Cache parcel/geocode resolution.** `_geocode_parcel` re-hits KC ArcGIS every run; add a short-lived on-disk cache keyed by normalized address. `[S]`

---

## Workstream D — Freshness & routing metadata

- [ ] **D1 · Surface staleness in output.** `routing_data.json.last_verified` (2026-06-23) is invisible to callers; emit a `stale` warning when it's older than N days. `[S]`
- [ ] **D2 · Scheduled refresh.** Wire `refresh.py` into a CI cron that opens an auto-PR when routing metadata drifts. `[M]`
- [ ] **D3 · Portal-drift detector.** Bellevue silently retired its EnerGov hostname; add a check that flags a city's portal returning unexpected shape before it becomes a silent coverage hole. `[M]`
- [x] **D4 · `refresh.py` trusted the MBP UI dropdown as ground truth (FIXED 2026-07-15).** Was: `fetch_mbp_jurisdictions()` scrapes the public dropdown and `--apply` did `data["cities_on_mbp"] = sorted(mbp_cities)` — a wholesale overwrite. MBP had dropped **Newcastle** (KC) and **Mill Creek** (Sno.) from the dropdown while the backend still served them by JurisId (85 real Newcastle permits, `Jurisdiction: Newcastle`), so `--apply` would have silently cut Newcastle → scorecard 🟢→🟡. **Fix:** added `mbp_backend_alive()` (probes JurisId for records / "too many results" cap across common streets); the refresh now confirms every dropdown drop against the backend and **never auto-drops** — UI-drift cities are retained silently, unconfirmed drops are kept and flagged for manual review. Apply changed to add-and-retain (`current | added`). Verified live: Newcastle + Mill Creek now report `RETAINED` instead of `LEFT MBP`. Tests: `tests/test_refresh.py::MbpDropdownDriftTests` (2 new; suite 130 green).

---

## Workstream E — Agent contract & interface

- [ ] **E1 · Distinguish `none` from `refine` by exit code.** Both currently exit 1, so an agent can't tell "no permits exist" from "a source failed, retry." Give `refine` its own code (e.g. 3). *(Contract change — coordinate with tool.json + sibling tools.)* `[S]`
- [ ] **E2 · Machine-readable error taxonomy.** `errors[]` is free-text today; add a structured `{source, code, message}` shape so agents can branch. `[M]`
- [ ] **E3 · `--version` flag.** Round out the CLI contract (follows the `--help` fix in #12). `[S]`
- [ ] **E4 · Per-city permit-number format validation.** Detect/validate permit-number shapes per jurisdiction to route exact-match searches without spraying every portal. `[M]`

---

## Workstream F — Distribution (parity with sibling tools)

- [ ] **F1 · MCP server wrapper.** Sibling tools (inkcheck, loudcheck, otio-diff) ship CLI **+ MCP**; this one is CLI + `--pipe` + `tool.json` but no MCP server. Add one over the same engine. `[M]`
- [ ] **F2 · pip-installable package.** `pyproject.toml` + `kcps` console entry point so it's `pipx install`-able, not clone-and-run. `[S]`

---

## Workstream G — Testing & CI

- [ ] **G1 · Opt-in live smoke suite.** All 128 current tests are network-free (good). Add a nightly-CI live integration suite hitting one known-good query per source, so a portal going dark is caught fast. `[M]`
- [ ] **G2 · Golden-output fixtures.** Capture a normalized golden result per source to catch schema drift in the mappers. `[S]`
- [ ] **G3 · Coverage gate.** Add coverage measurement to CI with a floor. `[S]`

---

## Rollup

| Workstream | PRs |
|---|---:|
| A · Coverage frontier (vendor-adapter driven, range) | 12–18 |
| B · Data quality | 6 |
| C · Performance & reliability | 4 |
| D · Freshness & routing | 3 |
| E · Agent contract | 4 |
| F · Distribution | 2 |
| G · Testing & CI | 3 |
| H · Docs & truth (P0 items) | 2 |
| **Total** | **36–42** |

*A's range is vendor-spread-dependent and resolves once A1 lands. Even at the low end the epic clears the 20-PR floor; the non-frontier workstreams (B–H = 24 PRs) are already firm.*

**Suggested first slice (one milestone, ~8 PRs):** B1, B2, H1, H2 (P0 integrity/truth) → A1, A2 (recon + generalize EnerGov) → A3, A4 (Redmond + Shoreline as the proof that the generalized client scales the frontier).
