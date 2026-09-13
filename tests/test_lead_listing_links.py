"""Tests for the source-listing click-through on the Sightings-module lead detail.

Pins the regression fix: GET /v2/partials/sourcing/leads/{lead_id} must render an
anchor to the sighting's originating listing. The URLs live in Sighting.raw_data
under click_url / octopart_url / vendor_url; only http(s) values may render, so a
javascript:/data: value is dropped rather than emitted into an href.

The search-module detail (htmx/partials/search/lead_detail.html) already surfaced
these; the sourcing detail did not. Covers: rendered anchors, the no-URL case, and
the hostile-scheme case.

Called by: pytest
Depends on: conftest fixtures (db_session, test_user, test_requisition, client)
"""

import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Requirement, Requisition
from app.models.sourcing import Sighting
from app.models.sourcing_lead import SourcingLead

HX = {"HX-Request": "true"}

VENDOR = "Mouser Electronics"
CLICK_URL = "https://www.mouser.com/ProductDetail/LM317T-listing"
OCTOPART_URL = "https://octopart.com/lm317t-mouser"
VENDOR_URL = "https://www.mouser.com"


def _make_lead(db: Session, requirement: Requirement, vendor_name: str = VENDOR) -> SourcingLead:
    lead = SourcingLead(
        lead_id=str(uuid.uuid4()),
        requirement_id=requirement.id,
        requisition_id=requirement.requisition_id,
        part_number_requested="LM317T",
        part_number_matched="LM317T",
        vendor_name=vendor_name,
        vendor_name_normalized=vendor_name.lower(),
        primary_source_type="api",
        primary_source_name="mouser",
        confidence_score=0.8,
        confidence_band="high",
        buyer_status="new",
        created_at=datetime.now(UTC),
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return lead


def _make_sighting(db: Session, requirement: Requirement, raw_data, vendor_name: str = VENDOR) -> Sighting:
    """A sighting the detail's best_sighting lookup will match — same requirement and
    normalized vendor as the lead."""
    sighting = Sighting(
        requirement_id=requirement.id,
        vendor_name=vendor_name,
        vendor_name_normalized=vendor_name.lower(),
        mpn_matched="LM317T",
        normalized_mpn="LM317T",
        source_type="mouser",
        raw_data=raw_data,
        created_at=datetime.now(UTC),
    )
    db.add(sighting)
    db.commit()
    db.refresh(sighting)
    return sighting


class TestListingLinksRendered:
    def test_click_url_renders_anchor(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """The owner-reported case: a Mouser sighting carrying click_url must give the
        buyer a click-through to the listing."""
        requirement = test_requisition.requirements[0]
        lead = _make_lead(db_session, requirement)
        _make_sighting(db_session, requirement, {"click_url": CLICK_URL})

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)

        assert resp.status_code == 200
        assert f'href="{CLICK_URL}"' in resp.text
        assert "View on Source" in resp.text

    def test_anchor_opens_in_new_tab_safely(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """External listing links must carry target=_blank + rel=noopener, matching the
        search-module detail."""
        requirement = test_requisition.requirements[0]
        lead = _make_lead(db_session, requirement)
        _make_sighting(db_session, requirement, {"click_url": CLICK_URL})

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)

        assert resp.status_code == 200
        anchor_start = resp.text.index(f'href="{CLICK_URL}"')
        anchor = resp.text[anchor_start : resp.text.index("</a>", anchor_start)]
        assert 'target="_blank"' in anchor
        assert 'rel="noopener"' in anchor

    def test_octopart_and_vendor_urls_render(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        requirement = test_requisition.requirements[0]
        lead = _make_lead(db_session, requirement)
        _make_sighting(
            db_session,
            requirement,
            {"click_url": CLICK_URL, "octopart_url": OCTOPART_URL, "vendor_url": VENDOR_URL},
        )

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)

        assert resp.status_code == 200
        assert f'href="{OCTOPART_URL}"' in resp.text
        assert "Octopart" in resp.text
        assert f'href="{VENDOR_URL}"' in resp.text
        assert "Vendor Website" in resp.text


class TestListingLinksAbsent:
    def test_no_urls_renders_no_listing_block(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """Nothing to link to -> no empty links row, no stray labels."""
        requirement = test_requisition.requirements[0]
        lead = _make_lead(db_session, requirement)
        _make_sighting(db_session, requirement, {"description": "IC REG LINEAR"})

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)

        assert resp.status_code == 200
        assert "View on Source" not in resp.text
        assert "Octopart" not in resp.text
        assert "Vendor Website" not in resp.text

    def test_null_raw_data_is_safe(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """raw_data is nullable — the detail must still render, without links."""
        requirement = test_requisition.requirements[0]
        lead = _make_lead(db_session, requirement)
        _make_sighting(db_session, requirement, None)

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)

        assert resp.status_code == 200
        assert "View on Source" not in resp.text

    def test_no_sighting_at_all_is_safe(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """A lead whose sighting has aged out still renders."""
        requirement = test_requisition.requirements[0]
        lead = _make_lead(db_session, requirement)

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)

        assert resp.status_code == 200
        assert "View on Source" not in resp.text


class TestHostileSchemesDropped:
    def test_javascript_scheme_not_rendered(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """A javascript: value must never reach an href."""
        requirement = test_requisition.requirements[0]
        lead = _make_lead(db_session, requirement)
        _make_sighting(db_session, requirement, {"click_url": "javascript:alert(1)"})

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)

        assert resp.status_code == 200
        assert "javascript:alert" not in resp.text
        assert "View on Source" not in resp.text

    def test_non_url_junk_not_rendered(self, client: TestClient, db_session: Session, test_requisition: Requisition):
        """Garbage that is not an absolute http(s) URL renders nothing."""
        requirement = test_requisition.requirements[0]
        lead = _make_lead(db_session, requirement)
        _make_sighting(db_session, requirement, {"click_url": "not a url", "vendor_url": "/relative/path"})

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)

        assert resp.status_code == 200
        assert "not a url" not in resp.text
        assert "/relative/path" not in resp.text
        assert "View on Source" not in resp.text
        assert "Vendor Website" not in resp.text

    def test_good_url_survives_alongside_bad_one(
        self, client: TestClient, db_session: Session, test_requisition: Requisition
    ):
        """One poisoned key must not suppress the healthy sibling links."""
        requirement = test_requisition.requirements[0]
        lead = _make_lead(db_session, requirement)
        _make_sighting(
            db_session,
            requirement,
            {"click_url": "javascript:alert(1)", "octopart_url": OCTOPART_URL},
        )

        resp = client.get(f"/v2/partials/sourcing/leads/{lead.id}", headers=HX)

        assert resp.status_code == 200
        assert "javascript:alert" not in resp.text
        assert f'href="{OCTOPART_URL}"' in resp.text
