"""Test that Company and VendorCard expose the 4 provenance+firmographic columns.

No DB required — just inspects the mapped class __table__.columns.
"""

from app.models.crm import Company
from app.models.vendors import VendorCard


def test_models_have_provenance_columns():
    for m in (Company, VendorCard):
        cols = m.__table__.columns
        for c in ("ticker", "naics", "revenue_range", "enrichment_provenance"):
            assert c in cols, f"{m.__name__} missing column '{c}'"
