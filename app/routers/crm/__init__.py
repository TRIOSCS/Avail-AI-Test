"""CRM router package — split from monolithic crm.py for maintainability.

Sub-modules:
  companies.py  — Company CRUD, enrichment, typeahead
  sites.py      — Customer sites + site contacts
  enrichment.py — Enrichment, suggested contacts, sync logs, users, customer import
  offers.py     — Offer CRUD, attachments, OneDrive, changelog
  quotes.py     — Quote CRUD, preview, send, pricing history
  buy_plans_v3.py — Unified buy plan V4 (submit/approve/verify/PO/complete)
  clone.py      — Requisition clone
"""

from fastapi import APIRouter

# Re-export helpers for backward compatibility (tests import from app.routers.crm)
from ._helpers import (  # noqa: F401
    _build_quote_email_html,
    _preload_last_quoted_prices,
    get_last_quoted_price,
    next_quote_number,
    quote_to_dict,
    record_changes,
)
from .buy_plans_v3 import router as buy_plans_router
from .clone import router as clone_router
from .companies import router as companies_router
from .enrichment import router as enrichment_router
from .offers import router as offers_router
from .quotes import router as quotes_router
from .sites import router as sites_router

router = APIRouter()
router.include_router(companies_router)
router.include_router(sites_router)
router.include_router(enrichment_router)
router.include_router(offers_router)
router.include_router(quotes_router)
router.include_router(buy_plans_router)
router.include_router(clone_router)
