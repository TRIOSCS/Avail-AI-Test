"""Tests verifying that auto-search machinery is gone.

Called by: pytest
Depends on: search-button-only sourcing design (2026-05-14)
"""

import importlib

import pytest


def _route_paths():
    """Return the set of registered route paths on the FastAPI app."""
    from app.main import app

    paths = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
    return paths


class TestAutoSearchRemoved:
    def test_v1_search_one_route_is_not_registered(self):
        """Used to be POST /api/requirements/{item_id}/search — removed entirely."""
        assert "/api/requirements/{item_id}/search" not in _route_paths()

    def test_v1_search_all_route_is_not_registered(self):
        """Used to be POST /api/requisitions/{req_id}/search — removed entirely."""
        assert "/api/requisitions/{req_id}/search" not in _route_paths()

    def test_sourcing_refresh_module_does_not_exist(self):
        """The 3 AM cron module is gone; the import path itself should not exist."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("app.jobs.sourcing_refresh_jobs")

    def test_jobs_init_does_not_register_sourcing_refresh(self):
        """register_sourcing_refresh_jobs is no longer imported/called from
        jobs/__init__.py."""
        from pathlib import Path

        path = Path(__file__).parent.parent / "app" / "jobs" / "__init__.py"
        text = path.read_text()
        assert "register_sourcing_refresh_jobs" not in text
        assert "sourcing_refresh_jobs" not in text
