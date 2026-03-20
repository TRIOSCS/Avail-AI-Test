"""Shared fixtures for UX Mega Test suite.

Provides Jinja2 environment, DB session, authenticated client,
and helper factories for test data creation.

Called by: pytest (auto-discovered)
Depends on: app.main, tests.conftest
"""

import pytest
from jinja2 import Environment


@pytest.fixture()
def jinja_env() -> Environment:
    """Return the Jinja2 template environment from the running app."""
    # FastAPI + Jinja2Templates stores the env on the Jinja2Templates instance.
    # We find it by inspecting app state or importing directly.
    from app.routers.htmx_views import templates

    return templates.env
