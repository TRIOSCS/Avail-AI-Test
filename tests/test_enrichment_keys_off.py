"""tests/test_enrichment_keys_off.py — Keys-off honesty for CRM Enrich (spec §7).

The free SAM.gov path needs no credential (public DEMO_KEY tier), so the router's
provider guard must NOT 503 a fully keyless instance — that was the guard bug that
blocked the free path. Paid providers absent → the result panel labels them off.

Called by: pytest
Depends on: app/routers/crm/enrichment.py, conftest fixtures (client, db_session,
    test_user), app/services/company_enrich_runs.py
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services.company_enrich_runs import CompanyEnrichOutcome, company_enrich_runs


@pytest.fixture(autouse=True)
def _keyless(monkeypatch):
    """Force the keys-off state regardless of the 60s credential cache."""
    import app.routers.crm.enrichment as enr

    monkeypatch.setattr(enr, "get_credential_cached", lambda *a, **k: None)
    monkeypatch.setattr(enr, "claude_configured", lambda: False)


@pytest.fixture(autouse=True)
def _clear_company_runs():
    """Reset the process-wide in-flight registry around every test (isolation)."""
    company_enrich_runs._state.clear()
    yield
    company_enrich_runs._state.clear()


# ── The guard itself ─────────────────────────────────────────────────────


def test_guard_passes_keyless_when_sam_gov_enabled(monkeypatch):
    """Root-cause fix: no paid keys but the free SAM.gov gate is on → no 503."""
    import app.routers.crm.enrichment as enr

    monkeypatch.setattr(enr.settings, "sam_gov_enrichment_enabled", True)
    enr._require_enrichment_provider()  # must not raise


def test_guard_503_only_when_nothing_can_run(monkeypatch):
    """No paid keys AND SAM.gov gated off → genuinely nothing to run → 503."""
    import app.routers.crm.enrichment as enr

    monkeypatch.setattr(enr.settings, "sam_gov_enrichment_enabled", False)
    with pytest.raises(HTTPException) as exc:
        enr._require_enrichment_provider()
    assert exc.value.status_code == 503


def test_guard_passes_on_paid_key_even_without_sam_gov(monkeypatch):
    """A configured paid key alone still opens the door (SAM.gov gate off)."""
    import app.routers.crm.enrichment as enr

    monkeypatch.setattr(enr.settings, "sam_gov_enrichment_enabled", False)
    monkeypatch.setattr(enr, "get_credential_cached", lambda *a, **k: "TEST_KEY")
    enr._require_enrichment_provider()  # must not raise


# ── End to end: the Enrich button runs keyless ───────────────────────────


def test_enrich_button_keyless_returns_poller_not_503(client, db_session, test_user, monkeypatch):
    """With zero provider keys the Enrich click reaches the endpoint body and returns
    the polling panel — the free SAM.gov waterfall is allowed to run."""
    import app.routers.crm.enrichment as enr
    from app.models import Company

    monkeypatch.setattr(enr, "_run_company_enrichment", AsyncMock())

    company = Company(name="Keyless Co", domain="keyless.com", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    resp = client.post(f"/api/enrich/company/{company.id}", headers={"HX-Request": "true"})
    assert resp.status_code == 200
    assert "Enriching" in resp.text


# ── Paid-providers-off label on the result panel ─────────────────────────


def test_result_panel_labels_paid_providers_off(client, db_session, test_user):
    """Keyless finished run → the result panel says paid providers are off."""
    from app.models import Company

    company = Company(name="Label Co", domain="label.com", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    company_enrich_runs.finish(
        company.id,
        CompanyEnrichOutcome(blocked=False, updated_fields=["industry"], suggested=[], errored_providers=[]),
    )

    resp = client.get(f"/api/enrich/company/{company.id}/status")
    assert resp.status_code == 286
    assert "Paid enrichment providers are off — free SAM.gov data only." in resp.text


def test_result_panel_no_label_when_paid_key_configured(client, db_session, test_user, monkeypatch):
    """With a paid key configured the honesty label stays out of the panel."""
    import app.routers.crm.enrichment as enr
    from app.models import Company

    monkeypatch.setattr(enr, "get_credential_cached", lambda *a, **k: "TEST_KEY")

    company = Company(name="Keyed Co", domain="keyed.com", is_active=True, account_owner_id=test_user.id)
    db_session.add(company)
    db_session.commit()
    db_session.refresh(company)

    company_enrich_runs.finish(
        company.id,
        CompanyEnrichOutcome(blocked=False, updated_fields=["industry"], suggested=[], errored_providers=[]),
    )

    resp = client.get(f"/api/enrich/company/{company.id}/status")
    assert resp.status_code == 286
    assert "Paid enrichment providers are off" not in resp.text
