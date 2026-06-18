"""P3-1 TDD: cadence call-list — dual clocks, cadence dots, stalest sorts, overdue chip.

Tests are written RED-first (before implementation). Each test documents its expected
behavior and the production code that must be written to make it pass.

Covers:
  - cdm_company_query with outbound_asc/reply_asc sorts (NULLs-first, oldest-first)
  - cdm_list_ctx sets c.cadence_state on each company row
  - cdm_overdue_count / chip predicate uses last_outbound_at (not last_activity_at)
  - Chip "needs_call" filter also queries on last_outbound_at
  - _account_list.html renders cadence dot classes (new/on_target/due/overdue)
  - _account_list.html renders dual-clock lines (Out … / Reply …)
  - list.html sort dropdown contains outbound_asc and reply_asc options
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.auth import User
from app.models.crm import Company
from app.services.crm_service import (
    CADENCE_RED_DAYS,
    cdm_company_query,
    cdm_list_ctx,
    cdm_overdue_count,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def _now():
    return datetime.now(timezone.utc)


def _ago(days: float) -> datetime:
    return _now() - timedelta(days=days)


# ═════════════════════════════════════════════════════════════════════════════
#  UNIT: cdm_company_query — outbound_asc / reply_asc sorts
# ═════════════════════════════════════════════════════════════════════════════


class TestCdmCompanyQueryCadenceSorts:
    """cdm_company_query with the two new cadence sort keys."""

    def test_outbound_asc_nulls_come_first(self, db_session: Session, test_user: User):
        """sort=outbound_asc puts NULL last_outbound_at accounts before touched ones."""
        never = Company(name="Never Outbound", is_active=True, last_outbound_at=None)
        touched = Company(name="Touched Outbound", is_active=True, last_outbound_at=_ago(5))
        db_session.add_all([never, touched])
        db_session.commit()

        rows = cdm_company_query(
            db_session, test_user, search="", staleness="", account_type="", my_only=False, sort="outbound_asc"
        ).all()
        names = [r.name for r in rows]
        assert names.index("Never Outbound") < names.index("Touched Outbound")

    def test_outbound_asc_oldest_outbound_before_recent(self, db_session: Session, test_user: User):
        """sort=outbound_asc puts oldest last_outbound_at before more-recent ones."""
        old = Company(name="Old Outbound", is_active=True, last_outbound_at=_ago(60))
        new_ = Company(name="New Outbound", is_active=True, last_outbound_at=_ago(3))
        db_session.add_all([old, new_])
        db_session.commit()

        rows = cdm_company_query(
            db_session, test_user, search="", staleness="", account_type="", my_only=False, sort="outbound_asc"
        ).all()
        names = [r.name for r in rows]
        assert names.index("Old Outbound") < names.index("New Outbound")

    def test_reply_asc_nulls_come_first(self, db_session: Session, test_user: User):
        """sort=reply_asc puts NULL last_reply_at accounts before replied ones."""
        no_reply = Company(name="No Reply Co", is_active=True, last_reply_at=None)
        replied = Company(name="Has Reply Co", is_active=True, last_reply_at=_ago(5))
        db_session.add_all([no_reply, replied])
        db_session.commit()

        rows = cdm_company_query(
            db_session, test_user, search="", staleness="", account_type="", my_only=False, sort="reply_asc"
        ).all()
        names = [r.name for r in rows]
        assert names.index("No Reply Co") < names.index("Has Reply Co")

    def test_reply_asc_oldest_reply_before_recent(self, db_session: Session, test_user: User):
        """sort=reply_asc puts oldest last_reply_at before more-recent ones."""
        old = Company(name="Old Reply", is_active=True, last_reply_at=_ago(90))
        new_ = Company(name="New Reply", is_active=True, last_reply_at=_ago(2))
        db_session.add_all([old, new_])
        db_session.commit()

        rows = cdm_company_query(
            db_session, test_user, search="", staleness="", account_type="", my_only=False, sort="reply_asc"
        ).all()
        names = [r.name for r in rows]
        assert names.index("Old Reply") < names.index("New Reply")

    def test_unknown_sort_falls_back_to_oldest(self, db_session: Session, test_user: User):
        """An unrecognised sort key falls back to the CDM_SORTS default (oldest-
        first)."""
        never = Company(name="NoActivity", is_active=True, last_activity_at=None)
        recent = Company(name="RecentActivity", is_active=True, last_activity_at=_ago(1))
        db_session.add_all([never, recent])
        db_session.commit()

        rows = cdm_company_query(
            db_session, test_user, search="", staleness="", account_type="", my_only=False, sort="bogus_sort"
        ).all()
        names = [r.name for r in rows]
        assert names.index("NoActivity") < names.index("RecentActivity")


# ═════════════════════════════════════════════════════════════════════════════
#  UNIT: cdm_list_ctx — c.cadence_state is set on every company row
# ═════════════════════════════════════════════════════════════════════════════


class TestCdmListCtxCadenceState:
    """cdm_list_ctx populates c.cadence_state on each returned company."""

    @pytest.mark.parametrize(
        ("tier", "last_outbound_days", "expected_state"),
        [
            pytest.param(None, None, "new", id="never_outbound_is_new"),
            pytest.param("key", 3, "on_target", id="key_3d_on_target"),
            pytest.param("key", 10, "due", id="key_10d_past_7d_target"),
            pytest.param("key", 35, "overdue", id="key_35d_overdue_ceiling"),
            pytest.param("standard", 20, "on_target", id="standard_20d_on_target"),
            pytest.param("standard", 32, "overdue", id="standard_32d_overdue"),
        ],
    )
    def test_cadence_state_set_on_row(
        self,
        db_session: Session,
        test_user: User,
        tier: str | None,
        last_outbound_days: int | None,
        expected_state: str,
    ):
        """cdm_list_ctx sets c.cadence_state from the company's tier + outbound
        clock."""
        last_outbound = None if last_outbound_days is None else _ago(last_outbound_days)
        co = Company(name=f"CadenceTest_{expected_state}", is_active=True, tier=tier, last_outbound_at=last_outbound)
        db_session.add(co)
        db_session.commit()

        ctx = cdm_list_ctx(
            db_session,
            test_user,
            search="",
            staleness="",
            account_type="",
            my_only=False,
            sort="name_asc",
            limit=50,
            offset=0,
        )
        companies = ctx["companies"]
        matching = [c for c in companies if c.name == f"CadenceTest_{expected_state}"]
        assert len(matching) == 1, f"Company CadenceTest_{expected_state} not found in result"
        assert matching[0].cadence_state == expected_state


# ═════════════════════════════════════════════════════════════════════════════
#  UNIT: cdm_overdue_count + chip predicate use last_outbound_at
# ═════════════════════════════════════════════════════════════════════════════


class TestCdmOverdueCountCadencePredicate:
    """The chip count and needs_call filter now use last_outbound_at (30d ceiling)."""

    def test_overdue_count_counts_null_outbound(self, db_session: Session, test_user: User):
        """Accounts with NULL last_outbound_at are counted as overdue (never
        touched)."""
        test_user.role = "sales"
        db_session.flush()
        never = Company(name="NullOutbound Corp", is_active=True, account_owner_id=test_user.id, last_outbound_at=None)
        db_session.add(never)
        db_session.commit()

        assert cdm_overdue_count(db_session, test_user) == 1

    def test_overdue_count_counts_31d_outbound(self, db_session: Session, test_user: User):
        """Accounts with last_outbound_at > 30 days ago are counted as overdue."""
        test_user.role = "sales"
        db_session.flush()
        old = Company(
            name="OldOutbound Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_outbound_at=_ago(CADENCE_RED_DAYS + 1),
        )
        db_session.add(old)
        db_session.commit()

        assert cdm_overdue_count(db_session, test_user) == 1

    def test_overdue_count_excludes_recent_outbound(self, db_session: Session, test_user: User):
        """Accounts with recent last_outbound_at are NOT counted as overdue."""
        test_user.role = "sales"
        db_session.flush()
        recent = Company(
            name="RecentOutbound Corp",
            is_active=True,
            account_owner_id=test_user.id,
            last_outbound_at=_ago(5),
        )
        db_session.add(recent)
        db_session.commit()

        assert cdm_overdue_count(db_session, test_user) == 0

    def test_needs_call_filter_matches_overdue_count(self, db_session: Session, test_user: User):
        """staleness=needs_call list rows match exactly what cdm_overdue_count
        counts."""
        test_user.role = "sales"
        db_session.flush()

        never = Company(name="NullOutboundFilter", is_active=True, account_owner_id=test_user.id, last_outbound_at=None)
        old = Company(
            name="OldOutboundFilter",
            is_active=True,
            account_owner_id=test_user.id,
            last_outbound_at=_ago(CADENCE_RED_DAYS + 2),
        )
        fresh = Company(
            name="FreshOutboundFilter",
            is_active=True,
            account_owner_id=test_user.id,
            last_outbound_at=_ago(5),
        )
        db_session.add_all([never, old, fresh])
        db_session.commit()

        count = cdm_overdue_count(db_session, test_user)
        assert count == 2  # never + old

        filter_rows = cdm_company_query(
            db_session, test_user, search="", staleness="needs_call", account_type="", my_only=True, sort="oldest"
        ).all()
        assert len(filter_rows) == 2
        names = {r.name for r in filter_rows}
        assert "NullOutboundFilter" in names
        assert "OldOutboundFilter" in names
        assert "FreshOutboundFilter" not in names


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTE / TEMPLATE: account-list partial renders cadence dots + dual clocks
# ═════════════════════════════════════════════════════════════════════════════


class TestAccountListCadenceDots:
    """_account_list.html renders cadence dot CSS classes from c.cadence_state."""

    @pytest.mark.parametrize(
        ("tier", "last_outbound_days", "expected_class"),
        [
            pytest.param(None, None, "bg-gray-300", id="new_shows_gray300"),
            pytest.param("standard", 5, "bg-emerald-400", id="on_target_shows_emerald"),
            pytest.param("key", 10, "bg-amber-400", id="due_shows_amber"),
            pytest.param("key", 35, "bg-rose-500", id="overdue_shows_rose"),
        ],
    )
    def test_cadence_dot_class_rendered(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        tier: str | None,
        last_outbound_days: int | None,
        expected_class: str,
    ):
        """Account list row renders the correct cadence dot color class."""
        last_outbound = None if last_outbound_days is None else _ago(last_outbound_days)
        c = Company(
            name=f"DotTest_{expected_class}",
            is_active=True,
            tier=tier,
            last_outbound_at=last_outbound,
        )
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers/account-list")
        assert resp.status_code == 200
        assert expected_class in resp.text


class TestAccountListDualClocks:
    """_account_list.html renders two clock lines per row (Out … / Reply …)."""

    def test_outbound_line_present(self, client: TestClient, db_session: Session, test_user: User):
        """Row contains the outbound clock label 'Out'."""
        c = Company(name="DualClock Co", is_active=True, last_outbound_at=_ago(5))
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers/account-list")
        assert resp.status_code == 200
        assert "Out" in resp.text

    def test_reply_line_present(self, client: TestClient, db_session: Session, test_user: User):
        """Row contains the reply clock label 'Reply'."""
        c = Company(name="ReplyClock Co", is_active=True, last_reply_at=_ago(3))
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers/account-list")
        assert resp.status_code == 200
        assert "Reply" in resp.text

    def test_null_outbound_shows_never(self, client: TestClient, db_session: Session, test_user: User):
        """NULL last_outbound_at renders 'never' (lowercase) in the outbound line."""
        c = Company(name="NoOutbound Co", is_active=True, last_outbound_at=None)
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers/account-list")
        assert resp.status_code == 200
        assert "never" in resp.text.lower()

    def test_null_reply_shows_dash_not_never(self, client: TestClient, db_session: Session, test_user: User):
        """NULL last_reply_at renders a dash/em-dash (NOT the word 'never')."""
        c = Company(name="NoReply Co", is_active=True, last_outbound_at=_ago(5), last_reply_at=None)
        db_session.add(c)
        db_session.commit()

        resp = client.get("/v2/partials/customers/account-list")
        assert resp.status_code == 200
        # NULL reply must NOT say "never" — it means "no reply yet", not "never contacted"
        html = resp.text
        # Find our company's row section and check it contains — (dash) not "never" for reply
        assert "—" in html or "&#8212;" in html or "&mdash;" in html
        # The spec forbids showing "never replied" as the reply label (dash is correct)
        assert "never replied" not in html


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTE / TEMPLATE: sort dropdown has new cadence sort options
# ═════════════════════════════════════════════════════════════════════════════


class TestSortDropdownCadenceOptions:
    """list.html sort dropdown contains outbound_asc and reply_asc options."""

    def test_outbound_asc_option_present(self, client: TestClient):
        """Sort dropdown includes value='outbound_asc'."""
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert 'value="outbound_asc"' in resp.text

    def test_reply_asc_option_present(self, client: TestClient):
        """Sort dropdown includes value='reply_asc'."""
        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert 'value="reply_asc"' in resp.text

    def test_outbound_asc_option_label(self, client: TestClient):
        """The outbound_asc option has a human-readable label."""
        resp = client.get("/v2/partials/customers")
        assert "Stalest outbound" in resp.text or "stalest outbound" in resp.text.lower()

    def test_reply_asc_option_label(self, client: TestClient):
        """The reply_asc option has a human-readable label."""
        resp = client.get("/v2/partials/customers")
        assert "Stalest reply" in resp.text or "stalest reply" in resp.text.lower()

    def test_outbound_asc_sort_works_end_to_end(self, client: TestClient, db_session: Session, test_user: User):
        """GET account-list?sort=outbound_asc returns 200 with correct row order."""
        old = Company(name="OutboundOld Zz", is_active=True, last_outbound_at=_ago(50))
        new_ = Company(name="OutboundNew Aa", is_active=True, last_outbound_at=_ago(2))
        db_session.add_all([old, new_])
        db_session.commit()

        resp = client.get("/v2/partials/customers/account-list?sort=outbound_asc")
        assert resp.status_code == 200
        html = resp.text
        assert html.index("OutboundOld Zz") < html.index("OutboundNew Aa")

    def test_reply_asc_sort_works_end_to_end(self, client: TestClient, db_session: Session, test_user: User):
        """GET account-list?sort=reply_asc returns 200 with correct row order."""
        old = Company(name="ReplyOld Zz", is_active=True, last_reply_at=_ago(80))
        new_ = Company(name="ReplyNew Aa", is_active=True, last_reply_at=_ago(1))
        db_session.add_all([old, new_])
        db_session.commit()

        resp = client.get("/v2/partials/customers/account-list?sort=reply_asc")
        assert resp.status_code == 200
        html = resp.text
        assert html.index("ReplyOld Zz") < html.index("ReplyNew Aa")


# ═════════════════════════════════════════════════════════════════════════════
#  ROUTE / TEMPLATE: chip count and filter use outbound clock
# ═════════════════════════════════════════════════════════════════════════════


class TestChipUsesOutboundClock:
    """The overdue chip count + click-through filter are driven by last_outbound_at."""

    def test_chip_shows_for_null_outbound_owned_account(self, client: TestClient, db_session: Session, test_user: User):
        """Chip appears when user owns an account with NULL last_outbound_at."""
        test_user.role = "sales"
        db_session.flush()
        co = Company(name="NoOutboundOwned Co", is_active=True, account_owner_id=test_user.id, last_outbound_at=None)
        db_session.add(co)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        assert "need" in resp.text.lower() and "call" in resp.text.lower()

    def test_chip_hidden_when_outbound_recent(self, client: TestClient, db_session: Session, test_user: User):
        """Chip button not rendered when all owned accounts have recent last_outbound_at
        (<30d).

        The hidden <option value='needs_call'> always contains the text, so we check for
        the chip *button* (bg-rose-50 rose badge) not appearing, not the text itself.
        """
        test_user.role = "sales"
        db_session.flush()
        co = Company(
            name="FreshOutboundOwned Co",
            is_active=True,
            account_owner_id=test_user.id,
            last_outbound_at=_ago(5),
        )
        db_session.add(co)
        db_session.commit()

        resp = client.get("/v2/partials/customers")
        assert resp.status_code == 200
        # The chip button only renders when overdue_count > 0.
        # When it does NOT render, the rose badge button is absent.
        assert "bg-rose-50 text-rose-700" not in resp.text
