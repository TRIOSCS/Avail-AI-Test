"""Connector startup visibility — log connector readiness (DB-first + health).

Called by: main.py lifespan (after seed_api_sources / seed_browser_workers).
Depends on: ApiSource model, ApiSourceStatus, credential resolution (DB-first).
"""

import os

from loguru import logger
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from .constants import ApiSourceStatus
from .database import SessionLocal
from .models import ApiSource


def _credential_present(src: ApiSource, env_var: str) -> bool:
    """DB-first presence for one env var: a saved DB credential wins over the process env.

    Mirrors ``credential_service.credential_is_set`` but reuses the already-loaded row
    (no per-var re-query) since the caller holds every ApiSource in hand.
    """
    creds: dict = dict(src.credentials or {})
    if creds.get(env_var):
        return True
    return bool(os.getenv(env_var))


def log_connector_status(db=None) -> dict[str, bool]:
    """Log connector readiness from DB-first credential resolution + health status.

    A connector is "ready" when its credentials resolve the way a search resolves them
    (a saved DB credential wins over the env var) AND it is not disabled — i.e. what the
    app actually runs on. The old check read raw ``settings.*`` env vars only, which
    diverged from the DB-first resolution + ``api_sources.status`` health and misled triage
    (a key saved only in the DB read as "disabled"; an env key with a dead upstream read as
    "enabled"). Keyless sources (no env vars — worker-backed / flag / consented-scopes)
    count as configured.

    Returns ``{display_name: ready}``. Opens its own session when none is supplied (the
    startup call site passes none, after seeding). Never raises — logs and returns ``{}``
    on a DB error so a telemetry hiccup can't wedge startup.
    """
    own_session = db is None
    if own_session:
        db = SessionLocal()
    status: dict[str, bool] = {}
    try:
        sources = db.query(ApiSource).order_by(ApiSource.display_name).all()
        live: list[str] = []
        configured: list[str] = []
        erroring: list[str] = []
        missing: list[str] = []
        for src in sources:
            env_vars = src.env_vars or []
            has_creds = not env_vars or any(_credential_present(src, v) for v in env_vars)
            ready = has_creds and src.status != ApiSourceStatus.DISABLED
            status[src.display_name] = ready
            if src.status == ApiSourceStatus.ERROR:
                erroring.append(src.display_name)
            elif src.status == ApiSourceStatus.LIVE:
                live.append(src.display_name)
            elif ready:
                configured.append(src.display_name)
            else:
                missing.append(src.display_name)

        if live:
            logger.info("Connectors live: {}", ", ".join(sorted(live)))
        if configured:
            logger.info(
                "Connectors configured (credentials present, not yet verified): {}",
                ", ".join(sorted(configured)),
            )
        if erroring:
            logger.warning("Connectors erroring (last health check failed): {}", ", ".join(sorted(erroring)))
        if missing:
            logger.warning("Connectors not configured (no credentials / disabled): {}", ", ".join(sorted(missing)))
    except (SQLAlchemyError, DBAPIError) as e:
        logger.warning("Connector status check failed: {}", e)
    finally:
        if own_session:
            db.close()
    return status
