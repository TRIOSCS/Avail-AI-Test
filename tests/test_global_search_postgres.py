"""test_global_search_postgres.py — PostgreSQL-only: fast_search orders each group by
pg_trgm similarity (QC-mediums item 40, pg-search-no-pg-tests).

fast_search gates nine ``ORDER BY similarity(...) DESC`` sites on ``_is_postgres`` —
none of them execute on the SQLite suite, so a regression there (dropped ordering, a
similarity() argument swap, a broken ``greatest()``) is invisible to the main suite.
Seeds two ILIKE-matching rows per representative group where trigram similarity
clearly separates them (exact-ish short value vs. long padded value) and asserts the
closer match ranks first. Runs only against a real Postgres (``PG_TEST_DSN`` set).

Called by: pytest (dedicated CI "postgres-paths" job, ``-m requires_postgres``)
Depends on: app.services.global_search_service.fast_search, tests.conftest
            (pg_session, requires_postgres — pg_engine installs pg_trgm)
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import User
from app.models.crm import Company
from app.models.intelligence import MaterialCard
from app.models.sourcing import Requirement, Requisition
from app.models.vendors import VendorCard
from app.services.global_search_service import fast_search
from tests.conftest import requires_postgres


def _seed_user(pg_session: Session) -> User:
    user = User(
        email="pgsearch@trioscs.com",
        name="PG Search",
        role="buyer",
        azure_id="pg-azure-search-001",
        m365_connected=True,
        created_at=datetime.now(UTC),
    )
    pg_session.add(user)
    pg_session.flush()
    return user


@requires_postgres
class TestFastSearchTrgmOrdering:
    """Each group: two rows both ILIKE-match the query; trgm ranks the closer first."""

    def test_requisitions_ordered_by_greatest_name_customer_similarity(self, pg_session: Session):
        user = _seed_user(pg_session)
        close = Requisition(name="Acme Server", status="open", customer_name="Zenith Corp", created_by=user.id)
        far = Requisition(
            name="Acme Server Refresh Program Q3 2026", status="open", customer_name="Zenith Corp", created_by=user.id
        )
        pg_session.add_all([far, close])  # insert far first so ordering isn't insertion order
        pg_session.flush()

        result = fast_search("acme server", pg_session)
        names = [r["name"] for r in result["groups"]["requisitions"]]
        assert names == ["Acme Server", "Acme Server Refresh Program Q3 2026"]

    def test_companies_ordered_by_name_similarity(self, pg_session: Session):
        pg_session.add_all(
            [
                Company(name="Globex International Manufacturing Holdings", domain="globex-intl.example"),
                Company(name="Globex", domain="globex.example"),
            ]
        )
        pg_session.flush()

        result = fast_search("globex", pg_session)
        names = [c["name"] for c in result["groups"]["companies"]]
        assert names == ["Globex", "Globex International Manufacturing Holdings"]

    def test_vendors_ordered_by_display_name_similarity(self, pg_session: Session):
        pg_session.add_all(
            [
                VendorCard(
                    normalized_name="arrow electronics global components division",
                    display_name="Arrow Electronics Global Components Division",
                ),
                VendorCard(normalized_name="arrow", display_name="Arrow"),
            ]
        )
        pg_session.flush()

        result = fast_search("arrow", pg_session)
        names = [v["display_name"] for v in result["groups"]["vendors"]]
        assert names == ["Arrow", "Arrow Electronics Global Components Division"]

    def test_parts_ordered_by_primary_mpn_similarity(self, pg_session: Session):
        user = _seed_user(pg_session)
        req = Requisition(name="PG trgm parts", status="open", customer_name="Acme", created_by=user.id)
        pg_session.add(req)
        pg_session.flush()
        pg_session.add_all(
            [
                Requirement(
                    requisition_id=req.id,
                    primary_mpn="LM317T-EXTENDED-REEL7",
                    normalized_mpn="lm317textendedreel7",
                    target_qty=10,
                    sourcing_status="open",
                ),
                Requirement(
                    requisition_id=req.id,
                    primary_mpn="LM317T",
                    normalized_mpn="lm317t",
                    target_qty=10,
                    sourcing_status="open",
                ),
            ]
        )
        pg_session.flush()

        result = fast_search("lm317t", pg_session)
        mpns = [p["primary_mpn"] for p in result["groups"]["parts"]]
        assert mpns == ["LM317T", "LM317T-EXTENDED-REEL7"]

    def test_material_cards_ordered_by_display_mpn_similarity(self, pg_session: Session):
        pg_session.add_all(
            [
                MaterialCard(
                    normalized_mpn="ssd870evo4tbretailbox",
                    display_mpn="SSD870EVO-4TB-RETAIL-BOX",
                    created_at=datetime.now(UTC),
                ),
                MaterialCard(normalized_mpn="ssd870evo", display_mpn="SSD870EVO", created_at=datetime.now(UTC)),
            ]
        )
        pg_session.flush()

        result = fast_search("ssd870evo", pg_session)
        mpns = [m["display_mpn"] for m in result["groups"]["material_cards"]]
        assert mpns == ["SSD870EVO", "SSD870EVO-4TB-RETAIL-BOX"]

    def test_both_rows_still_ilike_match(self, pg_session: Session):
        """Guard the premise: ordering asserts above only mean something if BOTH rows
        pass the ILIKE filter — a filter regression that dropped the long row would
        make the ordering asserts vacuously green."""
        pg_session.add_all(
            [
                Company(name="Globex International Manufacturing Holdings", domain="g1.example"),
                Company(name="Globex", domain="g2.example"),
            ]
        )
        pg_session.flush()

        result = fast_search("globex", pg_session)
        assert len(result["groups"]["companies"]) == 2
