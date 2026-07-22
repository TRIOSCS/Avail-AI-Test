"""test_activity_write_path.py — Tests for the unified activity write path.

Covers the ActivityType enum, log_activity() canonical writer, the
log_rfq_activity() delegating alias, requisition_id on email/call logging,
and get_requisition_activities().

Called by: pytest
Depends on: app/constants.py, app/services/activity_service.py, conftest.py
"""

from datetime import UTC

import pytest

from app.constants import ActivityType
from app.models import ActivityLog
from app.services.activity_service import (
    get_requisition_activities,
    log_activity,
    log_call_activity,
    log_email_activity,
    log_rfq_activity,
)
from app.shared_constants import RFQ_SUBJECT_TAG_RE


def test_activity_type_values_fit_column():
    """Every canonical activity_type value fits the activity_log.activity_type
    String(20) column."""
    for member in ActivityType:
        assert len(member.value) <= 20, f"{member.value} exceeds 20 chars"


def test_activity_type_has_expected_members():
    assert ActivityType.RFQ_SENT == "rfq_sent"
    assert ActivityType.STATUS_CHANGED == "status_changed"
    assert ActivityType.OFFER_STATUS_CHANGED == "offer_status_changed"


def test_log_activity_sets_requisition_id(db_session, test_requisition, test_user):
    record = log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        channel="system",
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="Status changed from active to sourcing",
    )
    assert record.id is not None
    assert record.requisition_id == test_requisition.id
    assert record.activity_type == "status_changed"
    assert record.channel == "system"
    assert record.notes == "Status changed from active to sourcing"


def test_log_rfq_activity_delegates_to_log_activity(db_session, test_requisition, test_user):
    record = log_rfq_activity(
        db=db_session,
        rfq_id=test_requisition.id,
        activity_type="status_change",
        description="legacy call path",
        user_id=test_user.id,
    )
    assert record.requisition_id == test_requisition.id
    assert record.notes == "legacy call path"
    rows = db_session.query(ActivityLog).filter_by(requisition_id=test_requisition.id).all()
    assert len(rows) == 1


def test_log_email_activity_accepts_requisition_id(db_session, test_requisition, test_user):
    record = log_email_activity(
        user_id=test_user.id,
        direction="sent",
        email_addr="vendor@realvendorparts.com",
        subject=f"RFQ [ref:{test_requisition.id}]",
        external_id="msg-req-001",
        contact_name="Vendor Rep",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.requisition_id == test_requisition.id


def test_log_call_activity_accepts_requisition_id(db_session, test_requisition, test_user):
    record = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15551234567",
        duration_seconds=120,
        external_id="call-req-001",
        contact_name="Vendor Rep",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.requisition_id == test_requisition.id


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        pytest.param("Quote request RE part [ref:4321]", "4321", id="ref_format"),
        pytest.param("Quote request [AVAIL-99]", "99", id="legacy_format"),
    ],
)
def test_avail_tag_re_matches(subject, expected):
    """The sent-folder scan must recognise the [ref:N] and legacy [AVAIL-N] tags."""
    assert RFQ_SUBJECT_TAG_RE.search(subject).group(1) == expected


def test_log_email_activity_accepts_none_user(db_session, test_requisition):
    """log_email_activity tolerates user_id=None (userless inbox scan)."""
    record = log_email_activity(
        user_id=None,
        direction="received",
        email_addr="vendor@realvendorparts.com",
        subject="RE: RFQ",
        external_id="msg-none-user-001",
        contact_name="Vendor Rep",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.user_id is None
    assert record.requisition_id == test_requisition.id


def test_log_email_activity_skips_unmatched_own_domain_sender(db_session, test_requisition):
    """ISS-030 write-time filter: an own-domain counterparty that doesn't independently
    resolve via match_email_to_entity is pure internal noise — log_email_activity must
    skip writing the row (return None) instead of logging it."""
    record = log_email_activity(
        user_id=None,
        direction="received",
        email_addr="colleague@trioscs.com",
        subject="RE: internal FYI",
        external_id="msg-own-domain-001",
        contact_name="Colleague",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is None
    assert db_session.query(ActivityLog).filter_by(external_id="msg-own-domain-001").first() is None


def test_log_email_activity_skips_unmatched_junk_prefix(db_session, test_requisition):
    """A junk local-part (e.g. mailer-daemon@) with no entity match is skipped too."""
    record = log_email_activity(
        user_id=None,
        direction="received",
        email_addr="mailer-daemon@some-mail-relay.example",
        subject="Undeliverable",
        external_id="msg-junk-prefix-001",
        contact_name=None,
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is None


def test_log_email_activity_still_logs_own_domain_when_matched(db_session, test_requisition, test_company):
    """An own-domain/junk address that DOES independently resolve (e.g. a customer
    contact whose email happens to collide with a junk prefix or own domain in a test
    fixture) is still logged — the match itself is authoritative, not the domain
    heuristic."""
    from app.models import SiteContact
    from app.models.crm import CustomerSite

    site = CustomerSite(company_id=test_company.id, site_name="HQ", is_active=True)
    db_session.add(site)
    db_session.flush()
    contact = SiteContact(customer_site_id=site.id, email="postmaster@trioscs.com", full_name="Ops Contact")
    db_session.add(contact)
    db_session.commit()

    record = log_email_activity(
        user_id=None,
        direction="received",
        email_addr="postmaster@trioscs.com",
        subject="RE: matched despite junk prefix",
        external_id="msg-matched-junk-001",
        contact_name="Ops Contact",
        db=db_session,
        requisition_id=test_requisition.id,
    )
    assert record is not None
    assert record.company_id == test_company.id


def test_get_requisition_activities_returns_scoped_rows(db_session, test_requisition, test_user):
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="first",
    )
    log_activity(
        db_session,
        activity_type=ActivityType.RFQ_SENT,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="second",
    )
    rows = get_requisition_activities(test_requisition.id, db_session)
    assert len(rows) == 2
    assert all(r.requisition_id == test_requisition.id for r in rows)


def test_get_requisition_activities_excludes_other_reqs(db_session, test_requisition, test_user):
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="mine",
    )
    assert get_requisition_activities(999999, db_session) == []


def test_activity_tab_renders_logged_event(client, db_session, test_requisition, test_user):
    """An event written via log_activity() appears on the requisition Activity tab."""
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="Status changed from active to sourcing",
    )
    db_session.commit()
    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/activity")
    assert resp.status_code == 200
    assert "Status changed from active to sourcing" in resp.text
    assert "No activity recorded yet" not in resp.text


def test_log_activity_resolves_company_from_requisition(db_session, test_user, test_company, test_customer_site):
    """log_activity() backfills company_id by walking requisition -> site -> company."""
    from app.models.sourcing import Requisition

    req = Requisition(
        name="REQ-COMPANY-RESOLVE",
        customer_name="Acme Electronics",
        status="open",
        created_by=test_user.id,
        customer_site_id=test_customer_site.id,
    )
    db_session.add(req)
    db_session.flush()

    record = log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=req.id,
        user_id=test_user.id,
        description="resolves company",
    )
    assert record.company_id == test_company.id


def test_log_activity_explicit_company_id_skips_resolution(db_session, test_user, test_company, test_customer_site):
    """An explicitly passed company_id is used as-is, not overwritten by resolution."""
    from app.models.sourcing import Requisition

    req = Requisition(
        name="REQ-EXPLICIT-COMPANY",
        customer_name="Acme Electronics",
        status="open",
        created_by=test_user.id,
        customer_site_id=test_customer_site.id,
    )
    db_session.add(req)
    db_session.flush()

    record = log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=req.id,
        company_id=test_company.id,
        user_id=test_user.id,
        description="explicit company",
    )
    assert record.company_id == test_company.id


def test_log_rfq_activity_maps_metadata_to_details(db_session, test_requisition, test_user):
    """The alias maps its `metadata` arg onto the `details` column and forces
    channel=system."""
    record = log_rfq_activity(
        db=db_session,
        rfq_id=test_requisition.id,
        activity_type="status_change",
        description="with metadata",
        metadata={"old": "active", "new": "sourcing"},
        user_id=test_user.id,
    )
    assert record.details == {"old": "active", "new": "sourcing"}
    assert record.channel == "system"


def _make_inbox_message(req_id):
    """Build a single fake Graph inbox message tagged for the given requisition."""
    return {
        "id": "poll-inbox-msg-001",
        "subject": f"RE: Quote request [AVAIL-{req_id}]",
        "from": {
            "emailAddress": {
                "address": "vendor@parts.com",
                "name": "Vendor Rep",
            }
        },
        "bodyPreview": "Quote attached",
        "body": {"content": "<p>Quote attached</p>"},
        "conversationId": "poll-inbox-conv-001",
        "receivedDateTime": None,
    }


async def test_poll_inbox_logs_email_received_activity(db_session, test_requisition, test_user):
    """Driving the real poll_inbox with one inbound vendor reply writes exactly one
    email_received ActivityLog row for the matched requisition."""
    from unittest.mock import AsyncMock, patch

    from app.email_service import poll_inbox

    mock_gc = AsyncMock()
    mock_gc.get_json.return_value = {"value": [_make_inbox_message(test_requisition.id)]}

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.email_service.get_credential_cached", return_value=None),
    ):
        await poll_inbox(
            token="fake-token",
            db=db_session,
            requisition_id=test_requisition.id,
        )

    rows = (
        db_session.query(ActivityLog)
        .filter(
            ActivityLog.requisition_id == test_requisition.id,
            ActivityLog.activity_type == "email_received",
        )
        .all()
    )
    assert len(rows) == 1


async def test_poll_inbox_skips_logging_auto_reply(db_session, test_requisition, test_user):
    """ISS-030: an inbound OOO/auto-reply is not genuine correspondence — poll_inbox must
    not write an ActivityLog row for it (the VendorResponse row is still recorded for the
    RFQ classifier)."""
    from unittest.mock import AsyncMock, patch

    from app.email_service import poll_inbox
    from app.models.offers import VendorResponse

    msg = _make_inbox_message(test_requisition.id)
    msg["id"] = "poll-inbox-ooo-001"
    msg["bodyPreview"] = "I am currently out of the office and will return next week."
    msg["body"] = {"content": "<p>I am currently out of the office and will return next week.</p>"}

    mock_gc = AsyncMock()
    mock_gc.get_json.return_value = {"value": [msg]}

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.email_service.get_credential_cached", return_value=None),
    ):
        await poll_inbox(token="fake-token", db=db_session, requisition_id=test_requisition.id)

    rows = db_session.query(ActivityLog).filter(ActivityLog.external_id == "poll-inbox-ooo-001").all()
    assert rows == [], "Auto-reply must not be logged as ActivityLog"
    # The raw VendorResponse row is still recorded for the classifier.
    assert db_session.query(VendorResponse).filter_by(message_id="poll-inbox-ooo-001").first() is not None


async def test_poll_inbox_skips_exchange_ndr_sender(db_session, test_requisition, test_user):
    """A Microsoft Exchange NDR/bounce mailbox (variable hex suffix, own domain) is
    treated as noise entirely — no VendorResponse and no ActivityLog row."""
    from unittest.mock import AsyncMock, patch

    from app.email_service import poll_inbox
    from app.models.offers import VendorResponse

    msg = _make_inbox_message(test_requisition.id)
    msg["id"] = "poll-inbox-ndr-001"
    msg["from"] = {
        "emailAddress": {
            "address": "MicrosoftExchange329e71ec88ae4615bbc36ab6ce41109e@trioscs.com",
            "name": "Microsoft Outlook",
        }
    }
    msg["subject"] = "Undeliverable: RE: Quote request"

    mock_gc = AsyncMock()
    mock_gc.get_json.return_value = {"value": [msg]}

    with (
        patch("app.utils.graph_client.GraphClient", return_value=mock_gc),
        patch("app.email_service.get_credential_cached", return_value=None),
    ):
        await poll_inbox(token="fake-token", db=db_session, requisition_id=test_requisition.id)

    assert db_session.query(ActivityLog).filter_by(external_id="poll-inbox-ndr-001").first() is None
    assert db_session.query(VendorResponse).filter_by(message_id="poll-inbox-ndr-001").first() is None


@pytest.mark.parametrize(
    ("activity_type", "description", "expected_meaningful"),
    [
        pytest.param(
            ActivityType.STATUS_CHANGED,
            "status changed",
            True,
            id="meaningful_type_flagged_true",
        ),
        pytest.param(
            ActivityType.SIGHTING_ADDED,
            "12 sightings added",
            None,
            id="ai_scored_type_left_none",
        ),
    ],
)
def test_log_activity_is_meaningful_flag(
    db_session, test_requisition, test_user, activity_type, description, expected_meaningful
):
    """Inherently-meaningful types are flagged True at write time; AI-scored types
    (sighting_added) are left None for the quality pass."""
    rec = log_activity(
        db_session,
        activity_type=activity_type,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description=description,
    )
    assert rec.is_meaningful is expected_meaningful


def test_get_requisition_activities_meaningful_only_filter(db_session, test_requisition, test_user):
    """meaningful_only hides is_meaningful=False rows but keeps True and None
    (unscored)."""
    log_activity(
        db_session,
        activity_type=ActivityType.STATUS_CHANGED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="meaningful",
    )
    unscored = log_activity(
        db_session,
        activity_type=ActivityType.SIGHTING_ADDED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="unscored",
    )
    noise = log_activity(
        db_session,
        activity_type=ActivityType.SIGHTING_ADDED,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="noise",
    )
    noise.is_meaningful = False
    db_session.flush()

    curated = get_requisition_activities(test_requisition.id, db_session, meaningful_only=True)
    assert noise.id not in {r.id for r in curated}
    assert unscored.id in {r.id for r in curated}
    all_rows = get_requisition_activities(test_requisition.id, db_session, meaningful_only=False)
    assert noise.id in {r.id for r in all_rows}


def test_log_call_activity_writes_canonical_call_logged(db_session, test_user):
    """Phone calls are logged as the canonical call_logged type; direction holds
    in/out."""
    rec = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15551234567",
        duration_seconds=120,
        external_id="call-canon-001",
        contact_name="Vendor Rep",
        db=db_session,
    )
    assert rec is not None
    assert rec.activity_type == ActivityType.CALL_LOGGED
    assert rec.direction == "outbound"


@pytest.mark.parametrize(
    ("force_meaningful", "duration_seconds", "expected"),
    [
        pytest.param(True, None, True, id="force_true_no_duration"),
        pytest.param(None, None, False, id="auto_gate_no_duration"),
        pytest.param(None, 5, True, id="auto_gate_above_threshold"),
    ],
)
def test_log_call_activity_force_meaningful(db_session, test_user, force_meaningful, duration_seconds, expected):
    """force_meaningful overrides the duration gate; None falls through to the existing
    auto-capture logic (unchanged)."""
    from app.services.activity_service import CALL_MEANINGFUL_MIN_SECONDS

    # Ensure the parametrized "above threshold" case is genuinely above it.
    if duration_seconds is not None and expected is True:
        duration_seconds = CALL_MEANINGFUL_MIN_SECONDS

    rec = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15550000001",
        duration_seconds=duration_seconds,
        external_id=f"force-meaningful-{force_meaningful}-{duration_seconds}",
        contact_name="Test Vendor",
        db=db_session,
        force_meaningful=force_meaningful,
    )
    assert rec is not None
    assert rec.is_meaningful is expected


def test_log_company_call_manual_is_meaningful(db_session, test_user, test_company):
    """Manual company-call log (force_meaningful=True) is is_meaningful even with no
    duration — mirrors the requisition log_call_activity fix (T3 / Fix C).

    The auto-capture path (force_meaningful=None with real duration) is unchanged.
    """
    from app.services.activity_service import log_company_call

    # Manual log — no duration, but must be meaningful
    rec = log_company_call(
        user_id=test_user.id,
        company_id=test_company.id,
        direction="outbound",
        phone="+15550000099",
        duration_seconds=None,
        contact_name="Test Contact",
        notes="Called to follow up on quote",
        db=db_session,
        force_meaningful=True,
    )
    assert rec.is_meaningful is True

    # Auto-capture path unchanged: no force_meaningful, no duration → not meaningful
    rec2 = log_company_call(
        user_id=test_user.id,
        company_id=test_company.id,
        direction="inbound",
        phone="+15550000088",
        duration_seconds=None,
        contact_name=None,
        notes=None,
        db=db_session,
    )
    assert rec2.is_meaningful is False


def test_activity_tab_renders_rfq_sent_event(client, db_session, test_requisition, test_user):
    """An RFQ-sent event written via log_activity() appears on the Activity tab.

    Regression guard: the activity tab must render from the `activities` list
    alone, with no dependency on the removed `contacts` query.
    """
    log_activity(
        db_session,
        activity_type=ActivityType.RFQ_SENT,
        requisition_id=test_requisition.id,
        user_id=test_user.id,
        description="RFQ sent to Acme",
    )
    db_session.commit()
    resp = client.get(f"/v2/partials/requisitions/{test_requisition.id}/tab/activity")
    assert resp.status_code == 200
    assert "RFQ sent to Acme" in resp.text


# ── Part B tests: log_call_activity occurred_at + details (WS1) ─────────────


def test_log_call_activity_occurred_at_stamped(db_session, test_user):
    """occurred_at param is written to the row when provided."""
    from datetime import datetime

    when = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)
    rec = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15550001001",
        duration_seconds=60,
        external_id="oat-001",
        contact_name="CDR Rep",
        db=db_session,
        occurred_at=when,
    )
    assert rec is not None
    assert rec.occurred_at == when


def test_log_call_activity_details_connected_is_meaningful(db_session, test_user):
    """details.call_outcome=connected → is_meaningful True (outcome gate beats
    duration)."""
    from app.constants import CallOutcome

    rec = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15550001002",
        duration_seconds=0,
        external_id="outcome-connected-001",
        contact_name="CDR Rep",
        db=db_session,
        details={"call_outcome": CallOutcome.CONNECTED.value, "source": "8x8_cdr"},
    )
    assert rec is not None
    assert rec.is_meaningful is True


def test_log_call_activity_details_left_message_is_meaningful(db_session, test_user):
    """details.call_outcome=left_message → is_meaningful True.

    LEFT_MESSAGE is in MEANINGFUL_CALL_OUTCOMES: leaving a voicemail is a meaningful
    outreach touch. This was previously only meaningful in the call-outcome router;
    now it is consistent across both write paths.
    """
    from app.constants import CallOutcome

    rec = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15550001007",
        duration_seconds=0,
        external_id="outcome-leftmsg-001",
        contact_name="VM Rep",
        db=db_session,
        details={"call_outcome": CallOutcome.LEFT_MESSAGE.value, "source": "manual"},
    )
    assert rec is not None
    assert rec.is_meaningful is True, "LEFT_MESSAGE must be meaningful in log_call_activity"


def test_log_call_activity_details_no_answer_not_meaningful_even_long(db_session, test_user):
    """details.call_outcome=no_answer → is_meaningful False even if duration >= 30s."""
    from app.constants import CallOutcome
    from app.services.activity_service import CALL_MEANINGFUL_MIN_SECONDS

    rec = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15550001003",
        duration_seconds=CALL_MEANINGFUL_MIN_SECONDS + 60,
        external_id="outcome-noanswer-001",
        contact_name="CDR Rep",
        db=db_session,
        details={"call_outcome": CallOutcome.NO_ANSWER.value, "source": "8x8_cdr"},
    )
    assert rec is not None
    assert rec.is_meaningful is False


def test_log_call_activity_no_details_uses_duration_gate(db_session, test_user):
    """Without details, duration gate unchanged: short call → not meaningful."""
    from app.services.activity_service import CALL_MEANINGFUL_MIN_SECONDS

    short = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15550001004",
        duration_seconds=CALL_MEANINGFUL_MIN_SECONDS - 1,
        external_id="duration-short-001",
        contact_name="CDR Rep",
        db=db_session,
    )
    assert short is not None
    assert short.is_meaningful is False

    long = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15550001005",
        duration_seconds=CALL_MEANINGFUL_MIN_SECONDS,
        external_id="duration-long-001",
        contact_name="CDR Rep",
        db=db_session,
    )
    assert long is not None
    assert long.is_meaningful is True


def test_log_call_activity_details_stored_on_row(db_session, test_user):
    """Details dict is written to the ActivityLog.details column."""
    rec = log_call_activity(
        user_id=test_user.id,
        direction="outbound",
        phone="+15550001006",
        duration_seconds=60,
        external_id="details-stored-001",
        contact_name="CDR Rep",
        db=db_session,
        details={"call_outcome": "connected", "department": "Sales", "source": "8x8_cdr"},
    )
    assert rec is not None
    assert rec.details["source"] == "8x8_cdr"
    assert rec.details["department"] == "Sales"


# ── Part B tests: cadence bump uses occurred_at ───────────────────────────────


def test_cadence_bump_uses_occurred_at_when_set(db_session):
    """bump_clocks_from_activity uses occurred_at (true call time) over created_at."""
    from datetime import datetime, timedelta

    from app.constants import ActivityType, Channel, Direction
    from app.models.crm import Company
    from app.models.intelligence import ActivityLog
    from app.services.cadence_service import bump_clocks_from_activity

    real_call_time = datetime(2026, 5, 1, 9, 0, 0, tzinfo=UTC)
    insert_time = real_call_time + timedelta(minutes=29)

    co = Company(name="Occurred Co")
    db_session.add(co)
    db_session.flush()

    act = ActivityLog(
        activity_type=ActivityType.CALL_LOGGED,
        channel=Channel.PHONE,
        company_id=co.id,
        direction=Direction.OUTBOUND,
        is_meaningful=True,
        occurred_at=real_call_time,
        created_at=insert_time,
    )
    db_session.add(act)
    db_session.flush()

    bump_clocks_from_activity(db_session, act)
    db_session.refresh(co)
    # Clock must reflect the real call time, not the late insert time
    assert co.last_outbound_at == real_call_time


def test_cadence_bump_falls_back_to_created_at_when_occurred_at_none(db_session):
    """bump_clocks_from_activity falls back to created_at when occurred_at is None."""
    from datetime import datetime

    from app.constants import ActivityType, Channel, Direction
    from app.models.crm import Company
    from app.models.intelligence import ActivityLog
    from app.services.cadence_service import bump_clocks_from_activity

    insert_time = datetime(2026, 5, 1, 10, 30, 0, tzinfo=UTC)

    co = Company(name="Fallback Co")
    db_session.add(co)
    db_session.flush()

    act = ActivityLog(
        activity_type=ActivityType.CALL_LOGGED,
        channel=Channel.PHONE,
        company_id=co.id,
        direction=Direction.OUTBOUND,
        is_meaningful=True,
        occurred_at=None,
        created_at=insert_time,
    )
    db_session.add(act)
    db_session.flush()

    bump_clocks_from_activity(db_session, act)
    db_session.refresh(co)
    assert co.last_outbound_at == insert_time
