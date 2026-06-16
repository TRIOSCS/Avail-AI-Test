"""Tests for app/management/enrich_specs.py — spec backfill command.

Tests the main() async function with mocked DB and service.

Called by: pytest
Depends on: app/management/enrich_specs.py
"""

import os
import sys

os.environ["TESTING"] = "1"

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The lazy imports inside main() resolve from:
#   app.database.SessionLocal
#   app.services.spec_enrichment_service.enrich_pending_specs
_SESSION_TARGET = "app.database.SessionLocal"
_ENRICH_TARGET = "app.services.spec_enrichment_service.enrich_pending_specs"


@contextmanager
def patched_main(enrich):
    """Patch SessionLocal + enrich_pending_specs and yield (main, mock_db,
    mock_session_cls).

    `enrich` is the AsyncMock used for enrich_pending_specs (configured per test).
    """
    mock_db = MagicMock()
    mock_session_cls = MagicMock(return_value=mock_db)
    with (
        patch(_SESSION_TARGET, mock_session_cls),
        patch(_ENRICH_TARGET, enrich),
    ):
        from app.management.enrich_specs import main

        yield main, mock_db, mock_session_cls


class TestEnrichSpecsMain:
    @pytest.mark.asyncio
    async def test_main_calls_enrich_pending_specs_with_default_limit(self):
        """Main() calls enrich_pending_specs with db and default limit=100."""
        enrich = AsyncMock(return_value={"processed": 5})
        with patched_main(enrich) as (main, mock_db, _):
            await main()

        enrich.assert_called_once_with(mock_db, limit=100)

    @pytest.mark.asyncio
    async def test_main_calls_enrich_pending_specs_with_custom_limit(self):
        """Main() passes custom limit to enrich_pending_specs."""
        enrich = AsyncMock(return_value={"processed": 3})
        with patched_main(enrich) as (main, mock_db, _):
            await main(limit=50)

        enrich.assert_called_once_with(mock_db, limit=50)

    @pytest.mark.asyncio
    async def test_main_closes_db_on_success(self):
        """Main() always closes the DB session on success."""
        enrich = AsyncMock(return_value={"processed": 5})
        with patched_main(enrich) as (main, mock_db, _):
            await main()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_closes_db_when_enrich_raises(self):
        """Main() closes the DB session even when enrich_pending_specs raises."""
        enrich = AsyncMock(side_effect=RuntimeError("service crashed"))
        with patched_main(enrich) as (main, mock_db, _):
            with pytest.raises(RuntimeError, match="service crashed"):
                await main()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_returns_none(self):
        """Main() returns None (no explicit return value)."""
        enrich = AsyncMock(return_value={"processed": 0})
        with patched_main(enrich) as (main, _, _session_cls):
            result = await main()

        assert result is None

    @pytest.mark.asyncio
    async def test_main_creates_session_from_sessionlocal(self):
        """Main() creates exactly one DB session via SessionLocal()."""
        enrich = AsyncMock(return_value={})
        with patched_main(enrich) as (main, _, mock_session_cls):
            await main(limit=25)

        mock_session_cls.assert_called_once_with()


class TestEnrichSpecsEntrypoint:
    def test_main_block_runs_asyncio(self):
        """The __main__ block parses args and calls asyncio.run(main(...))."""
        import runpy

        with (
            patch.object(sys, "argv", ["enrich_specs", "--limit", "10"]),
            patch("asyncio.run") as mock_run,
        ):
            sys.modules.pop("app.management.enrich_specs", None)
            runpy.run_module("app.management.enrich_specs", run_name="__main__", alter_sys=False)

        mock_run.assert_called_once()

    def test_main_block_uses_default_limit(self):
        """The __main__ block defaults to limit=100."""
        import asyncio
        import runpy

        with (
            patch.object(sys, "argv", ["enrich_specs"]),
            patch("asyncio.run") as mock_run,
        ):
            sys.modules.pop("app.management.enrich_specs", None)
            runpy.run_module("app.management.enrich_specs", run_name="__main__", alter_sys=False)

        mock_run.assert_called_once()
        coro = mock_run.call_args[0][0]
        assert asyncio.iscoroutine(coro)
        coro.close()
