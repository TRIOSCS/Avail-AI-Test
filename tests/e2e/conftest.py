"""
E2E test fixtures — Playwright against the live Docker app.

Creates a signed Starlette session cookie for user_id=1 (admin)
so tests can skip the Azure OAuth flow.

Base URL resolution (in order):
1. E2E_BASE_URL env var (explicit override)
2. Docker container IP (auto-detected if availai-app-1 is running)
3. https://app.availai.net (remote fallback)
"""

import base64
import json
import os
import subprocess

import pytest


# ── Base URL resolution ─────────────────────────────────────────────

def _get_docker_app_ip() -> str | None:
    """Auto-detect the app container's IP on the Docker network."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "availai-app-1", "--format",
             "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
            capture_output=True, text=True, timeout=5,
        )
        ip = result.stdout.strip()
        return ip if ip else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _resolve_base_url() -> str:
    env_url = os.getenv("E2E_BASE_URL")
    if env_url:
        return env_url
    docker_ip = _get_docker_app_ip()
    if docker_ip:
        return f"http://{docker_ip}:8000"
    return "https://app.availai.net"


BASE_URL = _resolve_base_url()


# ── Session cookie helper ────────────────────────────────────────────

def _get_secret_key() -> str:
    """Get the session secret: try Docker container first, then env, then default."""
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "app", "python3", "-c",
             "from app.config import settings; print(settings.secret_key)"],
            capture_output=True, text=True, cwd="/root/availai", timeout=10,
        )
        secret = result.stdout.strip()
        if secret:
            return secret
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return os.getenv(
        "SESSION_SECRET",
        os.getenv(
            "SECRET_KEY",
            "ea277450d8b187b493c424a734864512bef722de5229ae998a558c41a753e5e1",
        ),
    )


SECRET_KEY = _get_secret_key()


def _sign_session(data: dict, secret: str) -> str:
    """Replicate Starlette SessionMiddleware cookie signing."""
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
