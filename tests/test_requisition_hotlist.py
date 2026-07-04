"""Hot List option on the New Requisition modal (feature B).

A requisition can be created as a monitored **Hot List** (RequisitionStatus.HOTLIST)
instead of an active sourcing deal. The create path must:
  * honour the ``hotlist`` form flag → status HOTLIST (else OPEN);
  * populate ``Requisition.company_id`` from the chosen site (the Proactive matcher
    joins Company on ``Requisition.company_id`` — an unset company_id → zero matches);
  * let the requisitions list filter on ``?status=hotlist`` and render a "Hot List" pill.

Called by: pytest.
Depends on: app.routers.htmx.requisitions (import-save + list), app.services.proactive_matching,
    tests/conftest.py fixtures.
"""

from datetime import datetime, timezone
from decimal import Decimal

from app.constants import RequisitionStatus
from app.models import Company, CustomerSite, Offer, ProactiveMatch, Requirement, Requisition, User


def _make_site(db, *, owner: bool = True) -> CustomerSite:
    """An active CustomerSite whose company is (optionally) owned by a salesperson."""
    account_owner_id = None
    if owner:
        u = User(
            email=f"owner-{datetime.now(timezone.utc).timestamp()}@trioscs.com",
            name="Account Owner",
            role="sales",
            azure_id=f"azure-owner-{datetime.now(timezone.utc).timestamp()}",
            created_at=datetime.now(timezone.utc),
        )
        db.add(u)
        db.flush()
        account_owner_id = u.id
    co = Company(name="Hotlist Co", is_active=True, account_owner_id=account_owner_id)
    db.add(co)
    db.flush()
    site = CustomerSite(company_id=co.id, site_name="Hotlist HQ", is_active=True)
    db.add(site)
    db.commit()
    db.refresh(site)
    return site


def _import_save(client, *, name, site_id="", hotlist=None, mpn="LM317T"):
    """POST the unified modal's import-save with one part; returns the response."""
    data = {
        "name": name,
        "customer_name": "",
        "customer_site_id": str(site_id) if site_id else "",
        "deadline": "",
        "urgency": "normal",
        "reqs[0].primary_mpn": mpn,
        "reqs[0].manufacturer": "Texas Instruments",
        "reqs[0].target_qty": "500",
        "reqs[0].condition": "new",
    }
    if hotlist is not None:
        data["hotlist"] = hotlist
    return client.post("/v2/partials/requisitions/import-save", data=data)


def _fetch_req(db, name: str) -> Requisition:
    db.expire_all()
    return db.query(Requisition).filter_by(name=name).one()


# ── status + company_id on create ───────────────────────────────────────


def test_import_save_hotlist_sets_status_and_company(client, db_session):
    """Hotlist=true → status HOTLIST and company_id copied from the chosen site."""
    site = _make_site(db_session)
    resp = _import_save(client, name="Watch BOM", site_id=site.id, hotlist="true")
    assert resp.status_code == 200

    req = _fetch_req(db_session, "Watch BOM")
    assert req.status == RequisitionStatus.HOTLIST
    assert req.company_id == site.company_id


def test_import_save_default_is_open_with_company(client, db_session):
    """Hotlist absent → status OPEN, but company_id is still populated from the site."""
    site = _make_site(db_session)
    resp = _import_save(client, name="Active BOM", site_id=site.id)
    assert resp.status_code == 200

    req = _fetch_req(db_session, "Active BOM")
    assert req.status == RequisitionStatus.OPEN
    assert req.company_id == site.company_id


def test_import_save_hotlist_false_is_open(client, db_session):
    """An explicit falsey hotlist value creates an OPEN sourcing deal."""
    site = _make_site(db_session)
    resp = _import_save(client, name="Active BOM 2", site_id=site.id, hotlist="false")
    assert resp.status_code == 200

    req = _fetch_req(db_session, "Active BOM 2")
    assert req.status == RequisitionStatus.OPEN
    assert req.company_id == site.company_id


def test_import_save_no_site_leaves_company_null(client, db_session):
    """No customer site → company_id stays None (guarded) and create still succeeds."""
    resp = _import_save(client, name="No Site BOM", hotlist="true")
    assert resp.status_code == 200

    req = _fetch_req(db_session, "No Site BOM")
    assert req.status == RequisitionStatus.HOTLIST
    assert req.company_id is None


# ── proactive matcher integration ───────────────────────────────────────


def test_hotlist_req_seeds_proactive_match(client, db_session):
    """A HOTLIST req created via import-save surfaces a Proactive match on a matching
    offer.

    End-to-end for feature B: company_id populated on create is exactly what the
    hotlist join in proactive_matching needs — without it the matcher returns nothing.
    """
    from app.services.proactive_matching import find_matches_for_offer

    site = _make_site(db_session, owner=True)
    resp = _import_save(client, name="Proactive Watch", site_id=site.id, hotlist="true", mpn="HOTMPN123")
    assert resp.status_code == 200

    req = _fetch_req(db_session, "Proactive Watch")
    assert req.status == RequisitionStatus.HOTLIST
    assert req.company_id == site.company_id

    # The requirement resolved a material card on create; an offer for that card should match.
    requirement = db_session.query(Requirement).filter_by(requisition_id=req.id).one()
    assert requirement.material_card_id is not None

    offer = Offer(
        material_card_id=requirement.material_card_id,
        vendor_name="Arrow",
        mpn="HOTMPN123",
        unit_price=Decimal("10"),
        status="active",
    )
    db_session.add(offer)
    db_session.commit()

    matches = find_matches_for_offer(offer.id, db_session)
    assert any(m.requisition_id == req.id and m.company_id == site.company_id for m in matches)

    db_session.commit()
    seeded = db_session.query(ProactiveMatch).filter_by(requisition_id=req.id).all()
    assert len(seeded) == 1
    assert seeded[0].material_card_id == requirement.material_card_id


# ── list filter + pill ──────────────────────────────────────────────────


def test_list_status_hotlist_filters_and_renders_pill(client, db_session):
    """?status=hotlist returns only HOTLIST reqs; the "Hot List" pill renders."""
    site = _make_site(db_session)
    _import_save(client, name="Hotlist Only", site_id=site.id, hotlist="true", mpn="AAA111")
    _import_save(client, name="Open Only", site_id=site.id, mpn="BBB222")

    resp = client.get("/v2/partials/requisitions", params={"status": "hotlist"})
    assert resp.status_code == 200
    assert "Hotlist Only" in resp.text
    assert "Open Only" not in resp.text
    # The additive filter pill is present in the list chrome.
    assert "Hot List" in resp.text
