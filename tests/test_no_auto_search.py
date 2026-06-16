"""Tests verifying that auto-search machinery is gone.

Called by: pytest
Depends on: search-button-only sourcing design (2026-05-14)
"""

import importlib

import pytest


def _route_paths():
    """Return the set of registered route paths on the FastAPI app.

    Uses iter_routes so included-router routes (hidden behind fastapi 0.137's
    _IncludedRouter tree wrappers) are flattened in — otherwise the absence assertions
    below would pass vacuously for any router-mounted route.
    """
    from app.main import app
    from tests._route_helpers import iter_routes

    paths = set()
    for route in iter_routes(app.routes):
        path = getattr(route, "path", None)
        if path:
            paths.add(path)
    return paths


def _app_file_text(*parts):
    """Read an app/ source file relative to the repo root as text."""
    from pathlib import Path

    path = Path(__file__).parent.parent.joinpath("app", *parts)
    return path.read_text()


class TestAutoSearchRemoved:
    @pytest.mark.parametrize(
        "removed_path",
        [
            # Used to be POST /api/requirements/{item_id}/search — removed entirely.
            "/api/requirements/{item_id}/search",
            # Used to be POST /api/requisitions/{req_id}/search — removed entirely.
            "/api/requisitions/{req_id}/search",
        ],
    )
    def test_v1_search_route_is_not_registered(self, removed_path):
        assert removed_path not in _route_paths()

    def test_sourcing_refresh_module_does_not_exist(self):
        """The 3 AM cron module is gone; the import path itself should not exist."""
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module("app.jobs.sourcing_refresh_jobs")

    def test_jobs_init_does_not_register_sourcing_refresh(self):
        """register_sourcing_refresh_jobs is no longer imported/called from
        jobs/__init__.py."""
        text = _app_file_text("jobs", "__init__.py")
        assert "register_sourcing_refresh_jobs" not in text
        assert "sourcing_refresh_jobs" not in text

    def test_htmx_views_does_not_auto_search_on_requisition_create(self):
        """No background _bg_full_search after requisition create."""
        text = _app_file_text("routers", "htmx_views.py")
        assert "_bg_full_search" not in text
        assert "Auto-search all created requirements" not in text

    def test_no_background_task_runs_search_requirement(self):
        """No background_tasks.add_task(...) call wraps search_requirement anywhere in
        app/.

        The only path to search_requirement is the user-driven
        /v2/partials/sightings/{id}/refresh endpoint.
        """
        import subprocess

        result = subprocess.run(
            [
                "grep",
                "-rE",
                r"background_tasks\.add_task\([^)]*search_requirement",
                "/root/availai/app/",
                "--include=*.py",
            ],
            capture_output=True,
            text=True,
        )
        assert result.stdout == "", f"Found background search_requirement calls: {result.stdout!r}"
