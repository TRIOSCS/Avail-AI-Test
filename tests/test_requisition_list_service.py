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
    _build_row_mpn_chips,
    _hours_until_bid_due,
    _resolve_deal_value,
    get_requisition_detail,
    get_row_context,
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


# ── _build_row_mpn_chips ─────────────────────────────────────────────


class _FakeReq:
    def __init__(self, primary_mpn=None, substitutes=None):
        self.primary_mpn = primary_mpn
        self.substitutes = substitutes or []


def test_build_row_mpn_chips_orders_primaries_before_subs():
    reqs = [
        _FakeReq(primary_mpn="LM317", substitutes=[{"mpn": "LM337", "manufacturer": "TI"}]),
        _FakeReq(primary_mpn="NE555", substitutes=["LMC555"]),
    ]
    items = _build_row_mpn_chips(reqs)
    roles = [it["role"] for it in items]
    first_sub = roles.index("sub")
    assert all(r == "primary" for r in roles[:first_sub])
    assert all(r == "sub" for r in roles[first_sub:])
    assert [it["mpn"] for it in items] == ["LM317", "NE555", "LM337", "LMC555"]


def test_build_row_mpn_chips_dedupes_keeping_primary_role():
    reqs = [
        _FakeReq(primary_mpn="LM317"),
        _FakeReq(primary_mpn="NE555", substitutes=[{"mpn": "LM317", "manufacturer": "TI"}]),
    ]
    items = _build_row_mpn_chips(reqs)
    mpns = [it["mpn"] for it in items]
    assert mpns.count("LM317") == 1
    lm317 = next(it for it in items if it["mpn"] == "LM317")
    assert lm317["role"] == "primary"


def test_build_row_mpn_chips_empty_when_no_requirements():
    assert _build_row_mpn_chips([]) == []


def test_build_row_mpn_chips_ignores_empty_primary():
    reqs = [_FakeReq(primary_mpn="", substitutes=["SUB1"])]
    items = _build_row_mpn_chips(reqs)
    assert items == [{"mpn": "SUB1", "role": "sub"}]


def test_build_row_mpn_chips_dedupes_repeated_subs():
    reqs = [
        _FakeReq(primary_mpn="A", substitutes=["X", "Y"]),
        _FakeReq(primary_mpn="B", substitutes=["X", "Z"]),
    ]
    items = _build_row_mpn_chips(reqs)
    assert [it["mpn"] for it in items] == ["A", "B", "X", "Y", "Z"]


# ── list_requisitions aggregation additions ──────────────────────────


def test_list_row_exposes_deal_value_and_coverage(db_session, test_user, test_requisition):
    """New row keys for the v2 row template must be present and typed."""
    filters = ReqListFilters(status=ReqStatus.all)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    req = result["requisitions"][0]

    assert "hours_until_bid_due" in req
    assert "deal_value_display" in req
    assert "deal_value_source" in req
    assert req["deal_value_source"] in {"entered", "computed", "partial", "none"}
    assert "deal_value_priced_count" in req
    assert isinstance(req["deal_value_priced_count"], int)
    assert "deal_value_requirement_count" in req
    assert isinstance(req["deal_value_requirement_count"], int)
    assert "coverage_filled" in req
    assert isinstance(req["coverage_filled"], int)
    assert "coverage_total" in req
    assert isinstance(req["coverage_total"], int)
    assert req["coverage_filled"] <= req["coverage_total"]
    assert "mpn_chip_items" in req
    assert isinstance(req["mpn_chip_items"], list)


def test_list_row_coverage_counts_requirements_with_offers(db_session, test_user, test_requisition):
    """coverage_filled == count of requirements with >=1 offer (not sightings)."""
    filters = ReqListFilters(status=ReqStatus.all)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    req = result["requisitions"][0]
    assert req["coverage_filled"] == 0


def test_get_row_context_exposes_v2_fields(db_session, test_user, test_requisition):
    """Regression guard: get_row_context() must mirror list_requisitions() row-dict
    shape so _single_row.html's v2 branch renders correctly after inline edits.

    Without every v2 field, the v2 template access paths (req.mpn_chip_items,
    req.hours_until_bid_due, req.coverage_*, req.deal_value_*) resolve to
    Undefined, which silently corrupts the rendered row on every inline save.
    """
    ctx = get_row_context(db_session, test_requisition, test_user)
    req = ctx["req"]

    # Legacy fields — pre-existing contract
    assert "id" in req and "name" in req and "status" in req
    assert "customer_display" in req
    assert "requirement_count" in req
    assert "offer_count" in req
    assert "urgency" in req

    # v2 fields — must be present for v2 template rendering to work.
    for key in (
        "hours_until_bid_due",
        "deal_value_display",
        "deal_value_source",
        "deal_value_priced_count",
        "deal_value_requirement_count",
        "coverage_filled",
        "coverage_total",
        "mpn_chip_items",
        "match_reason",
        "matched_mpn",
    ):
        assert key in req, f"get_row_context() missing v2 field: {key}"

    # Types match list_requisitions() output
    assert req["deal_value_source"] in {"entered", "computed", "partial", "none"}
    assert isinstance(req["deal_value_priced_count"], int)
    assert isinstance(req["deal_value_requirement_count"], int)
    assert isinstance(req["coverage_filled"], int)
    assert isinstance(req["coverage_total"], int)
    assert isinstance(req["mpn_chip_items"], list)
    assert req["coverage_filled"] <= req["coverage_total"]
