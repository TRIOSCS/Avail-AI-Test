# Spec — Pre-send quote pre-flight (advisory)

**Date:** 2026-06-23
**Status:** awaiting review

## Goal
Surface three quality/compliance issues at the moment a broker sends a quote — **do-not-contact recipient, non-US country-of-origin, and MPN drift** — as **advisory warnings**. The send is **never blocked**; the warnings are shown after. **Deterministic only — no AI, no model/migration change.**

## Decisions (from brainstorming)
- Checks: **DNC**, **country-of-origin (non-US)**, **MPN mismatch**. (Margin sanity is explicitly out.)
- Gate behavior: **warn-only — the send always proceeds.** No blocking, no override.
- COO rule: the concern is **non-US stock**. Flag a line when its origin **is not confirmed US** (blank/unknown *or* a non-US country), nudging the buyer to **fill it in or request it from the vendor**. US-origin passes silently.
- No Claude call — plain deterministic flags.
- **Accepted tradeoff:** warn-only means a DNC recipient still gets emailed (just flagged). On the record.

## Verified implementation facts
- Quote lines exist as **`QuoteLine` ORM rows** (`quote.quote_lines`): each has `offer_id` (→ sourced `Offer`) and `mpn`. (There is also a legacy `Quote.line_items` JSON column; the structured rows are the queryable source.)
- `QuoteLine` has **no COO field**; country-of-origin lives on the **sourced `Offer`** (`Offer.country_of_origin`), reachable via `QuoteLine.offer_id`.
- Recipient resolves to the override email or `CustomerSite.contact_email`.
- **Two send paths exist** (see Open Question):
  - `send_quote_htmx` — `POST /v2/partials/quotes/{id}/send` — the **UI "Send Quote" button**; marks the quote `sent` and re-renders the detail partial. *Does not itself email.*
  - `send_quote` — `POST /api/quotes/{id}/send` — actually emails via Graph; returns JSON.

## Design
**Service** — new pure function in `app/services/quote_preflight.py`:
```
quote_preflight(quote, to_email, db) -> list[str]   # human-readable flag strings, [] when clean
```
- **DNC:** if a `SiteContact` with `lower(email) == lower(to_email)` has `do_not_contact=True` → `"Recipient {email} is on the do-not-contact list."`
- **COO (per line, lines with offer_id):** read `offer.country_of_origin`.
  - blank/unknown → `"Line {n} ({mpn}): country of origin not set — fill it in or request it from the vendor."`
  - non-US (not US/USA/United States, case-insensitive) → `"Line {n} ({mpn}): non-US origin ({coo}) — ensure COO documentation is on file or request it from the vendor."`
  - US → no flag.
  - **manual line (no `offer_id`, no source to read)** → `"Line {n} ({mpn}): origin not on file — confirm it's US or request COO from the vendor."` (origin isn't confirmed US, so it gets the same nudge.)
- **MPN mismatch (per line with offer_id):** if `normalize_mpn(line.mpn) != normalize_mpn(offer.mpn)` → `"Line {n}: quoted {line.mpn} ≠ sourced {offer.mpn}."` Manual lines (no `offer_id`) are skipped here — there is no sourced part to compare against.

**Wiring:** `send_quote_htmx` computes `flags = quote_preflight(...)` (before the existing mark-sent), then passes them into `quote_detail_partial(..., preflight_flags=flags)`. `quote_detail_partial` gains an optional `preflight_flags=None` param; all other callers are unaffected.

**UI:** `quotes/detail.html` renders an **amber "Heads up — this quote just went out" banner** listing the flags, only when `preflight_flags` is non-empty. Clean quote → no banner, byte-identical to today.

## Out of scope
Blocking/override, margin check, AI narration, threading, the legacy `line_items` JSON path, changes to `/api/quotes/{id}/send` (see Open Question).

## Open question for review
The UI "Send Quote" button (`send_quote_htmx`) marks-sent + re-renders but **does not email**; the actual email is the separate JSON `/api/quotes/{id}/send`. **Recommendation:** put the pre-flight on `send_quote_htmx` — it's the broker's "send" action and the natural home for the banner. If you also want the warnings on the real email path, we add them to the JSON response there too (small add). Which do you want?

## Tests (TDD)
- DNC recipient → flag; non-DNC → none.
- Line with non-US offer COO → flag; blank COO → flag; US COO → none; manual line (no offer_id) → COO flags "origin not on file" but MPN check skips it.
- MPN drift (line ≠ offer) → flag; matching → none.
- Clean quote → `quote_preflight` returns `[]`; `send_quote_htmx` still returns 200 + marks sent.
- Banner renders only when flags present.
