# Epic 2 — Resolution & Reach (Oct 2026)

Epic 1 ("coverage and hardening") reached the ceiling of what public portals
allow: **20 cities live, 19 indexed via the King County Assessor**, every row
proven weekly, hosted MCP + REST at `permits-api.secondlandings.ai`, listed in
the MCP Registry and on Glama. Epic 2 takes every remaining open issue and
either ships it or closes it with a reason. Scope rule (Dan, 2026-10-11): this
tool is an **atomic query** — *what permits are on file at this address, and
what is their status*. Anything that changes that purpose is a separate tool.

## Plan — one PR per row, in this order

| # | Issue | What ships | Size |
|---|---|---|---|
| 1 | #81 | **Beat MyBuildingPermit's result cap.** When MBP says "too many results", re-query in applied-date windows (`SelectedDateRanges=Applied` + `FromDate/ToDate` — verified to work: Kirkland City Hall 0 → 23+13…) back through the history, union the rows, note `source_capped_split`. If a single window is still capped, surface `next_step{reason: source_capped}`. | M |
| 2 | #79 | **Label the heuristic.** `is_open_basis` per permit (`finaled_date` / `status-vocabulary` / `null`), summary `open_basis_counts`, schema wording "heuristic, census-tested". | S |
| 3 | #36 | **Seattle answers parcel queries.** Socrata has no parcel column, so: PIN → site address (from the Assessor page we already fetch) → Seattle address search, in the parcel fan-out. QC case `real/parcel-Seattle`. | S |
| 4 | #80 | **Index depth on the scorecard.** `qc_scorecard.py --json` records live/index rows per city; the Monday sweep folds them into `source_health.json`; cells render `🗂️ 25`, `✅ 356`. | S |
| 5 | #82 | **Contractor search.** `--contractor "Name"` / `--license N` (CLI, MCP arg, REST param): MBP `SearchBy=People` across its jurisdictions, same schema + summary; documents that only MBP cities are covered. | M |
| 6 | #28 | **Per-type routing table.** `routing_data.json` gains `permit_type_routing` (building / electrical / mechanical-plumbing / land-use / engineering-ROW / fire / septic / grading → system per city); `route.py` reads it; the scorecard's type columns derive from it instead of "live ⇒ all types". | M |
| 7 | #27 | **Publish the vendor map.** `scripts/gen_vendor_map.py` → `vendor_map.json` + `docs/vendor-map.md` (drift-guarded), served at `/vendor-map` and `/vendor-map.json`; per-vendor query recipes and the blocked list with reasons. | M |
| 8 | #26 | **Close** — the 8 small towns are fingerprinted: no portal; Assessor-indexed; rows proven by QC. | — |
| 9 | #56 | **Decide.** Pierce-side records are Pierce County's; this is a King County tool. Default: close as out-of-scope now that Pierce parcels resolve (`pierce:<APN>`) and are labelled. Reopens only if Pierce's PALS exposes a public search cheaply. | — |
| 10 | #60, #18, #21, #23 | **Stay open, documented in the vendor map** as blocked (LAMA paging needs a browser capture; PermitTrax = Blazor; OpenGov = Turnstile; Tukwila = login). | — |
| 11 | release | `server.py` VERSION → 1.1.0; deploy; registry `server.json` version bump (publish needs Dan's `mcp-publisher`); umbrella #25 updated. | S |
| 12 | QC pass | unit suite · `qc_smoke` · `qc_scorecard` (40 cities) · `source_health --write` · hosted smoke (MCP handshake, shaped call, REST). Results posted on #25. | — |

## Acceptance

- Every item above is merged or closed with a written reason; no open PRs.
- Unit suite green; `qc_scorecard` ≥ 39 PASS with Milton the only WARN;
  `source_health` all `ok` from the runner; hosted `/healthz` on the new build.
- README scorecard regenerated; vendor map published; `tool.json` matches `--schema`.
