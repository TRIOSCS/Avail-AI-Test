"""
E2E test fixtures — Playwright against the live Docker app.

Creates a signed Starlette session cookie for user_id=1 (admin)
so tests can skip the Azure OAuth flow.
"""

import json
import base64
import hmac
import hashlib
import os
import time

import pytest

# ── Session cookie helper ────────────────────────────────────────────

SECRET_KEY = os.getenv(
    "SESSION_SECRET",
    os.getenv(
        "SECRET_KEY",
        "ea277450d8b187b493c424a734864512bef722de5229ae998a558c41a753e5e1",
    ),
)
BASE_URL = os.getenv("E2E_BASE_URL", "https://app.availai.net")


def _sign_session(data: dict, secret: str) -> str:
    """Replicate Starlette SessionMiddleware cookie signing.

    Starlette uses itsdangerous.TimestampSigner with the default
    'cookie-session' salt and sha1 digest.
    """
    import itsdangerous

    payload = base64.b64encode(json.dumps(data).encode()).decode()
    signer = itsdangerous.TimestampSigner(secret)
    return signer.sign(payload).decode()


def make_session_cookie(user_id: int = 1) -> str:
    """Build a valid 'session' cookie value for the given user_id."""
    return _sign_session({"user_id": user_id}, SECRET_KEY)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def browser_context_args():
    """Playwright browser context defaults."""
    return {
        "viewport": {"width": 1440, "height": 1080},
        "ignore_https_errors": True,
    }


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture()
def authed_page(page, base_url):
    """A Playwright page with a valid admin session cookie pre-set."""
    cookie_val = make_session_cookie(user_id=1)
    page.context.add_cookies(
        [
            {
                "name": "session",
                "value": cookie_val,
                "url": base_url,
            }
        ]
    )
    return page
