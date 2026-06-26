"""Tests for the POCancellation immutable model + cancellation reason vocabulary.

Covers the storage/validation contract only (key normalization + reason validation); the
days-to-cancel math and vendor-metric refresh live in the service tests.
"""

import pytest

from app.constants import POCancellationReason
from app.models import POCancellation
from app.utils.normalization import normalize_mpn_key
from app.vendor_utils import normalize_vendor_name


def test_po_cancellation_normalizes_keys(db_session):
    """vendor_name_normalized + normalized_mpn re-normalize through the canonical
    helpers (same key space as offers/unavailability) so lookups line up."""
    row = POCancellation(
        vendor_name_normalized="Arrow Electronics",
        normalized_mpn="lm317-t",
        po_number="PO-1001",
        reason_code=POCancellationReason.SOLD_ELSEWHERE.value,
    )
    db_session.add(row)
    db_session.commit()

    assert row.vendor_name_normalized == normalize_vendor_name("Arrow Electronics")
    assert row.normalized_mpn == normalize_mpn_key("lm317-t")


def test_po_cancellation_rejects_unknown_reason():
    """An off-vocabulary reason_code is unrepresentable (validated on assignment)."""
    with pytest.raises(ValueError):
        POCancellation(
            vendor_name_normalized="arrow electronics",
            normalized_mpn="LM317T",
            po_number="PO-1",
            reason_code="totally_made_up",
        )


def test_po_cancellation_rejects_unmatchable_mpn():
    """A key that normalizes to nothing would be unmatchable — reject it."""
    with pytest.raises(ValueError):
        POCancellation(
            vendor_name_normalized="arrow electronics",
            normalized_mpn="  ",
            po_number="PO-1",
            reason_code=POCancellationReason.OTHER.value,
        )


def test_po_cancellation_persists_full_row(db_session, test_vendor_card):
    """A fully-specified cancellation row round-trips with all metric fields."""
    row = POCancellation(
        vendor_card_id=test_vendor_card.id,
        vendor_name_normalized="arrow electronics",
        normalized_mpn="LM317T",
        po_number="PO-2002",
        days_to_cancel=12,
        reason_code=POCancellationReason.CANNOT_DELIVER.value,
        reason_text="Vendor stopped responding after PO cut.",
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)

    assert row.id is not None
    assert row.created_at is not None
    assert row.days_to_cancel == 12
    assert row.reason_code == "cannot_deliver"
