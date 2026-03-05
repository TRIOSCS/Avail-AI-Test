"""Tests for the file mapper service.

Covers: route scanning, file mapping, stable file detection.

Called by: pytest
Depends on: app.services.file_mapper
"""

from app.services.file_mapper import (
    STABLE_FILES,
    get_relevant_files,
    has_stable_files,
    scan_routers,
)


class TestScanRouters:
    def setup_method(self):
        scan_routers.cache_clear()

    def test_scan_finds_routes(self):
        """scan_routers should find real routes in app/routers/."""
        routes = scan_routers()
        assert len(routes) > 0
        # Should find trouble tickets route
        assert any("trouble-tickets" in k for k in routes)

    def test_routes_have_router_paths(self):
        """Each route should map to a real .py file."""
        routes = scan_routers()
        for pattern, path in routes.items():
            assert path.startswith("app/routers/")
            assert path.endswith(".py")

    def test_parametric_routes_normalized(self):
        """Routes like /api/X/{id} should be normalized to /api/X/{param}."""
        routes = scan_routers()
        for pattern in routes:
            assert "{id}" not in pattern or "{param}" in pattern


class TestGetRelevantFiles:
    def setup_method(self):
        scan_routers.cache_clear()

    def test_known_route(self):
        """Should return router file for a known route."""
        files = get_relevant_files(route_pattern="/api/trouble-tickets")
        assert len(files) >= 1
        roles = [f["role"] for f in files]
        assert "router" in roles

    def test_known_route_includes_service(self):
        """Should include service file if it exists."""
        files = get_relevant_files(route_pattern="/api/trouble-tickets")
        paths = [f["path"] for f in files]
        assert any("service" in p for p in paths)

    def test_unknown_route(self):
        """Unknown route should return empty list."""
        files = get_relevant_files(route_pattern="/api/nonexistent-thing")
        assert files == []

    def test_error_context_extracts_files(self):
        """Should extract file paths mentioned in error context."""
        files = get_relevant_files(error_context="Traceback: File app/services/health_monitor.py line 42")
        paths = [f["path"] for f in files]
        assert "app/services/health_monitor.py" in paths

    def test_stable_flag_set(self):
        """Files in STABLE_FILES should be flagged."""
        files = get_relevant_files(error_context="Error in app/main.py")
        main_entry = [f for f in files if f["path"] == "app/main.py"]
        assert len(main_entry) == 1
        assert main_entry[0]["stable"] is True

    def test_no_inputs(self):
        """No route or error context should return empty."""
        files = get_relevant_files()
        assert files == []


class TestHasStableFiles:
    def test_with_stable(self):
        assert has_stable_files([{"path": "app/main.py", "stable": True}]) is True

    def test_without_stable(self):
        assert has_stable_files([{"path": "app/routers/x.py", "stable": False}]) is False

    def test_empty(self):
        assert has_stable_files([]) is False


class TestStableFilesConstant:
    def test_stable_files_contains_critical(self):
        """STABLE_FILES should contain critical system files."""
        assert "app/main.py" in STABLE_FILES
        assert "app/database.py" in STABLE_FILES
        assert "app/config.py" in STABLE_FILES
        assert "app/dependencies.py" in STABLE_FILES
