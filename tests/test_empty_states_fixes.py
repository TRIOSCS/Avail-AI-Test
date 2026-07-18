"""Render tests for the UX-audit empty-state / alarming-display fixes.

Each test renders the real Jinja partial through the app's configured template
environment (so filters + globals + macros resolve exactly as in production) and
asserts the corrected empty/edge state.

Called by: pytest.
Depends on: app.template_env.templates.env, the shared empty_state.html partial and
    the sightings / approvals / tasks / parts / search / prospecting partials.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.template_env import templates

ENV = templates.env


def _render(name: str, **ctx) -> str:
    return ENV.get_template(name).render(**ctx)


# ── Fix 1: Sightings filtered-to-empty → real "Clear Filters" action ──────────


def test_empty_state_partial_supports_action_target():
    """The shared partial renders a working hx-get action pointed at a sub-container."""
    html = _render(
        "htmx/partials/shared/empty_state.html",
        message="No requirements match your filters",
        action_url="/v2/partials/sightings",
        action_label="Clear Filters",
        action_hx_target="#sightings-table",
    )
    assert "Clear Filters" in html
    assert 'hx-get="/v2/partials/sightings"' in html
    assert 'hx-target="#sightings-table"' in html


def test_empty_state_partial_defaults_target_to_main_content():
    """Existing callers (no action_hx_target) keep targeting #main-content."""
    html = _render(
        "htmx/partials/shared/empty_state.html",
        message="No accounts found",
        action_url="/v2/requisitions/new",
        action_label="Create one",
    )
    assert 'hx-target="#main-content"' in html


def test_sightings_empty_renders_clear_filters_button():
    html = _render(
        "htmx/partials/sightings/table.html",
        requirements=[],
        groups=None,
        dashboard_counters={},
        stat_counts={},
        total=0,
        status="offered",
        q="widget",
        group_by="",
        manufacturer="",
    )
    assert "Clear Filters" in html
    # A real action, not a dead label: the button re-fetches the table unfiltered.
    assert 'hx-get="/v2/partials/sightings"' in html
    assert 'hx-target="#sightings-table"' in html


# ── Fix 3: Tasks / My Day zero-tasks ──────────────────────────────────────────


def _tasks_ctx(**over):
    ctx = {
        "tasks": [],
        "now_utc": datetime.now(UTC),
        "filter_status": "",
        "filter_priority": "",
        "filter_due": "",
    }
    ctx.update(over)
    return ctx


def test_tasks_unfiltered_zero_state_shows_new_task_cta():
    html = _render("htmx/partials/tasks/_results.html", **_tasks_ctx())
    assert "No tasks yet" in html
    assert "New task" in html
    # The quiet "no match" line must NOT appear when nothing is filtered.
    assert "No tasks match these filters." not in html
    # The CTA is real: it reveals a form that posts to the create route.
    assert 'hx-post="/v2/partials/my-day/tasks"' in html


def test_tasks_filtered_zero_state_keeps_no_match_message():
    html = _render("htmx/partials/tasks/_results.html", **_tasks_ctx(filter_status="done"))
    assert "No tasks match these filters." in html
    assert "New task" not in html


# ── Fix 4: Parts Offers empty state ───────────────────────────────────────────


def test_parts_offers_empty_state_has_icon_and_hint():
    html = _render(
        "htmx/partials/parts/tabs/offers.html",
        offers=[],
        requirement=SimpleNamespace(primary_mpn="ABC123"),
        vendor_tier_map={},
    )
    assert "<svg" in html
    assert "Offers arrive from vendor replies." in html
    # The old bare dashed one-liner is gone.
    assert "border-dashed" not in html


# ── Fix 5: Search "All" tab View-all overflow ─────────────────────────────────


def _search_results(item_count):
    reqs = [{"id": i, "name": f"REQ-{i}", "customer_name": "Acme", "status": "open"} for i in range(item_count)]
    return {
        "best_match": None,
        "total_count": item_count,
        "groups": {"requisitions": reqs},
    }


def test_search_all_tab_shows_view_all_when_capped():
    html = _render(
        "htmx/partials/search/full_results.html",
        results=_search_results(11),
        query="acme",
        ai_search=False,
    )
    assert "View all 11 Requisitions" in html
    assert "tab = 'requisitions'" in html


def test_search_all_tab_no_view_all_when_within_cap():
    html = _render(
        "htmx/partials/search/full_results.html",
        results=_search_results(10),
        query="acme",
        ai_search=False,
    )
    assert "View all" not in html


# ── Fix 6: Prospecting un-scored placeholder (no red 0% bar) ───────────────────


def _prospect(**over):
    base = {
        "id": 7,
        "name": "Widgets Inc",
        "domain": "widgets.example",
        "status": "suggested",
        "fit_score": None,
        "readiness_score": None,
        "enrichment_data": {},
        "industry": None,
        "region": None,
        "discovery_source": "Manual add",
        "claimed_by_user": None,
        "dismissed_at": None,
        "fit_reasoning": None,
        "created_at": None,
        "company_id": None,
        "ai_writeup": None,
        "description": None,
        "employee_count_range": None,
        "revenue_range": None,
        "hq_location": None,
        "naics_code": None,
    }
    base.update(over)
    return SimpleNamespace(**base)


def test_prospect_card_unscored_shows_placeholder_not_red_bar():
    html = _render(
        "htmx/partials/prospecting/_card.html",
        prospect=_prospect(),
        snapshots={},
        contact_stats_map={},
        reclaim_ui_map={},
        status="",
    )
    assert "Not scored yet" in html
    # The alarming red 0% fill must not render for an un-scored prospect.
    assert "bg-rose-500" not in html


def test_prospect_card_low_score_still_shows_red_bar():
    """A genuine bad-fit account (real, low, non-zero score) keeps its red bar."""
    html = _render(
        "htmx/partials/prospecting/_card.html",
        prospect=_prospect(fit_score=5, readiness_score=8),
        snapshots={},
        contact_stats_map={},
        reclaim_ui_map={},
        status="",
    )
    assert "bg-rose-500" in html
    assert "Not scored yet" not in html


def test_prospect_detail_unscored_and_inline_discovery():
    html = _render(
        "htmx/partials/prospecting/detail.html",
        prospect=_prospect(),
        enrichment={},
        snapshot=None,
        signal_tags=[],
        contacts=[],
        contact_stats={},
        similar_customers=[],
        reclaim_ui={},
        warm_intro=None,
        enrich_state=None,
    )
    assert "Not scored yet" in html
    assert "bg-rose-500" not in html
    # Discovery source collapsed to an inline meta line (no full padded card).
    assert "Discovery source:" in html
    assert "card p-4" not in html
