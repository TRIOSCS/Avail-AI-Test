"""Tests for the requisition list service layer.

Covers: list with filters, search, pagination, sort, role-based access,
detail modal data.

Called by: pytest
Depends on: app/services/requisition_list_service.py, conftest fixtures
"""

from datetime import datetime, timezone

from app.models import Requisition
from app.schemas.requisitions2 import ReqListFilters, ReqStatus, SortColumn, SortOrder
from app.services.requisition_list_service import (
    _hours_until_bid_due,
    _resolve_deal_value,
    get_requisition_detail,
    get_team_users,
    list_requisitions,
)

# ── helpers: _hours_until_bid_due / _resolve_deal_value ──────────────


def test_hours_until_bid_due_none_and_empty():
    assert _hours_until_bid_due(None) is None
    assert _hours_until_bid_due("") is None
    assert _hours_until_bid_due("   ") is None


def test_hours_until_bid_due_unparseable_returns_none():
    # Literal "ASAP" and other non-ISO strings must degrade to None,
    # not raise, so the urgency accent just doesn't render.
    assert _hours_until_bid_due("ASAP") is None
    assert _hours_until_bid_due("next week") is None


def test_hours_until_bid_due_iso_date_treated_as_end_of_day():
    # A date in the past → negative hours; a date in the future → positive.
    past = _hours_until_bid_due("2000-01-01")
    future = _hours_until_bid_due("2999-01-01")
    assert past is not None and past < 0
    assert future is not None and future > 0


def test_hours_until_bid_due_iso_datetime_parses():
    iso = "2999-06-15T12:00:00+00:00"
    hrs = _hours_until_bid_due(iso)
    assert hrs is not None and hrs > 0


# ── _resolve_deal_value (extended signature) ─────────────────────────


def test_resolve_deal_value_prefers_entered():
    val, src = _resolve_deal_value(opportunity_value=50000.0, priced_sum=10.0, priced_count=1, requirement_count=5)
    assert val == 50000.0
    assert src == "entered"


def test_resolve_deal_value_all_priced_is_computed():
    val, src = _resolve_deal_value(opportunity_value=None, priced_sum=2500.0, priced_count=3, requirement_count=3)
    assert val == 2500.0
    assert src == "computed"


def test_resolve_deal_value_some_priced_is_partial():
    val, src = _resolve_deal_value(opportunity_value=None, priced_sum=1800.0, priced_count=3, requirement_count=5)
    assert val == 1800.0
    assert src == "partial"


def test_resolve_deal_value_zero_price_counts_as_priced():
    val, src = _resolve_deal_value(opportunity_value=None, priced_sum=1500.0, priced_count=4, requirement_count=4)
    assert val == 1500.0
    assert src == "computed"


def test_resolve_deal_value_none_priced_is_none():
    val, src = _resolve_deal_value(opportunity_value=None, priced_sum=0.0, priced_count=0, requirement_count=3)
    assert val is None
    assert src == "none"


def test_resolve_deal_value_zero_opportunity_falls_through():
    val, src = _resolve_deal_value(opportunity_value=0.0, priced_sum=1500.0, priced_count=2, requirement_count=2)
    assert val == 1500.0
    assert src == "computed"


# ── list_requisitions ────────────────────────────────────────────────


def test_list_row_exposes_v2_visual_fields(db_session, test_user, test_requisition):
    """New row keys required by the v2 row template must be present."""
    filters = ReqListFilters(status=ReqStatus.all)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    req = result["requisitions"][0]
    assert "hours_until_bid_due" in req  # may be None if no deadline set
    assert "deal_value_display" in req
    assert "deal_value_source" in req
    assert req["deal_value_source"] in {"entered", "computed", "partial", "none"}


def test_list_returns_correct_fields(db_session, test_user, test_requisition):
    """List result contains expected keys for each requisition."""
    filters = ReqListFilters(status=ReqStatus.all)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    assert "requisitions" in result
    assert "pagination" in result
    assert "filters" in result
    assert len(result["requisitions"]) >= 1
    req = result["requisitions"][0]
    assert "id" in req
    assert "name" in req
    assert "status" in req
    assert "requirement_count" in req
    assert "offer_count" in req
    assert "created_by_name" in req
    assert "urgency" in req
    assert "sourcing_score" in req


def test_list_filters_by_status(db_session, test_user, test_requisition):
    """Status filter excludes non-matching requisitions."""
    # test_requisition is 'open' status
    filters = ReqListFilters(status=ReqStatus.archived)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    assert len(result["requisitions"]) == 0


def test_list_search_by_name(db_session, test_user, test_requisition):
    """Search by name returns matching requisitions."""
    filters = ReqListFilters(q="REQ-TEST")
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    assert len(result["requisitions"]) >= 1
    assert result["requisitions"][0]["name"] == "REQ-TEST-001"


def test_list_search_no_match(db_session, test_user, test_requisition):
    """Search with non-matching query returns empty."""
    filters = ReqListFilters(q="NONEXISTENT-XYZ")
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    assert len(result["requisitions"]) == 0


def test_list_pagination_math(db_session, test_user):
    """Pagination computes correct total_pages."""
    # Create 3 requisitions
    for i in range(3):
        req = Requisition(
            name=f"PAGE-REQ-{i}",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, per_page=2)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    assert result["pagination"].total == 3
    assert result["pagination"].total_pages == 2
    assert len(result["requisitions"]) == 2  # page 1 has 2 items


def test_list_pagination_page_2(db_session, test_user):
    """Page 2 returns remaining items."""
    for i in range(3):
        req = Requisition(
            name=f"PAGE-REQ-{i}",
            status="active",
            created_by=test_user.id,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(req)
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, per_page=2, page=2)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    assert len(result["requisitions"]) == 1  # page 2 has 1 item


def test_list_sort_ascending(db_session, test_user):
    """Sort ascending by name orders A before Z."""
    req_a = Requisition(
        name="AAA-REQ",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    req_z = Requisition(
        name="ZZZ-REQ",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([req_a, req_z])
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, sort=SortColumn.name, order=SortOrder.asc)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert names[0] == "AAA-REQ"
    assert names[-1] == "ZZZ-REQ"


def test_list_sort_descending(db_session, test_user):
    """Sort descending by name orders Z before A."""
    req_a = Requisition(
        name="AAA-REQ",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    req_z = Requisition(
        name="ZZZ-REQ",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([req_a, req_z])
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, sort=SortColumn.name, order=SortOrder.desc)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert names[0] == "ZZZ-REQ"
    assert names[-1] == "AAA-REQ"


def test_sales_role_filtering(db_session, test_user, sales_user):
    """Sales role only sees own requisitions."""
    req_buyer = Requisition(
        name="BUYER-REQ",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    req_sales = Requisition(
        name="SALES-REQ",
        status="active",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([req_buyer, req_sales])
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active)
    result = list_requisitions(db_session, filters, sales_user.id, "sales")
    names = [r["name"] for r in result["requisitions"]]
    assert "SALES-REQ" in names
    assert "BUYER-REQ" not in names


# ── get_requisition_detail ───────────────────────────────────────────


def test_detail_returns_requirements(db_session, test_user, test_requisition):
    """Detail includes requirements list."""
    detail = get_requisition_detail(db_session, test_requisition.id, test_user.id, "buyer")
    assert detail is not None
    assert detail["req"]["name"] == "REQ-TEST-001"
    assert len(detail["requirements"]) >= 1
    assert detail["requirements"][0].primary_mpn == "LM317T"


def test_detail_returns_none_for_missing(db_session, test_user):
    """Detail returns None for nonexistent requisition."""
    detail = get_requisition_detail(db_session, 99999, test_user.id, "buyer")
    assert detail is None


def test_detail_sales_cannot_see_others(db_session, test_user, sales_user, test_requisition):
    """Sales user cannot see requisitions created by others."""
    detail = get_requisition_detail(db_session, test_requisition.id, sales_user.id, "sales")
    assert detail is None


# ── get_team_users ───────────────────────────────────────────────────


def test_detail_with_customer_site(db_session, test_user, test_requisition, test_customer_site):
    """Detail shows customer site display name when linked."""
    test_requisition.customer_site_id = test_customer_site.id
    db_session.commit()

    detail = get_requisition_detail(db_session, test_requisition.id, test_user.id, "buyer")
    assert detail is not None
    assert "Acme Electronics" in detail["req"]["customer_display"]
    assert "Acme HQ" in detail["req"]["customer_display"]


# ── Filter variations ────────────────────────────────────────────────


def test_list_filter_by_owner(db_session, test_user, sales_user):
    """Owner filter restricts to specific user."""
    req_buyer = Requisition(
        name="BUYER-OWN",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    req_sales = Requisition(
        name="SALES-OWN",
        status="active",
        created_by=sales_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([req_buyer, req_sales])
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, owner=test_user.id)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "BUYER-OWN" in names
    assert "SALES-OWN" not in names


def test_list_filter_by_urgency(db_session, test_user):
    """Urgency filter restricts to matching requisitions."""
    req_hot = Requisition(
        name="HOT-REQ",
        status="active",
        urgency="hot",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    req_normal = Requisition(
        name="NORMAL-REQ",
        status="active",
        urgency="normal",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([req_hot, req_normal])
    db_session.commit()

    from app.schemas.requisitions2 import Urgency

    filters = ReqListFilters(status=ReqStatus.active, urgency=Urgency.hot)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "HOT-REQ" in names
    assert "NORMAL-REQ" not in names


def test_list_filter_by_date_from(db_session, test_user):
    """date_from filter excludes older requisitions."""
    from datetime import date as date_type

    req = Requisition(
        name="OLD-REQ",
        status="active",
        created_by=test_user.id,
        created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, date_from=date_type(2025, 1, 1))
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "OLD-REQ" not in names


def test_list_filter_by_date_to(db_session, test_user):
    """date_to filter excludes newer requisitions."""
    from datetime import date as date_type

    req = Requisition(
        name="NEW-REQ",
        status="active",
        created_by=test_user.id,
        created_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
    )
    db_session.add(req)
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, date_to=date_type(2026, 12, 31))
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "NEW-REQ" not in names


def test_list_status_all(db_session, test_user):
    """Status 'all' shows all requisitions regardless of status."""
    req_active = Requisition(
        name="ALL-ACTIVE",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    req_archived = Requisition(
        name="ALL-ARCHIVED",
        status="archived",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([req_active, req_archived])
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.all)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "ALL-ACTIVE" in names
    assert "ALL-ARCHIVED" in names


def test_list_status_archived(db_session, test_user):
    """Status 'archived' shows only archived/won/lost/closed."""
    req_active = Requisition(
        name="ARCH-ACTIVE",
        status="active",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    req_archived = Requisition(
        name="ARCH-ARCHIVED",
        status="archived",
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add_all([req_active, req_archived])
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.archived)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "ARCH-ACTIVE" not in names
    assert "ARCH-ARCHIVED" in names


# ── get_team_users ───────────────────────────────────────────────────


def test_get_team_users(db_session, test_user):
    """get_team_users returns active users."""
    users = get_team_users(db_session)
    assert len(users) >= 1
    assert any(u["id"] == test_user.id for u in users)
