"""Tests for vendor_merge_service.py — extracted vendor card merge logic.

Verifies that merge correctly combines array fields, reassigns FK references,
sums sighting counts, and deletes the removed card.
"""

import pytest
from tests.conftest import engine

from app.models import VendorCard, VendorContact
from app.services.vendor_merge_service import merge_vendor_cards


def test_merge_combines_array_fields(db_session):
    """Array fields (emails, phones, alternate_names) are merged and deduplicated."""
    keep = VendorCard(
        normalized_name="arrow electronics",
        display_name="Arrow Electronics",
        emails=["a@arrow.com"],
        phones=["111"],
        alternate_names=[],
    )
    remove = VendorCard(
        normalized_name="arrow electronics inc",
        display_name="Arrow Electronics Inc",
        emails=["a@arrow.com", "b@arrow.com"],
        phones=["222"],
        alternate_names=["ARROW"],
    )
    db_session.add_all([keep, remove])
    db_session.commit()

    result = merge_vendor_cards(keep.id, remove.id, db_session)
    db_session.commit()

    assert result["ok"] is True
    merged = db_session.get(VendorCard, keep.id)
    assert "a@arrow.com" in merged.emails
    assert "b@arrow.com" in merged.emails
    assert "111" in merged.phones
    assert "222" in merged.phones
    assert "Arrow Electronics Inc" in merged.alternate_names


def test_merge_sums_sighting_counts(db_session):
    """Sighting counts from both cards are summed."""
    keep = VendorCard(
        normalized_name="digikey", display_name="DigiKey",
        emails=[], phones=[], sighting_count=100,
    )
    remove = VendorCard(
        normalized_name="digi-key", display_name="Digi-Key",
        emails=[], phones=[], sighting_count=50,
    )
    db_session.add_all([keep, remove])
    db_session.commit()

    merge_vendor_cards(keep.id, remove.id, db_session)
    db_session.commit()

    merged = db_session.get(VendorCard, keep.id)
    assert merged.sighting_count == 150


def test_merge_reassigns_contacts(db_session):
    """VendorContacts from removed card are reassigned to kept card."""
    keep = VendorCard(
        normalized_name="mouser", display_name="Mouser",
        emails=[], phones=[],
    )
    remove = VendorCard(
        normalized_name="mouser electronics", display_name="Mouser Electronics",
        emails=[], phones=[],
    )
    db_session.add_all([keep, remove])
    db_session.flush()

    contact = VendorContact(
        vendor_card_id=remove.id, full_name="John", email="john@mouser.com",
        source="manual",
    )
    db_session.add(contact)
    db_session.commit()

    merge_vendor_cards(keep.id, remove.id, db_session)
    db_session.commit()

    # Contact should now point to kept card
    refreshed = db_session.get(VendorContact, contact.id)
    assert refreshed.vendor_card_id == keep.id


def test_merge_deletes_removed_card(db_session):
    """The removed vendor card is deleted after merge."""
    keep = VendorCard(
        normalized_name="avnet", display_name="Avnet",
        emails=[], phones=[],
    )
    remove = VendorCard(
        normalized_name="avnet inc", display_name="Avnet Inc",
        emails=[], phones=[],
    )
    db_session.add_all([keep, remove])
    db_session.commit()
    remove_id = remove.id

    merge_vendor_cards(keep.id, remove.id, db_session)
    db_session.commit()

    assert db_session.get(VendorCard, remove_id) is None


def test_merge_same_id_raises(db_session):
    """Merging a vendor with itself raises ValueError."""
    card = VendorCard(
        normalized_name="test", display_name="Test",
        emails=[], phones=[],
    )
    db_session.add(card)
    db_session.commit()

    with pytest.raises(ValueError, match="Cannot merge a vendor with itself"):
        merge_vendor_cards(card.id, card.id, db_session)


def test_merge_missing_card_raises(db_session):
    """Merging with a nonexistent card raises ValueError."""
    card = VendorCard(
        normalized_name="test", display_name="Test",
        emails=[], phones=[],
    )
    db_session.add(card)
    db_session.commit()

    with pytest.raises(ValueError, match="not found"):
        merge_vendor_cards(card.id, 99999, db_session)
