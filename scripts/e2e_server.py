"""Launcher for the Playwright e2e webServer: seeded TESTING app, same process.

Boots the FastAPI app for the TypeScript e2e suite (playwright.config.ts
webServer). Under TESTING, run_startup_migrations short-circuits before any
DDL or seeding (app/startup.py), so the in-memory sqlite would otherwise have
no tables and no users — this script does the bootstrap the suite needs,
INSIDE the serving process (StaticPool gives every sqlite URL one shared
in-memory connection per process; a separate seed process would populate a
different throwaway DB):

1. Patch sqlite type compilation (ARRAY/TSVECTOR/JSONB) before any DDL.
2. Create the full schema on the process-global engine.
3. Seed the DEFAULT_USER_* admin via the production helper (PBKDF2, ORM —
   keeps column defaults such as m365_connected=False intact).
4. Serve via uvicorn, with every http request (except the SSE stream paths)
   holding one whole-request asyncio.Lock, so no two non-SSE requests are ever
   in flight at once — sync deps on the threadpool, async handler bodies on
   the loop thread, and response rendering all run under the same lock.

Dev/CI-only — never imported by app runtime code.

Called by: playwright.config.ts webServer command; tests/test_e2e_server_bootstrap.py
Depends on: app.main (model registry + ASGI app), app.database (engine,
            SessionLocal), app.startup._create_default_user_if_env_set
"""

import argparse
import os

from loguru import logger


def _patch_types_for_sqlite() -> None:
    """Register ARRAY → JSON, TSVECTOR → TEXT, JSONB → JSON sqlite adapters.

    Replicated from tests/conftest.py:70-80 (_patch_types_for_sqlite) — without
    it, ``Base.metadata.create_all`` on ``sqlite://`` raises CompileError on
    the first JSONB column. The conftest's ``_PG_ONLY_TABLES`` exclusion is a
    dead no-op there and is deliberately NOT replicated here.
    """
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"
    SQLiteTypeCompiler.visit_TSVECTOR = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"


def bootstrap() -> None:
    """Create the schema and seed the DEFAULT_USER_* admin. Idempotent.

    Runs against the process-global engine/SessionLocal so the rows live in the same
    StaticPool in-memory DB uvicorn will serve from.
    """
    email = os.environ.get("DEFAULT_USER_EMAIL")
    password = os.environ.get("DEFAULT_USER_PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "e2e bootstrap: DEFAULT_USER_EMAIL and DEFAULT_USER_PASSWORD must be "
            "set (the playwright.config.ts webServer command sets both)"
        )

    _patch_types_for_sqlite()

    import app.main  # noqa: F401 — imports every router/model, registering all tables on Base
    from app.database import SessionLocal, engine
    from app.models.base import Base
    from app.startup import _create_default_user_if_env_set

    Base.metadata.create_all(bind=engine)
    logger.info("e2e bootstrap: schema ready ({} tables)", len(Base.metadata.tables))

    # Production seeding path: PBKDF2-HMAC-SHA256 hash, ORM insert (column
    # defaults intact — auth.spec.ts depends on m365_connected=False). Keep it
    # ORM (plan §1/F11); a raw-SQL seed would NULL the default.
    _create_default_user_if_env_set()

    from app.models.auth import User

    db = SessionLocal()
    try:
        user = db.query(User).filter_by(email=email.lower()).first()
    finally:
        db.close()
    if user is None:
        raise RuntimeError(
            f"e2e bootstrap: seeded user {email!r} not found after "
            "_create_default_user_if_env_set() — check webServer env vars"
        )
    logger.info("e2e bootstrap: user {} ready (role={})", user.email, user.role)


# Held-open response streams (SSE) must NEVER hold the whole-request lock —
# they stay open for the client's lifetime and would deadlock every later
# request. Residual accepted as out-of-scope: an exempt stream's only DB touch
# is the auth dependency at stream-open, which runs outside the lock; no e2e
# spec opens an SSE stream (guardrail comments keep it that way).
_LOCK_EXEMPT_PREFIXES = ("/api/events/stream", "/v2/partials/search/stream")


class _SerializedRequests:
    """ASGI wrapper: one asyncio.Lock held for the FULL lifecycle of every http scope
    (checkpoint review Finding 1, replacing the thread-limiter shim of plan §2.1/F3).

    Why a whole-request lock: all requests share the one StaticPool sqlite connection,
    and interleaved access corrupts reads (spurious ``db.get`` -> None -> silent 401,
    JSONDecodeError on JSON columns, corrupted ``is_active`` -> 403 — reviewer-measured
    ~0.67% at 600 concurrent requests). The earlier anyio thread-limiter shim only
    serialized threadpool (sync-def) work; async-def handlers (the badge endpoints)
    query on the EVENT LOOP thread, outside the limiter, so one threadpool thread and
    the loop thread still interleaved. Holding the lock across the whole http scope
    serializes sync deps, async handler bodies, and response rendering alike — for
    non-exempt paths, nothing can interleave on the shared connection. SSE paths are
    exempt (see _LOCK_EXEMPT_PREFIXES); non-http scopes (lifespan) pass through. At
    workers:1 the throughput cost is negligible.
    """

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped
        self._lock = None  # created lazily inside the running event loop

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_LOCK_EXEMPT_PREFIXES):
            await self._wrapped(scope, receive, send)
            return
        if self._lock is None:
            import asyncio

            self._lock = asyncio.Lock()
        async with self._lock:
            await self._wrapped(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seeded TESTING app server for the Playwright e2e suite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    bootstrap()

    import uvicorn

    from app.main import app as fastapi_app

    logger.info(
        "e2e server: whole-request serialization active (exempt prefixes: {})",
        ", ".join(_LOCK_EXEMPT_PREFIXES),
    )
    uvicorn.run(_SerializedRequests(fastapi_app), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
