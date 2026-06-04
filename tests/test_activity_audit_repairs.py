"""Regression tests for the activity-tracking audit repairs (2026-06-04).

Each test pins one confirmed finding from docs/ACTIVITY_AUDIT.md and was written
test-first (RED) before the corresponding fix. Covers:
  #1  vendor display_name on the requisition activity timeline
  #2  ActivityType column-width contract + strategic-vendor-expiring canonicalization
  #3  log-activity form swap target (no self-nesting duplicate id)
  #5  raw JSON `details` dict rendered as text on the shared timeline
  #6  N+1 relationship loads on the account timeline
  #13 sightings status-change canonical activity_type

Depends on: tests/conftest.py fixtures (client, db_session, test_user, test_company,
            test_requisition, test_vendor_card).
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.constants import ActivityType, SourcingStatus
from app.models import ActivityLog

UTC_NOW = datetime.now(timezone.utc)


# ── #1 — vendor display_name on the requisition activity tab ──────────────
def test_req_activity_tab_renders_vendor_display_name(
    client, db_session, test_requisition, test_vendor_card, test_user
):
    """A vendor-linked requisition activity shows the vendor's display_name.

    The template previously read a.vendor_card.name (nonexistent) → blank label.
    """
    db_session.add(
        ActivityLog(
            user_id=test_user.id,
            activity_type="rfq_sent",
            channel="email",
            requisition_id=test_requisition.id,
            vendor_card_id=test_vendor_card.id,
            summary="RFQ sent to vendor",
            occurred_at=UTC_NOW,
        )
    )
    db_session.commit()

    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/activity?show_all=1")
    assert resp.status_code == 200
    assert test_vendor_card.display_name in resp.text  # "Arrow Electronics"


# ── #2 — ActivityType column-width contract + canonical strategic type ────
def test_all_activity_types_fit_column_width():
    """Every ActivityType value fits activity_log.activity_type String(20).

    A >20-char value silently truncates on Postgres and rolls back the batch.
    """
    for at in ActivityType:
        assert len(at.value) <= 20, f"{at.name}={at.value!r} exceeds String(20)"


def test_strategic_vendor_expiring_is_canonical_activity_type():
    """The strategic-vendor-expiring nudge uses a canonical, column-fitting type."""
    assert hasattr(ActivityType, "STRATEGIC_VENDOR_EXPIRING")
    assert len(ActivityType.STRATEGIC_VENDOR_EXPIRING.value) <= 20


# ── #3 — log-activity form swap target (no self-nesting duplicate id) ─────
def test_req_log_activity_form_targets_tab_body_not_itself(client, test_requisition):
    """The log-activity form must replace the wrapping tab body (#tab-content), not
    innerHTML-swap a full partial into its own #activity-tab-content (which nests a
    duplicate id and re-fires the paid AI digest on every submit)."""
    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/activity")
    assert resp.status_code == 200
    body = resp.text
    idx = body.find("log-activity")
    assert idx != -1
    window = body[idx : idx + 220]
    assert 'hx-target="#tab-content"' in window
    assert 'hx-target="#activity-tab-content"' not in window


# ── #5 — raw JSON `details` dict rendered as text on the shared timeline ──
def test_shared_timeline_does_not_render_raw_json_details():
    """activity_timeline.html must not stringify the JSON `details` column."""
    from app.template_env import templates

    tmpl = templates.env.get_template("htmx/partials/shared/activity_timeline.html")
    act = SimpleNamespace(
        user_id=1,
        user=None,
        notes=None,
        summary=None,
        details={"phase": "rfq", "vendor": "Arrow"},
        activity_type="rfq_sent",
        contact_name=None,
        created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        channel="email",
    )
    html = tmpl.render(activities=[act])
    assert "{'phase'" not in html
    assert "'vendor': 'Arrow'" not in html
    # Falls back to a human-readable label.
    assert "Rfq sent" in html


# ── #6 — N+1 relationship loads on the account timeline ───────────────────
def test_account_timeline_eager_loads_timeline_relationships(db_session, test_user, test_company):
    """The account timeline must eager-load the relationships its serializer reads
    (user/company/vendor_card), so rendering N rows does not fire O(N) lazy loads.

    We assert the relationships come back already loaded. expire_all() drops the in-
    memory state so the helper's query is what (re)loads them; without selectinload they
    stay unloaded (and would lazy-load per row at render time).
    """
    from sqlalchemy import inspect as sa_inspect

    from app.models import VendorCard
    from app.services.activity_service import get_account_timeline

    company_id = test_company.id
    for i in range(3):
        vc = VendorCard(normalized_name=f"n1plus-vendor-{i}", display_name=f"Vendor {i}")
        db_session.add(vc)
        db_session.flush()
        db_session.add(
            ActivityLog(
                user_id=test_user.id,
                activity_type="call_logged",
                channel="phone",
                company_id=company_id,
                vendor_card_id=vc.id,
                direction="outbound",
                event_type="call",
                created_at=UTC_NOW - timedelta(minutes=i),
            )
        )
    db_session.commit()
    db_session.expire_all()

    items, total = get_account_timeline(db_session, company_id, limit=50)
    assert total == 3
    assert len(items) == 3
    for a in items:
        unloaded = sa_inspect(a).unloaded
        assert "user" not in unloaded, "user not eager-loaded — N+1 risk"
        assert "company" not in unloaded, "company not eager-loaded — N+1 risk"
        assert "vendor_card" not in unloaded, "vendor_card not eager-loaded — N+1 risk"


# ── #13 — sightings status-change canonical activity_type ─────────────────
def test_sightings_batch_status_writes_canonical_status_changed(client, db_session, test_requisition):
    """Batch status change records ActivityType.STATUS_CHANGED ('status_changed'), not
    the legacy 'status_change', so the rule-meaningful filter recognizes it."""
    requirement = test_requisition.requirements[0]

    resp = client.post(
        "/v2/partials/sightings/batch-status",
        data={
            "requirement_ids": json.dumps([requirement.id]),
            "status": SourcingStatus.SOURCING.value,
        },
    )
    assert resp.status_code == 200

    row = (
        db_session.query(ActivityLog)
        .filter(ActivityLog.requirement_id == requirement.id)
        .order_by(ActivityLog.id.desc())
        .first()
    )
    assert row is not None
    assert row.activity_type == ActivityType.STATUS_CHANGED.value
