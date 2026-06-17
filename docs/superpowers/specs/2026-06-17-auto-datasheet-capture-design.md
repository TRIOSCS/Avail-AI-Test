# Auto-Datasheet Capture — Design Spec

**Date:** 2026-06-17
**Status:** Approved (brainstorm) — pending spec review → implementation plan

## Problem

`MaterialCard.datasheet_url` today holds only a **link** to a vendor/manufacturer
datasheet. Vendors routinely pull datasheets for EOL parts, so the link rots and the
spec sheet is lost exactly when it's hardest to re-find. We need a permanent **copy** we
control, attached to the item card, captured automatically as parts flow through the
two places they naturally surface: **search** and **RFQs**.

## Goal

When a part is searched (the Part Dossier) or used on an RFQ, automatically find its
datasheet, download a copy, **verify it is the right document**, store our own copy in
OneDrive, and attach it to the part's `MaterialCard` — surviving any later vendor
takedown. Never block the user; never save an unverified/wrong file as the permanent
record.

## Non-Goals (YAGNI / future follow-ups)

- Mass backfill of the existing catalog (this is on-demand via the two triggers).
- OCR for image-only / scanned datasheets (require extractable text for verification).
- Multiple datasheets / revision management per part (store one best copy to start).
- Clay as the datasheet finder (Clay is for GTM/contact enrichment; the in-app parts
  connectors + Claude `web_search` are the right tools here).

## Triggers

Both enqueue a **background** capture job and return immediately — no added latency to
the user request, and a capture failure can never break search or RFQ flows.

1. **Part search** — fires from the dossier hero read (the existing instant-DB-read on
   `GET /v2/partials/search/dossier/hero`). Enqueue capture for the searched MPN.
2. **Part used on an RFQ** — fires when an MPN is added to a requirement (the
   requirement create/add-MPN path). Enqueue capture for that MPN.

De-duplication is handled by the job's gate (below), so repeated searches/RFQs of the
same part do not repeat work.

## Capture Pipeline (per MPN)

A single background job, isolated per MPN, runs these steps in order:

1. **Gate (dedup + negative cache)**
   - If the card already has a stored, verified datasheet → **skip**.
   - If `datasheet_searched_at` is set and < **30 days** ago with no stored datasheet
     → **skip** (negative cache; avoids re-hunting parts with no public datasheet and
     repeat web-search spend).
   - Otherwise proceed.

2. **Find candidate URL(s)** — in priority order:
   1. **Connector/enrichment link** — `MaterialCard.datasheet_url` (already captured by
      the enrichment ladder from Octopart/DigiKey/Mouser/OEMSecrets). **Trusted source.**
   2. **Web fallback** — if no connector link, Claude `web_search` for the official
      datasheet PDF for `<MPN> <manufacturer>`. Returns candidate PDF URL(s). **Untrusted
      source** (must pass verification).

3. **Download** — fetch the candidate. Guards: response must be a PDF (content-type
   `application/pdf` or `%PDF` magic bytes), size ≤ **25 MB**, with a request timeout.
   Failures fall through to the next candidate, then to "none found."

4. **Verify**
   - **Trusted source** (connector link) → accept as-is.
   - **Untrusted source** (web fallback) → extract PDF text; require the part's MPN —
     exact normalized match or a close variant via `fuzzy_mpn_match` — to appear in the
     document text. Match → accept. No match → reject that candidate.
   - If no candidate is accepted → **none found** (go to step 7b).

5. **Store** — upload our copy to **OneDrive via Graph**, reusing the existing attachment
   storage path (the mechanism behind requisition/requirement/offer attachments). File
   named deterministically, e.g. `<MPN>-datasheet.pdf`.

6. **Attach (success)** — create one `material_card_datasheets` row linked to the card
   (schema below) and stamp `MaterialCard.datasheet_captured_at`.

7. **Stamp (outcome)**
   - a. Success → `datasheet_captured_at = now`.
   - b. None found → `datasheet_searched_at = now` (drives the 30-day cooldown). No row
     created.

## Cardless MPNs (approved rule change)

Today a bare search never creates a `MaterialCard` (dossier decision #6). **Approved
exception:** if a **verified** datasheet is found for an MPN that has no card, create a
minimal `MaterialCard` (normalized + display MPN, manufacturer if known) to hold it — a
verified datasheet is genuine material enrichment and justifies the card. A *failed*
hunt (none found) never creates a card. RFQ-triggered captures use the requirement's
linked/created card as usual.

## Data Model

New table `material_card_datasheets` (one row per stored datasheet; one per card to
start). Follows the existing `*_attachments` OneDrive pattern:

| column            | type          | notes                                              |
|-------------------|---------------|----------------------------------------------------|
| id                | int PK        |                                                    |
| material_card_id  | int FK → material_cards (CASCADE), indexed |                       |
| file_name         | str(500)      | e.g. `<MPN>-datasheet.pdf`                          |
| onedrive_item_id  | str(500)      | stable handle for fetch/serve                      |
| onedrive_url      | text          | convenience (may expire; item_id is source of truth)|
| content_type      | str(100)      | `application/pdf`                                   |
| size_bytes        | int           |                                                    |
| source            | str(50)       | `connector` \| `web` — provenance                  |
| original_url      | text          | where the copy came from (audit/provenance)        |
| verified          | bool          | true for connector links and MPN-matched web hits  |
| captured_at       | UTCDateTime   |                                                    |

New `MaterialCard` columns:
- `datasheet_captured_at` (UTCDateTime, nullable) — set on successful attach.
- `datasheet_searched_at` (UTCDateTime, nullable) — set on every attempt; drives cooldown.

(`datasheet_url` stays as the original link/provenance; the stored copy is the new record.)

## UI

On the dossier **"Specs & enrichment"** section, render the datasheet state:
- **Stored** → a **Datasheet** button that downloads *our* copy (served through the app
  from OneDrive by `onedrive_item_id` — never the vendor link), with a small
  "captured `<date>` · `<source>`" caption.
- **In flight** (job enqueued, not done) → "Fetching datasheet…".
- **None found** → "No datasheet found (will retry later)".

The download is an authenticated app endpoint that streams the OneDrive copy.

## Error Handling

- The whole job is wrapped so any failure (find/download/verify/upload) is logged and
  never propagates to the search/RFQ request (background + isolated per MPN).
- Download/verify failure on a candidate → try the next candidate, else stamp
  `datasheet_searched_at` (cooldown retry).
- OneDrive/Graph upload failure → retry within the job up to **2×**; if still failing,
  leave unstored and stamp `datasheet_searched_at` (picked up next cooldown).

## Concurrency & Cost Control

- Dedup + 30-day negative cache prevent repeat hunts and repeat web-search spend.
- Web/AI fallback runs **only** when no connector link exists.
- Reuses the existing background-task/enrichment-worker infrastructure (no new runtime).

## Testing

- **Unit:** verification (MPN-in-PDF accept/reject incl. `fuzzy_mpn_match` variants);
  gate logic (skip-if-stored, skip-within-cooldown, proceed-after-cooldown);
  candidate-ordering (connector before web); download guards (non-PDF / oversize reject).
- **Integration:** search and RFQ triggers each enqueue a capture job; a successful job
  creates a `material_card_datasheets` row + stamps the card (Graph upload + HTTP
  download mocked); cardless-MPN-with-verified-datasheet creates a minimal card; the
  dossier specs section renders each of the three UI states; the download endpoint
  streams the stored copy.
- **Live-verify** after deploy (per project norm): drive a real search on a part with a
  known datasheet and confirm a stored copy attaches and downloads.

## Migration

One Alembic migration: create `material_card_datasheets`; add `datasheet_captured_at` +
`datasheet_searched_at` to `material_cards`. (Revision id ≤ 32 chars; coordinate via
`MIGRATION_NUMBERS_IN_FLIGHT.txt`.)

## Dependencies / Notes

- Datasheet coverage from connectors depends on those connectors being live; pre-go-live
  most are off (see project memory on broker APIs at go-live), but the web fallback and
  the two live connectors (Mouser/element14) provide partial coverage now, and full
  coverage arrives when the connector APIs are enabled at go-live.
- Update the relevant `docs/APP_MAP_*.md` after implementation.
