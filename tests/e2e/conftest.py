"""E2E test fixtures — Playwright against the live Docker app.

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
            [
                "docker",
                "inspect",
                "availai-app-1",
                "--format",
                "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
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
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "app",
                "python3",
                "-c",
                "from app.config import settings; print(settings.secret_key)",
            ],
            capture_output=True,
            text=True,
            cwd="/root/availai",
            timeout=10,
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


# ── Deterministic seed ───────────────────────────────────────────────
# The e2e suite drives the LIVE app, so core pages (requisitions, sightings,
# vendors, …) render nothing when the DB is empty — and several tests then
# ``pytest.skip`` on the missing element, so a genuinely broken page reads green.
# This fixture guarantees a deterministic core dataset exists by running the
# idempotent, additive ``seed_sample_data`` command INSIDE the app container. Once
# it succeeds, core-page tests can hard-ASSERT their elements (a miss is a real
# failure). Where the app is remote / docker-less (no container to exec into),
# seeding is genuinely unavailable → ``seed_e2e_data`` is False and data-dependent
# tests keep their environment-specific skip.


def _run_seed() -> bool:
    """Run the idempotent sample-data seeder inside the app container.

    True on success.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "-e",
                "ALLOW_SAMPLE_DATA_SEED=true",
                "app",
                "python",
                "-m",
                "app.management.seed_sample_data",
            ],
            capture_output=True,
            text=True,
            cwd="/root/availai",
            timeout=300,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


@pytest.fixture(scope="session")
def seed_e2e_data() -> bool:
    """Session-scoped: ensure deterministic core data exists. Returns whether seeding ran.

    ``True``  → the app DB is under our control; core pages MUST render their
                elements and a missing element is a hard FAILURE.
    ``False`` → the environment cannot be seeded (remote / no docker); data-dependent
                tests may skip (genuinely environment-specific).
    """
    return _run_seed()


def _require_core_or_skip(seeded: bool, present: bool, element: str) -> None:
    """Assert a core element is present when we control the data; else skip (env-
    specific).

    Bridges "core pages MUST render" with "keep skips for genuinely optional /
    environment-specific cases": when ``seed_e2e_data`` succeeded a missing element
    FAILS the test; when seeding was unavailable the test skips instead.
    """
    if seeded:
        assert present, f"core element missing after deterministic seed: {element}"
    elif not present:
        pytest.skip(f"{element} absent and environment could not be seeded (env-specific)")


@pytest.fixture()
def core_guard():
    """Return the ``_require_core_or_skip`` helper (a fixture so no cross-module import
    is needed)."""
    return _require_core_or_skip
