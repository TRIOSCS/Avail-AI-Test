"""
routers/htmx/admin.py — HTMX partials for admin operations: dedup, merge, health, imports, source testing.

Provides vendor/company dedup suggestions, merge actions, system health dashboard,
CSV import for customers and vendors, and API source connectivity testing.

Called by: htmx router package (htmx_views.py imports this module)
Depends on: _helpers (router, templates, _base_ctx), models (VendorCard, Company, User, ApiSource),
            services (vendor_merge_service, company_merge_service),
            dependencies (require_admin), database (get_db)
"""

import csv
import io
from datetime import datetime, timezone

from fastapi import Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...database import get_db
from ...dependencies import require_admin
from ...models import ApiSource, Company, CustomerSite, User, VendorCard
from ._helpers import router


@router.get("/v2/partials/admin/vendor-dedup", response_class=HTMLResponse)
async def admin_vendor_dedup_partial(
    request: Request,
    threshold: int = Query(85, ge=70, le=100),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Show vendor dedup suggestions as an HTML table with merge buttons."""
    try:
        from ...vendor_utils import find_vendor_dedup_candidates

        candidates = find_vendor_dedup_candidates(db, threshold, limit)
    except Exception as exc:
        logger.error("Vendor dedup failed: {}", exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Error loading dedup suggestions: {exc}</div>"
        )

    if not candidates:
        return HTMLResponse(
            "<div class='alert alert-info'>No duplicate vendors found.</div>"
        )

    rows = ""
    for c in candidates:
        keep_id = c.get("id_a") or c.get("keep_id", "")
        remove_id = c.get("id_b") or c.get("remove_id", "")
        name_a = c.get("name_a", c.get("keep_name", ""))
        name_b = c.get("name_b", c.get("remove_name", ""))
        score = c.get("score", c.get("similarity", ""))
        rows += (
            f"<tr>"
            f"<td>{name_a} (#{keep_id})</td>"
            f"<td>{name_b} (#{remove_id})</td>"
            f"<td>{score}</td>"
            f"<td>"
            f"<button class='btn btn-sm btn-warning' "
            f"hx-post='/v2/partials/admin/vendor-merge' "
            f"hx-vals='{{\"keep_id\": {keep_id}, \"remove_id\": {remove_id}}}' "
            f"hx-target='closest tr' hx-swap='outerHTML' "
            f"hx-confirm='Merge {name_b} into {name_a}?'>"
            f"Merge</button>"
            f"</td></tr>"
        )

    html = (
        "<table class='table table-sm'><thead><tr>"
        "<th>Keep</th><th>Remove</th><th>Score</th><th>Action</th>"
        "</tr></thead><tbody>"
        f"{rows}</tbody></table>"
    )
    return HTMLResponse(html)


@router.post("/v2/partials/admin/vendor-merge", response_class=HTMLResponse)
async def admin_vendor_merge_partial(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Merge two vendors and return success HTML."""
    try:
        from ...services.vendor_merge_service import merge_vendor_cards

        merge_vendor_cards(keep_id, remove_id, db)
        db.commit()
        logger.info("Vendor merge via HTMX: keep={} remove={} by user={}", keep_id, remove_id, user.id)
        return HTMLResponse(
            f"<tr class='table-success'><td colspan='4'>"
            f"Merged vendor #{remove_id} into #{keep_id} successfully.</td></tr>",
            headers={"HX-Trigger": "vendorMerged"},
        )
    except ValueError as exc:
        return HTMLResponse(
            f"<div class='alert alert-danger'>Merge failed: {exc}</div>",
            status_code=400,
        )
    except Exception as exc:
        logger.error("Vendor merge failed: {}", exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Error: {exc}</div>",
            status_code=500,
        )


@router.get("/v2/partials/admin/company-dedup", response_class=HTMLResponse)
async def admin_company_dedup_partial(
    request: Request,
    threshold: int = Query(85, ge=70, le=100),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Show company dedup suggestions as an HTML table with merge buttons."""
    try:
        from ...company_utils import find_company_dedup_candidates

        candidates = find_company_dedup_candidates(db, threshold, limit)
    except Exception as exc:
        logger.error("Company dedup failed: {}", exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Error loading dedup suggestions: {exc}</div>"
        )

    if not candidates:
        return HTMLResponse(
            "<div class='alert alert-info'>No duplicate companies found.</div>"
        )

    rows = ""
    for c in candidates:
        keep_id = c.get("id_a") or c.get("keep_id", "")
        remove_id = c.get("id_b") or c.get("remove_id", "")
        name_a = c.get("name_a", c.get("keep_name", ""))
        name_b = c.get("name_b", c.get("remove_name", ""))
        score = c.get("score", c.get("similarity", ""))
        rows += (
            f"<tr>"
            f"<td>{name_a} (#{keep_id})</td>"
            f"<td>{name_b} (#{remove_id})</td>"
            f"<td>{score}</td>"
            f"<td>"
            f"<button class='btn btn-sm btn-warning' "
            f"hx-post='/v2/partials/admin/company-merge' "
            f"hx-vals='{{\"keep_id\": {keep_id}, \"remove_id\": {remove_id}}}' "
            f"hx-target='closest tr' hx-swap='outerHTML' "
            f"hx-confirm='Merge {name_b} into {name_a}?'>"
            f"Merge</button>"
            f"</td></tr>"
        )

    html = (
        "<table class='table table-sm'><thead><tr>"
        "<th>Keep</th><th>Remove</th><th>Score</th><th>Action</th>"
        "</tr></thead><tbody>"
        f"{rows}</tbody></table>"
    )
    return HTMLResponse(html)


@router.post("/v2/partials/admin/company-merge", response_class=HTMLResponse)
async def admin_company_merge_partial(
    request: Request,
    keep_id: int = Form(...),
    remove_id: int = Form(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Merge two companies and return success HTML."""
    try:
        from ...services.company_merge_service import merge_companies

        merge_companies(keep_id, remove_id, db)
        db.commit()
        logger.info("Company merge via HTMX: keep={} remove={} by user={}", keep_id, remove_id, user.id)
        return HTMLResponse(
            f"<tr class='table-success'><td colspan='4'>"
            f"Merged company #{remove_id} into #{keep_id} successfully.</td></tr>",
            headers={"HX-Trigger": "companyMerged"},
        )
    except ValueError as exc:
        return HTMLResponse(
            f"<div class='alert alert-danger'>Merge failed: {exc}</div>",
            status_code=400,
        )
    except Exception as exc:
        logger.error("Company merge failed: {}", exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Error: {exc}</div>",
            status_code=500,
        )


@router.get("/v2/partials/admin/health", response_class=HTMLResponse)
async def admin_health_partial(
    request: Request,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """System health dashboard with DB row counts and service status."""
    try:
        counts = {
            "users": db.query(func.count(User.id)).scalar() or 0,
            "companies": db.query(func.count(Company.id)).scalar() or 0,
            "vendors": db.query(func.count(VendorCard.id)).scalar() or 0,
            "sites": db.query(func.count(CustomerSite.id)).scalar() or 0,
        }

        try:
            from ...models import Requisition
            counts["requisitions"] = db.query(func.count(Requisition.id)).scalar() or 0
        except Exception:
            counts["requisitions"] = "N/A"

        try:
            from ...models import KnowledgeEntry
            counts["knowledge_entries"] = db.query(func.count(KnowledgeEntry.id)).scalar() or 0
        except Exception:
            counts["knowledge_entries"] = "N/A"

        rows = ""
        for name, count in counts.items():
            label = name.replace("_", " ").title()
            rows += f"<tr><td>{label}</td><td>{count}</td></tr>"

        html = (
            "<div class='card'><div class='card-header'><h5>System Health</h5></div>"
            "<div class='card-body'>"
            "<table class='table table-sm'><thead><tr>"
            "<th>Resource</th><th>Count</th>"
            "</tr></thead><tbody>"
            f"{rows}"
            "</tbody></table>"
            f"<small class='text-muted'>As of {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</small>"
            "</div></div>"
        )
        return HTMLResponse(html)
    except Exception as exc:
        logger.error("Health check failed: {}", exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Health check error: {exc}</div>",
            status_code=500,
        )


@router.post("/v2/partials/admin/import/customers", response_class=HTMLResponse)
async def admin_import_customers_partial(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Import customers from CSV. Creates companies and sites. Returns result counts."""
    try:
        content = await file.read()
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))

        created = 0
        skipped = 0
        errors = []

        for row_num, row in enumerate(reader, start=2):
            name = (row.get("name") or row.get("company_name") or "").strip()
            if not name:
                skipped += 1
                continue

            existing = db.query(Company).filter(func.lower(Company.name) == name.lower()).first()
            if existing:
                skipped += 1
                continue

            try:
                company = Company(
                    name=name,
                    website=(row.get("website") or "").strip() or None,
                    industry=(row.get("industry") or "").strip() or None,
                    is_active=True,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(company)
                db.flush()

                site_name = (row.get("site_name") or "HQ").strip()
                site = CustomerSite(
                    company_id=company.id,
                    site_name=site_name,
                    address_line1=(row.get("address") or "").strip() or None,
                    city=(row.get("city") or "").strip() or None,
                    state=(row.get("state") or "").strip() or None,
                    country=(row.get("country") or "").strip() or None,
                )
                db.add(site)
                created += 1
            except Exception as row_exc:
                errors.append(f"Row {row_num}: {row_exc}")
                db.rollback()

        db.commit()
        logger.info("Customer import: created={} skipped={} errors={}", created, skipped, len(errors))

        error_html = ""
        if errors:
            error_items = "".join(f"<li>{e}</li>" for e in errors[:10])
            error_html = f"<div class='alert alert-warning mt-2'><ul>{error_items}</ul></div>"

        html = (
            f"<div class='alert alert-success'>"
            f"Import complete: {created} created, {skipped} skipped."
            f"</div>{error_html}"
        )
        return HTMLResponse(html, headers={"HX-Trigger": "importComplete"})
    except Exception as exc:
        logger.error("Customer import failed: {}", exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Import failed: {exc}</div>",
            status_code=400,
        )


@router.post("/v2/partials/admin/import/vendors", response_class=HTMLResponse)
async def admin_import_vendors_partial(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Import vendors from CSV. Creates vendor cards. Returns result counts."""
    try:
        content = await file.read()
        text = content.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))

        created = 0
        skipped = 0
        errors = []

        for row_num, row in enumerate(reader, start=2):
            name = (row.get("name") or row.get("vendor_name") or row.get("display_name") or "").strip()
            if not name:
                skipped += 1
                continue

            normalized = name.lower().strip()
            existing = db.query(VendorCard).filter(VendorCard.normalized_name == normalized).first()
            if existing:
                skipped += 1
                continue

            try:
                card = VendorCard(
                    display_name=name,
                    normalized_name=normalized,
                    domain=(row.get("domain") or row.get("website") or "").strip() or None,
                    hq_country=(row.get("country") or row.get("hq_country") or "").strip() or None,
                    industry=(row.get("industry") or "").strip() or None,
                    sighting_count=0,
                    created_at=datetime.now(timezone.utc),
                )
                db.add(card)
                created += 1
            except Exception as row_exc:
                errors.append(f"Row {row_num}: {row_exc}")
                db.rollback()

        db.commit()
        logger.info("Vendor import: created={} skipped={} errors={}", created, skipped, len(errors))

        error_html = ""
        if errors:
            error_items = "".join(f"<li>{e}</li>" for e in errors[:10])
            error_html = f"<div class='alert alert-warning mt-2'><ul>{error_items}</ul></div>"

        html = (
            f"<div class='alert alert-success'>"
            f"Import complete: {created} created, {skipped} skipped."
            f"</div>{error_html}"
        )
        return HTMLResponse(html, headers={"HX-Trigger": "importComplete"})
    except Exception as exc:
        logger.error("Vendor import failed: {}", exc)
        return HTMLResponse(
            f"<div class='alert alert-danger'>Import failed: {exc}</div>",
            status_code=400,
        )


@router.get("/v2/partials/settings/sources/{source_id}/test", response_class=HTMLResponse)
async def admin_test_source_partial(
    request: Request,
    source_id: int,
    user: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Test an API source connectivity and return a result badge."""
    source = db.get(ApiSource, source_id)
    if not source:
        raise HTTPException(404, "API source not found")

    try:
        is_healthy = source.is_active and source.status == "active"
        status = "healthy" if is_healthy else "unhealthy"
        badge_class = "bg-success" if is_healthy else "bg-danger"
        label = source.display_name or source.name or f"Source #{source_id}"

        html = (
            f"<span class='badge {badge_class}'>"
            f"{label}: {status}</span>"
        )
        return HTMLResponse(html)
    except Exception as exc:
        logger.error("Source test failed for {}: {}", source_id, exc)
        return HTMLResponse(
            f"<span class='badge bg-warning'>Test failed: {exc}</span>"
        )
