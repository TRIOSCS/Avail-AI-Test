"""Tests for the consolidated nav-badge endpoint + service (spec §5.5).

GET /v2/partials/nav/badges replaces the six per-badge pollers: it returns every
{key}-nav-badge pill as an hx-swap-oob="innerHTML" span in ONE response. Covers:
all six spans always present (empty at 0 so stale pills clear), per-source counts
landing in the right span, the proactive flag gate, the module_access_map gate
(hidden modules skip their count queries), the amber follow-ups pill, fail-quiet
behavior, and the mobile_nav template's single visibility-filtered poller.

Called by: pytest
Depends on: tests/conftest.py (client, db_session, test_user, test_requisition,
            test_offer)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from app.constants import ContactStatus
from app.models import ProactiveMatch
from app.models.offers import Contact as RfqContact
from app.services.nav_badges import NAV_BADGE_KEYS, collect_badge_counts

_NAV_TEMPLATE = Path(__file__).resolve().parent.parent / "app/templates/htmx/partials/shared/mobile_nav.html"


def _span_inner(text: str, key: str) -> str:
    """Inner HTML of the OOB span for *key* (up to the first close tag — enough to see
    the pill's count or emptiness)."""
    m = re.search(rf'<span id="{key}-nav-badge" hx-swap-oob="innerHTML">(.*?)</span>', text)
    assert m is not None, f"OOB span for {key} missing in response: {text!r}"
    return m.group(1)


def _stale_contact(db, user, requisition, days: int = 30) -> RfqContact:
    c = RfqContact(
        requisition_id=requisition.id,
        user_id=user.id,
        contact_type="email",
        vendor_name="Arrow",
        vendor_contact="sales@arrow.com",
        status=ContactStatus.SENT.value,
        created_at=datetime.now(UTC) - timedelta(days=days),
    )
    db.add(c)
    db.commit()
    return c


# ── endpoint ───────────────────────────────────────────────────────────────


def test_all_six_spans_present_and_empty_when_no_counts(client):
    r = client.get("/v2/partials/nav/badges")
    assert r.status_code == 200
    for key in NAV_BADGE_KEYS:
        assert _span_inner(r.text, key) == ""


def test_count_lands_in_its_own_span_only(client):
    with patch("app.routers.alerts.collect_badge_counts", return_value={"requisitions": 3}):
        r = client.get("/v2/partials/nav/badges")
    assert r.status_code == 200
    assert "3" in _span_inner(r.text, "requisitions")
    for key in NAV_BADGE_KEYS:
        if key != "requisitions":
            assert _span_inner(r.text, key) == ""


def test_proactive_new_match_renders_emerald_pill(client, db_session, test_user, test_offer):
    db_session.add(ProactiveMatch(offer_id=test_offer.id, mpn="LM317T", salesperson_id=test_user.id, status="new"))
    db_session.commit()
    r = client.get("/v2/partials/nav/badges")
    inner = _span_inner(r.text, "proactive")
    assert "1" in inner
    assert "bg-emerald-500" in inner


def test_proactive_flag_off_zeroes_count(client, db_session, test_user, test_offer):
    """Wave-2 park switch: flag off → proactive span empty, others unaffected."""
    db_session.add(ProactiveMatch(offer_id=test_offer.id, mpn="LM317T", salesperson_id=test_user.id, status="new"))
    db_session.commit()
    with patch("app.services.nav_badges.get_effective_flag", return_value=False):
        r = client.get("/v2/partials/nav/badges")
    assert _span_inner(r.text, "proactive") == ""


def test_proactive_access_revoked_zeroes_count(client, db_session, test_user, test_offer):
    db_session.add(ProactiveMatch(offer_id=test_offer.id, mpn="LM317T", salesperson_id=test_user.id, status="new"))
    test_user.access_overrides = {"proactive": False}
    db_session.commit()
    r = client.get("/v2/partials/nav/badges")
    assert _span_inner(r.text, "proactive") == ""


def test_follow_up_stale_contact_renders_amber_pill(client, db_session, test_user, test_requisition):
    _stale_contact(db_session, test_user, test_requisition)
    r = client.get("/v2/partials/nav/badges")
    inner = _span_inner(r.text, "follow-ups")
    assert "1" in inner
    assert "bg-amber-500" in inner


def test_collection_failure_is_quiet(client):
    """Service blows up → 200 with all six spans empty, never a 500."""
    with patch("app.routers.alerts.collect_badge_counts", side_effect=RuntimeError("boom")):
        r = client.get("/v2/partials/nav/badges")
    assert r.status_code == 200
    for key in NAV_BADGE_KEYS:
        assert _span_inner(r.text, key) == ""


# ── service ────────────────────────────────────────────────────────────────


def test_collect_badge_counts_returns_every_key(db_session, test_user):
    counts = collect_badge_counts(db_session, test_user)
    assert set(counts) == set(NAV_BADGE_KEYS)
    assert all(isinstance(v, int) for v in counts.values())


def test_collect_badge_counts_fail_quiet_per_source(db_session, test_user):
    """A raising source contributes 0; the other keys still resolve."""
    with patch("app.services.nav_badges.count_for_tab", side_effect=RuntimeError("tab boom")):
        counts = collect_badge_counts(db_session, test_user)
    assert counts["requisitions"] == 0
    assert counts["my-day"] == 0
    assert set(counts) == set(NAV_BADGE_KEYS)


def test_badge_key_registry_colors():
    """One registry drives both span order and pill color: amber follow-ups only."""
    assert NAV_BADGE_KEYS["follow-ups"] == "amber"
    assert all(color == "emerald" for key, color in NAV_BADGE_KEYS.items() if key != "follow-ups")


def test_hidden_module_contributes_zero_without_queries(db_session, test_user):
    """A module the nav gate hides never pays its count queries (buy-plans is the
    expensive one) — but its key still resolves to 0 so the span clears."""
    seen_tabs: list[str] = []

    def spy(db, user, tab):
        seen_tabs.append(tab)
        return 5

    with (
        patch("app.services.nav_badges.count_for_tab", side_effect=spy),
        patch("app.routers.admin.users.module_access_map", return_value={"buy-plans": False}),
    ):
        counts = collect_badge_counts(db_session, test_user)
    assert counts["buy-plans"] == 0
    assert "buy-plans" not in seen_tabs
    assert "requisitions" in seen_tabs  # unhidden tabs still counted
    assert set(counts) == set(NAV_BADGE_KEYS)


def test_follow_ups_gated_by_requisitions_access(db_session, test_user):
    """The amber pill hangs on the Sales Hub item, so requisitions access gates it."""
    with (
        patch("app.routers.admin.users.module_access_map", return_value={"requisitions": False}),
        patch("app.services.nav_badges._follow_up_count") as follow_up,
    ):
        counts = collect_badge_counts(db_session, test_user)
    assert counts["follow-ups"] == 0
    assert counts["requisitions"] == 0
    assert not follow_up.called


# ── nav template: ONE poller, no per-badge pollers ─────────────────────────


class TestMobileNavSinglePoller:
    def _nav(self) -> str:
        return _NAV_TEMPLATE.read_text(encoding="utf-8")

    def test_single_poller_mounted(self):
        nav = self._nav()
        assert nav.count('hx-get="/v2/partials/nav/badges"') == 1
        poller = nav[nav.index('id="nav-badge-poller"') :]
        # The visibility filter skips the 60s poll in hidden tabs. It must never
        # grow nested [..] — nested brackets kill the htmx trigger parse.
        assert "hx-trigger=\"load, every 60s [document.visibilityState==='visible']\"" in poller
        assert 'hx-swap="none"' in poller
        assert 'hx-push-url="false"' in poller

    def test_old_per_badge_pollers_gone(self):
        nav = self._nav()
        assert "/v2/partials/proactive/badge" not in nav
        assert "/v2/partials/follow-ups/badge" not in nav
        assert "/v2/partials/alerts/" not in nav

    def test_static_badge_targets_remain(self):
        nav = self._nav()
        assert 'id="follow-ups-nav-badge"' in nav
        # proactive + the four alert tabs render {{ id }}-nav-badge via the loop:
        assert 'id="{{ id }}-nav-badge"' in nav
        assert "'proactive', 'requisitions', 'buy-plans', 'crm', 'my-day'" in nav
