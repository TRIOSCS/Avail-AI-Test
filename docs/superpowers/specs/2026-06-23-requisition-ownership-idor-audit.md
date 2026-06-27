<!-- PRESERVED 2026-06-27 from an ephemeral session scratchpad. Original: session
6b5c5cbf .../scratchpad/IDOR_AUDIT_2026-06-23.md (2026-06-23). -->

> **Preserved reference audit.** Remediation is in flight via PR #534 (deals role-scoping)
> + the authz-hardening work. main currently enforces requisition ownership via
> `get_req_for_user`, NOT the audit's proposed `_require_requisition_access` helper. Keep as
> the authz coverage reference until #534/authz-hardening fully land, then reconcile.

# AvailAI Requisition-Ownership IDOR Audit — 2026-06-23

Multi-agent audit (71 agents) + adversarial verification. Scope: router endpoints loading a requisition-scoped resource by path id that mutate/send WITHOUT the SALES/TRADER `created_by` ownership check.

**Candidates:** 64 · **Confirmed:** 58 (high 20, medium 30, low 8) · **Refuted:** 6

> Context: single-user staging today → ~no immediate exploitability; latent gap for multi-user/role rollout. Already fixed on `worktree-ai-email-drafting`: 5 email-drafting + sibling endpoints.

## HIGH (20)

### `POST /v2/partials/requisitions/{req_id}/rfq-send`
- **fix:** Replace `req = get_requisition_or_404(db, req_id)` at line 2789 with `req = _require_requisition_access(db, req_id, user)`.
- **why:** Confirmed by re-reading htmx_views.py. rfq_send (line 2778) depends only on require_user (any authenticated user) and at line 2789 calls get_requisition_or_404(db, req_id), which fetches by id alone with no ownership check. The endpoint then performs a real external Graph send vi

### `POST /v2/partials/requisitions/{req_id}/create-quote`
- **fix:** Replace get_requisition_or_404 at line 1999 with _require_requisition_access(db, req_id, user).
- **why:** create_quote_from_offers uses Depends(require_user) and get_requisition_or_404 (existence-only) at line 1999, not _require_requisition_access, so no SALES/TRADER created_by check. It loads req-scoped Offers (line 2001) and commits a new Quote plus QuoteLines (line 2043). A SALES/

### `POST /v2/partials/requisitions/{req_id}/offers/{offer_id}/mark-sold`
- **fix:** Add `_require_requisition_access(db, req_id, user)` at the top of mark_offer_sold_htmx before loading/mutating the offer.
- **why:** Re-read htmx_views.py:2402-2452. mark_offer_sold_htmx uses user=Depends(require_user), which (dependencies.py:50-69) only enforces authentication/active status — no role or ownership check. The offer is loaded only by Offer.id == offer_id AND Offer.requisition_id == req_id (line

### `POST /v2/partials/requisitions/{req_id}/offers/{offer_id}/review`
- **fix:** At the start of review_offer, replace the implicit existence check with _require_requisition_access(db, req_id, user) before querying/mutating the offer.
- **why:** Re-read htmx_views.py:2057-2112. review_offer is guarded only by Depends(require_user) (dependencies.py:50, any authenticated user — no role/ownership check). It loads the Offer at line 2075 via db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id), then mu

### `PUT /api/offers/{offer_id}`
- **fix:** After loading the offer, enforce ownership via get_req_for_user(db, user, offer.requisition_id) (skip/allow when requisition_id is None) before mutating, mirroring the create paths at offers.py:120/336.
- **why:** Confirmed by re-reading offers.py:583-628. update_offer is guarded only by Depends(require_buyer). dependencies.py:114/122-132 shows require_buyer admits BUYER_ROLES = {BUYER, SALES, TRADER, MANAGER, ADMIN} and only checks role membership — no requisition ownership. The handler d

### `DELETE /api/offers/{offer_id}`
- **fix:** In delete_offer, after loading the offer, call _require_requisition_access(db, offer.requisition_id, user) (skipping/allowing null requisition_id per existing convention) before db.delete.
- **why:** Confirmed by re-reading offers.py:631-638: delete_offer(offer_id, user=Depends(require_buyer), db) does `offer = db.get(Offer, offer_id)`, a bare 404 check, then `db.delete(offer); db.commit()` — the full function body, no other filtering. require_buyer (dependencies.py:114, BUYE

### `PUT /api/offers/{offer_id}/approve`
- **fix:** In approve_offer, after loading offer, enforce requisition ownership for SALES/TRADER via the requirement's requisition (mirror mark_offer_sold's guard / use _require_requisition_access on offer.requirement.requisition_id) before mutating.
- **why:** Re-read offers.py:661-686. approve_offer takes path offer_id, loads the requisition-scoped Offer via db.get(Offer, offer_id), and mutates deal state: offer.status pending_review→ACTIVE, approved_by_id=user.id, calls maybe_release_on_offer (releases vendor unavailability), record_

### `PUT /api/offers/{offer_id}/reject`
- **fix:** Add the SALES/TRADER created_by check after loading the offer, e.g. call _require_requisition_access(db, offer.requisition_id, user) (or require_buyer) before mutating status.
- **why:** Confirmed by re-reading offers.py:689-712. reject_offer is gated only by Depends(require_user); require_user (dependencies.py:50) checks authentication/active status only — no role and no requisition-ownership. The function loads the resource via offer = db.get(Offer, offer_id) w

### `POST /api/offers/{offer_id}/promote`
- **fix:** Resolve the offer via its requisition ownership (e.g. load Offer, then call get_req_for_user(db, user, offer.requisition_id) / a _require_requisition_access helper to apply the SALES/TRADER created_by check) before mutating, returning 404 when not owned.
- **why:** Re-read offers.py:980-1010. promote_offer is guarded only by require_user (dependencies.py:50-69), which authenticates but applies NO ownership/role restriction. It then does `offer = db.get(Offer, offer_id)` directly by path id — no created_by filter, no get_req_for_user/get_quo

### `POST /api/offers/{offer_id}/reject`
- **fix:** Add the SALES/TRADER created_by guard: load offer.requisition and call _require_requisition_access(db, offer.requisition_id, user) before mutating status.
- **why:** offers.py:1013-1039 confirms: reject_offer_t4_review uses only `user = Depends(require_user)` (line 1016), the router (line 36) has no router-level dependency, then `offer = db.get(Offer, offer_id)` (line 1024) loads a requisition-scoped resource (Offer.requisition_id FK to requi

### `POST /v2/partials/sightings/send-inquiry (sightings_send_inquiry, app/routers/sightings.py)`
- **fix:** After loading requirements (line 2238), enforce ownership for each distinct r.requisition_id via _require_requisition_access(db, r.requisition_id, user) (or filter requirements to ones the SALES/TRADER user created) before building vendor_groups/sending.
- **why:** Re-read sightings.py:2215-2359. Auth is only require_user (any authenticated user — confirmed dependencies.py:50-69, no role/ownership check; SALES/TRADER are in BUYER_ROLES so they pass) plus require_fresh_token (Graph token only). Line 2238 loads requisition-scoped Requirement

### `POST /v2/partials/sightings/{requirement_id}/offers/{offer_id}/request/{index}/send`
- **fix:** After loading the offer, enforce requisition ownership before sending: call get_req_for_user(db, user, offer.requisition_id) (or _require_requisition_access) so SALES/TRADER are restricted to requisitions where created_by == user.id.
- **why:** Confirmed by re-reading sightings.py:3091-3189 and dependencies.py:114-132. Auth is require_buyer, whose BUYER_ROLES set explicitly includes UserRole.SALES and UserRole.TRADER (dependencies.py:114), so it does NOT enforce requisition ownership. The handler loads the requisition-s

### `PATCH /v2/partials/sightings/{requirement_id}/advance-status`
- **fix:** Replace `db.get(Requirement, requirement_id)` with a lookup that joins Requisition and applies the SALES/TRADER created_by restriction (e.g. call get_req_for_user(db, user, requirement.requisition_id) after fetch, or filter Requirement->Requisition.created_by==user.id for non-buyer-unrestricted roles) before mutating sourcing_status.
- **why:** Confirmed by re-reading sightings.py:1180-1219. Auth is only `user: User = Depends(require_user)` (line 1186); require_user (dependencies.py:50-69) is pure authentication and admits any role including SALES/TRADER. The endpoint loads the requisition-scoped Requirement via raw `db

### `POST /v2/partials/sightings/{requirement_id}/offers/{offer_id}/review`
- **fix:** In sightings_review_offer, call _require_requisition_access(db, requirement.requisition_id, user) before invoking approve_offer/reject_offer (and/or add the SALES/TRADER created_by check inside approve_offer/reject_offer).
- **why:** Re-read sightings.py:2714-2739. Route uses only `user=Depends(require_user)` and the sole guard is offer→requirement scoping (line 2732 `offer.requirement_id != requirement_id`); there is no created_by/_require_requisition_access check. It then calls approve_offer/reject_offer. R

### `POST /v2/partials/sightings/{requirement_id}/offers`
- **fix:** Replace get_req_for_user's SALES-only filter (dependencies.py:147) with the canonical SALES+TRADER created_by check (user.role in (UserRole.SALES, UserRole.TRADER)), or have create_offer call _require_requisition_access.
- **why:** Partially refuted but a genuine gap remains. sightings_create_offer (sightings.py:2600 require_buyer, which admits SALES+TRADER per auth.py BUYER_ROLES) loads Requirement by path id with no check (2611), then calls create_offer(requirement.requisition_id,...) (2708). create_offer

### `POST /v2/partials/sightings/{requirement_id}/offers/{offer_id} (sightings_update_offer)`
- **fix:** After loading the requirement, enforce requisition ownership for SALES/TRADER via _require_requisition_access(db, requirement.requisition_id, user) before mutating.
- **why:** Confirmed real. require_buyer (dependencies.py:114, BUYER_ROLES) explicitly admits UserRole.SALES and UserRole.TRADER, so those users pass the only auth gate. The handler (sightings.py:2916-2924) loads the requisition-scoped Offer by path offer_id and only checks offer.requiremen

### `DELETE /v2/partials/sightings/{requirement_id}/offers/{offer_id}`
- **fix:** In sightings_delete_offer, replace the bare db.get(Requirement, requirement_id) with an ownership-scoped lookup via _require_requisition_access(db, requirement.requisition_id, user) (or filter created_by for SALES/TRADER) before calling delete_offer.
- **why:** Verified in sightings.py:2788-2808. Auth is user=Depends(require_buyer) (2794); BUYER_ROLES (dependencies.py:114) explicitly includes UserRole.SALES and UserRole.TRADER, so has_buyer_role/require_buyer admit both. The handler loads the offer by caller-supplied path id (db.get(Off

### `DELETE /api/requisition-attachments/{att_id}`
- **fix:** After loading att, call get_req_for_user(db, user, att.requisition_id) (returns 404 for SALES non-owners) before any OneDrive delete or db.delete.
- **why:** attachments.py:183-210 — delete_requisition_attachment depends only on require_user, then `att = db.get(RequisitionAttachment, att_id)` loads the requisition-scoped resource purely by path id, issues an external Graph DELETE via `_delete_onedrive_item(att.onedrive_item_id, token)

### `DELETE /api/requirement-attachments/{att_id}`
- **fix:** In delete_requirement_attachment, after loading att, resolve the parent and authorize: `req = db.get(Requirement, att.requirement_id); get_req_for_user(db, user, req.requisition_id)` before any OneDrive/DB deletion.
- **why:** Confirmed by re-reading attachments.py:288-315. delete_requirement_attachment does `att = db.get(RequirementAttachment, att_id)` keyed only on the path id, then performs an irreversible external send/destroy (`await _delete_onedrive_item(att.onedrive_item_id, token)`) plus `db.de

### `POST /v2/partials/quote-builder/{req_id}/save`
- **fix:** In get_req_for_user (dependencies.py:147) change the role check to `if user.role in (UserRole.SALES, UserRole.TRADER):` so TRADER is also restricted to created_by==user.id, matching _require_requisition_access.
- **why:** quote_builder_save (quote_builder.py:176-201) takes path req_id, loads the requisition via get_req_for_user, then calls save_quote_from_builder which db.add(quote)/db.flush()/db.commit() (quote_builder_service.py:288-307) — a mutation that creates/changes deal/quote state. The on

## MEDIUM (30)

### `POST /v2/partials/requisitions/{req_id}/add-offer`
- **fix:** Replace get_requisition_or_404(db, req_id) on line 2139 with _require_requisition_access(db, req_id, user).
- **why:** Re-read add_offer (htmx_views.py:2131-2222). It takes req_id, loads the requisition with get_requisition_or_404(db, req_id) at line 2139 which is existence-only (the helper does no ownership check), then mutates: builds an Offer(requisition_id=req_id, entered_by_id=user.id, ...)

### `POST /v2/partials/requisitions/{req_id}/offers/{offer_id}/edit`
- **fix:** Add _require_requisition_access(db, req_id, user) at the top of edit_offer (right after the function body begins / before loading the offer).
- **why:** Re-read htmx_views.py:2274-2380. edit_offer depends only on require_user (dependencies.py:50, which checks auth + is_active only — no role, no created_by, no requisition-ownership). It loads the resource by offer_id+requisition_id (line 2285), then mutates many trackable fields,

### `DELETE /v2/partials/requisitions/{req_id}/offers/{offer_id}`
- **fix:** Add _require_requisition_access(db, req_id, user) at the start of delete_offer_htmx before querying/deleting the offer.
- **why:** Re-read htmx_views.py:2383-2399. delete_offer_htmx depends only on require_user (any authenticated user). It loads the requisition-scoped Offer via db.query(Offer).filter(Offer.id == offer_id, Offer.requisition_id == req_id).first() (2392), then db.delete(offer) + db.commit() (23

### `POST /v2/partials/requisitions/{req_id}/offers/{offer_id}/reconfirm`
- **fix:** Add `_require_requisition_access(db, req_id, user)` at the top of reconfirm_offer (before the Offer query).
- **why:** Confirmed by re-reading htmx_views.py:2225-2249. reconfirm_offer takes req_id+offer_id path params, loads the Offer by id scoped only to requisition_id (line 2234: filter(Offer.id==offer_id, Offer.requisition_id==req_id)), then mutates it — resets reconfirmed_at, reconfirm_count,

### `PATCH /v2/partials/requisitions/{req_id}/responses/{response_id}/status`
- **fix:** Add _require_requisition_access(db, req_id, user) immediately after loading VendorResponse (before the mutation), mirroring review_response_htmx at line 3107.
- **why:** Confirmed by re-reading htmx_views.py. update_response_status (line 7380) loads a requisition-scoped VendorResponse by response_id+requisition_id (7391-7398), then mutates vr.status = new_status and db.commit() (7408-7409). Auth is only user=Depends(require_user) at 7385 — no _re

### `POST /v2/partials/requisitions/{req_id}/save-parsed-offers`
- **fix:** Replace `req = get_requisition_or_404(db, req_id)` at line 1573 with `req = _require_requisition_access(db, req_id, user)`.
- **why:** Confirmed by re-reading htmx_views.py:1565-1670. save_parsed_offers depends only on require_user (any authenticated user) and loads the requisition via get_requisition_or_404(db, req_id) at line 1573, which the confirmed auth model does NOT ownership-check. It then mutates: build

### `POST /v2/partials/requisitions/{req_id}/requirements`
- **fix:** Replace req = get_requisition_or_404(db, req_id) at line 1184 with req = _require_requisition_access(db, req_id, user).
- **why:** Re-read htmx_views.py:1155-1227. add_requirement is gated only by user=Depends(require_user) (any authenticated user) and loads the requisition via get_requisition_or_404(db, req_id) at line 1184, which (per _lookup_helpers.py and the confirmed auth model) fetches by id with NO o

### `PUT /v2/partials/requisitions/{req_id}/requirements/{item_id}`
- **fix:** Add `_require_requisition_access(db, req_id, user)` in place of `req = get_requisition_or_404(db, req_id)` at line 3341 (assign its return to req).
- **why:** Re-read htmx_views.py:3308-3390. update_requirement takes path ids req_id+item_id, loads the requisition via get_requisition_or_404(db, req_id) (which the confirmed auth model says does NOT check ownership), loads the Requirement filtered by requisition_id==req_id, mutates ~15 fi

### `DELETE /v2/partials/requisitions/{req_id}/requirements/{item_id}`
- **fix:** Add `_require_requisition_access(db, req_id, user)` at the top of delete_requirement (replacing the bare get_requisition_or_404 call), as done on the sibling endpoints.
- **why:** Re-read htmx_views.py:3287-3305. delete_requirement depends only on require_user (any authenticated user) and calls get_requisition_or_404(db, req_id) which the codebase confirms checks existence only, not ownership. The requirement is loaded scoped by item_id + requisition_id (l

### `POST /v2/partials/requisitions/{req_id}/log-phone`
- **fix:** Replace the `get_requisition_or_404(db, req_id)` call at line 7264 with `_require_requisition_access(db, req_id, user)` to enforce the SALES/TRADER created_by ownership restriction.
- **why:** Confirmed by re-reading htmx_views.py:7256-7312. The handler signature uses only `user: User = Depends(require_user)` (require_user in app/dependencies.py:50 only enforces authentication, not requisition ownership). At line 7264 it calls `get_requisition_or_404(db, req_id)`, whic

### `POST /v2/partials/search/add-to-requisition`
- **fix:** Add req = _require_requisition_access(db, int(requisition_id), user) and drop the manual db.get/404 block (lines 3825-3830).
- **why:** Re-read htmx_views.py:3800-3872. The endpoint depends only on require_user (any authenticated user). It reads requisition_id from the JSON body (line 3815), loads the requisition via db.get(Requisition, requisition_id) (3825) with no ownership/role filter, then creates a Requirem

### `PATCH /v2/partials/requisitions/{req_id}/archive`
- **fix:** Replace `requisition = db.get(Requisition, req_id)` (plus the not-found check) with `requisition = _require_requisition_access(db, req_id, user)` in archive_requisition.
- **why:** Re-read htmx_views.py:12819-12846. archive_requisition is gated only by require_user (any authenticated user). It loads the requisition via raw `db.get(Requisition, req_id)` (line 12827) with no created_by/role check, then mutates `requisition.status = RequisitionStatus.ARCHIVED`

### `PATCH /v2/partials/requisitions/{req_id}/unarchive`
- **fix:** Replace `requisition = db.get(Requisition, req_id); if not requisition: raise HTTPException(404,...)` with `requisition = _require_requisition_access(db, req_id, user)`.
- **why:** Confirmed real. unarchive_requisition (htmx_views.py:12849-12877) loads the requisition via db.get(Requisition, req_id) by id only, sets requisition.status = RequisitionStatus.ACTIVE, cascades child Requirement.sourcing_status to OPEN, logs activity, and db.commit() — a clear mut

### `PATCH /v2/partials/parts/{requirement_id}/header`
- **fix:** After loading req (line 12344), call _require_requisition_access(db, req.requisition_id, user) before any mutation.
- **why:** Re-read part_header_save (htmx_views.py:12331-12396). It takes path id requirement_id, loads a requisition-scoped resource via db.get(Requirement, requirement_id) (12344), then mutates header fields — sourcing_status (via transition_requirement, which only checks transition legal

### `PATCH /v2/partials/parts/{requirement_id}/cell`
- **fix:** After loading req, derive the parent requisition and enforce the role check, e.g. `_require_requisition_access(db, req.requisition_id, user)` before mutating.
- **why:** Confirmed real. htmx_views.py:12456-12503 part_cell_save guards only with user=Depends(require_user). It loads the resource by path id alone (12469 `req = db.get(Requirement, requirement_id)`), mutates requisition-scoped state (sourcing_status via transition_requirement, target_q

### `PUT /api/offers/{offer_id}/reconfirm`
- **fix:** Add the SALES/TRADER ownership check: after loading the offer, call _require_requisition_access(db, offer.requisition_id, user) (or require_buyer) before mutating.
- **why:** Re-read offers.py:641-658. reconfirm_offer takes path offer_id, loads a requisition-scoped Offer via db.get(Offer, offer_id) (Offer has requisition_id FK per models/offers.py:34), then mutates offer.reconfirmed_at and offer.reconfirm_count and db.commit(). The only guard is Depen

### `POST /api/offers/{offer_id}/attachments`
- **fix:** After loading the offer, enforce the SALES/TRADER created_by check, e.g. `_require_requisition_access(db, offer.requisition_id, user)` (or load via get_requisition_or_404 and apply the role-based ownership guard) before performing the upload.
- **why:** offers.py:776-835 — upload_offer_attachment(offer_id, file, user=Depends(require_user)) then `offer = db.get(Offer, offer_id)` with only a 404-if-missing check. require_user (dependencies.py:50-69) only verifies authentication + is_active; it does NOT enforce requisition ownershi

### `POST /api/offers/{offer_id}/attachments/onedrive`
- **fix:** Add a requisition-ownership/role guard after loading the offer (e.g. if offer.requisition_id: _require_requisition_access(db, offer.requisition_id, user)) or change the dependency to require_buyer to match create_offer/delete_offer.
- **why:** offers.py:838-869 — attach_from_onedrive(offer_id, ..., user=Depends(require_user)) loads offer = db.get(Offer, offer_id). Offer is requisition-scoped (models/offers.py:34: requisition_id FK to requisitions). The function then inserts OfferAttachment(offer_id=offer_id, uploaded_b

### `DELETE /api/offer-attachments/{att_id}`
- **fix:** In delete_offer_attachment, load the parent Offer and call _require_requisition_access(db, att.offer.requisition_id, user) (or check att.offer.entered_by_id == user.id or _is_admin) before db.delete(att).
- **why:** Confirmed by re-reading offers.py:878-901. delete_offer_attachment(att_id, user=Depends(require_user), db) loads `att = db.get(OfferAttachment, att_id)` purely by path id, then unconditionally `db.delete(att); db.commit()` (a requisition-scoped mutation) plus an external Graph DE

### `POST /api/requisitions/{req_id}/clone`
- **fix:** In clone.py replace `get_req_for_user(db, user, req_id)` with `_require_requisition_access(db, req_id, user)` (or extend get_req_for_user's filter to `user.role in (UserRole.SALES, UserRole.TRADER)`).
- **why:** Confirmed by re-reading the code. clone.py:23 guards only with `require_user` (no require_buyer/admin/manager), then clone.py:26 calls `get_req_for_user(db, user, req_id)`. That helper (dependencies.py:138-152) applies the created_by ownership filter ONLY for `user.role == UserRo

### `POST /v2/partials/sightings/{requirement_id}/offers/{offer_id}/reconfirm`
- **fix:** Add `_require_requisition_access(db, offer.requisition_id, user)` (or scope via the requirement's requisition) after loading the offer in sightings_reconfirm_offer, matching the SALES/TRADER created_by pattern; at minimum align with siblings by using require_buyer.
- **why:** Confirmed by re-reading sightings.py:2742-2762. Auth is only `user=Depends(require_user)` (2748). The endpoint loads the requisition-scoped Offer by path `offer_id` (2757) and guards only `offer.requirement_id != requirement_id` (2758) — that confirms the offer belongs to the req

### `POST /v2/partials/sightings/{requirement_id}/offers/{offer_id}/request`
- **fix:** Resolve the parent requisition with the ownership-aware helper before mutating, e.g. call get_req_for_user(db, user, offer.requisition_id) (which 404s for SALES not matching created_by) immediately after the requirement_id scope check at line 3068.
- **why:** Re-read sightings.py:3040-3084: sightings_offer_request is gated only by require_buyer (dependencies.py:114,122-132 — BUYER_ROLES includes UserRole.SALES and UserRole.TRADER), then loads a requisition-scoped resource by path id (offer = db.get(Offer, offer_id), 3064) and mutates

### `POST /v2/partials/sightings/{requirement_id}/mark-unavailable`
- **fix:** Add an ownership check after loading the requirement, e.g. `_require_requisition_access(db, requirement.requisition_id, user)` (enforce Requisition.created_by == user.id for SALES/TRADER) before record_unavailability; apply identically to the sibling mark-available endpoint.
- **why:** Confirmed by re-reading sightings.py:1070-1117 and dependencies.py:50-69. The endpoint declares only `user: User = Depends(require_user)` (line 1076); require_user is pure authentication (401/403 on missing/deactivated user only — no requisition ownership or role check). It loads

### `POST /v2/partials/sightings/{requirement_id}/mark-available`
- **fix:** After loading the requirement at line 1136, enforce SALES/TRADER ownership of requirement.requisition_id via _require_requisition_access (or equivalent created_by check) before calling clear_unavailability.
- **why:** Confirmed against the code. sightings.py:1126 auths with only require_user (router at line 72 has no dependencies=). Line 1136 loads the requisition-scoped Requirement (models/sourcing.py:106: requirement_id->requisitions FK, non-nullable) by caller-supplied path id with no creat

### `PATCH /v2/partials/sightings/{requirement_id}/assign`
- **fix:** Add `_require_requisition_access(db, requirement.requisition_id, user)` (or equivalent SALES/TRADER created_by check) immediately after the 404 check at sightings.py:1170.
- **why:** Re-read app/routers/sightings.py:1155-1177. The endpoint declares only `user: User = Depends(require_user)` (line 1161). dependencies.py:50-69 confirms require_user enforces authentication/active-status ONLY — no role or created_by check. It loads a requisition-scoped resource by

### `POST /api/requirements/{requirement_id}/notes`
- **fix:** After the 404 check at line 1337, add: `if not get_req_for_user(db, user, req.requisition_id): raise HTTPException(403, "Not authorized")`.
- **why:** Confirmed against the code. requirements.py:1335 loads the resource with `req = db.query(Requirement).filter(Requirement.id == requirement_id).first()` and 1341-1342 mutates+commits `req.notes`. The only auth dependency is require_user (line 1331), which does NOT enforce the SALE

### `POST /api/requirements/{requirement_id}/tasks`
- **fix:** After loading `req` (line 1422), add `get_req_for_user(db, user, req.requisition_id)` to enforce the SALES created_by ownership check (matching the pattern at line 1138).
- **why:** Re-read requirements.py:1410-1443. create_requirement_task loads `req = db.query(Requirement).filter(Requirement.id == requirement_id).first()` then creates and commits a RequisitionTask(requisition_id=req.requisition_id, created_by=user.id) under only Depends(require_user). It n

### `POST /api/ai/generate-description/{requirement_id}`
- **fix:** Add an ownership check after loading: `if req.requisition_id: get_req_for_user(db, user, req.requisition_id, options=[])` (raises 404 for SALES users who don't own the parent requisition), mirroring ai_parse_response at ai.py:504.
- **why:** Confirmed by re-reading ai.py:448-480. Handler depends only on require_user (ai.py:453) and loads a requisition-scoped resource with `req = db.get(Requirement, requirement_id)` (ai.py:459) — Requirement.requisition_id is nullable=False (sourcing.py:106), so it is strictly requisi

### `POST /api/ai/parse-response/{response_id}`
- **fix:** In ai_parse_response replace the get_req_for_user guard with _require_requisition_access(db, vr.requisition_id, user), or extend get_req_for_user to also filter created_by for UserRole.TRADER.
- **why:** Confirmed. ai.py:501-505 loads VendorResponse by response_id (requisition-scoped via requisition_id) and guards with get_req_for_user(db, user, vr.requisition_id). get_req_for_user (dependencies.py:146-148) filters created_by ONLY for UserRole.SALES; TRADER is not filtered. The c

### `POST /api/ai/save-parsed-offers`
- **fix:** In dependencies.py get_req_for_user, change the SALES-only filter to `if user.role in (UserRole.SALES, UserRole.TRADER): q = q.filter_by(created_by=user.id)` so TRADER is also restricted to owned requisitions.
- **why:** ai.py:571 loads the requisition via `get_req_for_user(db, user, payload.requisition_id, ...)`, then ai.py:579-580 calls `_save(db, payload.requisition_id, payload.response_id, payload.offers, user.id)` and `db.commit()` — a mutation that writes Offers under the requisition. The g

## LOW (8)

### `POST /v2/partials/requisitions/{req_id}/log-activity`
- **fix:** Replace get_requisition_or_404(db, req_id) at line 2598 with _require_requisition_access(db, req_id, user).
- **why:** Confirmed at htmx_views.py:2588-2620. The handler depends only on require_user, then calls get_requisition_or_404(db, req_id) (existence-only, no ownership) at 2598 and performs a mutation: ActivityLog(user_id=user.id, requisition_id=req_id, ...) followed by db.add(log)/db.commit

### `POST /v2/partials/requisitions/{req_id}/search-all`
- **fix:** Add _require_requisition_access(db, req_id, user) immediately after loading the requisition at line 1240 to enforce the SALES/TRADER created_by restriction.
- **why:** Re-read htmx_views.py:1230-1286 and _lookup_helpers.py:14-18. The endpoint uses user=Depends(require_user) (any authenticated user, no role enforcement). It loads the requisition via get_requisition_or_404(db, req_id), which only does db.get(Requisition, req_id) with no created_b

### `PATCH /v2/partials/parts/{requirement_id}/save-spec`
- **fix:** In part_spec_save, before mutating, resolve the parent requisition and enforce access: `_require_requisition_access(db, req.requisition_id, user)`.
- **why:** Confirmed by re-reading htmx_views.py:12538-12571. part_spec_save loads the resource by path id only (12551 `req = db.get(Requirement, requirement_id)`), then mutates a spec field and commits (12559 `setattr(req, field, clean)`; 12560 `db.commit()`). Requirement is requisition-sc

### `PATCH /v2/partials/parts/{requirement_id}/notes (save_part_notes)`
- **fix:** Add `_require_requisition_access(db, req.requisition_id, user)` after loading `req` (before mutating sale_notes) in save_part_notes.
- **why:** Re-read htmx_views.py:12653-12680. save_part_notes uses user=Depends(require_user) only. It loads req = db.get(Requirement, requirement_id) by id (line 12662), then mutates req.sale_notes (12666) and db.commit() (12677). Requirement is requisition-scoped (it carries req.requisiti

### `POST /v2/partials/parts/{requirement_id}/tasks`
- **fix:** In create_part_task, after loading req, call _require_requisition_access(db, req.requisition_id, user) to enforce the SALES/TRADER created_by restriction before creating the task.
- **why:** Confirmed by re-reading htmx_views.py. create_part_task (line 12683) uses user=Depends(require_user) only. It loads the requisition-scoped resource via `req = db.get(Requirement, requirement_id)` (12691) with only a None check, then creates and commits a RequisitionTask scoped to

### `PATCH /v2/partials/parts/{requirement_id}/archive`
- **fix:** After db.get(Requirement, requirement_id), call _require_requisition_access(db, part.requisition_id, user) before mutating (apply the same to unarchive_single_part).
- **why:** Confirmed by re-reading htmx_views.py:12779-12797. archive_single_part is guarded only by Depends(require_user). It loads a requisition-scoped resource by path id alone (12787 `part = db.get(Requirement, requirement_id)`), then mutates and commits (12791 `part.sourcing_status = S

### `POST /v2/partials/sightings/{requirement_id}/log-activity`
- **fix:** Add `_require_requisition_access(db, requirement.requisition_id, user)` immediately after loading the requirement (or switch to require_buyer plus the SALES/TRADER created_by check).
- **why:** Re-read sightings.py:1222-1292. Auth is `user: User = Depends(require_user)` only; dependencies.py:50-69 confirms require_user enforces only authentication + is_active, no role/ownership check. The handler loads a requisition-scoped resource by caller-supplied path id (`requireme

### `POST /v2/partials/sightings/{requirement_id}/refresh`
- **fix:** After loading, enforce ownership for SALES/TRADER, e.g. call the requisition-access helper via requirement.requisition_id (raise 403 unless created_by == user.id for SALES/TRADER) before invoking search_requirement.
- **why:** Re-read sightings.py:744-786. The endpoint's only auth is user=Depends(require_user) (line 750); the router (line 72, APIRouter(tags=["sightings"])) has no dependencies= guard. require_user (dependencies.py:50-69) only checks authentication and is_active — it applies NO role chec

## Refuted (adversarial verification rejected)

- `POST /v2/partials/requisitions/{req_id}/poll-inbox` — Re-read htmx_views.py:3272-3284. The endpoint uses require_user + get_requisition_or_404 (no ownership check), so a SALES/TRADER could pass any req_id. But the only MUTATION/scan it triggers is _run_i
- `POST /v2/partials/sightings/{requirement_id}/offers/{offer_id}/mark-sold` — Refuted. The wrapper sightings_mark_offer_sold (sightings.py:2765-2785) delegates the mutation to mark_offer_sold (crm/offers.py:716-740), which enforces its own stricter per-offer ownership model at
- `GET /v2/partials/quote-builder/{req_id}/export/excel` — The endpoint (quote_builder.py:204-237) is a pure READ: it streams an Excel export and performs no DB write/commit and no external send (Graph/email). The target IDOR class requires criterion (c): a M
- `POST /v2/partials/excess/{list_id}/solicit` — The code facts are accurate: htmx_solicit (excess.py:526-552) loads an ExcessList by path list_id via get_excess_list, which is a bare db.get(ExcessList, list_id) with no owner check (excess_service.p
- `POST /api/excess-lists/{list_id}/solicitations` — ExcessList (app/models/excess.py:35-69) is NOT a requisition-scoped resource. It has no requisition_id; it is a standalone top-level entity with its OWN distinct ownership model (owner_id, line 42). T
- `PATCH /api/excess-lists/{list_id}/line-items/{item_id}/bids/{bid_id}` — REFUTED as an instance of the requisition-scoped SALES/TRADER IDOR class. Reading app/models/excess.py:35-70, ExcessList has its OWN distinct ownership model (owner_id, company_id, customer_site_id) a
