"""Tests for company_merge_service.py — extracted company merge logic.

Verifies that merge moves sites, combines tags/notes/fields, reassigns FK references,
and deletes the removed company while preserving all data.
"""

import pytest

from app.models import Company, CustomerSite
from app.services.company_merge_service import merge_companies


def _make_pair(db_session, keep_kwargs, remove_kwargs):
    """Create a keep/remove Company pair, add + flush them, and return both.

    Flush assigns ids without committing; every caller commits later.
    """
    keep = Company(is_active=True, **keep_kwargs)
    remove = Company(is_active=True, **remove_kwargs)
    db_session.add_all([keep, remove])
    db_session.flush()
    return keep, remove


def test_merge_moves_sites(db_session):
    """Non-empty sites from removed company are moved to kept company."""
    keep, remove = _make_pair(db_session, {"name": "Acme Corp"}, {"name": "Acme Corporation"})

    site = CustomerSite(company_id=remove.id, site_name="West Coast Office", contact_email="a@acme.com")
    db_session.add(site)
    db_session.commit()

    result = merge_companies(keep.id, remove.id, db_session)
    db_session.commit()

    assert result["sites_moved"] == 1
    refreshed = db_session.get(CustomerSite, site.id)
    assert refreshed.company_id == keep.id


def test_merge_deletes_empty_hq(db_session):
    """Empty HQ sites from removed company are deleted, not moved."""
    keep, remove = _make_pair(db_session, {"name": "Widget Co"}, {"name": "Widget Company"})

    empty_hq = CustomerSite(company_id=remove.id, site_name="HQ")
    db_session.add(empty_hq)
    db_session.commit()
    hq_id = empty_hq.id

    result = merge_companies(keep.id, remove.id, db_session)
    db_session.commit()

    assert result["sites_deleted"] == 1
    assert db_session.get(CustomerSite, hq_id) is None


def test_merge_combines_tags(db_session):
    """Tags from both companies are merged and deduplicated."""
    keep, remove = _make_pair(
        db_session,
        {"name": "A Corp", "brand_tags": ["tag1"], "commodity_tags": ["c1"]},
        {"name": "A Corporation", "brand_tags": ["tag1", "tag2"], "commodity_tags": ["c2"]},
    )

    merge_companies(keep.id, remove.id, db_session)
    db_session.commit()

    merged = db_session.get(Company, keep.id)
    assert "tag1" in merged.brand_tags
    assert "tag2" in merged.brand_tags
    assert "c1" in merged.commodity_tags
    assert "c2" in merged.commodity_tags


def test_merge_appends_notes(db_session):
    """Notes from removed company are appended to kept company."""
    keep, remove = _make_pair(
        db_session,
        {"name": "B Corp", "notes": "Original notes"},
        {"name": "B Corporation", "notes": "Extra info"},
    )

    merge_companies(keep.id, remove.id, db_session)
    db_session.commit()

    merged = db_session.get(Company, keep.id)
    assert "Original notes" in merged.notes
    assert "Extra info" in merged.notes
    assert "Merged from" in merged.notes


def test_merge_fills_missing_fields(db_session):
    """Missing fields on kept company are filled from removed company."""
    keep, remove = _make_pair(
        db_session,
        {"name": "C Corp", "domain": None, "industry": "Tech"},
        {"name": "C Corporation", "domain": "ccorp.com", "industry": None},
    )

    merge_companies(keep.id, remove.id, db_session)
    db_session.commit()

    merged = db_session.get(Company, keep.id)
    assert merged.domain == "ccorp.com"
    assert merged.industry == "Tech"  # Not overwritten


def test_merge_deletes_removed_company(db_session):
    """The removed company is deleted after merge."""
    keep, remove = _make_pair(db_session, {"name": "D Corp"}, {"name": "D Corporation"})
    remove_id = remove.id

    merge_companies(keep.id, remove.id, db_session)
    db_session.commit()

    assert db_session.get(Company, remove_id) is None


def test_merge_same_id_raises(db_session):
    """Merging a company with itself raises ValueError."""
    co = Company(name="Test", is_active=True)
    db_session.add(co)
    db_session.commit()

    with pytest.raises(ValueError, match="Cannot merge a company with itself"):
        merge_companies(co.id, co.id, db_session)


def test_merge_missing_company_raises(db_session):
    """Merging with a nonexistent company raises ValueError."""
    co = Company(name="Test", is_active=True)
    db_session.add(co)
    db_session.commit()

    with pytest.raises(ValueError, match="not found"):
        merge_companies(co.id, 99999, db_session)


def test_merge_reassign_failure_aborts_and_preserves_remove(db_session):
    """A failed FK-reassignment must fail CLOSED: merge re-raises and does NOT delete
    the removed company (mirrors vendor_merge_service / delete_companies).

    Regression: previously the loop swallowed the exception and proceeded to
    db.delete(remove)/flush anyway, orphaning or cascade-deleting the un-reassigned rows.
    """
    from app.models import ActivityLog

    keep, remove = _make_pair(db_session, {"name": "F Corp"}, {"name": "F Corporation"})
    remove_id = remove.id
    db_session.commit()

    real_query = db_session.query

    def _boom_on_activity_log(model, *args, **kwargs):
        if model is ActivityLog:
            raise RuntimeError("simulated bulk UPDATE failure (unique-constraint conflict)")
        return real_query(model, *args, **kwargs)

    db_session.query = _boom_on_activity_log
    try:
        with pytest.raises(ValueError, match="Company merge aborted"):
            merge_companies(keep.id, remove_id, db_session)
    finally:
        db_session.query = real_query

    db_session.rollback()
    # The removed company must still exist — merge aborted before deleting it.
    assert db_session.get(Company, remove_id) is not None


def test_merge_renames_colliding_sites(db_session):
    """Sites with duplicate names get prefixed with removed company name."""
    keep, remove = _make_pair(db_session, {"name": "E Corp"}, {"name": "E Corporation"})

    keep_site = CustomerSite(company_id=keep.id, site_name="Main Office", contact_email="x@e.com")
    remove_site = CustomerSite(company_id=remove.id, site_name="Main Office", contact_email="y@e.com")
    db_session.add_all([keep_site, remove_site])
    db_session.commit()

    merge_companies(keep.id, remove.id, db_session)
    db_session.commit()

    refreshed = db_session.get(CustomerSite, remove_site.id)
    assert "E Corporation" in refreshed.site_name
