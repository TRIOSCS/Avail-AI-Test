<!-- PRESERVED 2026-06-27 from an ephemeral session scratchpad, lifted into version
control so the design survives scratchpad/worktree cleanup. Original: session
46711e5f .../scratchpad/c2-blueprint.md (2026-06-26). -->

> **Preserved design doc.** C2a (migration 160) and C2b (migration 161) **SHIPPED**.
> **C2c — the vendor-facing QP share link — is HELD** pending Mike's review of the native
> QP view and confirmation of the redaction whitelist (see companion
> `2026-06-26-qp-c2-field-inventory.md`). NOTE: the C2c "migration 162" below is now TAKEN
> (`162_resource_and_cancellations`) — renumber to the next free head when built.

# QP Phase C2 — blueprint (3 sub-builds). Builds on C1 (engine + buy_plan gate, head 159).
Field inventory + vendor-export decision: see qp-field-inventory-c2.md. QP is the subject for ALL gates
(subject_type=QUALITY_PLAN); gate_type discriminates the section. SALES_ORDER/PURCHASE_ORDER already in
ApprovalGateType. Approvers = per-user toggles, never names. Each sub-build: own PR + migration + review +
deploy; throwaway-PG round-trip every migration; full suite + alembic single head.

## C2a — SO/PO gates + toggles (migration 160, down_revision 159_approval_subject_poly)
- `app/models/auth.py` (after can_approve_prepayments ~L70): add `can_approve_sales_orders`,
  `can_approve_pos` (Boolean NOT NULL server_default false).
- `app/services/approvals/routing.py` `route_request`: add `elif gate==SALES_ORDER` → users with
  can_approve_sales_orders; `elif gate==PURCHASE_ORDER` → can_approve_pos (no amount check). Before the else-raise.
- `app/services/approvals/service.py` `decide()`: after the BUY_PLAN dispatch block, add — if gate_type in
  (SALES_ORDER, PURCHASE_ORDER) and subject_type==QUALITY_PLAN: LAZY import + call
  `_on_section_approved(db, subject_id, gate_type, approved)`; flush. (Lazy import — quality_plan_service imports approvals.service.)
- `app/services/quality_plan_service.py`: `_on_section_approved(db, qp_id, gate_type, approved)` (C2a: stub that
  logs an activity; C2b writes the section timestamp); `submit_section(db, qp_id, gate_type, user)` → validate
  (stub C2a), then `create_request(gate_type=gate_type, amount=None, subject=qp, requested_by=user, owner=user)`.
  Catch `NoEligibleApproverError` → inline "no approver configured" banner (NOT a 500).
- `app/routers/quality_plans.py`: `POST /v2/qp/{id}/submit-sales`, `POST /v2/qp/{id}/submit-purchasing` → refreshed
  detail. Extend `_qp_detail_response` with `_get_gate(db, qp_id, gate_type)` queries (latest ApprovalRequest per
  gate) → pass sales_gate/purchasing_gate/buy_plan_gate/prepayment_gate to context.
- `app/routers/admin/users.py`: `set_sales_order_approver`, `set_po_approver` endpoints mirroring
  set_buyplan_approver (audit APPROVAL_GRANT/REVOKE); add both cols to `users_context` rows.
- `app/templates/htmx/partials/settings/users.html`: two new checkbox columns (Approve SOs / Approve POs),
  hidden-false+checkbox-true, hx-trigger=change, mirror the buy-plan column.
- `app/templates/htmx/partials/qp/detail.html`: replace the Sales/Purchasing "Phase 2" badges with gate-status chips.
- Tests `tests/test_c2a_gates.py`: route_request for each gate (step+recipient created), NoEligibleApprover raises,
  the two admin toggle endpoints flip the column.

## C2b — native QP sections (migration 161, down 160)
- `app/models/quality_plan.py`: add Sales fields (sales_so_number, sales_condition, sales_quantity(Int),
  sales_fw_hw_rev, sales_product_commodity, sales_testing_required(Bool), sales_testing_option,
  sales_testing_specifics, sales_test_location, sales_serial_preapproval_required(Bool),
  sales_authorized_ship_early(Bool), sales_authorized_ship_partial(Bool), sales_routing_prescreening_whs,
  sales_vendor_rating, sales_third_party_pkg_ok(Bool), sales_pkg_requirements, sales_bom_matrix_links, sales_notes)
  + Purchasing fields (purchasing_po_number, purchasing_condition, purchasing_fw_hw_rev,
  purchasing_product_commodity, purchasing_testing_required(Bool), purchasing_testing_option,
  purchasing_routing_prescreening_whs, purchasing_packaging, purchasing_tpo_ship_complete(Bool),
  purchasing_tpo_notes) — all nullable Text/String(255)/Boolean. + `sales_section_approved_at`,
  `purchasing_section_approved_at` (UTCDateTime nullable).
  + `QpSerialEntry` (qp_id FK CASCADE, buyer_id/submitted_by_id FK users SET NULL, buyer_date(Date),
  has_sn_prev_received(Bool), purchase_order, part_number, serial_number, seagate_sn, tso, customer_po,
  submitted_to_customer_date(Date), customer_approved(Bool), customer_approved_date(Date), ops_received(Bool),
  created_at; ix_qp_serial_qp).
  + `QpFruLookup` (qp_id FK CASCADE, fru_norm String(64); UniqueConstraint(qp_id,fru_norm); ix_qp_fru_qp).
- `app/models/__init__.py`: export QpSerialEntry, QpFruLookup.
- `app/services/quality_plan_service.py`: `_validate_sales_section`/`_validate_purchasing_section` (required:
  sales_so_number / purchasing_po_number + the QC-required fields) called by submit_section; implement
  `_on_section_approved` to set the section timestamp + log activity.
- `app/routers/quality_plans.py`: `PATCH /v2/qp/{id}/sales`, `PATCH /v2/qp/{id}/purchasing` (inline field edit →
  refreshed section partial); serial CRUD `POST /v2/qp/{id}/serial` + `DELETE .../serial/{entry_id}`; FRU pin/unpin
  `POST /v2/qp/{id}/fru` + `DELETE .../fru/{lookup_id}`; eager-load serial_entries + fru_lookups (joined FruLink).
- Templates: new `qp/_section_sales.html`, `_section_purchasing.html`, `_section_serial.html`, `_section_fru.html`
  (FRU live-joins FruLink by fru_norm); detail.html `{% include %}`s them, replacing the 4 Phase-2 stubs. Each
  section: field grid (read-only if section approved, editable if draft) + "Submit for Approval" button (hidden once
  a non-rejected request exists) + section_errors (server-driven completeness, disables the submit button).
- Tests `tests/test_c2b_sections.py`: validation (missing SO#/PO#), submit creates the right gate request,
  _on_section_approved sets timestamp, serial create/delete, FRU pin/unpin.

## C2c — vendor share link (migration 162, down 161)
- `app/models/quality_plan.py`: `share_token` String(64) UNIQUE nullable + `share_expires_at` (UTCDateTime
  nullable); ix_qp_share_token.
- `app/services/quality_plan_service.py`: `generate_share_token` (secrets.token_urlsafe(48); non-expiring default;
  wrap flush in try/except IntegrityError → retry once on the unique race), `revoke_share_token`, and
  `build_vendor_safe_context(qp, bp, bp_lines)` — the WHITELIST serializer. `VENDOR_SAFE_FIELDS` frozenset =
  the sourcing-requirements-only set (Sales+Purchasing technical fields, buy-plan MPN+qty+condition NO pricing,
  FRU). EXCLUDE entirely: ALL customer identity (customer name, customer_po, SO#, owner names), unit price/margin,
  and the whole Serial section (customer fields). Returns a plain dict — the share template NEVER gets the raw qp.
- `app/routers/quality_plans.py`: public `GET /qp/share/{token}` (no require_user; 404 if token None/unknown; 410 if
  expired) → render `qp/share_view.html` with the whitelist dict only. Authenticated `POST /v2/qp/{id}/share/generate`
  + `POST /v2/qp/{id}/share/revoke` → refreshed `_share_panel.html`.
- Templates: `app/templates/qp/share_view.html` (standalone, minimal layout, CONFIDENTIAL banner, renders ONLY the
  whitelist dict — no `qp` in scope); `qp/_share_panel.html` (current link + copy + regenerate + revoke, embedded in
  detail). detail.html adds the share panel below the header.
- Route safety (verified by architect): `/qp/share/...` prefix is NOT under ModuleAccessMiddleware's guarded bases
  (app/access_paths.py) and uses no session → naturally public; GET isn't CSRF-protected. No middleware change.
- Tests `tests/test_c2c_share_link.py`: token is urlsafe/64, revoke clears, public GET no-session→200,
  bad token→404, expired→410, and **test_share_view_no_customer_data** (the critical redaction assertion — render
  must NOT contain customer name, SO#, unit price, or any serial-section customer field).

## Risks (from the architect): (1) public route auth-bypass safety — verified safe; (2) redaction leak — whitelist
## dict not hide-list, share template never sees raw qp + the no-customer-data test; (3) create_request typing — QP
## subject matches existing branch; (4) decide() circular import — lazy import mandatory; (5) NoEligibleApprover on
## section submit → inline banner not 500; (6) share_token unique race → retry once.
