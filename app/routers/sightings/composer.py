"""RFQ composer routes — vendor modal / affinity / search / add-vendor.

W4.1 split of the 3,811-line app/routers/sightings.py — pure structural move: URLs and
behavior unchanged; every route attaches to the shared router imported from .common
(registration assembled in __init__).
"""

import asyncio

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func as sqlfunc
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import (
    require_user,
)
from ...models import User
from ...models.intelligence import ActivityLog, MaterialCard, MaterialCardDatasheet
from ...models.sourcing import Requirement
from ...models.vendor_sighting_summary import VendorSightingSummary
from ...models.vendors import VendorCard, VendorContact
from ...services.vendor_duplicates import check_vendor_duplicate
from ...services.vendor_reachability import cards_with_resolvable_email as _cards_with_resolvable_email
from ...services.vendor_reachability import dnc_emails_for_cards as _dnc_emails_for_cards
from ...services.vendor_unavailability import (
    excluded_vendor_norms,
)
from ...template_env import template_response
from ...utils.normalization import parse_website_domain
from ...vendor_utils import normalize_vendor_name
from .common import (  # noqa: F401
    _EXCLUDED_REQ_STATUSES,
    _EXCLUDED_SOURCING_STATUSES,
    _SEARCH_FANOUT_LIMIT,
    MAX_BATCH_SIZE,
    _active_sourcing_status_clause,
    _append_oob_toast,
    _best_contacts_by_card,
    _get_cached,
    _invalidate_cache,
    _mpn_link_map,
    _oob_toast,
    _oob_toast_html,
    _publish_if_user_source,
    _refresh_offers_panel,
    _render_offers_panel,
    _toast_suppressed_for_sse,
    _with_toast,
    router,
)
from .coverage import (  # noqa: F401
    CoverageEntry,
    RankedVendor,
    SuggestedVendor,
    _coverage_ranked_vendor_rows,
    _find_affinity_in_thread,
    _vss_vendor_card_join,
)


@router.get("/v2/partials/sightings/vendor-modal", response_class=HTMLResponse)
async def sightings_vendor_modal(
    request: Request,
    requirement_ids: str = "",
    preselect: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Return vendor selection + email compose modal content."""
    req_id_list = [int(x) for x in requirement_ids.split(",") if x.strip().isdigit()]

    requirements = (db.query(Requirement).filter(Requirement.id.in_(req_id_list)).all()) if req_id_list else []

    parts = [
        {
            "mpn": r.primary_mpn,
            "qty": r.target_qty,
            "target_price": float(r.target_price) if r.target_price else None,
        }
        for r in requirements
    ]

    # Active-only unavailability exclusion (alongside the blacklist filter): a vendor
    # durably marked unavailable for ANY selected part is not suggested — deliberately
    # conservative multi-requirement semantics. Expired/released/cleared records do
    # not exclude (active-only is enforced inside the service).
    excluded = excluded_vendor_norms(db, requirements) if requirements else set()

    suggested_vendors: list[SuggestedVendor] = []
    coverage: dict[object, CoverageEntry] = {}
    if req_id_list:
        rows = _coverage_ranked_vendor_rows(db, req_id_list, excluded)
        # Group key: card id for carded, normalized vendor_name for cardless. coverage is
        # keyed by this same key so both carded and cardless rows resolve their chip. The
        # Alpine selection / send-path key (normalized_name) is the card's stored
        # normalized_name for carded rows (unchanged) and the normalized vendor_name for
        # cardless rows.
        for r in rows:
            norm = normalize_vendor_name(r.vendor_name)
            key = r.card.id if r.card is not None else norm
            suggested_vendors.append(
                SuggestedVendor(
                    id=key,
                    card=r.card,
                    normalized_name=(r.card.normalized_name if r.card is not None else norm),
                    display_name=r.vendor_name,
                    vendor_name=r.vendor_name,
                    has_contact=r.has_contact,
                    response_rate=(r.card.response_rate if r.card is not None else None),
                    engagement_score=(r.card.engagement_score if r.card is not None else None),
                    vendor_score=r.vendor_score,
                    lead_time_days=r.lead_time_days,
                )
            )
            coverage[key] = CoverageEntry(
                count=r.covered_count,
                avg_score=float(r.avg_score) if r.avg_score is not None else None,
                mpns="",
            )
        if coverage:
            # Covered-MPN list per vendor (rendered in the row's `title`) — a second
            # plain query; no string_agg/group_concat (SQLite vs PG divergence). LEFT
            # join so cardless rows (no card) contribute their MPNs too; the per-row key
            # mirrors the ranking grouping (card id when joined, else normalized name).
            mpn_rows = (
                db.query(
                    VendorCard.id,
                    VendorSightingSummary.vendor_name,
                    Requirement.primary_mpn,
                )
                .select_from(VendorSightingSummary)
                .join(Requirement, VendorSightingSummary.requirement_id == Requirement.id)
                .outerjoin(VendorCard, _vss_vendor_card_join())
                .filter(VendorSightingSummary.requirement_id.in_(req_id_list))
                .distinct()
                .all()
            )
            mpns_by_key: dict[object, set[str]] = {}
            for card_id, vendor_name, mpn in mpn_rows:
                if not mpn:
                    continue
                row_key: object = card_id if card_id is not None else normalize_vendor_name(vendor_name or "")
                if row_key in coverage:
                    mpns_by_key.setdefault(row_key, set()).add(mpn)
            for cov_key, mpns in mpns_by_key.items():
                coverage[cov_key]["mpns"] = ", ".join(sorted(mpns))

    # ── Preselect union: append any named vendor not already in coverage ────────
    # Split on comma, normalize each name, skip blanks, dedup against the
    # already-suggested set (keyed by normalized_name to match the Alpine selection
    # key). `has_contact` is resolved via the same _cards_with_resolvable_email
    # helper used for coverage rows — so the Alpine seed stays consistent.
    if preselect.strip():
        existing_norms = {sv.normalized_name for sv in suggested_vendors}
        preselect_names = [n.strip() for n in preselect.split(",") if n.strip()]
        for raw_name in preselect_names:
            norm = normalize_vendor_name(raw_name)
            if not norm or norm in existing_norms:
                continue
            # Resolve against VendorCard by normalized_name
            card = db.query(VendorCard).filter(VendorCard.normalized_name == norm).first()
            if card is not None:
                contactable = _cards_with_resolvable_email(db, [card.id])
                has_contact = card.id in contactable
                sv = SuggestedVendor(
                    id=card.id,
                    card=card,
                    normalized_name=card.normalized_name,
                    display_name=card.display_name or raw_name,
                    vendor_name=card.display_name or raw_name,
                    has_contact=has_contact,
                    response_rate=card.response_rate,
                    engagement_score=card.engagement_score,
                    vendor_score=card.vendor_score,
                    lead_time_days=None,  # preselect-only: no VSS context to compute min
                )
            else:
                # No matching card — cardless synthetic row, no contact resolvable
                sv = SuggestedVendor(
                    id=norm,
                    card=None,
                    normalized_name=norm,
                    display_name=raw_name,
                    vendor_name=raw_name,
                    has_contact=False,
                    response_rate=None,
                    engagement_score=None,
                    vendor_score=None,
                    lead_time_days=None,
                )
            suggested_vendors.append(sv)
            existing_norms.add(norm)

    # Compute how many distinct requisitions the basket spans (for the "Spanning N
    # requisitions" note in the Parts panel). Only shown when >1 to keep the modal quiet
    # for the single-requirement case.
    requisition_count = len({r.requisition_id for r in requirements})

    # Advisory DNC computation — determine which suggested vendors have a DNC-flagged
    # contact email. Two steps:
    # 1. Collect the card ids of all carded suggested vendors.
    # 2. _dnc_emails_for_cards returns the lowercased DNC emails for those cards.
    # 3. Fetch each carded vendor's best contact (same ordering as the send path), then
    #    cross-reference: if the resolved email is in dnc_emails → vendor is advisory DNC.
    # Result is `dnc_norms`: set of normalized_names passed to the template so it can
    # disable the checkbox and render the rose chip WITHOUT lazy-loading relationships.
    carded_ids = [sv.card.id for sv in suggested_vendors if sv.card is not None]
    dnc_emails: set[str] = _dnc_emails_for_cards(db, carded_ids) if carded_ids else set()

    dnc_norms: set[str] = set()
    if dnc_emails and carded_ids:
        # Best-contact-per-card (same ordering as send path) to determine which vendor's
        # resolved email is in dnc_emails.
        best_contacts = _best_contacts_by_card(db, carded_ids)
        card_best_contact: dict[int, VendorContact] = {c.vendor_card_id: c for c in best_contacts}
        for sv in suggested_vendors:
            if sv.card is not None:
                contact = card_best_contact.get(sv.card.id)
                if contact and contact.email and contact.email.lower() in dnc_emails:
                    dnc_norms.add(sv.normalized_name)

    # Contactable, non-DNC normalized names for the Alpine selectedVendors seed.
    # Passed explicitly so the template doesn't need Jinja2 set-member filtering.
    contactable_non_dnc = [
        sv.normalized_name for sv in suggested_vendors if sv.has_contact and sv.normalized_name not in dnc_norms
    ]

    # Resolve available datasheets for the basket's material cards — passed to the
    # compose step as an opt-in checkbox list ("Attachments (N available)").
    # Collapsed in the template when >3. No bytes fetched here; send-time only.
    available_datasheets: list[dict] = []
    if requirements:
        mc_ids = [r.material_card_id for r in requirements if r.material_card_id]
        if mc_ids:
            ds_rows = (
                db.query(MaterialCardDatasheet)
                .filter(
                    MaterialCardDatasheet.material_card_id.in_(mc_ids),
                    MaterialCardDatasheet.library_item_id.isnot(None),
                    MaterialCardDatasheet.library_drive_id.isnot(None),
                )
                .all()
            )
            available_datasheets = [
                {"id": ds.id, "file_name": ds.file_name, "size_bytes": ds.size_bytes} for ds in ds_rows
            ]

    # Build the tagged subject shown read-only in the compose step — LOCKSTEP with the
    # preview/send path (sightings_preview_inquiry / send_batch_rfq): one [ref:{id}] token
    # per involved requisition, ascending requisition id, prefixed by the part count. So
    # the buyer sees exactly what will be sent before previewing, even after a modal refresh.
    requisition_ids_sorted = sorted({r.requisition_id for r in requirements}) if requirements else []
    num_parts = len(parts)
    avail_token_display = " ".join(f"[ref:{rid}]" for rid in requisition_ids_sorted)
    raw_subject_display = f"RFQ — {num_parts} part{'s' if num_parts != 1 else ''}"
    compose_subject = f"{raw_subject_display} {avail_token_display}" if avail_token_display else raw_subject_display

    # Commodity-segmented engagement signal — read-only, NO schema change, NO ranking
    # change. ONE bounded query over ActivityLog → Requirement → MaterialCard counts
    # this vendor's outbound/inbound activity FILTERED to the current commodity, so the
    # compose chip can say "have they replied to us about THIS kind of part?".
    # Only runs when (a) all selected requirements share one commodity and (b) at least
    # one suggested vendor is carded — otherwise it is skipped entirely (empty dicts).
    commodity_signals: dict[int, dict] = {}  # card_id → {"outbound": N, "inbound": N}
    current_commodity: str | None = None
    if requirements:
        cats = {r.material_card.category for r in requirements if r.material_card_id and r.material_card}
        current_commodity = cats.pop() if len(cats) == 1 else None  # only when all reqs share one commodity

    carded_signal_ids = [sv.id for sv in suggested_vendors if sv.card is not None]
    if carded_signal_ids and current_commodity:
        signal_rows = (
            db.query(
                ActivityLog.vendor_card_id,
                ActivityLog.direction,
                sqlfunc.count().label("cnt"),
            )
            .join(Requirement, ActivityLog.requirement_id == Requirement.id)
            .join(MaterialCard, Requirement.material_card_id == MaterialCard.id)
            .filter(
                ActivityLog.vendor_card_id.in_(carded_signal_ids),
                ActivityLog.direction.in_(["outbound", "inbound"]),
                ActivityLog.requirement_id.isnot(None),
                MaterialCard.category == current_commodity,
            )
            .group_by(ActivityLog.vendor_card_id, ActivityLog.direction)
            .all()
        )
        for card_id, direction, cnt in signal_rows:
            sig = commodity_signals.setdefault(card_id, {"outbound": 0, "inbound": 0})
            sig[direction] = cnt

    ctx = {
        "request": request,
        "suggested_vendors": suggested_vendors,
        "coverage": coverage,
        "requirement_ids": req_id_list,
        "parts": parts,
        "requisition_count": requisition_count,
        "dnc_norms": dnc_norms,
        "contactable_non_dnc": contactable_non_dnc,
        "available_datasheets": available_datasheets,
        "compose_subject": compose_subject,
        "commodity_signals": commodity_signals,
        "current_commodity": current_commodity,
    }
    return template_response("htmx/partials/sightings/vendor_modal.html", ctx)


@router.get("/v2/partials/sightings/vendor-affinity", response_class=HTMLResponse)
async def sightings_vendor_affinity(
    request: Request,
    requirement_ids: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """On-demand affinity vendor suggestions for the RFQ vendor modal.

    Called by: vendor_modal.html "Suggest more vendors" button (hx-get swaps the
    rows into #rfq-affinity-section, replacing the button — second click impossible).
    Runs find_vendor_affinity per selected primary MPN, merges/dedupes by vendor
    keeping the highest confidence, drops vendors already coverage-suggested (same
    query as the modal — self-contained) or unavailability-excluded, caps at 10.

    THREADING: find_vendor_affinity is SYNC with a blocking Anthropic L3 call inside
    (3-12s for 6 parts) — each per-MPN call runs via asyncio.to_thread with its own
    short-lived session (_find_affinity_in_thread), gathered under a Semaphore(3) so
    a wide selection can't exhaust the thread pool. Never call it bare from this
    async route: it would block the uvicorn worker.
    """
    req_id_list = [int(x) for x in requirement_ids.split(",") if x.strip().isdigit()]
    requirements = (db.query(Requirement).filter(Requirement.id.in_(req_id_list)).all()) if req_id_list else []

    affinity_vendors: list[dict] = []
    affinity_partial = False
    if requirements:
        excluded = excluded_vendor_norms(db, requirements)
        suggested_norms: set[str] = set()
        for r in _coverage_ranked_vendor_rows(db, req_id_list, excluded):
            # Canonical normalization of the row's display name — covers carded AND
            # cardless rows uniformly (affinity matches are compared canonically). For
            # carded rows also add the stored normalized_name, which may be legacy-
            # suffixed and so differ from the canonical re-normalization.
            suggested_norms.add(normalize_vendor_name(r.vendor_name))
            if r.card is not None:
                suggested_norms.add(r.card.normalized_name or "")

        # One affinity call per UNIQUE primary MPN (order-preserving dedupe — no
        # double L3 spend when requirements share an MPN).
        mpns = list(dict.fromkeys(r.primary_mpn for r in requirements if r.primary_mpn))
        sem = asyncio.Semaphore(3)

        async def _bounded(mpn: str) -> list[dict]:
            async with sem:
                return await asyncio.to_thread(_find_affinity_in_thread, mpn)

        # F6: one MPN's failure must not blank the whole panel (or 500 the swap).
        # Failed MPNs are logged with the MPN in context; survivors render, and
        # the template shows a quiet "suggestions incomplete" notice.
        per_mpn_results = await asyncio.gather(*(_bounded(m) for m in mpns), return_exceptions=True)
        per_mpn_matches: list[list[dict]] = []
        for mpn, matches in zip(mpns, per_mpn_results):
            if isinstance(matches, BaseException):
                affinity_partial = True
                logger.error("Vendor affinity lookup failed for MPN {}: {}", mpn, matches)
                continue
            per_mpn_matches.append(matches)

        best: dict[str, dict] = {}
        for matches in per_mpn_matches:
            for match in matches:
                norm = normalize_vendor_name(match.get("vendor_name") or "")
                if not norm or norm in suggested_norms or norm in excluded:
                    continue
                if norm not in best or match["confidence"] > best[norm]["confidence"]:
                    best[norm] = {**match, "normalized_name": norm}
        affinity_vendors = sorted(best.values(), key=lambda m: m["confidence"], reverse=True)[:10]

    ctx = {"request": request, "affinity_vendors": affinity_vendors, "affinity_partial": affinity_partial}
    return template_response("htmx/partials/sightings/vendor_affinity_rows.html", ctx)


@router.get("/v2/partials/sightings/vendor-search", response_class=HTMLResponse)
async def sightings_vendor_search(
    request: Request,
    q: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """P5.2: server-rendered debounced dropdown for the composer's "Find any vendor"
    picker (sightings/vendor_modal.html, rfqVendorModal.searchVendors()).

    Reuses the SAME VendorCard name/alternate-names match that backs
    /api/autocomplete/names (vendors_crud.autocomplete_names) — that JSON endpoint mixes
    vendors + customers for a different caller and is left intact; this is a vendors-
    only HTML sibling so the picker's dropdown is a real hx-get swap instead of a
    client-side fetch + filter.
    """
    from sqlalchemy import String, cast

    from ...utils.search_builder import SearchBuilder

    query = q.strip().lower()
    vendors: list[VendorCard] = []
    limit = 8
    if len(query) >= 2:
        sb = SearchBuilder(query)
        # Primary: match on normalized_name (mirrors autocomplete_names).
        vendors = (
            db.query(VendorCard)
            .filter(VendorCard.normalized_name.ilike(f"%{sb.safe}%", escape="\\"))
            .order_by(VendorCard.sighting_count.desc().nullslast(), VendorCard.display_name)
            .limit(limit)
            .all()
        )
        # Secondary: match on alternate_names JSON (cast to text for ILIKE), deduped
        # against the primary hits and appended after them — same order as
        # autocomplete_names (vendors_crud.py).
        seen_ids = {v.id for v in vendors}
        vendors_by_alt = (
            db.query(VendorCard)
            .filter(
                cast(VendorCard.alternate_names, String).ilike(f"%{sb.safe}%", escape="\\"),
                VendorCard.id.notin_(seen_ids) if seen_ids else True,
            )
            .order_by(VendorCard.sighting_count.desc().nullslast(), VendorCard.display_name)
            .limit(limit)
            .all()
        )
        vendors = (vendors + vendors_by_alt)[:limit]
    ctx = {"request": request, "vendors": vendors}
    return template_response("htmx/partials/sightings/_vendor_search_results.html", ctx)


@router.post("/v2/partials/sightings/composer-vendor", response_class=HTMLResponse)
async def sightings_composer_vendor(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Resolve-or-create a vendor for the RFQ composer's any-vendor picker.

    Called by: vendor_modal.html — both the "Find any vendor" autocomplete pick and
    the "Add new vendor" inline mini-form POST here (form fields: vendor_name
    required; website, email, requirement_ids optional); the returned row is
    appended into the stable-id #rfq-added-vendors sub-container.

    Flow: check_vendor_duplicate (the extracted service — direct call, never
    loopback HTTP). A confident duplicate is an EXACT normalized-name match (the
    service's own classification: exact short-circuits at score 100; fuzzy >= 80
    are suggestions, not dupes) → return the EXISTING vendor as a selected row with
    a "matched existing vendor" notice, no new DB row. Otherwise create the minimal
    VendorCard (normalized_name, display_name, optional domain parsed from the
    website — the crm/offers manual-entry pattern) plus a VendorContact when an
    email was given, commit, then fire _background_enrich_vendor post-commit
    (identical to the materials.py / vendor_contacts.py patterns; imports are lazy
    so tests mock both gates at their source modules).

    If the resolved vendor is unavailability-excluded for the selected
    requirement_ids, the row renders the rose "marked unavailable" chip with a
    DISABLED checkbox — send-time re-validation stays the backstop.
    """
    form = await request.form()
    vendor_name = str(form.get("vendor_name") or "").strip()
    norm = normalize_vendor_name(vendor_name)
    if not vendor_name or not norm:
        raise HTTPException(status_code=400, detail="vendor_name required")
    website = str(form.get("website") or "").strip()
    email = str(form.get("email") or "").strip()
    if email and "@" not in email:
        raise HTTPException(status_code=400, detail="invalid contact email")
    domain = ""
    if website:
        domain = parse_website_domain(website)
        if not domain:
            raise HTTPException(status_code=400, detail="invalid website — could not extract a domain")
    req_id_list = [int(x) for x in form.getlist("requirement_ids") if str(x).strip().isdigit()]

    matches = check_vendor_duplicate(vendor_name, db)
    matched_existing = bool(matches) and matches[0]["match"] == "exact"
    contact_added = False

    # TOCTOU: check_vendor_duplicate ran an earlier, separate query, so the matched
    # card can be deleted between that check and this fetch. A None card here would
    # AttributeError → generic 500; instead treat it as "no confident duplicate" and
    # fall through to the create branch (re-resolving the name the user typed).
    matched_card = db.get(VendorCard, matches[0]["id"]) if matched_existing else None
    if matched_existing and matched_card is None:
        matched_existing = False

    if matched_existing:
        # Confident duplicate: hand back the existing card — but a typed email /
        # website must NOT be silently discarded (F4): attach the email as a
        # VendorContact (deduped case-insensitively against the card's existing
        # contacts) and backfill a missing domain. The row's notice reports the
        # email attach explicitly.
        assert matched_card is not None  # narrowed by the TOCTOU guard above
        card = matched_card
        updated = False
        if email:
            existing_emails = {
                (vc.email or "").lower()
                for vc in db.query(VendorContact).filter(VendorContact.vendor_card_id == card.id).all()
            }
            if email.lower() not in existing_emails:
                db.add(
                    VendorContact(
                        vendor_card_id=card.id,
                        email=email,
                        contact_type="company",
                        source="rfq_manual",
                        confidence=80,
                        is_verified=False,
                    )
                )
                contact_added = True
                updated = True
        if domain and not card.domain:
            card.domain = domain
            updated = True
        if updated:
            db.commit()
    else:
        card = VendorCard(
            normalized_name=norm,
            display_name=vendor_name,
            domain=domain or None,
            emails=[],
            phones=[],
        )
        db.add(card)
        db.flush()
        if email:
            db.add(
                VendorContact(
                    vendor_card_id=card.id,
                    email=email,
                    contact_type="company",
                    source="rfq_manual",
                    confidence=80,
                    is_verified=False,
                )
            )
        db.commit()

        # Post-commit background enrichment when a usable domain came with the
        # website. Lazy imports: tests mock _background_enrich_vendor and
        # get_credential_cached at their SOURCE modules (CLAUDE.md), and the
        # coroutine opens its own session (never the request session). F7: the
        # card is already committed — a failure ANYWHERE in this block is logged
        # and the created row is still returned (enrichment is best-effort;
        # turning it into a 500 would misreport a successful create).
        try:
            if card.domain and not card.last_enriched_at:
                from ...services.credential_service import get_credential_cached

                if get_credential_cached("explorium_enrichment", "EXPLORIUM_API_KEY") or get_credential_cached(
                    "anthropic_ai", "ANTHROPIC_API_KEY"
                ):
                    from ...utils.async_helpers import safe_background_task
                    from ...utils.vendor_helpers import _background_enrich_vendor

                    await safe_background_task(
                        _background_enrich_vendor(card.id, card.domain, card.display_name),
                        task_name="enrich_vendor_from_composer",
                    )
        except Exception:
            logger.error(
                "Post-create enrichment kickoff failed for vendor card {} — card committed and returned",
                card.id,
                exc_info=True,
            )

    # Active-only unavailability re-check for the selected parts: an excluded
    # vendor renders the rose chip with a DISABLED checkbox and never joins the
    # selection. Both norm spellings are checked (stored normalized_name may be
    # legacy-suffixed), same belt-and-braces as _coverage_ranked_vendor_rows.
    is_excluded = False
    if req_id_list:
        requirements = db.query(Requirement).filter(Requirement.id.in_(req_id_list)).all()
        if requirements:
            excluded = excluded_vendor_norms(db, requirements)
            canonical = normalize_vendor_name(card.display_name or card.normalized_name or "")
            is_excluded = canonical in excluded or (card.normalized_name or "") in excluded

    ctx = {
        "request": request,
        "vendor": card,
        "matched_existing": matched_existing,
        "contact_added": contact_added,
        "is_excluded": is_excluded,
    }
    return template_response("htmx/partials/sightings/composer_vendor_row.html", ctx)
