"""Tests for the requisition list service layer.

Covers: list with filters, search, pagination, sort, role-based access,
detail modal data.

Called by: pytest
Depends on: app/services/requisition_list_service.py, conftest fixtures
"""

from datetime import datetime, timezone

import pytest

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


def _make_req(db_session, name, created_by, *, status="active", created_at=None, **extra):
    """Build, persist, and return a Requisition with sensible test defaults."""
    req = Requisition(
        name=name,
        status=status,
        created_by=created_by,
        created_at=created_at or datetime.now(timezone.utc),
        **extra,
    )
    db_session.add(req)
    return req


# ── helpers: _hours_until_bid_due / _resolve_deal_value ──────────────


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        # Literal "ASAP" and other non-ISO strings must degrade to None,
        # not raise, so the urgency accent just doesn't render.
        pytest.param("ASAP", id="literal_asap"),
        pytest.param("next week", id="prose"),
    ],
)
def test_hours_until_bid_due_returns_none(value):
    assert _hours_until_bid_due(value) is None


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


@pytest.mark.parametrize(
    ("opportunity_value", "priced_sum", "priced_count", "requirement_count", "expected_val", "expected_src"),
    [
        pytest.param(50000.0, 10.0, 1, 5, 50000.0, "entered", id="prefers_entered"),
        pytest.param(None, 2500.0, 3, 3, 2500.0, "computed", id="all_priced_is_computed"),
        pytest.param(None, 1800.0, 3, 5, 1800.0, "partial", id="some_priced_is_partial"),
        pytest.param(None, 1500.0, 4, 4, 1500.0, "computed", id="zero_price_counts_as_priced"),
        pytest.param(None, 0.0, 0, 3, None, "none", id="none_priced_is_none"),
        pytest.param(0.0, 1500.0, 2, 2, 1500.0, "computed", id="zero_opportunity_falls_through"),
    ],
)
def test_resolve_deal_value(opportunity_value, priced_sum, priced_count, requirement_count, expected_val, expected_src):
    val, src = _resolve_deal_value(
        opportunity_value=opportunity_value,
        priced_sum=priced_sum,
        priced_count=priced_count,
        requirement_count=requirement_count,
    )
    assert val == expected_val
    assert src == expected_src


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
        _make_req(db_session, f"PAGE-REQ-{i}", test_user.id)
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, per_page=2)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    assert result["pagination"].total == 3
    assert result["pagination"].total_pages == 2
    assert len(result["requisitions"]) == 2  # page 1 has 2 items


def test_list_pagination_page_2(db_session, test_user):
    """Page 2 returns remaining items."""
    for i in range(3):
        _make_req(db_session, f"PAGE-REQ-{i}", test_user.id)
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, per_page=2, page=2)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    assert len(result["requisitions"]) == 1  # page 2 has 1 item


def test_list_sort_ascending(db_session, test_user):
    """Sort ascending by name orders A before Z."""
    _make_req(db_session, "AAA-REQ", test_user.id)
    _make_req(db_session, "ZZZ-REQ", test_user.id)
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, sort=SortColumn.name, order=SortOrder.asc)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert names[0] == "AAA-REQ"
    assert names[-1] == "ZZZ-REQ"


def test_list_sort_descending(db_session, test_user):
    """Sort descending by name orders Z before A."""
    _make_req(db_session, "AAA-REQ", test_user.id)
    _make_req(db_session, "ZZZ-REQ", test_user.id)
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, sort=SortColumn.name, order=SortOrder.desc)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert names[0] == "ZZZ-REQ"
    assert names[-1] == "AAA-REQ"


def test_sales_role_filtering(db_session, test_user, sales_user):
    """Sales role only sees own requisitions."""
    _make_req(db_session, "BUYER-REQ", test_user.id)
    _make_req(db_session, "SALES-REQ", sales_user.id)
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
    _make_req(db_session, "BUYER-OWN", test_user.id)
    _make_req(db_session, "SALES-OWN", sales_user.id)
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, owner=test_user.id)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "BUYER-OWN" in names
    assert "SALES-OWN" not in names


def test_list_filter_by_urgency(db_session, test_user):
    """Urgency filter restricts to matching requisitions."""
    _make_req(db_session, "HOT-REQ", test_user.id, urgency="hot")
    _make_req(db_session, "NORMAL-REQ", test_user.id, urgency="normal")
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

    _make_req(db_session, "OLD-REQ", test_user.id, created_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, date_from=date_type(2025, 1, 1))
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "OLD-REQ" not in names


def test_list_filter_by_date_to(db_session, test_user):
    """date_to filter excludes newer requisitions."""
    from datetime import date as date_type

    _make_req(db_session, "NEW-REQ", test_user.id, created_at=datetime(2099, 1, 1, tzinfo=timezone.utc))
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.active, date_to=date_type(2026, 12, 31))
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "NEW-REQ" not in names


def test_list_status_all(db_session, test_user):
    """Status 'all' shows all requisitions regardless of status."""
    _make_req(db_session, "ALL-ACTIVE", test_user.id)
    _make_req(db_session, "ALL-ARCHIVED", test_user.id, status="archived")
    db_session.commit()

    filters = ReqListFilters(status=ReqStatus.all)
    result = list_requisitions(db_session, filters, test_user.id, "buyer")
    names = [r["name"] for r in result["requisitions"]]
    assert "ALL-ACTIVE" in names
    assert "ALL-ARCHIVED" in names


def test_list_status_archived(db_session, test_user):
    """Status 'archived' shows only archived/won/lost/closed."""
    _make_req(db_session, "ARCH-ACTIVE", test_user.id)
    _make_req(db_session, "ARCH-ARCHIVED", test_user.id, status="archived")
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
