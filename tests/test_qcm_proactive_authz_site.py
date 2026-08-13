"""test_qcm_proactive_authz_site.py — proactive outbound authz + single-site guard.

Two 2026-08-08 QC audit findings:
- Mutating proactive routes gated on require_user, not PROACTIVE access — a user
  with proactive revoked could still run the whole outbound flow.
- send_proactive_offer never asserted all selected matches share one customer
  site, so a mixed match_ids selection leaked one customer's parts/prices to
  another (built the body from all matches, sent under matches[0]'s site).

Called by: pytest
Depends on: conftest fixtures (client, db_session, test_user, test_offer,
    test_customer_site, test_requisition)
"""

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.constants import AccessKey
from app.models import User
from app.models.crm import CustomerSite
from app.models.intelligence import ProactiveMatch

# ── Authz: revoked PROACTIVE blocks a mutating route ──────────────────────


def test_revoked_proactive_blocks_mutating_route(client: TestClient, db_session: Session, test_user: User):
    """A user with PROACTIVE explicitly denied gets 403 on a mutating route that
    previously only required a logged-in user."""
    test_user.access_overrides = {AccessKey.PROACTIVE.value: False}
    db_session.commit()
    resp = client.post(
        "/v2/partials/proactive/draft",
        data={"match_ids": "1"},
        headers={"HX-Request": "true"},
    )
    assert resp.status_code == 403


# ── Confidentiality: mixed-site selection is rejected pre-send ─────────────


@pytest.mark.anyio
async def test_send_proactive_offer_rejects_mixed_sites(
    db_session: Session, test_user, test_offer, test_customer_site, test_requisition
):
    from app.services.proactive_service import send_proactive_offer

    site_b = CustomerSite(company_id=test_customer_site.company_id, site_name="Other Site", is_active=True)
    db_session.add(site_b)
    db_session.commit()

    def _match(site_id):
        m = ProactiveMatch(
            offer_id=test_offer.id,
            requisition_id=test_requisition.id,
            customer_site_id=site_id,
            salesperson_id=test_user.id,
            mpn="LM317T",
            status="new",
            created_at=datetime.now(UTC),
        )
        db_session.add(m)
        db_session.commit()
        db_session.refresh(m)
        return m

    m1 = _match(test_customer_site.id)
    m2 = _match(site_b.id)

    with pytest.raises(ValueError, match="single customer site"):
        await send_proactive_offer(db_session, test_user, "tok", [m1.id, m2.id], [], {})


# ── Scorecard accuracy + resilience ───────────────────────────────────────


def test_scorecard_breakdown_excludes_draft_and_failed(db_session: Session, test_user, test_customer_site):
    """The per-rep breakdown must exclude DRAFT + FAILED, matching the headline."""
    from app.constants import ProactiveOfferStatus
    from app.models.intelligence import ProactiveOffer
    from app.services.proactive_service import get_scorecard

    for st in (ProactiveOfferStatus.SENT, ProactiveOfferStatus.DRAFT, ProactiveOfferStatus.FAILED):
        db_session.add(
            ProactiveOffer(
                customer_site_id=test_customer_site.id, salesperson_id=test_user.id, status=st, line_items=[]
            )
        )
    db_session.commit()

    result = get_scorecard(db_session, None)  # admin/all view builds the breakdown
    mine = [b for b in result["breakdown"] if b["salesperson_id"] == test_user.id]
    assert mine and mine[0]["sent"] == 1  # only the SENT one counts


def test_scorecard_route_survives_service_error(client: TestClient, monkeypatch):
    """A get_scorecard error renders a zeroed fallback (200), never a 500."""
    from app.services import proactive_service

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(proactive_service, "get_scorecard", _boom)
    resp = client.get("/v2/partials/proactive/scorecard", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    # The zeroed fallback still renders the real scorecard panel (not a 500/error),
    # and its keys match the template so the values render as 0, not blanks.
    assert "Proactive Scorecard" in resp.text
    assert "Gross Profit" in resp.text
