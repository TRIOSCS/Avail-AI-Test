# AI Opportunity Survey — data entry, data management, data out
2026-08-21 · produced by a 9-agent survey (4 codebase mappers → 3 ideation lenses → 2 adversarial verifiers).
23 raw ideas; 1 killed as an in-batch duplicate; 22 below. Every idea extends existing plumbing
(claude_client.py, paste-parse services, amber "AI — verify" chip, preview-then-confirm imports).
All are AI-drafts-human-confirms; none touch Acctivate; no auto-approve or margin logic.

Recommended order (owner accepted 2026-08-21):
1. Quick wins (S effort, high value): QP serial paste · PO-confirm paste-prefill · vendor-name dup nudge · equivalence-aware search recall
2. Then: resell reply → offer prefill (the codebase reserved this seam in email_service.py)
3. Big ticket, own session: Ask AVAIL (whitelisted query templates)

Merge notes from verification: ideas 8 + 19 = ONE QP drafting service (deterministic deal-copy +
pasted-TSO extraction primary, prior-QP carry-forward secondary); idea 14's search half is idea 21.

---
## 1. PDF-to-lines: quote/RFQ PDFs feed the existing paste-parse flows

**Value:** high · **Effort:** M

**Where:** sourcing intake — /root/availai/app/services/freeform_parser_service.py, /root/availai/app/services/ai_email_parser.py, /root/availai/app/routers/htmx/requisitions.py (import-parse line 569), /root/availai/app/routers/htmx/offers/crud.py (parse-email line 79), /root/availai/app/templates/htmx/partials/requisitions/unified_modal.html, /root/availai/app/utils/claude_client.py

**What:** Input: the vendor-quote or customer-RFQ PDF that today is 100% hand-keyed (the reader survey confirmed no PDF field-extraction path exists anywhere; attachments are stored blobs only). AI step: extract text with pypdf (already a dep, used by datasheet_capture) and feed it to the existing parse_freeform_rfq / ai_email_parser prompts; for scanned PDFs, add a document-block input option to claude_client. Output: the same editable review grids the paste flows already land in (unified modal parts grid, offer cards) — the human corrects and saves exactly as today. The human stops re-typing 20-line PDF quotes cell by cell; only the file-type gate changes.

**Builds on:** freeform_parser_service/ai_email_parser paste-parse flows + claude_structured; the review-grid-before-save UX is untouched

**Risk:** Scanned/image PDFs need the claude_client document-block extension (a real but small API addition); table-heavy PDFs can mis-column — mitigated because output always lands in the mandatory edit grid.

**Verifier (pass):** Verified genuine gap: pypdf is imported ONLY by app/services/datasheet_capture.py; app/services/attachment_parser.py has zero PDF handling; requisition_import_parse (app/routers/htmx/requisitions.py:604-612) handles .xlsx/.csv and decodes everything else as utf-8 text — a PDF becomes garbage. Reuses parse_freeform_rfq/ai_email_parser and the existing review-grid-before-save UX; no ERP touch, no silent writes.

**Verifier (pass):** All citations verified: pypdf==6.16.1 in requirements.txt (used only by datasheet_capture.py), import-parse POST at requisitions.py:569 already accepts UploadFile with a file-type gate that handles only .xlsx/.xls/.csv (PDF falls to raw byte-decode garbage — the 'no PDF path' claim is true), parse-email at offers/crud.py:79, parse_freeform_rfq/parse_freeform_offer in freeform_parser_service.py:143/172. Adding a pypdf branch to the existing gate lands in the same editable review grids. Effort M is right (S if you skip the scanned-PDF document-block half; claude_client.py has no document input today so that half is real work). Would build.

## 2. QP serial section: paste a packing list, get the rows

**Value:** high · **Effort:** S

**Where:** Quality Plan serial/FRU — /root/availai/app/templates/htmx/partials/qp/_section_serial.html, /root/availai/app/routers/quality_plans.py (POST /v2/qp/{id}/serial at ~507)

**What:** Input: the vendor packing list / test report text (or column) pasted into a new textarea above the serial grid. AI step: one claude_structured call maps it to QpSerialEntry rows (PO, part #, serial #, Seagate SN, TSO, customer PO). Output: a preview table with per-row checkboxes; confirm bulk-inserts. The human stops doing 50 one-row-per-submit passes of long alphanumerics — the single worst hand-keying surface per the entry survey.

**Builds on:** claude_structured JSON-schema pattern; same preview-then-confirm shape as the resell import_preview.html flow

**Risk:** Lowest-risk idea here; serials are verbatim strings so the only failure mode is column mis-assignment, caught in the preview.

**Verifier (pass):** Verified: the only write path is one-row POST /v2/qp/{qp_id}/serial (app/routers/quality_plans.py:499) and _section_serial.html contains no textarea/paste/bulk affordance. Preview-with-checkboxes then confirm matches the resell import_preview pattern; human confirms every insert. Smallest, highest-pain win in the list.

**Verifier (pass):** Verified exactly: POST /v2/qp/{qp_id}/serial at quality_plans.py:499 is one-row-per-submit, and QpSerialEntry (models/quality_plan.py:238) has precisely the cited fields — purchase_order, part_number, serial_number, seagate_sn, tso, customer_po. The resell import_preview.html preview-confirm shape exists to copy. High-volume verbatim string extraction from pasted text is the good side of the extraction-trap line (hand-keying 50 serials has a worse error rate than checkbox-reviewing a preview). Effort S is fair for a strong dev: textarea + one claude_structured call + preview + bulk-insert endpoint.

## 3. Resell 'Convert to offer' AI prefill from the matched buyer reply

**Value:** high · **Effort:** M

**Where:** Resell — /root/availai/app/email_service.py (Tier 2.5 match at ~974-988, has_offer=False), /root/availai/app/routers/resell.py (offer-form line 1316), /root/availai/app/templates/htmx/partials/resell/offer_form.html, /root/availai/app/services/ai_email_parser.py

**What:** Input: the buyer's bid email, which poll_inbox already matches to the resell outreach but deliberately leaves unparsed. AI step: an on-demand 'Draft offer from reply' button runs the reply body through an ai_email_parser-style extraction scoped to the list's line MPNs (per-line bids vs take-all, qty, unit price, lead time, terms). Output: the existing offer_form.html modal pre-filled per line, human reviews and submits each. The owner stops hand-compiling multi-bidder Excel sheets and traders stop 10 modal passes per 10-line bid. Customer identity stays hidden — the parse only sees the reply and line MPNs.

**Builds on:** EXTENSION of response_parser/ai_email_parser confidence-gated extraction + the existing Tier 2.5 reply matching; button-triggered like parse-response-attachments

**Risk:** Bid emails are terser and messier than vendor quotes (phone-typed one-liners); mis-scoped per-line vs take-all needs a prominent toggle in the prefilled form.

**Verifier (pass):** Verified: app/email_service.py ~974 comments 'offer extraction stays MANUAL (the Convert-to-offer quick-add), so has_offer is False', and _reply_viewer.html carries the quick-add with no AI prefill — the delta is real. One guard to honor: keep it strictly the on-demand button as written; do NOT wire resell replies into the mining pipeline's >=0.8 auto-create-draft path, since manual extraction there is a deliberate design decision recorded in that comment. Customer identity untouched.

**Verifier (pass):** Strongest citation of the batch: email_service.py:968-993 is the Tier 2.5 resell-reply match with has_offer=False and an in-code comment saying offer extraction 'stays MANUAL (the Convert-to-offer quick-add)' — the codebase literally reserved this seam. offer-form at resell.py:1316, offer_form.html, ai_email_parser.py and response_parser.py all exist. Button-triggered, human submits each pre-filled modal. Effort M is right. One overstatement: traders still do the modal passes, just pre-filled — the typing, not the clicks, is what's saved.

## 4. Revision-email diff applier for requirements

**Value:** high · **Effort:** M

**Where:** Requisition detail — /root/availai/app/routers/htmx/requisitions_edit.py (update_requirement line 436), /root/availai/app/services/freeform_parser_service.py, /root/availai/app/services/field_audit.py, /root/availai/app/templates/htmx/partials/requisitions/inline_cell.html

**What:** Input: the customer's revision email ('qty now 800, need-by moved to 9/15, drop line 3') pasted into a box on the requisition detail. AI step: claude_structured receives the current requirement rows plus the pasted text and returns a field-level change proposal (requirement, field, old, new). Output: a checklist diff the human ticks and applies; accepted changes go through the existing update_requirement PUT so field_audit records old→new normally. The human stops hunting cells to apply a revision line by line — today's explicit pain in the entry survey.

**Builds on:** freeform_parser_service structured extraction + existing inline-edit PUT path and field_audit trail; no new write seam

**Risk:** Ambiguous references ('the Seagate line') can target the wrong row — mitigate by showing the matched MPN on every proposed change and defaulting unmatched proposals to unchecked.

**Verifier (pass):** Verified: update_requirement exists at app/routers/htmx/requisitions_edit.py:437 and app/services/field_audit.py exists. Human-ticked checklist applied through the existing PUT means field_audit records every old->new; nothing writes without confirmation. Clean reuse of freeform_parser_service.

**Verifier (pass):** Verified: update_requirement PUT at requisitions_edit.py:437 (decorator at 436 — citation exact), field_audit.py has diff_fields/log_field_edits so old→new audit falls out of the existing seam, inline_cell.html exists. Checklist-diff review (old vs new per field) is exactly the right UX for AI output here — the human verifies deltas, not a wall of fields. Note the PUT takes the full field set (primary_mpn etc. are Form(...)), so the apply step must post current values plus accepted changes — the diff UI holds that state anyway. Effort M correct.

## 5. Signature paste-to-contact in the CRM contact modal

**Value:** medium · **Effort:** S

**Where:** CRM — /root/availai/app/services/signature_parser.py, /root/availai/app/templates/htmx/partials/customers/tabs/_contact_form.html, contacts routes under /root/availai/app/routers/htmx/companies/

**What:** Input: an email signature block pasted into a small box at the top of the existing ~15-field contact modal. AI step: the already-built signature_parser (regex fast-path, Claude fallback, EmailSignatureExtract cache) runs on demand instead of only inside inbox mining. Output: name/title/email/phone/secondary/LinkedIn pre-filled in the same form; human fixes and saves. Each new deal counterpart drops from a full modal pass to a paste plus corrections.

**Builds on:** EXTENSION of signature_parser (currently only invoked by the batch mining jobs) surfaced into the contact form via one HTMX endpoint

**Risk:** Near zero — parser and cache exist; the only work is the endpoint and form-fill wiring.

**Verifier (pass):** Verified: grep shows NO router references signature_parser — it runs only inside the batch mining jobs today, so the on-demand surface is a genuine delta; _contact_form.html exists at the cited path. One HTMX endpoint reusing an already-built parser + EmailSignatureExtract cache; prefill-then-human-save.

**Verifier (pass):** Verified: signature_parser.py has parse_signature_regex:129, parse_signature_ai:238, extract_signature:293 (regex first, Haiku fallback under 0.7 confidence) and cache_signature_extract with EmailSignatureExtract — and its only callers are jobs/core_jobs.py and services/contact_intelligence.py, confirming 'batch mining only' today. Surfacing an already-shipped, already-hardened parser through one HTMX endpoint into _contact_form.html is genuinely S. Lowest-risk idea on the list.

## 6. PO-confirm paste-prefill (human carries the text, AI fills the form)

**Value:** high · **Effort:** S

**Where:** Approvals PO tab — /root/availai/app/templates/htmx/partials/approvals/_pane_po_line.html (confirm-po form), /root/availai/app/routers/htmx/approvals_hub.py (render_po_pane, confirm-po POST)

**What:** Input: the buyer selects-all and copies the PO confirmation text from the ERP screen (or uses the emailed PO PDF via the PDF extractor) and pastes it into the confirm pane — the inbound twin of the existing 'Copy for ERP' clipboard chip. AI step: claude_structured pulls po_number, est. ship date, payment method, and serial numbers, cross-checking vendor/MPN/qty against the plan line and flagging mismatches. Output: the confirm-po form pre-filled for review. Kills the highest-stakes double-entry (a po_number typo propagates to prepayments and the QP). No integration: no read, no sync — the human transports the text every time, exactly as today.

**Builds on:** claude_structured with interactive max_attempts=1 mode; mirrors the existing Copy-for-ERP chip in the same pane

**Risk:** Owner-judgment risk: manual ERP re-keying is 'by design', so confirm the owner reads paste-prefill as staying manual (it does — the human initiates and reviews every field). Also keep field names ERP-neutral; the parser prompt must not assume Acctivate screen layout.

**Verifier (pass):** Verified: _pane_po_line.html has the raw po_number input (line 118) and the outbound Copy-for-ERP copy_chip (line 113) with no inbound prefill. The human transports the text every time — no read/sync/write against Acctivate, so the manual-by-design constraint holds; mismatch flags are advisory, the human still clicks Confirm PO. Mirror-image of an existing chip in the same pane; interactive max_attempts=1 mode already exists in claude_client.

**Verifier (pass):** Verified: confirm-po form at _pane_po_line.html:104 with po_number:118, estimated_ship_date:122, payment_method:126, purchasing_serial_numbers:154, and the Copy-for-ERP chip at 113 (the outbound twin exists as claimed); render_po_pane at approvals_hub.py:651. This is the closest idea to the low-volume/high-stakes extraction trap, but it clears it: the source paste sits in the same pane so verifying po_number is one glance, the vendor/MPN/qty cross-check catches wrong-PO-on-wrong-line errors hand-keying never catches, and serials are the volume payload. Build note: extract po_number regex-first, AI for the rest. Effort S correct.

## 7. AI row repair in import previews (resell + stock lists)

**Value:** medium · **Effort:** M

**Where:** Imports — /root/availai/app/templates/htmx/partials/resell/import_preview.html, /root/availai/app/routers/resell.py (import-preview 1560/import-confirm 1606, bids/upload-preview 1649), /root/availai/app/services/stock_list_ingest.py, /root/availai/app/services/attachment_parser.py

**What:** Input: the rows a tabular import rejects (today's dead end: fix the source file and re-upload, or re-key via the one-line modal). AI step: a 'Fix with AI' button sends each failed row's raw cells plus the validation error to Claude fast tier, which proposes a corrected structured row. Output: proposals rendered inline in the preview with the existing amber 'AI — verify' chip; only human-confirmed rows join the confirm payload. The human stops round-tripping through Excel for a handful of bad rows.

**Builds on:** parse_tabular_file preview-confirm flow + the part-equivalence amber verify-chip UX; claude_structured per failed row

**Risk:** AI may 'fix' a row by inventing a value the source never had — constrain the prompt to rearrange/normalize only (same guardrail as source_ingest/ai_correct.py: never fabricate, null if absent).

**Verifier (pass):** Verified dead end: import_preview.html renders errors as a flat string list (lines 18-21) with only a Cancel/re-upload affordance, and import-preview/confirm + bids/upload-preview exist at app/routers/resell.py:1560/1606/1649. Note the preview endpoints must first be extended to carry per-row raw cells with their errors (today errors are strings detached from row payloads) — within the idea's stated scope. Only human-confirmed rows join the confirm payload; amber-chip pattern already exists.

**Verifier (adjust):** Two corrections. (1) Citation: parse_tabular_file lives in app/file_utils.py:83, NOT app/services/attachment_parser.py (that file's parse_attachment is the email-attachment path — a different flow). (2) import_preview.html renders errors as flat strings in a <ul> (lines 18-24) and the endpoints (resell.py:1560/1606/1649 — verified) don't carry failed-row raw cells into the preview context, so the build must first thread structured failed rows through — still M, but that's where the effort goes, not the AI call. Dead-end claim confirmed ('Try another file' is the only recovery). With those fixed, viable.

## 8. QP draft-from-deal: TSO document extraction plus dedupe of triple-keyed fields

**Value:** medium · **Effort:** M

**Where:** Quality Plan — /root/availai/app/templates/htmx/partials/qp/_section_sales.html, /root/availai/app/templates/htmx/partials/qp/_section_purchasing.html, /root/availai/app/routers/quality_plans.py, /root/availai/app/models/quality_plan.py

**What:** Input: a 'Draft QP' action that takes (a) the deal's existing AVAIL records (requirement condition/qty/FW/HW/rev, chosen offer terms) and (b) an optional pasted customer TSO/PO document. AI step: deterministic copy for fields AVAIL already holds; claude_structured extracts the genuinely external answers (testing requirements, packaging constraints, ship-early/partial, serial pre-approval) from the pasted document. Output: both QP grids pre-filled, every AI-sourced field visibly badged, auto-PATCH-on-change editing unchanged. Kills typing the same condition/FW/HW/commodity up to three times per TSO.

**Builds on:** claude_structured + the QP's existing auto-PATCH grid; deterministic prefill reuses the buy-plan/QP get-or-create linkage (/v2/qp/for-buy-plan)

**Risk:** The sales/purchasing sections intentionally answer from different perspectives (customer doc vs vendor quote) — prefill must not blur that; badge provenance per field and never copy sales answers into purchasing silently.

**Verifier (adjust):** Genuine (no draft/prefill AI anywhere in app/routers/quality_plans.py), but two fixes: (1) it overlaps idea 19 on the same ~28 QP fields — build ONE drafting service, with this idea's deterministic deal-copy + pasted-TSO extraction as the core and 19's prior-QP carry-forward as an additional suggestion source; (2) because the QP grids auto-PATCH per field, 'pre-filled' must mean staged suggestions that only PATCH on per-field accept (19's wording), otherwise AI-extracted values become saved DB values on one button click — brushing the no-silent-mutation line.

**Verifier (pass):** Verified: _section_sales.html PATCHes /v2/qp/{id}/sales on change (line 67), get-or-create front door at quality_plans.py:231, ~38 typed fields in the coercion map so '~28 free-text answers' holds. Deterministic copy of deal facts AVAIL already owns is the unambiguous win; the claude_structured extraction only covers genuinely external answers from a pasted doc, badged. Effort M correct. This is the QP-prefill idea to build — idea 19 should fold into it (see that verdict).

## 9. Entry-time part lint: MPN sanity + manufacturer suggestion in the requisition grid

**Value:** high · **Effort:** M

**Where:** requisitions module — /root/availai/app/templates/htmx/partials/requisitions/unified_modal.html, /root/availai/app/routers/htmx/requisitions.py, /root/availai/app/routers/htmx/requisitions_edit.py, /root/availai/app/services/ai_part_normalizer.py, /root/availai/app/services/tagging_ai_triage.py (heuristics reused)

**What:** On MPN blur (or once per AI-parsed batch), a debounced HTMX call runs the row through the existing batch normalizer: heuristic real-MPN-vs-internal-PN triage plus one Haiku call that returns normalized MPN + inferred manufacturer. The row gets a non-blocking amber chip: 'looks like an internal PN', 'possible typo of LTSR15-NP (known card)', or a pre-filled manufacturer suggestion for the required red-border mfr field — one tap accepts, typing overrides. Kills the biggest residual pain of the AI bulk-fill path (manual mfr fill-in per row) and stops malformed MPNs from ever minting a bad material card.

**Builds on:** ai_part_normalizer.py (/api/ai/normalize-parts already does batch MPN normalization + manufacturer inference, ≥0.7 confidence) + tagging_ai_triage heuristics + the amber verify-chip UX from part_equivalence

**Risk:** Chip noise during heads-down 20-row grid entry — must be strictly non-blocking, debounced, and silent below confidence threshold; interactive claude_client mode (max_attempts=1) so a slow API never delays a save.

**Verifier (pass):** Verified: /api/ai/normalize-parts exists (app/routers/ai.py:318) but grep finds it wired into ZERO templates, and unified_modal.html shows the pain is real — manual manufacturer autocomplete per row, red-border validation, and a 'need manufacturer' counter (lines 228-237, 458). Non-blocking amber chip, tap-to-accept; reuses an already-built endpoint. (Minor overstatement in the pitch: a non-blocking chip nudges, it cannot 'stop' a bad card.)

**Verifier (pass):** Verified: /api/ai/normalize-parts at routers/ai.py:318, ai_part_normalizer.py with CONFIDENCE_THRESHOLD=0.7 and return-original-on-low-confidence fallback, tagging_ai_triage.py exists. One gap the builder should know: the 'possible typo of known card' suggestion needs a materials similarity lookup (pg_trgm, like vendor_duplicates) that the normalizer does NOT do — it's an add, covered by the M estimate. Non-blocking amber chips with typing-overrides is the right failure posture. Effort M correct.

## 10. Vendor-name duplicate nudge on the manual offer form

**Value:** high · **Effort:** S

**Where:** offers module — /root/availai/app/templates/htmx/partials/offers/_offer_form_fields.html (raw required vendor_name text input), /root/availai/app/routers/htmx/offers/crud.py, /root/availai/app/services/vendor_duplicates.py

**What:** The offer form's vendor_name is today a bare text input — the single biggest source of vendor-row fragmentation. Add an hx-trigger blur check that reuses the existing create-time dup logic (exact normalized name + pg_trgm ≥0.3), with a Haiku confirm only for borderline fuzzy hits, rendering 'Did you mean Arrow Electronics (existing vendor)?' with one tap to adopt the canonical name. The trader stops accidentally minting 'Arrow Elec.' as a new vendor; downstream RFQ reachability and vendor history stop splitting.

**Builds on:** vendor_duplicates.py fuzzy matching + the companies /v2/partials/customers/check-duplicate HTMX dup-check pattern (routers/htmx/companies/core.py:549)

**Risk:** Fuzzy threshold false positives ('Micron' vs 'Microsemi') — nudge must never block save, and adopting a suggestion must only swap the name string, never silently re-link existing rows.

**Verifier (pass):** Verified: _offer_form_fields.html line 15 is a bare required text input with no dup check; app/services/vendor_duplicates.py exists; the check-duplicate HTMX pattern to copy exists at app/routers/htmx/companies/core.py:549. Deterministic-first with Haiku only for borderline hits; one-tap adopt, never auto-rewrites. S effort, directly attacks vendor-row fragmentation.

**Verifier (pass):** Verified exactly: vendor_name is a bare required text input at _offer_form_fields.html:15; check_vendor_duplicate in vendor_duplicates.py:75 with TRIGRAM_SIMILARITY_THRESHOLD=0.3 and rapidfuzz fallback; the check-duplicate HTMX pattern at companies/core.py:549 to clone. Effort S correct. Build note: the Haiku confirm tier is probably unnecessary — deterministic top-N suggestions with one-tap adopt covers it; keep AI out of the hot blur path unless fuzzy noise proves real.

## 11. Manufacturer alias harvester with confirm queue (feeds the seed-only lookup table)

**Value:** high · **Effort:** M

**Where:** materials/admin — /root/availai/app/services/manufacturer_normalizer.py (manufacturers table: canonical_name + aliases JSON), new nightly job registered in /root/availai/app/jobs/maintenance_jobs.py, admin confirm UI modeled on /root/availai/app/routers/admin/spec_codes.py; raw-string sources: Sighting.manufacturer (/root/availai/app/models/sourcing.py:277), Offer.manufacturer (/root/availai/app/models/offers.py:46)

**What:** Nightly batch job collects distinct manufacturer strings from sightings/offers/requirements that miss normalize_brand_name (miss currently returns verbatim, so variants accumulate silently), clusters them, and asks Claude via Batch API to map each variant to an existing canonical or 'new/unknown'. Proposals land in a pending-approval admin list (exactly the spec-codes pattern: human approves before promotion); approval appends the alias to the manufacturers table and optionally backfills the raw columns. 'Seagate'/'SEAGATE'/'Seagate Technology' stop splitting every manufacturer facet and filter.

**Builds on:** manufacturer_normalizer.py alias table + oem_spec_codes_pending human-approval pattern (admin/spec_codes.py) + claude_batch_submit for the cheap overnight classification

**Risk:** The alias map is cached until restart (seed-only by design) — needs an explicit cache-refresh path on approval; a wrong alias silently rewrites future MaterialCard brand writes, so approvals must show sample rows and be reversible.

**Verifier (adjust):** Core is sound and genuinely missing (manufacturers table with canonical_name + aliases JSON confirmed; only a one-shot CLI backfill exists), and the spec_codes pending-approval pattern is the right template. Two adjustments: (1) DROP the 'optionally backfills the raw columns' step — rewriting raw vendor-reported manufacturer strings on Sighting/Offer destroys provenance and contradicts the codebase's store-verdict-don't-rewrite pattern (part_equivalence); normalize at read/display as today. (2) manufacturer_normalizer.py's in-memory cache is explicitly seed-only and 'never invalidated (restart refreshes)' (~line 51) — runtime alias appends require adding cache invalidation, which the effort estimate must include.

**Verifier (pass):** All citations exact: manufacturers table (canonical_name + aliases JSON) in manufacturer_normalizer.py, Sighting.manufacturer at sourcing.py:277, Offer.manufacturer at offers.py:46, spec_codes.py pending/approve/reject queue as the pattern, auto_dedup registration in maintenance_jobs.py, claude_batch_submit at claude_client.py:535. One thing the builder MUST handle: the alias map is cached per-process and 'never invalidated (table is seed-only — restart refreshes)' per the comment at ~line 51 — approving an alias must bust that cache or nothing changes until restart. Effort is honest-M trending M/L (the admin queue clone is real work). Would build.

## 12. Offer free-text structurizer: lead time, date code, packaging into filterable columns

**Value:** high · **Effort:** M

**Where:** offers module — /root/availai/app/models/offers.py (lead_time/date_code/packaging all String(100), no days column; migration adds lead_time_days + normalized date-code bounds), /root/availai/app/utils/normalization.py (existing deterministic lead-time/date-code normalizers), nightly job in /root/availai/app/jobs/maintenance_jobs.py, save paths in /root/availai/app/routers/htmx/offers/crud.py

**What:** At offer save (and via a nightly backfill sweep over existing rows), run the free-text lead_time/date_code/packaging through the deterministic normalizers first; only rows they can't parse go to one Haiku structured call, hard-guarded to return null rather than guess. Parsed values land in new sibling columns with an amber 'AI — verify' chip on the offer row where the AI (not the regex) produced the value. A buyer can finally sort/filter offers by 'ships inside 2 weeks' or 'stock newer than 2023' instead of eyeballing prose like '2-3wk'.

**Builds on:** normalization.py normalizers already applied at email/attachment parse time (this closes the manual-form gap) + auto_dedup-style nightly maintenance job + part_equivalence amber-chip verify UX

**Risk:** Migration on a hot table; ambiguous prose ('stock rotation', 'TBD') must stay null — a wrong lead_time_days is worse than none. Raw columns stay authoritative; structured columns are additive so nothing breaks if parsing is off.

**Verifier (pass):** Verified real gap: normalize_lead_time/normalize_date_code exist (app/utils/normalization.py:215/297) and already populate Sighting.lead_time_days at ingest (app/routers/requisitions/requirements.py:1099, sources.py:180), but Offer has only String lead_time/date_code/packaging (app/models/offers.py:50-53) — stored offers were never normalized. Deterministic-first, AI null-over-guess, raw string never overwritten, amber chip on AI values; matcher untouched (offer-list filtering is not the part-number matcher). ABSORB idea 23 into this one: use its Batch-API approach for the backfill sweep, and claim the migration in MIGRATION_NUMBERS_IN_FLIGHT per house rules.

**Verifier (pass):** Verified: Offer.lead_time/date_code/packaging are String(100) at offers.py:50-53 with no normalized siblings; normalize_lead_time (returns int days) at normalization.py:215, normalize_date_code:297, normalize_packaging:373, already applied at search/parse time (search_service.py:1156-1158) but never to stored rows or the manual form — the gap claim is true. Deterministic-first with null-not-guess AI fallback and amber chips only on AI-produced values is the right shape. Effort M correct. IMPORTANT: this and idea 23 are the same feature — build THIS one and pull in 23's Batch-API backfill + filter UI as its backfill half.

## 13. Requirement notes fact miner: surface buried constraints as one-tap field suggestions

**Value:** medium · **Effort:** M

**Where:** requisitions module — Requirement.notes/sale_notes/description/date_codes (/root/availai/app/models/sourcing.py:149-163), suggestion chips rendered in /root/availai/app/templates/htmx/partials/requisitions/inline_cell.html + _inline_field_form.html, applied via the existing PUT in /root/availai/app/routers/htmx/requisitions_edit.py (update_requirement line 436), extraction via claude_structured in /root/availai/app/utils/claude_client.py

**What:** A per-requisition on-demand action (button on the detail page, Haiku, one call per req) reads each requirement's free-text notes and extracts only facts that already have empty structured homes: condition, packaging, target date-code window, substitutes-accepted, need-by. Each extraction renders as an amber suggestion chip next to the empty inline cell — tap to apply through the existing inline-edit PUT (so field_audit logs it), dismiss to discard. Never writes silently. Constraints a trader typed into notes ('customer needs conformal coating, 2022+ DC only') become visible to matching and filtering instead of being invisible prose.

**Builds on:** claude_structured tool-forced JSON + the existing inline-cell edit endpoints and field_audit trail + amber verify-chip pattern

**Risk:** Extraction hallucination — schema must mandate null-if-absent with a source quote per fact so the human can eyeball the evidence before tapping; keep it on-demand (not a sweep) to cap cost and noise.

**Verifier (pass):** Verified: Requirement.notes/sale_notes/date_codes/packaging/condition all exist (app/models/sourcing.py:145-163) and the inline-edit PUT + field_audit trail are real. Extraction only proposes into EMPTY structured fields, applies through the existing audited PUT on tap, never writes silently. On-demand per requisition keeps cost trivial.

**Verifier (pass):** Verified: Requirement.notes:155/sale_notes:163/description:157/date_codes:153 in models/sourcing.py (cited 149-163 — exact range), inline_cell.html + _inline_field_form.html exist, update_requirement PUT at requisitions_edit.py:437 gives the audited write seam. Only-empty-fields + tap-to-apply + never-silent is the correct posture; on-demand per-req keeps cost bounded. Value medium is honest. Effort M correct. Pass, though it's the first idea I'd cut if the tranche needs trimming — chips beside inline cells across two templates is fiddlier than it sounds.

## 14. Part-equivalence everywhere: entry-time near-miss check + search expansion (EXTENSION of part_equivalence)

**Value:** high · **Effort:** M

**Where:** cross-cutting — /root/availai/app/services/part_equivalence.py (classify_new_pairs line 207, expand_parts line 112), enqueue hooks in /root/availai/app/routers/htmx/requisitions.py (import-save) and /root/availai/app/routers/htmx/offers/crud.py, chips reusing /root/availai/app/templates/htmx/partials/proactive/_match_row.html verdict-form pattern, search read in /root/availai/app/services/global_search_service.py (_add_mpn_match line 100)

**What:** Two clearly-scoped extensions of the shipped equivalence service. (1) When a requirement or offer save mints a normalized key that near-misses an existing material card (shared prefix, differing suffix), enqueue the pair for the existing classify_new_pairs pass and show the amber 'AI — verify / not the same part' chip on the requisition/offer row — same table, same human-override rules, just surfaced outside the Proactive tab. (2) Make global search's exact-MPN arm also OR in stored 'same'-verdict spellings via expand_parts, so searching LTSR15-NP stops silently missing LTSR15-NPR history. No new classification logic, no new tables.

**Builds on:** part_equivalence.py stored-verdict table + amber verify chips + human-outranks-AI rule; search change is a stored-table join only (no live LLM in the search path)

**Risk:** Entry-time enqueue volume vs the 25-pairs-per-pass cap — pairs queue rather than classify inline, so chips may appear on next visit; search expansion must remain a cheap indexed OR so fast_search stays fast.

**Verifier (adjust):** Half (1) is genuine and passes: expand_parts/classify_new_pairs exist (app/services/part_equivalence.py:112/207) and the entry-time enqueue + chip on requisition/offer rows is a clean surface extension of the owner-approved mechanism. But half (2) — global-search expansion via expand_parts — is a verbatim duplicate of idea 21, which covers it more completely (dossier + zero-hit enqueue). Adjust: strip the search half and keep only the entry-time near-miss enqueue/chips; let 21 own every read-side surface.

**Verifier (adjust):** Citations exact (expand_parts at part_equivalence.py:112, classify_new_pairs:207, _add_mpn_match at global_search_service.py:100) — but half (2), the search expansion, is a duplicate of idea 21, which covers it better (adds the dossier and zero-hit enqueue) at S effort. Adjust: strip this idea to half (1) only — the entry-time near-miss enqueue on requirement/offer save plus the amber chip on those rows — and let idea 21 own every read-side surface. Scoped that way effort drops to S and the two ideas stop colliding in global_search_service.py.

## 15. Cross-account contact dedupe sweep (EXTENSION of nightly auto-dedup)

**Value:** medium · **Effort:** M

**Where:** CRM — /root/availai/app/services/auto_dedup_service.py (new contact pass), /root/availai/app/services/contact_dedup.py (existing email/name normalization reused), /root/availai/app/services/contact_merge_service.py (merge executor), job registration in /root/availai/app/jobs/maintenance_jobs.py (lines 31-33), suggestions surfaced like the dup-materials section in /root/availai/app/services/proactive_digest.py (find_duplicate_material_groups:92)

**What:** The nightly auto-dedup job gains a third pass: cluster site_contacts across companies on normalized email (exact) and fuzzy name+company-domain, with Claude confirming only ambiguous name-only pairs. Unlike the vendor/company passes, contacts NEVER auto-merge — every pair lands in a suggestion list ('same person at ACME row A and ACME row B?') with one-tap merge via the existing contact_merge_service or dismiss. Closes the documented gap that cross-company contact duplicates have zero detection today, without touching the policy-allowed same-name-different-owner company carve-out.

**Builds on:** auto_dedup_service job + Claude-confirm tier + contact_dedup normalization + contact_merge_service; digest dup-section pattern for surfacing

**Risk:** Two real different people sharing a name is common — that is why suggestion-only with no auto-merge tier at all; merges must respect per-rep ownership so one rep cannot merge away another's contact.

**Verifier (pass):** Verified: app/services/contact_dedup.py, contact_merge_service.py, and auto_dedup_service.py all exist, and the inventory confirms the current job has no contact pass. Critically it NEVER auto-merges — suggestion list with one-tap merge/dismiss — so it avoids the silent-mutation trap the vendor/company passes were flagged for in the 08-10 reviews, and it preserves the same-name-different-owner carve-out.

**Verifier (pass):** Verified: auto_dedup_service.py, contact_dedup.py, contact_merge_service.py all exist; auto_dedup job registered in maintenance_jobs.py (~line 32); find_duplicate_material_groups at proactive_digest.py:92 exactly; site_contacts is the real table (models/crm.py:66/271). Never-auto-merge for contacts with one-tap merge via the existing contact_merge_service respects the same-name-different-owner company carve-out. Effort M correct (the suggestion-surfacing UI is most of it).

## 16. ERP reference-number transcription lint (SO# / customer PO# / PO#)

**Value:** medium · **Effort:** S

**Where:** approvals workspace — /root/availai/app/templates/htmx/partials/approvals/_sheet_header.html (sales_order_number, customer_po_number inline cells), /root/availai/app/templates/htmx/partials/approvals/_pane_po_line.html (po_number in confirm-po form), save endpoints in /root/availai/app/routers/htmx/approvals_hub.py, checker as a small service using claude_client fast tier

**What:** These hand-keyed reference numbers are how AVAIL cross-links the ERP paper trail, and today they are unvalidated free text. On save, compare the typed value against the field's own accepted history in AVAIL (per-field: recent buy_plans_v3 values): a cheap deterministic shape check first (length/prefix/charset drawn from history), with a once-a-day Haiku call that distills the historical values into a cached per-field pattern. Mismatch renders a dismissible amber warning — 'this doesn't match the usual SO# shape (e.g. 12847)' — plus a same-value-already-used-on-another-plan duplicate check. Reads nothing from Acctivate, stores nothing vendor-named; it lints AVAIL's own reference columns.

**Builds on:** existing inline header-edit endpoints in approvals_hub.py + claude_client fast tier with interactive max_attempts=1 + amber-nudge UX

**Risk:** Cold start: with few historical values the learned shape is unreliable — suppress the lint until N accepted examples exist; must stay a dismissible warning (formats will legitimately change at the 2027 Dynamics move).

**Verifier (pass):** Verified: _sheet_header.html line 70 renders sales_order_number/customer_po_number inline cells and _pane_po_line.html:118 is a raw po_number input — all unvalidated free text today. It reads only AVAIL's own buy_plans_v3 history (no Acctivate read), field names stay ERP-neutral, and the warning is dismissible amber, never blocking. Learned-from-history shapes also self-correct across the 2027 Dynamics cutover. The duplicate-value check is the highest-value piece.

**Verifier (adjust):** Files check out (_sheet_header.html:70 iterates exactly sales_order_number + customer_po_number inline cells; po_number in the confirm form at _pane_po_line.html:118) — but drop the once-a-day Haiku 'pattern distillation'. Length/prefix/charset stats over recent buy_plans_v3 values is 20 lines of Python, deterministic and debuggable; an LLM-distilled pattern is an unnecessary failure mode that will hallucinate over-tight patterns on sparse early history and train users to dismiss the amber warning. Keep: deterministic shape check + pure-SQL same-value-on-another-plan duplicate check. Zero AI, effort still S, and now I'd build it.

## 17. Handoff Brief: one-tap AI summary of a requisition / buy plan

**Value:** high · **Effort:** M

**Where:** New sibling service following app/services/activity_digest_service.py; surfaced via app/routers/htmx/requisitions.py (detail page) and app/routers/htmx/approvals_hub.py (detail pane); rendered like the insight panels in app/routers/htmx/insights_views.py

**What:** Input: one requisition or buy plan id. AI step: claude_structured over SQL-computed facts (line/offer/quote status counts, QP gate states from quality_plans, ApprovalAction timeline, recent ActivityLog) produces a fixed-schema brief: what the customer asked for, where each line stands, open blockers, suggested next actions. Output: a cached HTMX panel (Redis nx-lock + cooldown, exactly the ActivityDigest pattern) regenerated lazily when the timeline basis changes. The human stops re-reading a whole requisition thread to hand a deal to a backup buyer or answer 'where is this at?' from a phone — the brief is the handoff.

**Builds on:** activity_digest_service.py get_or_build_digest pattern (claude_structured + anti-stampede lock + cooldown) and the existing insight-panel rendering

**Risk:** Prose drift/hallucination — mitigated by computing every number in SQL and letting the model write only the narrative around supplied facts; stale brief if basis-change detection misses an edge, same known tradeoff as ActivityDigest

**Verifier (adjust):** Partially already built: get_or_build_digest ships TODAY for DigestEntityType.REQUISITION, surfaced in app/routers/htmx/insights_views.py:46-49 — a one-tap AI summary of a requisition exists. Genuine delta: (a) a BUY_PLAN entity with SQL-computed deal-state facts (QP gate states, ApprovalAction timeline, line/offer/quote counts — the existing digest's basis is ActivityLog only) surfaced in the approvals detail pane, and (b) enriching the requisition digest's basis with those same facts. Build it as an extension of activity_digest_service, not a new sibling service.

**Verifier (adjust):** One citation error: ApprovalAction does not exist — app/models/approvals.py defines ApprovalRequest/ApprovalStep/ApprovalStepRecipient/ApprovalEvent/ApprovalOutbox; the timeline model is ApprovalEvent (line 182). One-line fix. Everything else verified: activity_digest_service.py get_or_build_digest at 142 with the nx-lock (line 179, 45s > claude_structured timeout) and cooldown (156/217), insights_views.py exists. Summarization-over-SQL-facts is the reliable end of the AI spectrum (facts computed, prose generated). Effort M correct. With the citation fixed, would build.

## 18. Ask AVAIL: natural-language questions answered by whitelisted query templates

**Value:** high · **Effort:** L

**Where:** Extend app/services/global_search_service.py (ai_search tier) + app/routers/htmx/search_views.py; feeder queries live in app/services/reporting_service.py and app/services/forecast_service.py; CSV output via app/utils/csv_export.py; audit questions read app/services/field_audit.py and app/models/approvals.py (ApprovalAction)

**What:** Input: a typed question ('open reqs for ACME past deadline', 'what did user X edit last week', 'quotes sent but unanswered >7 days', 'buy-plan approval cycle time this month'). AI step: claude_structured maps the question to ONE of ~15 named, parameterized query templates (never free SQL) and extracts the parameters. Output: an HTMX result table showing which template+params ran, with a one-tap CSV of that exact result. This fills the confirmed reporting-page gap (unshipped-reaudit 2026-08-20) without building a BI page: the manager stops exporting 3 CSVs and VLOOKUP-ing on a phone-hostile spreadsheet.

**Builds on:** global_search_service.ai_search (Haiku intent parsing + Redis cache) extended from filter-suggestion to template dispatch; csv_export.py streamer; reporting_service/forecast_service feeders that exist but have no surface

**Risk:** Template coverage disappoints early users ('it couldn't answer my question') — mitigate by logging unmatched questions to grow the template set; misinterpretation is visible because the chosen template and parameters are printed above the results

**Verifier (pass):** Verified: reporting_service.py, forecast_service.py, csv_export.py all exist with no reporting surface (matching the 08-20 unshipped re-audit), and global_search_service.ai_search (line 645) is the right dispatcher to extend. Whitelisted parameterized templates — never free SQL — keeps it inside the simple-over-clever and no-new-infra constraints. Largest effort in the list (L); recommend starting at ~6-8 templates rather than 15, but that is scoping, not a constraint problem.

**Verifier (adjust):** Two corrections, then build. (1) Same ApprovalAction→ApprovalEvent fix (app/models/approvals.py:182). (2) forecast_service is NOT surfaceless — htmx_views.py:413 already renders pipeline_summary; only reporting_service.coverage_report has no router (verified: zero router imports). Core mechanism verified: global_search_service.py has the ai_search Haiku tier with Redis cache helpers (lines 40-48), csv_export.py exists, and the reporting-page gap is confirmed in the 08-20 unshipped re-audit. Whitelisted-templates-never-free-SQL with the template+params shown is the right trust design. Effort L is honest — this is the biggest build on the list; do not let it creep past ~15 templates.

## 19. QP pre-draft from prior similar TSOs (accept-per-field)

**Value:** high · **Effort:** M

**Where:** New drafting service using app/utils/claude_client.py claude_structured; wired into app/routers/quality_plans.py; pre-fill UX in app/templates/htmx/partials/qp/_section_sales.html and _section_purchasing.html; source data from app/models/quality_plan.py, app/models/buy_plan.py, and the linked requisition/requirement rows

**What:** Input: an empty/new QP plus its buy plan, requirement lines, and the 2-3 most recent approved QPs for the same customer and commodity (plain SQL similarity: customer id + commodity/MPN family, no vectors). AI step: claude_structured drafts the ~28 free-text sales+purchasing answers (condition, FW/HW/REV, testing option, routing, packaging, TPO answers), carrying forward what this customer always requires. Output: every drafted field renders amber 'AI — verify' (the part_equivalence chip UX); nothing PATCHes until the human accepts it, per field or accept-all. Kills most of the double/triple re-keying between sales and purchasing sections for repeat customers.

**Builds on:** claude_structured tool-forced output + the amber verify-chip accept pattern from part_equivalence (_offers_drilldown.html/_match_row.html); QP grids already auto-PATCH per field so accept=PATCH needs no new save plumbing

**Risk:** Wrong carry-forward of compliance-flavored answers (testing, packaging, AS9120B-adjacent purchasing fields) — bounded because drafts are never auto-saved, each field shows its source QP, and the approval gates stay fully human

**Verifier (adjust):** Genuine (no AI drafting in quality_plans.py) and its accept-per-field write model is exactly right for the auto-PATCH grids — but it collides with idea 8 on the same ~28 QP fields. Adjust: merge into one QP drafting service where idea 8's deterministic deal-copy + pasted-TSO extraction is primary (grounded in THIS deal's documents) and this idea's prior-QP carry-forward is a secondary suggestion source (clearly labeled — carried-forward preferences can be stale). This idea's nothing-PATCHes-until-accepted rule governs the merged feature.

**Verifier (adjust):** All files verified (QP grids auto-PATCH per _section_sales.html:67, buy_plan.py exists, ~38 typed QP fields) — but this is the third QP-prefill mechanism alongside idea 8's two (deterministic deal facts + pasted TSO doc). Building it standalone puts two competing prefill flows in the same grids. Adjust: fold into idea 8's Draft-QP action as a third source tier (prior approved QPs for same customer+commodity, plain SQL similarity), with per-field SOURCE attribution ('from TSO-1234', 'from pasted doc', 'from deal') rather than one undifferentiated amber chip — carried-forward answers on a quality document are the rubber-stamp risk case, and knowing WHERE a draft value came from is what makes per-field review honest. Merged, the combined effort stays M.

## 20. Weekly data-health digest: staleness and invisibility flags

**Value:** medium · **Effort:** M

**Where:** Extend app/services/proactive_digest.py (new section beside find_duplicate_material_groups) + the weekly job in app/jobs/offers_jobs.py; notify via app/services/in_app_notifications.py; checks read app/services/sighting_status.py + vendor_unavailability.py (staleness), Offer.status pending_review (app/models/offers.py), app/services/vendor_reachability.py, app/services/crm_completeness.py

**What:** Input: nothing new — a deterministic SQL sweep computes: parsed offers stuck in pending_review >N days (currently invisible to the proactive matcher), proactive matches backed by sightings older than sighting_stale_days, requisitions past deadline with no quote, vendors with recent sightings but zero reachable RFQ email, key accounts missing owner/primary contact. AI step: Claude Haiku writes only the 5-line 'what to fix first' summary over the computed rows. Output: a capped section in the existing weekly digest draft + in-app notifications for the worst items. The trader stops discovering these holes one card at a time at send/render time.

**Builds on:** proactive_digest.py already ships data-quality warnings (duplicate-spelling section) in the same weekly draft-never-send job; in_app_notifications write seam; all five checks reuse existing per-card services, just rolled up

**Risk:** Nag fatigue if uncapped — cap items per section and keep weekly cadence; near-zero AI risk since every flag is deterministic and Claude only summarizes

**Verifier (pass):** Verified: proactive_digest.find_duplicate_material_groups exists at line 92 (precedent for data-quality sections in the same draft-never-send weekly job), and sighting_status.py, vendor_reachability.py, crm_completeness.py, in_app_notifications.py all exist; the pending_review limbo is real (app/services/response_parser.py:300). All checks are deterministic SQL; Claude only writes the 5-line summary. No sends, no mutations.

**Verifier (pass):** Every citation verified: sighting_status.py, vendor_unavailability.py, vendor_reachability.py, crm_completeness.py, in_app_notifications.py all exist; the digest job is real (offers_jobs.py:51/88 _job_proactive_digest_drafts → proactive_digest.generate_digests) and find_duplicate_material_groups at proactive_digest.py:92 proves the data-quality-section precedent; pending_review is a real Offer status (constants.py:76, created by email_service.py:1523, promoted at offers/crud.py:752) despite the stale 'active | sold' comment at offers.py:87. AI writes only the 5-line summary over deterministic rows — minimal-risk AI. Effort M correct.

## 21. Equivalence-aware recall in global search and the part dossier

**Value:** high · **Effort:** S

**Where:** app/services/global_search_service.py (fast_search MPN branch, _add_mpn_match) + app/routers/part_dossier.py, reading app/services/part_equivalence.py expand_parts; chip markup reused from app/templates/htmx/partials/proactive/_offers_drilldown.html / _match_row.html

**What:** Input: any MPN searched in the top bar or opened as a dossier. Step: expand the normalized key through the stored part_equivalences table (human verdicts + AI 'same' verdicts) — no LLM call at query time — and append 'also known as LTSR15-NPR' result rows/panels carrying the amber 'AI — verify' chip with the existing one-tap 'not the same part' verdict POST. On a zero-hit search, enqueue classify_new_pairs for that MPN so the next look is covered. Output: the trader asking 'do we have anything under any variant?' gets the answer in search and the dossier, not only in the Proactive tab.

**Builds on:** part_equivalence.py stored-table matching + verify-chip UX and verdict endpoint (/v2/partials/proactive/equivalence/verdict) — pure surface extension of an existing, owner-approved mechanism; respects the part-number-only rule because it IS the sanctioned pooling table

**Risk:** A wrong AI 'same' verdict now propagates to more surfaces — acceptable because every AI row is visibly amber, one tap demotes it permanently (human verdict outranks AI), and human-verified rows dominate over time

**Verifier (pass):** Verified: grep shows part_equivalence/expand_parts referenced NOWHERE in global_search_service.py, search_service.py, or part_dossier.py — the recall gap is real; _add_mpn_match exists at global_search_service.py:100 and the verdict endpoint/chip UX already ship. Stored-table join only, no LLM at query time; it IS the sanctioned part-number pooling table, so the part-number-only matching rule is respected. This idea owns the read-side surfaces; idea 14 keeps only the entry-time half. S effort, high recall value.

**Verifier (pass):** Verified end to end: expand_parts at part_equivalence.py:112, _add_mpn_match at global_search_service.py:100, fast_search:206, part_dossier.py exists, the verdict endpoint is real (proactive.py:919, posted from _offers_drilldown.html:43). No LLM in the query path — a stored-table join plus an enqueue on zero-hit. Effort S correct. Of the overlapping pair, this beats idea 14's half (2): same search change plus the dossier and the zero-hit enqueue. Would build first of the whole list — highest value per line of code.

## 22. Customer 360 that pools owner-fragmented sibling accounts (read-only)

**Value:** high · **Effort:** M

**Where:** EXTENSION of app/services/account_summary_service.py; surfaced where it already renders in app/routers/crm/companies.py (overview tab); pooled facts from app/services/purchase_history_service.py (CustomerPartHistory), quote/requisition rows, and Company.normalized_name siblings in app/models/crm.py; concentration tags via app/services/customer_analysis_service.py

**What:** Input: a company id. Step: before the existing Claude summary runs, gather the sibling Company rows sharing normalized_name (the policy-allowed different-owner duplicates that auto-dedup deliberately skips) and pool their part history, open reqs, quotes, and last-activity into the summary context. AI step: same claude_structured account summary, now over the whole real customer. Output: the summary panel gains an 'includes N sibling accounts (owned by X, Y)' banner; rows stay unmerged and untouched. A manager asking 'what does ACME actually ask us for?' stops getting a per-fragment partial answer.

**Builds on:** account_summary_service.py on-demand summary + auto_dedup's existing normalized-name matching logic, reused read-only instead of merging

**Risk:** Cross-rep visibility: the pooled view exposes sibling-account activity to another owner — keep it read-only, name the owning rep on every pooled fact, and gate the pooled variant to manager/admin if the owner objects

**Verifier (pass):** Verified: Company.normalized_name exists and is kept in lockstep on rename (app/models/crm.py:27,148-159), purchase_history_service.py exists, and account_summary_service is the cited on-demand surface. Strictly read-only pooling with an explicit 'includes N sibling accounts' banner — no merge, so the auto-dedup ownership carve-out is untouched; resell-identity constraint is irrelevant here (CRM-internal). Fine.

**Verifier (pass):** Verified: account_summary_service.py, crm/companies.py, customer_analysis_service.py exist; Company.normalized_name at crm.py:27 with the _sync_normalized_name validator at 148 (so sibling lookup is a plain indexed equality — cheap); CustomerPartHistory at models/purchase_history.py:30. Read-only pooling before the existing summary, rows untouched, owners named in the banner — the right way to handle the policy-allowed duplicates auto-dedup skips. Effort M correct. Minor build note: the summary renders to any CRM viewer, not just managers, so the 'owned by X, Y' banner is doing real disclosure work — keep it.

---

## Killed in verification
- **Offer lead-time and date-code backfill so offers become filterable** — Duplicate of idea 12 within this same batch — identical new columns (lead_time_days + normalized date-code) on app/models/offers.py, identical deterministic-normalizers-first-then-AI approach (normalization.py:215/297), identical filter goal. Idea 12 is the superset (adds the at-save path and packaging). Not a constraint violation — killed purely as redundant; its one genuine contribution, the Batch-API backfill sweep (50% cheaper via batch_queue/tagging_ai_batch pattern), should be folded into idea 12's nightly sweep as noted there.
