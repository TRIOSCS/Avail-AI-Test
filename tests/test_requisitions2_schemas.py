"""Tests for Pydantic schemas used by the Requisitions 2 HTMX page.

Covers: filter defaults, pagination bounds, bulk ID parsing/validation.

Called by: pytest
Depends on: app/schemas/requisitions2.py
"""

import pytest

from app.schemas.requisitions2 import (
    BulkActionForm,
    PaginationContext,
    ReqListFilters,
    ReqStatus,
    SortColumn,
    SortOrder,
)


# ── ReqListFilters ───────────────────────────────────────────────────


def test_default_filters():
    """Defaults should be sensible for a first page load."""
    f = ReqListFilters()
    assert f.q == ""
    assert f.status == ReqStatus.active
    assert f.owner is None
    assert f.urgency is None
    assert f.date_from is None
    assert f.date_to is None
    assert f.sort == SortColumn.created_at
    assert f.order == SortOrder.desc
    assert f.page == 1
    assert f.per_page == 25


def test_filters_accept_valid_values():
    """Filters parse valid enum values correctly."""
    f = ReqListFilters(status="archived", sort="name", order="asc", page=3, per_page=50)
    assert f.status == ReqStatus.archived
    assert f.sort == SortColumn.name
    assert f.order == SortOrder.asc
    assert f.page == 3
    assert f.per_page == 50


def test_invalid_page_number():
    """Page must be >= 1."""
    with pytest.raises(Exception):
        ReqListFilters(page=0)


def test_per_page_max_100():
    """per_page must be <= 100."""
    with pytest.raises(Exception):
        ReqListFilters(per_page=101)


def test_per_page_min_1():
    """per_page must be >= 1."""
    with pytest.raises(Exception):
        ReqListFilters(per_page=0)


# ── BulkActionForm ───────────────────────────────────────────────────


def test_bulk_ids_parsing():
    """Valid comma-separated IDs parse correctly."""
    form = BulkActionForm(ids="1,2,3")
    assert form.id_list() == [1, 2, 3]


def test_bulk_ids_with_spaces():
    """IDs with spaces around commas still parse."""
    form = BulkActionForm(ids="10 , 20 , 30")
    assert form.id_list() == [10, 20, 30]


def test_bulk_ids_single():
    """Single ID is valid."""
    form = BulkActionForm(ids="42")
    assert form.id_list() == [42]


def test_bulk_ids_empty_rejected():
    """Empty string is rejected."""
    with pytest.raises(Exception):
        BulkActionForm(ids="")


def test_bulk_ids_non_numeric_rejected():
    """Non-numeric IDs are rejected."""
    with pytest.raises(Exception):
        BulkActionForm(ids="1,abc,3")


def test_bulk_ids_max_200():
    """More than 200 IDs are rejected."""
    ids = ",".join(str(i) for i in range(201))
    with pytest.raises(Exception):
        BulkActionForm(ids=ids)


def test_bulk_ids_exactly_200():
    """Exactly 200 IDs is acceptable."""
    ids = ",".join(str(i) for i in range(1, 201))
    form = BulkActionForm(ids=ids)
    assert len(form.id_list()) == 200


def test_bulk_form_with_owner_id():
    """owner_id is optional on bulk form."""
    form = BulkActionForm(ids="1,2", owner_id=5)
    assert form.owner_id == 5
    assert form.id_list() == [1, 2]


# ── PaginationContext ────────────────────────────────────────────────


def test_pagination_context():
    """PaginationContext holds correct values."""
    p = PaginationContext(page=2, per_page=25, total=75, total_pages=3)
    assert p.page == 2
    assert p.total_pages == 3
