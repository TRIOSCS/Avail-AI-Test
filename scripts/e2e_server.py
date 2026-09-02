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
4. Serve via uvicorn, with the sync-route threadpool serialized to 1 thread
   so concurrent requests cannot interleave on the shared sqlite connection.

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


class _SerializedThreadpool:
    """ASGI wrapper that caps the anyio threadpool at 1 token (plan §2.1/F3).

    Every sync route/dependency runs on the anyio default thread limiter and shares the
    one StaticPool sqlite connection; concurrent badge-endpoint bursts interleave on it
    (spurious 401 / InterfaceError / JSONDecodeError, amplified by get_user's
    session.clear()). The limiter is per-event-loop state, so it must be set from INSIDE
    the running loop: the first scope this wrapper handles is uvicorn's lifespan scope,
    which runs in the loop — set it there, then delegate every scope untouched. Async
    I/O (SSE pings, the event loop itself) is unaffected; at workers:1 the throughput
    cost is negligible.
    """

    def __init__(self, wrapped) -> None:
        self._wrapped = wrapped
        self._serialized = False

    async def __call__(self, scope, receive, send) -> None:
        if not self._serialized:
            self._serialized = True
            import anyio.to_thread

            anyio.to_thread.current_default_thread_limiter().total_tokens = 1
            logger.info("e2e server: sync threadpool serialized (limiter tokens=1)")
        await self._wrapped(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(description="Seeded TESTING app server for the Playwright e2e suite")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    bootstrap()

    import uvicorn

    from app.main import app as fastapi_app

    uvicorn.run(_SerializedThreadpool(fastapi_app), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
