"""test_connector_status.py — Tests for app/connector_status.py.

log_connector_status now reports readiness from DB-first credential resolution +
api_sources.status health (was: raw settings.* env-var presence only). Readiness means
"credentials resolve (DB row wins over env) AND not disabled" — what the app runs on.
"""

import os

os.environ["TESTING"] = "1"

from sqlalchemy.orm import Session

from app.connector_status import log_connector_status
from app.constants import ApiSourceStatus
from app.models import ApiSource
from tests.conftest import engine  # noqa: F401 — ensures SQLite engine is used


def _mk(db, name, *, env_vars=None, status="pending", credentials=None):
    src = ApiSource(
        name=name,
        display_name=name.replace("_", " ").title(),
        category="api",
        source_type="api",
        status=status,
        env_vars=env_vars if env_vars is not None else [],
        credentials=credentials or {},
        total_searches=0,
        total_results=0,
        avg_response_ms=0,
    )
    db.add(src)
    db.commit()
    db.refresh(src)
    return src


class TestLogConnectorStatus:
    def test_returns_dict_of_display_name_to_ready(self, db_session: Session):
        _mk(db_session, "mouser", env_vars=["MOUSER_API_KEY"], status="live", credentials={})
        result = log_connector_status(db_session)
        assert isinstance(result, dict)
        assert set(result.keys()) == {"Mouser"}

    def test_missing_credentials_not_ready(self, db_session: Session, monkeypatch):
        monkeypatch.delenv("MOUSER_API_KEY", raising=False)
        _mk(db_session, "mouser", env_vars=["MOUSER_API_KEY"], status="pending")
        result = log_connector_status(db_session)
        assert result["Mouser"] is False

    def test_db_credential_resolves_ready_even_without_env(self, db_session: Session, monkeypatch):
        """A key saved ONLY in the DB row reads as ready — the old env-only check missed
        this and mislabeled it disabled."""
        monkeypatch.delenv("MOUSER_API_KEY", raising=False)
        _mk(db_session, "mouser", env_vars=["MOUSER_API_KEY"], status="live", credentials={"MOUSER_API_KEY": "enc"})
        result = log_connector_status(db_session)
        assert result["Mouser"] is True

    def test_env_credential_resolves_ready(self, db_session: Session, monkeypatch):
        monkeypatch.setenv("MOUSER_API_KEY", "k")
        _mk(db_session, "mouser", env_vars=["MOUSER_API_KEY"], status="live")
        result = log_connector_status(db_session)
        assert result["Mouser"] is True

    def test_keyless_source_is_ready(self, db_session: Session):
        """A keyless source (no env vars — worker/flag/scopes) counts as configured."""
        _mk(db_session, "ai_live_web", env_vars=[], status="pending")
        result = log_connector_status(db_session)
        assert result["Ai Live Web"] is True

    def test_disabled_source_not_ready(self, db_session: Session, monkeypatch):
        monkeypatch.setenv("MOUSER_API_KEY", "k")
        _mk(db_session, "mouser", env_vars=["MOUSER_API_KEY"], status=ApiSourceStatus.DISABLED.value)
        result = log_connector_status(db_session)
        assert result["Mouser"] is False

    def test_erroring_source_still_configured(self, db_session: Session, monkeypatch):
        """An erroring source with credentials is still 'configured' (error is a health
        state, not a config state) — it counts toward readiness but is logged as
        erroring."""
        monkeypatch.setenv("MOUSER_API_KEY", "k")
        _mk(db_session, "mouser", env_vars=["MOUSER_API_KEY"], status=ApiSourceStatus.ERROR.value)
        result = log_connector_status(db_session)
        assert result["Mouser"] is True

    def test_empty_db_returns_empty(self, db_session: Session):
        assert log_connector_status(db_session) == {}
