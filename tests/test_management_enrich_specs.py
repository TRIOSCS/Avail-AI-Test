"""Tests for app/management/enrich_specs.py — spec backfill command.

Tests the main() async function with mocked DB and service.

Called by: pytest
Depends on: app/management/enrich_specs.py
"""

import os
import sys

os.environ["TESTING"] = "1"

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# The lazy imports inside main() resolve from:
#   app.database.SessionLocal
#   app.services.spec_enrichment_service.enrich_pending_specs
_SESSION_TARGET = "app.database.SessionLocal"
_ENRICH_TARGET = "app.services.spec_enrichment_service.enrich_pending_specs"


class TestEnrichSpecsMain:
    @pytest.mark.asyncio
    async def test_main_calls_enrich_pending_specs_with_default_limit(self):
        """main() calls enrich_pending_specs with db and default limit=100."""
        mock_db = MagicMock()
        mock_session_cls = MagicMock(return_value=mock_db)
        mock_enrich = AsyncMock(return_value={"processed": 5})

        with (
            patch(_SESSION_TARGET, mock_session_cls),
            patch(_ENRICH_TARGET, mock_enrich),
        ):
            from app.management.enrich_specs import main

            await main()

        mock_enrich.assert_called_once_with(mock_db, limit=100)

    @pytest.mark.asyncio
    async def test_main_calls_enrich_pending_specs_with_custom_limit(self):
        """main() passes custom limit to enrich_pending_specs."""
        mock_db = MagicMock()
        mock_session_cls = MagicMock(return_value=mock_db)
        mock_enrich = AsyncMock(return_value={"processed": 3})

        with (
            patch(_SESSION_TARGET, mock_session_cls),
            patch(_ENRICH_TARGET, mock_enrich),
        ):
            from app.management.enrich_specs import main

            await main(limit=50)

        mock_enrich.assert_called_once_with(mock_db, limit=50)

    @pytest.mark.asyncio
    async def test_main_closes_db_on_success(self):
        """main() always closes the DB session on success."""
        mock_db = MagicMock()
        mock_session_cls = MagicMock(return_value=mock_db)
        mock_enrich = AsyncMock(return_value={"processed": 5})

        with (
            patch(_SESSION_TARGET, mock_session_cls),
            patch(_ENRICH_TARGET, mock_enrich),
        ):
            from app.management.enrich_specs import main

            await main()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_closes_db_when_enrich_raises(self):
        """main() closes the DB session even when enrich_pending_specs raises."""
        mock_db = MagicMock()
        mock_session_cls = MagicMock(return_value=mock_db)
        mock_enrich = AsyncMock(side_effect=RuntimeError("service crashed"))

        with (
            patch(_SESSION_TARGET, mock_session_cls),
            patch(_ENRICH_TARGET, mock_enrich),
        ):
            from app.management.enrich_specs import main

            with pytest.raises(RuntimeError, match="service crashed"):
                await main()

        mock_db.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_main_returns_none(self):
        """main() returns None (no explicit return value)."""
        mock_db = MagicMock()
        mock_session_cls = MagicMock(return_value=mock_db)
        mock_enrich = AsyncMock(return_value={"processed": 0})

        with (
            patch(_SESSION_TARGET, mock_session_cls),
            patch(_ENRICH_TARGET, mock_enrich),
        ):
            from app.management.enrich_specs import main

            result = await main()

        assert result is None

    @pytest.mark.asyncio
    async def test_main_creates_session_from_sessionlocal(self):
        """main() creates exactly one DB session via SessionLocal()."""
        mock_db = MagicMock()
        mock_session_cls = MagicMock(return_value=mock_db)
        mock_enrich = AsyncMock(return_value={})

        with (
            patch(_SESSION_TARGET, mock_session_cls),
            patch(_ENRICH_TARGET, mock_enrich),
        ):
            from app.management.enrich_specs import main

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
