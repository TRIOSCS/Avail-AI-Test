"""test_connector_keyless_test.py — Phase-0 FIX C: keyless per-source Test is no longer
a cosmetic no-op that falsely reports OK.

Before this fix _get_connector_for_source had no branch for ai_live_web, so its Test
resolved to None ("No connector available", swallowed); with env_vars=[] the has_env_vars
gate skipped status persistence, so the keyless card stayed "all OK". sam_gov_enrichment /
stock_list_import have no test path at all yet still rendered a Test button.

Covers:
- _get_connector_for_source wires AIWebSearchConnector (key present -> connector, absent -> None)
- source_has_test_path: real-test-path gate (ai_live_web yes w/ key, sam_gov / stock_list no)
- run_source_test persists a keyless source's ok/error status (has_env_vars gate dropped)
- _enrich_source testability follows a real test path (Test hidden where none exists)
- _test_toast_header builds the single-Test showToast payload

Called by: pytest
Depends on: app/routers/sources.py, app/routers/htmx/settings.py, tests/conftest.py
"""

import json
import os

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.orm import Session

from app.constants import ApiSourceStatus
from app.models import ApiSource
from app.routers.sources import (
    _get_connector_for_source,
    _persist_test_result,
    _test_toast_header,
    run_source_test,
    source_has_test_path,
)
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used


def _mk_source(db, name, *, env_vars=None, status="pending"):
    src = ApiSource(
        name=name,
        display_name=name.replace("_", " ").title(),
        category="market_data",
        source_type="api",
        status=status,
        env_vars=env_vars if env_vars is not None else [],
        total_searches=0,
        total_results=0,
        avg_response_ms=0,
    )
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


# ── _get_connector_for_source: AI web search wiring ──────────────────────────


class TestAiLiveWebWiring:
    def test_ai_live_web_resolves_connector_with_key(self, db_session: Session, monkeypatch):
        from app.connectors.ai_live_web import AIWebSearchConnector

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        conn = _get_connector_for_source("ai_live_web", db_session)
        assert isinstance(conn, AIWebSearchConnector)

    def test_ai_live_web_returns_none_without_key(self, db_session: Session, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        with patch("app.services.credential_service.get_credential", return_value=None):
            assert _get_connector_for_source("ai_live_web", db_session) is None


# ── source_has_test_path: the real-test-path gate ────────────────────────────


class TestSourceHasTestPath:
    def test_ai_live_web_has_path_with_key(self, db_session: Session, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        assert source_has_test_path("ai_live_web", db_session) is True

    def test_sam_gov_has_no_test_path(self, db_session: Session):
        assert source_has_test_path("sam_gov_enrichment", db_session) is False

    def test_stock_list_import_has_no_test_path(self, db_session: Session):
        assert source_has_test_path("stock_list_import", db_session) is False


# ── run_source_test persistence (has_env_vars gate dropped) ──────────────────


class TestKeylessTestPersistence:
    async def test_keyless_success_persists_live_status(self, db_session: Session):
        """A keyless source (env_vars=[]) that tests OK now records status=live —
        previously the has_env_vars gate discarded the result and it stayed
        'pending'."""
        src = _mk_source(db_session, "ai_live_web", env_vars=[], status="pending")

        mock_conn = MagicMock()
        mock_conn.search = AsyncMock(return_value=[{"vendor_name": "X", "status": "ok"}])
        with patch("app.routers.sources._get_connector_for_source", return_value=mock_conn):
            result = await run_source_test(src, db_session)

        assert result["status"] == "ok"
        db_session.refresh(src)
        assert src.status == ApiSourceStatus.LIVE
        assert src.last_success is not None
        assert src.last_error is None

    async def test_keyless_failure_persists_error_status(self, db_session: Session):
        """A keyless source that fails its probe now records status=error (was: silently
        untested -> summary said 'all OK')."""
        src = _mk_source(db_session, "ai_live_web", env_vars=[], status="pending")

        mock_conn = MagicMock()
        mock_conn.search = AsyncMock(side_effect=ValueError("Claude unavailable"))
        with patch("app.routers.sources._get_connector_for_source", return_value=mock_conn):
            result = await run_source_test(src, db_session)

        assert result["status"] == "error"
        db_session.refresh(src)
        assert src.status == ApiSourceStatus.ERROR
        assert "Claude unavailable" in src.last_error

    async def test_no_connector_keyless_persists_error(self, db_session: Session):
        """No connector resolvable -> recorded as error, not swallowed."""
        src = _mk_source(db_session, "sam_gov_enrichment", env_vars=[], status="pending")
        result = await run_source_test(src, db_session)
        assert result["status"] == "error"
        db_session.refresh(src)
        assert src.status == ApiSourceStatus.ERROR


# ── _enrich_source testability follows a real test path ──────────────────────


class TestEnrichSourceTestable:
    def test_ai_live_web_testable_with_key(self, db_session: Session, monkeypatch):
        from app.routers.htmx.settings import _enrich_source

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
        src = _mk_source(db_session, "ai_live_web", env_vars=[])
        enriched = _enrich_source(src, db_session)
        assert enriched["testable"] is True

    def test_sam_gov_not_testable_hides_test_button(self, db_session: Session):
        from app.routers.htmx.settings import _enrich_source

        src = _mk_source(db_session, "sam_gov_enrichment", env_vars=[])
        enriched = _enrich_source(src, db_session)
        assert enriched["testable"] is False

    def test_stock_list_import_not_testable(self, db_session: Session):
        from app.routers.htmx.settings import _enrich_source

        src = _mk_source(db_session, "stock_list_import", env_vars=[])
        enriched = _enrich_source(src, db_session)
        assert enriched["testable"] is False


# ── _test_toast_header: single-Test showToast payload ────────────────────────


class TestTestToastHeader:
    def test_ok_toast_is_success(self):
        payload = json.loads(
            _test_toast_header(
                {"source": "BrokerBin", "status": "ok", "results_count": 3, "elapsed_ms": 412, "error": None}
            )
        )
        assert payload["showToast"]["type"] == "success"
        assert "3 result(s)" in payload["showToast"]["message"]
        assert "412ms" in payload["showToast"]["message"]

    def test_error_toast_is_error(self):
        payload = json.loads(
            _test_toast_header(
                {"source": "Nexar", "status": "error", "results_count": 0, "elapsed_ms": 20, "error": "bad key"}
            )
        )
        assert payload["showToast"]["type"] == "error"
        assert "bad key" in payload["showToast"]["message"]

    def test_no_results_toast_is_info(self):
        payload = json.loads(
            _test_toast_header(
                {"source": "Mouser", "status": "no_results", "results_count": 0, "elapsed_ms": 99, "error": None}
            )
        )
        assert payload["showToast"]["type"] == "info"


def test_persist_test_result_shape(db_session: Session):
    """_persist_test_result returns the canonical result dict and writes status."""
    src = _mk_source(db_session, "ai_live_web", env_vars=[])
    out = _persist_test_result(src, db_session, results=[{"a": 1}], elapsed_ms=50, error=None)
    assert out["status"] == "ok"
    assert out["results_count"] == 1
    assert out["test_mpn"] == "LM358N"
    db_session.refresh(src)
    assert src.status == ApiSourceStatus.LIVE
