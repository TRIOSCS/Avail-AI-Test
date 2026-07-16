"""startup.py — Runtime Database Operations (Idempotent)

Schema DDL (columns, indexes, constraints, extensions) lives in Alembic migrations.
This file handles PostgreSQL-specific runtime operations that must run every boot:
triggers, seeds, backfills, and ANALYZE.

Called by: main.py lifespan
Depends on: database.py (engine), models.py (Base)
"""

import os
from pathlib import Path

from loguru import logger
from sqlalchemy import text as sqltext
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from .constants import DeferredBackfillState
from .database import SessionLocal, engine
from .utils.normalization import normalize_mpn_key as _norm_key


def ensure_screenshot_storage() -> None:
    """Guarantee the trouble-ticket screenshot dir exists and is writable.

    Trouble-ticket screenshots (filed from the report button on any page, e.g.
    /v2/search) are written to ``error_reports.UPLOAD_DIR``, which lives on the
    ``uploads`` Docker named volume. An existing/upgraded volume can be
    root-owned while the app runs as the non-root ``appuser``, so writes fail at
    runtime with PermissionError. Create the dir and fail fast at boot — a clear
    RuntimeError here beats silently dropping screenshots later.

    Unlike most ops here this is NOT gated by run_startup_migrations' TESTING
    short-circuit: it is called directly from the main.py lifespan so the guard
    runs on every real boot. No DDL — a filesystem mkdir/writability check only.

    Called by: main.py lifespan (real boots), tests (directly)
    Depends on: app/routers/error_reports.py (UPLOAD_DIR)
    """
    from .routers.error_reports import UPLOAD_DIR

    path = Path(UPLOAD_DIR)
    path.mkdir(parents=True, exist_ok=True)
    if not os.access(path, os.W_OK):
        raise RuntimeError(f"Screenshot storage {path} is not writable by the app process")
    logger.info("Screenshot storage ready and writable: {}", path)


def ensure_avatar_storage() -> None:
    """Guarantee the profile-avatar dir exists and is writable.

    Profile photos (uploaded from the Settings → Profile tab) are written to
    ``avatars.AVATARS_DIR``, a parallel subdir of the same ``uploads`` Docker
    named volume as trouble-ticket screenshots. The same root-owned-volume
    failure mode applies (the non-root ``appuser`` can't write), so this mirrors
    ``ensure_screenshot_storage`` exactly: create the dir and fail fast at boot
    with a clear RuntimeError rather than silently dropping uploads later.

    Called from the main.py lifespan on every real boot (not gated by the
    TESTING short-circuit). No DDL — a filesystem mkdir/writability check only.

    Called by: main.py lifespan (real boots), tests (directly)
    Depends on: app/routers/avatars.py (AVATARS_DIR)
    """
    from .routers.avatars import AVATARS_DIR

    path = Path(AVATARS_DIR)
    path.mkdir(parents=True, exist_ok=True)
    if not os.access(path, os.W_OK):
        raise RuntimeError(f"Avatar storage {path} is not writable by the app process")
    logger.info("Avatar storage ready and writable: {}", path)


def run_startup_migrations() -> None:
    """Execute the FAST, order-critical startup operations (pre-yield, main.py
    lifespan).

    P2.7 (docs/CODE_AUDIT_AND_HARDENING_PLAN.md) split the ~20 ops that used to run here
    sequentially into FAST (kept here, blocking /health) and SLOW (moved to
    ``run_deferred_startup_backfills``, launched as a post-yield background task so a
    prod-sized DB can no longer make /health miss the compose healthcheck / deploy.sh
    wait-loop budget). Classification below is by measured/reasoned cost, not just
    order-criticality — every FAST op is either a fixed-size seed/reconcile or a
    single-row check; every SLOW op is a full-table-shaped scan, chunked backfill, or
    ANALYZE.

    | Operation                                    | Class | Why                                                        |
    |-----------------------------------------------|-------|-------------------------------------------------------------|
    | _create_fts_triggers                          | FAST  | CREATE OR REPLACE FUNCTION/TRIGGER only — no data scan       |
    | _seed_system_config                           | FAST  | 7-row INSERT ON CONFLICT DO NOTHING                          |
    | _reconcile_system_config                      | FAST  | 4-row UPDATE by key                                          |
    | _seed_manufacturers                           | FAST  | ~50-row INSERT ON CONFLICT DO NOTHING                        |
    | _seed_tag_threshold_config                     | FAST  | 6-row INSERT ON CONFLICT DO NOTHING                          |
    | _create_count_triggers                        | FAST  | CREATE OR REPLACE FUNCTION/TRIGGER only — no data scan       |
    | _reconcile_connector_active                    | FAST  | no-op by design                                              |
    | _verify_encryption_canary                     | FAST  | single-row decrypt; must fail fast before any encrypted read |
    | _create_default_user_if_env_set               | FAST  | single-row query; order-critical for password login          |
    | _seed_admin_user_if_env_set                    | FAST  | single-row query; order-critical for admin login              |
    | _seed_agent_user                              | FAST  | single-row query/update                                      |
    | _seed_verification_group_from_admin_emails     | FAST  | bounded by len(ADMIN_EMAILS)                                 |
    | _seed_commodity_schemas                        | FAST  | bounded by the schema registry, not live-data size            |
    | _backfill_fts                                  | SLOW → deferred | full-table UPDATE on vendor_cards/material_cards       |
    | _seed_site_contacts                            | SLOW → deferred | one-time INSERT SELECT across customer_sites           |
    | _backfill_company_counts                       | SLOW → deferred | full-table correlated-subquery UPDATE                  |
    | legacy site_type / trouble_tickets UPDATEs     | SLOW → deferred | unindexed full-table predicate scans                   |
    | _analyze_hot_tables (via _maybe_analyze_hot_tables) | SLOW → deferred | ANALYZE on 3 hot tables; also since-last-deploy gated (item 3) |
    | _backfill_normalized_mpn                        | SLOW → deferred | chunked full-table backfill (requirements + material_cards) |
    | _backfill_sighting_offer_normalized_mpn         | SLOW → deferred | chunked full-table backfill (sightings + offers)        |
    | _backfill_sighting_vendor_normalized             | SLOW → deferred | chunked full-table backfill (sightings)                 |
    | _backfill_offer_vendor_normalized                | SLOW → deferred | chunked full-table backfill (offers)                    |
    | _backfill_proactive_offer_qty                    | SLOW → deferred | scans proactive_matches/requirements/proactive_offers   |
    | _backfill_ticket_defaults                        | SLOW → deferred | unbatched per-row ORM update over trouble_tickets       |
    | _backfill_material_cards                         | SLOW → deferred | per-row resolve_material_card() call over unlinked requirements |
    | _backfill_sweep_cooldown                         | SLOW → deferred | unbounded ORM query over prospect_accounts              |
    | _complete_reverted_active_plans                  | SLOW → deferred | per-candidate check_completion() business logic         |
    | _warn_non_canonical_categories                    | SLOW → deferred | full-table GROUP BY scan; pure observability            |

    Safe to call on every app boot.
    """
    # Fail-boot guard: password login is an auth bypass. On a real (non-TESTING)
    # boot it may run ONLY when the operator has explicitly acknowledged the risk
    # via ALLOW_PASSWORD_LOGIN_RISK=true (staging sets this). Otherwise refuse to
    # start so the bypass can never reach an unacknowledged environment. Read via
    # the runtime os.getenv helpers (not settings.*, which froze at import) and
    # keep the auth import function-local (avoids import-time capture and an
    # auth-router circular import).
    from .routers.auth import password_login_env_enabled, password_login_risk_acknowledged

    if password_login_env_enabled() and not os.getenv("TESTING"):
        if not password_login_risk_acknowledged():
            raise RuntimeError(
                "ENABLE_PASSWORD_LOGIN=true creates an authentication bypass and is "
                "refused at boot. Disable it, or set ALLOW_PASSWORD_LOGIN_RISK=true to "
                "acknowledge the risk (non-production environments only, e.g. staging)."
            )
        logger.critical(
            "ENABLE_PASSWORD_LOGIN is active in non-test mode with "
            "ALLOW_PASSWORD_LOGIN_RISK=true — authentication bypass acknowledged. "
            "Acceptable only on non-production environments."
        )

    if os.environ.get("TESTING"):
        logger.info("TESTING mode — skipping startup migrations")
        return

    with engine.connect() as conn:
        _create_fts_triggers(conn)
        _seed_system_config(conn)
        _reconcile_system_config(conn)
        _seed_manufacturers(conn)
        _seed_tag_threshold_config(conn)
        _create_count_triggers(conn)
        _reconcile_connector_active(conn)

    _verify_encryption_canary()
    if password_login_env_enabled():
        _create_default_user_if_env_set()
    _seed_admin_user_if_env_set()
    _seed_agent_user()
    _seed_verification_group_from_admin_emails()
    _seed_commodity_schemas()
    logger.info("Fast startup migrations complete")


# Tri-state readiness tracker for the P2.7 deferred backfill/ANALYZE phase. Defaults
# COMPLETED so a boot that never schedules the deferred phase (TESTING=1) doesn't need
# special-casing in readers; main.py flips it to RUNNING via
# mark_deferred_backfills_pending() right before scheduling run_deferred_startup_
# backfills as a background task, and the task flips it to COMPLETED or FAILED when
# it finishes (never leaves it stuck RUNNING on a crash).
deferred_backfills_state: str = DeferredBackfillState.COMPLETED


def mark_deferred_backfills_pending() -> None:
    """Flip the P2.7 readiness state to RUNNING before scheduling the deferred phase.

    Called by: main.py lifespan (real boots only, immediately before scheduling
    run_deferred_startup_backfills as a background task)
    Depends on: nothing
    """
    global deferred_backfills_state
    deferred_backfills_state = DeferredBackfillState.RUNNING


def is_deferred_backfills_ready() -> bool:
    """Live read of whether the P2.7 deferred-backfill phase completed successfully.

    Only True when the state is COMPLETED — a FAILED phase (crashed backfill) must
    never be reported as ready.

    Called by: GET /health/ready (app/main.py)
    Depends on: nothing
    """
    return deferred_backfills_state == DeferredBackfillState.COMPLETED


def get_deferred_backfills_state() -> str:
    """Live read of the P2.7 deferred-backfill tri-state (running/completed/failed).

    Called by: GET /health/ready (app/main.py)
    Depends on: nothing
    """
    return deferred_backfills_state


def run_deferred_startup_backfills() -> None:
    """Execute the SLOW, idempotent startup backfills + ANALYZE off the request path.

    Runs inside a background asyncio task launched right after main.py's lifespan
    yields (via ``asyncio.to_thread`` + ``safe_background_task``), so /health can
    answer immediately while a prod-sized DB is still being backfilled. See the
    classification table in ``run_startup_migrations``'s docstring for why each op
    below is here rather than in the fast pre-yield path.

    TESTING short-circuits identically to run_startup_migrations (main.py never
    schedules this function under TESTING=1 anyway; the guard here is defense-in-
    depth for tests that call it directly).

    Called by: main.py lifespan (background task, real boots only), tests (directly)
    Depends on: database.py (engine), the _backfill_*/_seed_site_contacts/
        _maybe_analyze_hot_tables helpers below
    """
    global deferred_backfills_state
    if os.environ.get("TESTING"):
        deferred_backfills_state = DeferredBackfillState.COMPLETED
        return

    try:
        with engine.connect() as conn:
            _backfill_fts(conn)
            _seed_site_contacts(conn)
            _backfill_company_counts(conn)
            # Normalize legacy site_type 'headquarters' → 'hq' (idempotent)
            _exec(conn, "UPDATE customer_sites SET site_type='hq' WHERE site_type='headquarters'")
            _exec(
                conn,
                "UPDATE trouble_tickets SET resolved_at = COALESCE(diagnosed_at, created_at) + INTERVAL '1 hour' "
                "WHERE status = 'resolved' AND resolved_at IS NULL",
            )
            _maybe_analyze_hot_tables(conn)

        _backfill_normalized_mpn()
        _backfill_sighting_offer_normalized_mpn()
        _backfill_sighting_vendor_normalized()
        _backfill_offer_vendor_normalized()
        _backfill_proactive_offer_qty()
        _backfill_ticket_defaults()
        _backfill_material_cards()
        _backfill_sweep_cooldown()
        _complete_reverted_active_plans()
        _warn_non_canonical_categories()
    except Exception:
        deferred_backfills_state = DeferredBackfillState.FAILED
        logger.exception("Deferred startup backfills failed")
        raise
    else:
        deferred_backfills_state = DeferredBackfillState.COMPLETED
        logger.info("Deferred startup backfills complete")


def _seed_verification_group_from_admin_emails() -> None:
    """Seed the ops verification group from ADMIN_EMAILS (idempotent).

    For each email in settings.admin_emails, if a matching user exists and has no
    VerificationGroupMember row, create one (is_active=True). Users that haven't logged
    in yet are skipped; an admin can add them via Settings > Ops Group once they exist.
    Ensures the group is non-empty so SO/PO verification and buy-plan completion are
    reachable out of the box. filter_by(...).first() + the UNIQUE(user_id) guard make
    this safe to run on every boot.
    """
    from .config import settings
    from .models import User
    from .models.buy_plan import VerificationGroupMember

    admin_emails = settings.admin_emails
    if not admin_emails:
        return

    db = SessionLocal()
    try:
        seeded = 0
        for email in admin_emails:
            user = db.query(User).filter_by(email=email).first()
            if not user:
                continue
            if db.query(VerificationGroupMember).filter_by(user_id=user.id).first():
                continue
            db.add(VerificationGroupMember(user_id=user.id, is_active=True))
            seeded += 1
        if seeded:
            db.commit()
            logger.info("Seeded {} ops verification group member(s) from ADMIN_EMAILS", seeded)
    except Exception:
        logger.exception("Failed seeding ops verification group members")
        db.rollback()
    finally:
        db.close()


def _create_default_user_if_env_set() -> None:
    """Create a default user from env vars if provided:
    DEFAULT_USER_EMAIL, DEFAULT_USER_PASSWORD, DEFAULT_USER_ROLE (optional).
    If the vars are missing, do nothing.
    """
    import base64
    import hashlib

    email = os.environ.get("DEFAULT_USER_EMAIL")
    password = os.environ.get("DEFAULT_USER_PASSWORD")
    # Least privilege: an unspecified role yields a buyer, never an admin.
    # Set DEFAULT_USER_ROLE explicitly to grant a higher tier (CRIT-SEC-2).
    role = os.environ.get("DEFAULT_USER_ROLE", "buyer")
    if not email or not password:
        return

    # Avoid importing heavy ORM until we need it
    from .models.auth import User

    db = SessionLocal()
    try:
        existing = db.query(User).filter_by(email=email).first()
        if existing:
            logger.info("Default user {} already exists, skipping creation", email)
            return

        # PBKDF2-HMAC-SHA256 with random salt
        salt = os.urandom(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        store = base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()

        user = User(email=email.lower(), name=email.split("@")[0], role=role, password_hash=store)
        db.add(user)
        db.commit()
        logger.info("Created default user {} with role {}", email, role)
    except Exception:
        logger.exception("Failed creating default user")
        db.rollback()
        raise
    finally:
        db.close()


def _seed_admin_user_if_env_set(db=None) -> None:
    """Seed admin user from SEED_ADMIN_EMAIL / SEED_ADMIN_NAME env vars.

    Called by: run_startup_migrations
    Depends on: User model, SessionLocal
    """
    email = os.environ.get("SEED_ADMIN_EMAIL")
    if not email:
        # No hard-coded default: seeding an admin into every fresh install without
        # the operator asking for it is an access-control decision the env must make.
        logger.debug("SEED_ADMIN_EMAIL not set — skipping admin seed")
        return
    name = os.environ.get("SEED_ADMIN_NAME", email.split("@")[0])

    from .models.auth import User

    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        existing = db.query(User).filter_by(email=email).first()
        if existing:
            logger.info("Admin user {} already exists, skipping", email)
            return
        user = User(email=email, name=name, role="admin")
        db.add(user)
        db.commit()
        logger.info("Created admin user {}", email)
    except Exception:
        logger.exception("Failed creating admin user {}", email)
        db.rollback()
        raise
    finally:
        if own_session:
            db.close()


def _seed_agent_user() -> None:
    """Seed the agent service account if it doesn't exist.

    Called by: run_startup_migrations
    Depends on: User model, UserRole enum
    """
    from .constants import UserRole
    from .models.auth import User

    db = SessionLocal()
    try:
        existing = db.query(User).filter_by(email="agent@availai.local").first()
        if existing:
            # Correct a legacy agent row seeded with an over-privileged role.
            # The agent is a non-interactive service account and must hold
            # only UserRole.AGENT — never admin or buyer (see dependencies.py).
            if existing.role != UserRole.AGENT:
                logger.warning(
                    "Demoting agent service account from role '{}' to '{}'",
                    existing.role,
                    UserRole.AGENT.value,
                )
                existing.role = UserRole.AGENT
                db.commit()
            return
        user = User(email="agent@availai.local", name="Agent", role=UserRole.AGENT, is_active=True)
        db.add(user)
        db.commit()
        logger.info("Seeded agent service account")
    except Exception:
        logger.exception("Failed seeding agent user")
        db.rollback()
        raise
    finally:
        db.close()


def _exec(conn, stmt: str, params: dict | None = None) -> None:
    """Execute a single SQL statement (data fix / runtime operation) with rollback on
    failure."""
    try:
        conn.execute(sqltext(stmt), params or {})
        conn.commit()
    except (SQLAlchemyError, DBAPIError) as e:
        logger.warning("DDL failed: {}", e)
        conn.rollback()


def _reconcile_connector_active(conn) -> None:
    """Boot-time connector reconciliation seam — intentionally leaves ``is_active``
    alone.

    ``ApiSource.status`` is the auto-managed health state (set by
    ``app/services/health_monitor.py``; ``'disabled'`` means "no connector
    available"); ``ApiSource.is_active`` is the *operator* toggle (set by
    ``PUT /api/sources/{id}/activate`` in ``app/routers/sources.py``). They are
    orthogonal concerns. A previous version coupled them here —
    ``UPDATE api_sources SET is_active = false WHERE status = 'disabled'`` — which
    silently wiped operator intent on every reboot (the boot-reset defect).

    Root cause: a source the operator turned on simply can't run while it has no
    connector, but its toggle must be *retained* so it resumes automatically once
    health recovers. So boot reconciliation must change ``is_active`` in neither
    direction; only an explicit operator action may flip it. The readers downstream
    (``app/routers/sources.py``, ``app/services/health_monitor.py``) all merely
    *filter* on ``is_active`` and never assume "disabled ⇒ inactive", so leaving
    the toggle intact is safe.

    Kept as a named, directly-testable seam (``run_startup_migrations`` short-
    circuits under ``TESTING``, so the regression test calls this helper directly).
    Takes ``conn`` for symmetry with its sibling startup steps and so any future
    reconciliation has the connection already wired.

    Called by: run_startup_migrations (real boots), tests (directly)
    Depends on: nothing — a no-op by design; the regression test pins the contract.
    """


# ── Full-text search triggers (PostgreSQL-specific) ─────────────────


def _create_fts_triggers(conn) -> None:
    """Create trigger functions and triggers for FTS on vendor_cards and
    material_cards."""
    _exec(
        conn,
        """
        CREATE OR REPLACE FUNCTION vendor_cards_fts_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', COALESCE(NEW.display_name, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.normalized_name, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.domain, '')), 'B') ||
                setweight(to_tsvector('english', COALESCE(NEW.industry, '')), 'C');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """,
    )

    _exec(
        conn,
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_vc_fts') THEN
                CREATE TRIGGER trg_vc_fts BEFORE INSERT OR UPDATE ON vendor_cards
                FOR EACH ROW EXECUTE FUNCTION vendor_cards_fts_update();
            END IF;
        END $$;
    """,
    )

    _exec(
        conn,
        """
        CREATE OR REPLACE FUNCTION material_cards_fts_update() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                setweight(to_tsvector('english', COALESCE(NEW.display_mpn, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.normalized_mpn, '')), 'A') ||
                setweight(to_tsvector('english', COALESCE(NEW.manufacturer, '')), 'B') ||
                setweight(to_tsvector('english', COALESCE(NEW.description, '')), 'C') ||
                setweight(to_tsvector('english', COALESCE(NEW.category, '')), 'C');
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """,
    )

    _exec(
        conn,
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mc_fts') THEN
                CREATE TRIGGER trg_mc_fts BEFORE INSERT OR UPDATE ON material_cards
                FOR EACH ROW EXECUTE FUNCTION material_cards_fts_update();
            END IF;
        END $$;
    """,
    )


# ── One-time FTS backfill ────────────────────────────────────────────


def _backfill_fts(conn) -> None:
    """Backfill search_vector on existing rows where NULL."""
    _exec(
        conn,
        """
        UPDATE vendor_cards SET search_vector =
            setweight(to_tsvector('english', COALESCE(display_name, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(normalized_name, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(domain, '')), 'B') ||
            setweight(to_tsvector('english', COALESCE(industry, '')), 'C')
        WHERE search_vector IS NULL
    """,
    )

    _exec(
        conn,
        """
        UPDATE material_cards SET search_vector =
            setweight(to_tsvector('english', COALESCE(display_mpn, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(normalized_mpn, '')), 'A') ||
            setweight(to_tsvector('english', COALESCE(manufacturer, '')), 'B') ||
            setweight(to_tsvector('english', COALESCE(description, '')), 'C') ||
            setweight(to_tsvector('english', COALESCE(category, '')), 'C')
        WHERE search_vector IS NULL
    """,
    )


# ── Seed data ────────────────────────────────────────────────────────


def _seed_system_config(conn) -> None:
    """Seed default feature flags (INSERT ON CONFLICT DO NOTHING)."""
    seeds = [
        ("inbox_scan_interval_min", "30", "Minutes between inbox scan cycles"),
        ("email_mining_enabled", "false", "Enable email mining background job"),
        ("proactive_matching_enabled", "true", "Enable proactive offer matching"),
        ("activity_tracking_enabled", "true", "Enable CRM activity tracking"),
        # Prepayment-notification recipients — empty by default (channel skipped until set).
        ("accounting_group_email", "", "Accounting group email for prepayment notifications"),
        ("ap_group_email", "", "AP group email for prepayment notifications"),
        ("prepayment_teams_webhook", "", "Teams incoming-webhook URL for prepayment cards"),
    ]
    for key, value, desc in seeds:
        _exec(
            conn,
            """INSERT INTO system_config (key, value, description)
            VALUES (:key, :value, :desc)
            ON CONFLICT (key) DO NOTHING""",
            {"key": key, "value": value, "desc": desc},
        )


def _reconcile_system_config(conn) -> None:
    """No-surprise cutover: mirror the current env value into each flag's DB row.

    Task 10 makes the system_config DB row authoritative for the 4 feature flags (the
    UI toggle becomes real). To avoid behaviour flipping at the cutover deploy, point
    each never-admin-edited row (``updated_by IS NULL``) at the value the background
    jobs read today — the env-backed Pydantic setting. Rows an admin has deliberately
    set (``updated_by IS NOT NULL``) are never touched. Idempotent: re-running rewrites
    the same value. Runtime data op only — no DDL.
    """
    from .config import settings

    # Serialize to the seed's string format so the resolver parses it identically.
    env_values = {
        "inbox_scan_interval_min": str(int(settings.inbox_scan_interval_min)),
        "email_mining_enabled": "true" if settings.email_mining_enabled else "false",
        "proactive_matching_enabled": "true" if settings.proactive_matching_enabled else "false",
        "activity_tracking_enabled": "true" if settings.activity_tracking_enabled else "false",
    }
    for key, value in env_values.items():
        _exec(
            conn,
            """UPDATE system_config
            SET value = :value
            WHERE key = :key AND updated_by IS NULL""",
            {"key": key, "value": value},
        )


def _seed_manufacturers(conn) -> None:
    """Seed manufacturer lookup table (INSERT ON CONFLICT DO NOTHING).

    Called by: run_startup_migrations
    Depends on: manufacturers table
    """
    import json

    seeds = [
        ("Texas Instruments", ["TI", "Texas Inst", "Texas Instruments (TI)"]),
        ("Analog Devices", ["ADI", "Analog"]),
        ("Microchip Technology", ["Microchip", "MCHP"]),
        ("STMicroelectronics", ["ST", "STMicro"]),
        ("NXP Semiconductors", ["NXP", "Freescale"]),
        ("ON Semiconductor", ["ON Semi", "onsemi"]),
        ("Infineon Technologies", ["Infineon", "IFX"]),
        ("Renesas Electronics", ["Renesas"]),
        ("Vishay Intertechnology", ["Vishay"]),
        ("Murata Manufacturing", ["Murata"]),
        ("TDK Corporation", ["TDK"]),
        ("Samsung Electronics", ["Samsung"]),
        ("SK Hynix", ["Hynix"]),
        ("Micron Technology", ["Micron"]),
        ("Intel Corporation", ["Intel"]),
        ("AMD", ["Advanced Micro Devices"]),
        ("Broadcom Inc.", ["Broadcom", "Avago"]),
        ("Qualcomm", ["QCOM"]),
        ("NVIDIA", []),
        ("Xilinx", ["AMD/Xilinx"]),
        ("Lattice Semiconductor", ["Lattice"]),
        ("Maxim Integrated", ["Maxim", "ADI/Maxim"]),
        ("TE Connectivity", ["TE", "Tyco"]),
        ("Amphenol", []),
        ("Molex", []),
        ("Wurth Elektronik", ["Wurth", "Wuerth"]),
        ("KEMET", ["Yageo/KEMET"]),
        ("Yageo Corporation", ["Yageo"]),
        ("AVX Corporation", ["AVX", "Kyocera/AVX"]),
        ("Panasonic", []),
        ("Rohm Semiconductor", ["Rohm", "ROHM"]),
        ("Diodes Incorporated", ["Diodes Inc"]),
        ("Nexperia", []),
        ("Toshiba Electronic Devices", ["Toshiba"]),
        ("Cypress Semiconductor", ["Cypress", "Infineon/Cypress"]),
        ("Silicon Labs", ["SiLabs", "Silicon Laboratories"]),
        ("Allegro MicroSystems", ["Allegro"]),
        ("Sensata Technologies", ["Sensata"]),
        ("Littelfuse", []),
        ("Bourns", []),
        ("CUI Devices", ["CUI"]),
        ("MEAN WELL", ["MeanWell"]),
        ("Winbond Electronics", ["Winbond"]),
        ("ISSI", ["Integrated Silicon Solution"]),
        ("Alliance Memory", []),
        ("Seagate Technology", ["Seagate"]),
        ("Western Digital", ["WD"]),
        ("IBM", []),
        # Canonical "HPE" (brand canonicalization, migration 106 for existing DBs): the
        # live catalog's HPE family was split four ways (Hewlett Packard Enterprise /
        # HP / HPE / HEWLETT PACKARD) — the short canonical folds them into one facet
        # slot; the long form is now an alias.
        ("HPE", ["Hewlett Packard Enterprise", "HP", "Hewlett Packard", "Hewlett-Packard"]),
        ("Dell Technologies", ["Dell"]),
        # Dual-brand normalization seeds (SPEC_DUAL_BRAND_FILTERS §2): canonical homes
        # for the brand/maker names normalize_brand_name resolves. "Toshiba" the
        # canonical deliberately coexists with the "Toshiba" alias of "Toshiba
        # Electronic Devices" above — canonical names win the lookup-map collision.
        ("Lenovo", []),
        ("Toshiba", []),
        ("Hitachi", []),
        ("Maxtor", []),
        ("Fujitsu", []),
        ("Quantum", []),
        ("SanDisk", []),
        ("Kingston Technology", ["Kingston"]),
    ]
    for canonical_name, aliases in seeds:
        _exec(
            conn,
            """INSERT INTO manufacturers (canonical_name, aliases)
            VALUES (:name, :aliases)
            ON CONFLICT (canonical_name) DO NOTHING""",
            {"name": canonical_name, "aliases": json.dumps(aliases)},
        )


# Default tag-visibility thresholds — (entity_type, tag_type, min_count, min_percentage).
# Canonical values mirror alembic migrations 042 (vendor_card/customer_site) + 046 (company).
# Keep this list in lockstep with those migrations; the two-gate visibility system
# (recalculate_entity_tag_visibility) treats a MISSING row as "never visible", so an
# empty tag_threshold_config silently suppresses every AI brand/commodity tag.
TAG_THRESHOLD_SEEDS = [
    ("vendor_card", "brand", 2, 0.05),
    ("vendor_card", "commodity", 3, 0.05),
    ("customer_site", "brand", 3, 0.05),
    ("customer_site", "commodity", 3, 0.05),
    ("company", "brand", 2, 0.05),
    ("company", "commodity", 3, 0.05),
]


def _seed_tag_threshold_config(conn) -> None:
    """Seed the default tag-visibility thresholds (INSERT ON CONFLICT DO NOTHING).

    The rows are seeded by migrations 042/046, but a DB materialized outside the
    incremental chain (``Base.metadata.create_all`` + ``alembic stamp``, or a restore
    from the 001 schema-only baseline) has the ``tag_threshold_config`` table present
    yet unseeded — the data-only ``bulk_insert`` in 042/046 never runs. When the table
    is empty, ``tagging.recalculate_entity_tag_visibility`` marks EVERY AI brand/
    commodity EntityTag ``is_visible=False`` (no threshold row → fail the gate), so the
    whole tag-visibility feature silently does nothing. This idempotent boot-time seed
    self-heals such a DB on the next deploy without touching a correctly-seeded one.

    Called by: run_startup_migrations
    Depends on: tag_threshold_config table (uq_threshold_entity_tag on entity_type,tag_type)
    """
    for entity_type, tag_type, min_count, min_percentage in TAG_THRESHOLD_SEEDS:
        _exec(
            conn,
            """INSERT INTO tag_threshold_config (entity_type, tag_type, min_count, min_percentage)
            VALUES (:entity_type, :tag_type, :min_count, :min_percentage)
            ON CONFLICT (entity_type, tag_type) DO NOTHING""",
            {
                "entity_type": entity_type,
                "tag_type": tag_type,
                "min_count": min_count,
                "min_percentage": min_percentage,
            },
        )


def _seed_site_contacts(conn) -> None:
    """One-time: copy contact_name/email/phone/title from customer_sites into site_contacts."""
    try:
        row = conn.execute(sqltext("SELECT COUNT(*) FROM site_contacts")).scalar()
        if row and row > 0:
            return  # already seeded
        conn.execute(
            sqltext("""
            INSERT INTO site_contacts (customer_site_id, full_name, title, email, phone, is_primary)
            SELECT id, contact_name, contact_title, contact_email, contact_phone, TRUE
            FROM customer_sites
            WHERE contact_name IS NOT NULL AND contact_name != ''
        """)
        )
        conn.commit()
        logger.info("Seeded site_contacts from existing customer_sites data")
    except (SQLAlchemyError, DBAPIError) as e:
        logger.warning("Seed site_contacts failed: {}", e)
        conn.rollback()


# ── One-time backfills ───────────────────────────────────────────────


_BACKFILL_BATCH_SIZE = 500


def _verify_encryption_canary() -> None:
    """Fail loudly at boot if the live ENCRYPTION_SALT/SECRET_KEY can't decrypt stored
    data.

    A wrong salt would otherwise silently empty every encrypted credential app-wide. See
    app/utils/encrypted_type.py::verify_encryption_canary.
    """
    from app.database import SessionLocal

    from .utils.encrypted_type import verify_encryption_canary

    db = SessionLocal()
    try:
        verify_encryption_canary(db)
    finally:
        db.close()


def _backfill_normalized_mpn() -> None:
    """One-time backfill: populate requirements.normalized_mpn and re-normalize material_cards."""
    with engine.connect() as conn:
        # 1. Backfill requirements.normalized_mpn where NULL — chunked batch writes
        try:
            offset = 0
            total_reqs = 0
            while True:
                rows = conn.execute(
                    sqltext(
                        "SELECT id, primary_mpn FROM requirements"
                        " WHERE normalized_mpn IS NULL AND primary_mpn IS NOT NULL"
                        " LIMIT :lim OFFSET :off"
                    ),
                    {"lim": _BACKFILL_BATCH_SIZE, "off": offset},
                ).fetchall()
                if not rows:
                    break
                batch = [{"nk": nk, "id": r[0]} for r in rows if (nk := _norm_key(r[1]))]
                if batch:
                    conn.execute(
                        sqltext("UPDATE requirements SET normalized_mpn = :nk WHERE id = :id"),
                        batch,
                    )
                    conn.commit()
                    total_reqs += len(batch)
                offset += len(rows)
                if len(rows) < _BACKFILL_BATCH_SIZE:
                    break
            if total_reqs:
                logger.info("Backfilled normalized_mpn on {} requirements", total_reqs)
        except (SQLAlchemyError, DBAPIError) as e:
            logger.warning("Backfill requirements.normalized_mpn failed: {}", e)
            conn.rollback()

        # 2. Backfill material_cards.normalized_mpn where NULL only (skip full re-scan)
        # Compute all candidates in Python, find collisions via one GROUP BY query, then batch update.
        try:
            cards = conn.execute(
                sqltext(
                    "SELECT id, display_mpn FROM material_cards WHERE normalized_mpn IS NULL AND display_mpn IS NOT NULL"
                )
            ).fetchall()
            # Build candidate map: normalized_mpn -> list of (id, norm)
            candidates: dict = {}
            for c in cards:
                new_norm = _norm_key(c[1])
                if new_norm:
                    candidates.setdefault(new_norm, []).append(c[0])
            # Find normalized_mpn values already in the table
            existing_norms = set()
            if candidates:
                existing_rows = conn.execute(
                    sqltext(
                        "SELECT normalized_mpn FROM material_cards"
                        " WHERE normalized_mpn IS NOT NULL"
                        " GROUP BY normalized_mpn"
                    )
                ).fetchall()
                existing_norms = {r[0] for r in existing_rows}
            batch = []
            total_updated = 0
            for norm, ids in candidates.items():
                if norm in existing_norms:
                    continue  # collision with existing row
                # Only take the first id if multiple NULL cards map to the same norm
                batch.append({"n": norm, "id": ids[0]})
                existing_norms.add(norm)
                if len(batch) >= _BACKFILL_BATCH_SIZE:
                    conn.execute(
                        sqltext("UPDATE material_cards SET normalized_mpn = :n WHERE id = :id"),
                        batch,
                    )
                    conn.commit()
                    total_updated += len(batch)
                    batch = []
            if batch:
                conn.execute(
                    sqltext("UPDATE material_cards SET normalized_mpn = :n WHERE id = :id"),
                    batch,
                )
                conn.commit()
                total_updated += len(batch)
            if total_updated:
                logger.info("Backfilled normalized_mpn on {} material_cards", total_updated)
        except (SQLAlchemyError, DBAPIError) as e:
            logger.warning("Backfill material_cards.normalized_mpn failed: {}", e)
            conn.rollback()


def _backfill_sighting_offer_normalized_mpn() -> None:
    """One-time backfill: populate sightings.normalized_mpn and offers.normalized_mpn."""
    with engine.connect() as conn:
        # Sightings: compute from mpn_matched — chunked batch writes
        try:
            offset = 0
            total_sightings = 0
            while True:
                rows = conn.execute(
                    sqltext(
                        "SELECT id, mpn_matched FROM sightings"
                        " WHERE normalized_mpn IS NULL AND mpn_matched IS NOT NULL"
                        " LIMIT :lim OFFSET :off"
                    ),
                    {"lim": _BACKFILL_BATCH_SIZE, "off": offset},
                ).fetchall()
                if not rows:
                    break
                batch = [{"nk": nk, "id": r[0]} for r in rows if (nk := _norm_key(r[1]))]
                if batch:
                    conn.execute(
                        sqltext("UPDATE sightings SET normalized_mpn = :nk WHERE id = :id"),
                        batch,
                    )
                    conn.commit()
                    total_sightings += len(batch)
                offset += len(rows)
                if len(rows) < _BACKFILL_BATCH_SIZE:
                    break
            if total_sightings:
                logger.info("Backfilled normalized_mpn on {} sightings", total_sightings)
        except (SQLAlchemyError, DBAPIError) as e:
            logger.warning("Backfill sightings.normalized_mpn failed: {}", e)
            conn.rollback()

        # Offers: compute from mpn — chunked batch writes
        try:
            offset = 0
            total_offers = 0
            while True:
                rows = conn.execute(
                    sqltext(
                        "SELECT id, mpn FROM offers"
                        " WHERE normalized_mpn IS NULL AND mpn IS NOT NULL"
                        " LIMIT :lim OFFSET :off"
                    ),
                    {"lim": _BACKFILL_BATCH_SIZE, "off": offset},
                ).fetchall()
                if not rows:
                    break
                batch = [{"nk": nk, "id": r[0]} for r in rows if (nk := _norm_key(r[1]))]
                if batch:
                    conn.execute(
                        sqltext("UPDATE offers SET normalized_mpn = :nk WHERE id = :id"),
                        batch,
                    )
                    conn.commit()
                    total_offers += len(batch)
                offset += len(rows)
                if len(rows) < _BACKFILL_BATCH_SIZE:
                    break
            if total_offers:
                logger.info("Backfilled normalized_mpn on {} offers", total_offers)
        except (SQLAlchemyError, DBAPIError) as e:
            logger.warning("Backfill offers.normalized_mpn failed: {}", e)
            conn.rollback()


def _backfill_sighting_vendor_normalized() -> None:
    """Backfill sightings.vendor_name_normalized from vendor_name until none remain."""
    from .vendor_utils import normalize_vendor_name

    with engine.connect() as conn:
        # Check column exists first
        try:
            conn.execute(sqltext("SELECT vendor_name_normalized FROM sightings LIMIT 0"))
        except (SQLAlchemyError, DBAPIError):
            conn.rollback()
            return  # Column not yet created

        total = 0
        last_id = 0
        while True:
            try:
                rows = conn.execute(
                    sqltext(
                        "SELECT id, vendor_name FROM sightings "
                        "WHERE vendor_name_normalized IS NULL AND vendor_name IS NOT NULL "
                        "AND id > :last_id ORDER BY id LIMIT :lim"
                    ),
                    {"last_id": last_id, "lim": _BACKFILL_BATCH_SIZE},
                ).fetchall()
                if not rows:
                    break
                # Advance the cursor past every row we examined. Rows whose vendor_name
                # normalizes to '' (e.g. "LLC", "Inc.") never get an UPDATE, so filtering
                # only on "IS NULL" would re-select them forever and hang startup; the
                # id cursor skips them instead of looping on them.
                last_id = rows[-1][0]
                batch = []
                for r in rows:
                    nv = normalize_vendor_name(r[1])
                    if nv:
                        batch.append({"nv": nv, "id": r[0]})
                if batch:
                    conn.execute(
                        sqltext("UPDATE sightings SET vendor_name_normalized = :nv WHERE id = :id"),
                        batch,
                    )
                    conn.commit()
                total += len(batch)
            except Exception as e:
                logger.warning("Backfill sightings.vendor_name_normalized failed: {}", e)
                conn.rollback()
                break
        if total:
            logger.info("Backfilled vendor_name_normalized on {} sightings", total)


def _backfill_offer_vendor_normalized() -> None:
    """Backfill offers.vendor_name_normalized from vendor_name until none remain.

    The vendor detail offers tab filters Offer.vendor_name_normalized == normalized_name
    (aligned with sightings/leads). Every offer write-path already populates the column,
    but a legacy row created before that was universal would be NULL and silently hidden
    by the tab; this idempotent backfill closes that gap.
    """
    from .vendor_utils import normalize_vendor_name

    with engine.connect() as conn:
        # Check column exists first
        try:
            conn.execute(sqltext("SELECT vendor_name_normalized FROM offers LIMIT 0"))
        except (SQLAlchemyError, DBAPIError):
            conn.rollback()
            return  # Column not yet created

        total = 0
        last_id = 0
        while True:
            try:
                rows = conn.execute(
                    sqltext(
                        "SELECT id, vendor_name FROM offers "
                        "WHERE vendor_name_normalized IS NULL AND vendor_name IS NOT NULL "
                        "AND id > :last_id ORDER BY id LIMIT :lim"
                    ),
                    {"last_id": last_id, "lim": _BACKFILL_BATCH_SIZE},
                ).fetchall()
                if not rows:
                    break
                # Advance the cursor past every row we examined. Rows whose vendor_name
                # normalizes to '' (e.g. "LLC", "Inc.") never get an UPDATE, so filtering
                # only on "IS NULL" would re-select them forever and hang startup; the
                # id cursor skips them instead of looping on them.
                last_id = rows[-1][0]
                batch = []
                for r in rows:
                    nv = normalize_vendor_name(r[1])
                    if nv:
                        batch.append({"nv": nv, "id": r[0]})
                if batch:
                    conn.execute(
                        sqltext("UPDATE offers SET vendor_name_normalized = :nv WHERE id = :id"),
                        batch,
                    )
                    conn.commit()
                total += len(batch)
            except Exception as e:
                logger.warning("Backfill offers.vendor_name_normalized failed: {}", e)
                conn.rollback()
                break
        if total:
            logger.info("Backfilled vendor_name_normalized on {} offers", total)


# ── Denormalized company count triggers ──────────────────────────────


def _create_count_triggers(conn) -> None:
    """Create triggers to keep companies.site_count and open_req_count in sync.

    NOTE: Status strings in PL/pgSQL below must stay in sync with RequisitionStatus in app/constants.py.
    """
    # Trigger function: update site_count when customer_sites change
    _exec(
        conn,
        """
        CREATE OR REPLACE FUNCTION trg_update_company_site_count() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' OR TG_OP = 'UPDATE' THEN
                UPDATE companies SET site_count = (
                    SELECT COUNT(*) FROM customer_sites
                    WHERE company_id = OLD.company_id AND is_active = TRUE
                ) WHERE id = OLD.company_id;
            END IF;
            IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
                UPDATE companies SET site_count = (
                    SELECT COUNT(*) FROM customer_sites
                    WHERE company_id = NEW.company_id AND is_active = TRUE
                ) WHERE id = NEW.company_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """,
    )

    _exec(
        conn,
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cs_site_count') THEN
                CREATE TRIGGER trg_cs_site_count AFTER INSERT OR UPDATE OR DELETE ON customer_sites
                FOR EACH ROW EXECUTE FUNCTION trg_update_company_site_count();
            END IF;
        END $$;
    """,
    )

    # Trigger function: update open_req_count when requisitions change
    _exec(
        conn,
        """
        CREATE OR REPLACE FUNCTION trg_update_company_req_count() RETURNS trigger AS $$
        DECLARE
            v_company_id INTEGER;
        BEGIN
            IF TG_OP = 'DELETE' OR TG_OP = 'UPDATE' THEN
                SELECT cs.company_id INTO v_company_id
                FROM customer_sites cs WHERE cs.id = OLD.customer_site_id;
                IF v_company_id IS NOT NULL THEN
                    UPDATE companies SET open_req_count = (
                        SELECT COUNT(*) FROM requisitions r
                        JOIN customer_sites cs2 ON r.customer_site_id = cs2.id
                        WHERE cs2.company_id = v_company_id
                          AND r.status NOT IN ('won', 'lost', 'cancelled')
                    ) WHERE id = v_company_id;
                END IF;
            END IF;
            IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
                SELECT cs.company_id INTO v_company_id
                FROM customer_sites cs WHERE cs.id = NEW.customer_site_id;
                IF v_company_id IS NOT NULL THEN
                    UPDATE companies SET open_req_count = (
                        SELECT COUNT(*) FROM requisitions r
                        JOIN customer_sites cs2 ON r.customer_site_id = cs2.id
                        WHERE cs2.company_id = v_company_id
                          AND r.status NOT IN ('won', 'lost', 'cancelled')
                    ) WHERE id = v_company_id;
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """,
    )

    _exec(
        conn,
        """
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_req_count') THEN
                CREATE TRIGGER trg_req_count AFTER INSERT OR UPDATE OR DELETE ON requisitions
                FOR EACH ROW EXECUTE FUNCTION trg_update_company_req_count();
            END IF;
        END $$;
    """,
    )


def _backfill_company_counts(conn) -> None:
    """Idempotent backfill of site_count and open_req_count on companies."""
    _exec(
        conn,
        """
        UPDATE companies c SET site_count = (
            SELECT COUNT(*) FROM customer_sites cs
            WHERE cs.company_id = c.id AND cs.is_active = TRUE
        )
        WHERE c.site_count != (
            SELECT COUNT(*) FROM customer_sites cs
            WHERE cs.company_id = c.id AND cs.is_active = TRUE
        )
    """,
    )
    _exec(
        conn,
        """
        UPDATE companies c SET open_req_count = (
            SELECT COUNT(*) FROM requisitions r
            JOIN customer_sites cs ON r.customer_site_id = cs.id
            WHERE cs.company_id = c.id
              AND r.status NOT IN ('won', 'lost', 'cancelled')
        )
        WHERE c.open_req_count != (
            SELECT COUNT(*) FROM requisitions r
            JOIN customer_sites cs ON r.customer_site_id = cs.id
            WHERE cs.company_id = c.id
              AND r.status NOT IN ('won', 'lost', 'cancelled')
        )
    """,
    )


def _analyze_hot_tables(conn) -> None:
    """Run ANALYZE on hot tables to keep pg_stat estimates fresh."""
    for tbl in ("companies", "customer_sites", "requisitions"):
        _exec(conn, "ANALYZE " + tbl)


_ANALYZE_MARKER_KEY = "startup_last_analyze_build"


def _maybe_analyze_hot_tables(conn) -> None:
    """Gate _analyze_hot_tables behind a since-last-deploy marker in system_config (P2.7
    item 3).

    ANALYZE on prod-sized hot tables is exactly the kind of full-table-adjacent scan
    a plain container restart on an UNCHANGED image shouldn't repeat every boot. The
    marker is keyed to BUILD_COMMIT — the same tag GET /health reports and
    deploy.sh verifies after a rebuild — so a genuinely new deploy (new BUILD_COMMIT)
    re-runs it once, a same-image restart skips it, and an operator can force a
    re-run by deleting the ``startup_last_analyze_build`` system_config row.
    """
    current_build = os.environ.get("BUILD_COMMIT", "unknown")
    try:
        row = conn.execute(
            sqltext("SELECT value FROM system_config WHERE key = :k"),
            {"k": _ANALYZE_MARKER_KEY},
        ).fetchone()
    except (SQLAlchemyError, DBAPIError) as e:
        logger.warning("ANALYZE marker read failed: {}", e)
        conn.rollback()
        row = None

    if row and row[0] == current_build:
        logger.debug("Skipping ANALYZE hot tables — already run for build {}", current_build)
        return

    _analyze_hot_tables(conn)
    _exec(
        conn,
        """INSERT INTO system_config (key, value, description)
        VALUES (:k, :v, 'Last BUILD_COMMIT ANALYZE ran for (P2.7 since-last-deploy gate)')
        ON CONFLICT (key) DO UPDATE SET value = :v""",
        {"k": _ANALYZE_MARKER_KEY, "v": current_build},
    )


def _backfill_proactive_offer_qty() -> None:
    """Fix proactive offer totals: use customer target_qty instead of vendor qty_available.

    Bug: send_proactive_offer() was using offer.qty_available (vendor's entire stock)
    to calculate total_sell/total_cost. The correct qty is min(qty_available, target_qty).
    This backfill recalculates totals and fixes line_items[].qty for all affected offers.

    Called by: run_startup_migrations
    Depends on: proactive_offers, proactive_matches, requirements tables
    """
    import json
    from decimal import Decimal

    with engine.connect() as conn:
        try:
            # Pre-load match_id -> target_qty map
            match_rows = conn.execute(
                sqltext("""
                    SELECT pm.id, r.target_qty
                    FROM proactive_matches pm
                    JOIN requirements r ON r.id = pm.requirement_id
                    WHERE r.target_qty IS NOT NULL AND r.target_qty > 0
                """)
            ).fetchall()
            target_map = {r[0]: r[1] for r in match_rows}

            if not target_map:
                return

            offers = conn.execute(
                sqltext("SELECT id, line_items FROM proactive_offers WHERE line_items IS NOT NULL")
            ).fetchall()

            fixed = 0
            for offer_id, raw_items in offers:
                if not raw_items:
                    continue
                items = raw_items if isinstance(raw_items, list) else json.loads(raw_items)

                new_items = []
                total_sell = Decimal("0")
                total_cost = Decimal("0")
                changed = False

                for item in items:
                    match_id = item.get("match_id")
                    old_qty = item.get("qty", 0)
                    target_qty = target_map.get(match_id)

                    new_qty = min(old_qty, target_qty) if target_qty else old_qty
                    if new_qty != old_qty:
                        changed = True

                    item["qty"] = new_qty
                    new_items.append(item)

                    try:
                        sell_price = float(item.get("sell_price") or item.get("unit_price", 0))
                        cost_price = float(item.get("unit_price", 0))
                    except (ValueError, TypeError):
                        sell_price = 0.0
                        cost_price = 0.0
                    total_sell += Decimal(str(sell_price)) * new_qty
                    total_cost += Decimal(str(cost_price)) * new_qty

                if changed:
                    conn.execute(
                        sqltext(
                            "UPDATE proactive_offers "
                            "SET line_items = :items, total_sell = :sell, total_cost = :cost "
                            "WHERE id = :id"
                        ),
                        {
                            "items": json.dumps(new_items),
                            "sell": float(total_sell),
                            "cost": float(total_cost),
                            "id": offer_id,
                        },
                    )
                    fixed += 1

            if fixed:
                conn.commit()
                logger.info("Fixed proactive offer quantities on {} offers", fixed)
        except Exception as e:
            logger.warning("Backfill proactive offer qty failed: {}", e)
            conn.rollback()


def _seed_commodity_schemas() -> None:
    """Seed commodity_spec_schemas table at startup. Idempotent.

    Called by: run_startup_migrations
    Depends on: commodity_registry, SessionLocal
    """
    from .services.commodity_registry import reseed_changed_schemas, seed_commodity_schemas

    db = SessionLocal()
    try:
        seed_commodity_schemas(db)
        # Reconcile rows whose seed definition drifted (the inserter never updates existing
        # rows). Idempotent — a no-op (single SELECT) when the DB already matches the seed.
        reseed_changed_schemas(db)
    except Exception:
        logger.exception("Failed seeding commodity schemas")
        db.rollback()
        raise
    finally:
        db.close()


def _warn_non_canonical_categories(db=None) -> None:
    """Surface material_cards rows whose category no commodity filter can bucket.

    Observability only — never mutates. The faceted sidebar matches
    ``lower(trim(category))`` against canonical COMMODITY_TREE keys, so any card whose
    category falls outside both the canonical keys and the alias cut line defined by
    migration 093 silently vanishes from commodity browsing. That residue is logged on
    every boot (count + worst offenders) so it stays a visible number instead of a
    "why doesn't this part show under any commodity?" archaeology question — and so a
    new vendor taxonomy string surfaces as a CATEGORY_ALIASES + backfill TODO.

    Called by: run_startup_migrations
    Depends on: material_cards, commodity_registry.get_all_commodities
    """
    from .services.commodity_registry import get_all_commodities

    canonical = set(get_all_commodities())
    session = db if db is not None else SessionLocal()
    try:
        rows = session.execute(
            sqltext(
                "SELECT LOWER(TRIM(category)) AS cat, COUNT(*) AS n FROM material_cards "
                "WHERE category IS NOT NULL AND TRIM(category) != '' "
                "GROUP BY LOWER(TRIM(category))"
            )
        ).fetchall()
    except SQLAlchemyError:
        logger.exception("Non-canonical category residue check failed")
        return
    finally:
        if db is None:
            session.close()
    residue = {cat: n for cat, n in rows if cat not in canonical}
    if residue:
        top = dict(sorted(residue.items(), key=lambda kv: (-kv[1], kv[0]))[:10])
        logger.warning(
            "materials: {} material_cards across {} non-canonical categories are invisible to "
            "commodity filters — top: {} (extend category_normalizer.CATEGORY_ALIASES and ship "
            "a backfill; migration 093 defined the current cut line)",
            sum(residue.values()),
            len(residue),
            top,
        )


def _backfill_material_cards() -> None:
    """Ensure every requirement MPN (primary + substitutes) has a material card.

    Idempotent: resolve_material_card finds existing or creates new.
    Also links requirements to their primary card if not yet linked.

    Called by: run_startup_migrations
    Depends on: Requirement model, resolve_material_card, SessionLocal
    """
    from .models.sourcing import Requirement
    from .search_service import resolve_material_card

    db = SessionLocal()
    try:
        reqs = (
            db.query(Requirement)
            .filter(Requirement.primary_mpn.isnot(None), Requirement.material_card_id.is_(None))
            .all()
        )
        if not reqs:
            return
        linked = 0
        for r in reqs:
            card = resolve_material_card(r.primary_mpn, db, manufacturer=r.manufacturer or "")
            if card:
                r.material_card_id = card.id
                linked += 1
            for sub in r.substitutes or []:  # type: ignore[union-attr]  # JSON column is a list at instance level
                sub_mpn = sub.get("mpn") if isinstance(sub, dict) else sub
                if sub_mpn:
                    resolve_material_card(sub_mpn, db)
        db.commit()
        if linked:
            logger.info("Backfilled {} requirement→material_card links", linked)
    except Exception:
        logger.exception("Failed backfilling material cards")
        db.rollback()
    finally:
        db.close()


def _backfill_ticket_defaults() -> None:
    """Backfill tickets with null risk_tier/category (report_button source).

    Sets default values so they appear in stats breakdowns.
    Idempotent: only updates rows where both fields are NULL.

    Called by: run_startup_migrations
    Depends on: TroubleTicket model, SessionLocal
    """
    from .models.trouble_ticket import TroubleTicket

    db = SessionLocal()
    try:
        null_tickets = (
            db.query(TroubleTicket)
            .filter(
                TroubleTicket.risk_tier.is_(None),
                TroubleTicket.category.is_(None),
            )
            .all()
        )
        if null_tickets:
            for t in null_tickets:
                t.risk_tier = "low"
                t.category = "other"
            db.commit()
            logger.info("Backfilled {} tickets with default risk_tier/category", len(null_tickets))
    except Exception:
        logger.exception("Failed backfilling null ticket fields")
        db.rollback()
    finally:
        db.close()


def _backfill_sweep_cooldown() -> None:
    """Backfill reclaim_blocked_until for swept rows that are missing it.

    Closes the two-commit crash window introduced in Phase 4: send_company_to_prospecting
    commits first, then the sweep sets reclaim_blocked_until in a second commit. A crash
    between them leaves swept rows with NULL cooldown, letting the former owner reclaim
    immediately via claim (bypassing the 30-day block).

    Idempotent: only touches rows where swept_at IS NOT NULL AND reclaim_blocked_until IS
    NULL AND status != 'dismissed'. Computes the deadline in Python (swept_at + 30 days)
    so it works on both PostgreSQL and the SQLite test path.

    Called by: run_startup_migrations
    Depends on: ProspectAccount model, RECLAIM_COOLDOWN_DAYS constant, SessionLocal
    """
    from datetime import timedelta

    from .models.prospect_account import ProspectAccount
    from .services.prospect_reclamation import RECLAIM_COOLDOWN_DAYS

    db = SessionLocal()
    try:
        rows = (
            db.query(ProspectAccount)
            .filter(
                ProspectAccount.swept_at.is_not(None),
                ProspectAccount.reclaim_blocked_until.is_(None),
                ProspectAccount.status != "dismissed",
            )
            .all()
        )
        if not rows:
            return
        for pa in rows:
            pa.reclaim_blocked_until = pa.swept_at + timedelta(days=RECLAIM_COOLDOWN_DAYS)
        db.commit()
        logger.info(
            "Backfilled reclaim_blocked_until on {} swept ProspectAccount(s) missing cooldown",
            len(rows),
        )
    except Exception:
        logger.exception("Failed backfilling sweep cooldown on ProspectAccounts")
        db.rollback()
    finally:
        db.close()


def _complete_reverted_active_plans() -> None:
    """Complete ACTIVE buy plans whose every line is already terminal.

    Phase-3 migration 176 reverts every plan parked in the retired ``INBOUND``
    holding state back to ``ACTIVE``. A plan whose lines were ALL already
    verified/cancelled (and whose SO is approved) should then actually complete,
    but generating a correct ``case_report`` needs the real
    ``check_completion``/``_complete_plan`` Python — not raw SQL in the migration.
    This runtime sweep closes that gap by feeding each such plan through the
    canonical completion path.

    Idempotent: ``check_completion`` re-validates and no-ops unless the plan is
    ACTIVE with all lines terminal and ``so_status == approved``; a plan already
    completed on a prior boot is no longer ACTIVE, so it is not re-selected. The
    candidate query pre-filters to keep the sweep O(reverted plans), not O(all
    ACTIVE plans).

    Called by: run_startup_migrations (after migration 176 lands its data change)
    Depends on: BuyPlan/BuyPlanLine models, check_completion, SessionLocal
    """
    from .constants import BuyPlanLineStatus, BuyPlanStatus, SOVerificationStatus
    from .models.buy_plan import BuyPlan, BuyPlanLine
    from .services.buyplan_workflow import check_completion

    terminal = {BuyPlanLineStatus.VERIFIED.value, BuyPlanLineStatus.CANCELLED.value}

    db = SessionLocal()
    try:
        # ACTIVE, SO approved, has at least one line, and no line outside the
        # terminal set — the exact precondition check_completion acts on.
        non_terminal_exists = (
            db.query(BuyPlanLine.id)
            .filter(
                BuyPlanLine.buy_plan_id == BuyPlan.id,
                BuyPlanLine.status.notin_(terminal),
            )
            .exists()
        )
        has_a_line = db.query(BuyPlanLine.id).filter(BuyPlanLine.buy_plan_id == BuyPlan.id).exists()
        candidates = (
            db.query(BuyPlan.id)
            .filter(
                BuyPlan.status == BuyPlanStatus.ACTIVE.value,
                BuyPlan.so_status == SOVerificationStatus.APPROVED.value,
                has_a_line,
                ~non_terminal_exists,
            )
            .all()
        )
        if not candidates:
            return
        completed = 0
        for (plan_id,) in candidates:
            plan = check_completion(plan_id, db)
            if plan and plan.status == BuyPlanStatus.COMPLETED.value:
                completed += 1
        db.commit()
        if completed:
            logger.info(
                "Startup sweep completed {} reverted-ACTIVE buy plan(s) with all-terminal lines",
                completed,
            )
    except Exception:
        logger.exception("Failed startup completion sweep for reverted-ACTIVE buy plans")
        db.rollback()
    finally:
        db.close()


def seed_api_sources() -> None:
    """Seed the api_sources table with all known data sources.

    Uses a version hash so it only writes when the source list changes. Source
    definitions live in app/data/api_sources.json.

    Called by: main.py lifespan (after startup migrations)
    Depends on: ApiSource model, api_sources.json
    """
    import hashlib
    import json
    from pathlib import Path

    from .constants import ApiSourceStatus
    from .models import ApiSource
    from .models.config import ApiUsageLog

    sources_path = Path(__file__).parent / "data" / "api_sources.json"
    SOURCES = json.loads(sources_path.read_text())

    db = SessionLocal()
    try:
        # Version hash — skip if source list hasn't changed
        source_hash = hashlib.md5(
            str([(s["name"], s["description"]) for s in SOURCES]).encode(),
            usedforsecurity=False,
        ).hexdigest()[:12]
        existing_map = {s.name: s for s in db.query(ApiSource).all()}

        # Quick check: if all sources exist and count matches, skip update
        if len(existing_map) == len(SOURCES) and all(s["name"] in existing_map for s in SOURCES):
            logger.debug("API sources up to date ({} sources, hash={})", len(SOURCES), source_hash)
            return

        # Batch fetch all existing sources (1 query instead of 25+)
        logger.info("Seeding API sources ({} sources, hash={})", len(SOURCES), source_hash)
        for src in SOURCES:
            existing = existing_map.get(src["name"])
            if existing:
                existing.display_name = src["display_name"]
                existing.category = src["category"]
                existing.source_type = src["source_type"]
                existing.description = src["description"]
                existing.signup_url = src["signup_url"]
                existing.env_vars = src["env_vars"]
                existing.setup_notes = src["setup_notes"]
            else:
                status = ApiSourceStatus.PENDING.value
                env_vars = src.get("env_vars", [])
                if env_vars:
                    all_set = all(os.getenv(v) for v in env_vars)
                    if all_set:
                        status = ApiSourceStatus.LIVE.value
                is_active = status == ApiSourceStatus.LIVE.value
                db.add(ApiSource(status=status, is_active=is_active, **src))

        # Remove legacy "newark" source (renamed to "element14")
        if "newark" in existing_map and "element14" in existing_map:
            old_newark = existing_map["newark"]  # type: ignore[index]  # dict is keyed by instance-level str values
            db.query(ApiUsageLog).filter(ApiUsageLog.source_id == old_newark.id).delete()
            db.delete(old_newark)
            logger.info("Removed duplicate 'newark' source (merged into 'element14')")

        # Prune dead providers no longer in the catalog (idempotent — safe to re-run)
        _PRUNE_NAMES = (
            "aliexpress",
            "arrow",
            "avnet",
            "partfuse",
            "rs_components",
            "siliconexpert",
            "winsource",
            "rocketreach_enrichment",
            "clearbit_enrichment",
            "apollo_enrichment",
        )
        for dead in _PRUNE_NAMES:
            row = db.query(ApiSource).filter_by(name=dead).first()
            if row:
                db.delete(row)
                logger.info("Pruned retired source '{}'", dead)

        # Backfill known monthly quotas (only sets if currently NULL)
        quota_map = {
            "hunter_enrichment": 500,
            "lusha_enrichment": 6400,
            "digikey": 1000,
            "mouser": 1000,
            "oemsecrets": 5000,
            "nexar": 1000,
        }
        for name, quota in quota_map.items():
            src = db.query(ApiSource).filter_by(name=name).first()
            if src and not src.monthly_quota:
                src.monthly_quota = quota

        db.commit()
    except (SQLAlchemyError, DBAPIError) as e:
        logger.warning("API source seed error: {}", e)
        db.rollback()
    finally:
        db.close()


def seed_browser_worker_sources(db) -> None:
    """Flip every BROWSER_WORKER_SOURCES api_sources row to live + active.

    (icsource + netcomponents + thebrokersite.) The browser workers
    (avail-ics-worker, avail-nc-worker, avail-tbf-worker) are queue-driven
    so the dashboard surfaces them as 'live' rather than 'pending'/'disabled'.
    `health_monitor.run_health_checks` excludes BROWSER_WORKER_SOURCES so this
    seed survives the 15-min ping loop. Idempotent.

    Called by: seed_browser_workers (lifespan)
    Depends on: ApiSource model, ApiSourceStatus, BROWSER_WORKER_SOURCES
    """
    from .constants import BROWSER_WORKER_SOURCES, ApiSourceStatus
    from .models import ApiSource

    for name in BROWSER_WORKER_SOURCES:
        row = db.query(ApiSource).filter_by(name=name).one_or_none()
        if row is None:
            continue
        row.status = ApiSourceStatus.LIVE.value
        row.is_active = True


def seed_ics_worker_status_singleton(db) -> None:
    """Insert ics_worker_status id=1 row if absent.

    The worker's update_worker_status() is a no-op when the row is missing,
    so heartbeats and daily stats silently never persist. Seeding makes the
    worker's writes effective from first startup. Idempotent.

    Called by: seed_browser_workers (lifespan)
    Depends on: IcsWorkerStatus model
    """
    from .models import IcsWorkerStatus

    existing = db.query(IcsWorkerStatus).filter_by(id=1).one_or_none()
    if existing is not None:
        return
    db.add(IcsWorkerStatus(id=1, is_running=False))


def seed_nc_worker_status_singleton(db) -> None:
    """Insert nc_worker_status id=1 row if absent.

    Same pattern as the ICS singleton — the NC worker's update_worker_status()
    silently no-ops when the row is missing, dropping every heartbeat. Idempotent.

    Called by: seed_browser_workers (lifespan)
    Depends on: NcWorkerStatus model
    """
    from .models import NcWorkerStatus

    existing = db.query(NcWorkerStatus).filter_by(id=1).one_or_none()
    if existing is not None:
        return
    db.add(NcWorkerStatus(id=1, is_running=False))


def seed_tbf_worker_status_singleton(db) -> None:
    """Insert tbf_worker_status id=1 row if absent.

    Same pattern as the ICS/NC singletons — the TBF worker's
    update_worker_status() silently no-ops when the row is missing, dropping
    every heartbeat. Migration 130 seeds the row at deploy; this is the
    idempotent backup for fresh DBs/tests. Idempotent.

    Called by: seed_browser_workers (lifespan)
    Depends on: TbfWorkerStatus model
    """
    from .models import TbfWorkerStatus

    existing = db.query(TbfWorkerStatus).filter_by(id=1).one_or_none()
    if existing is not None:
        return
    db.add(TbfWorkerStatus(id=1, is_running=False))


def seed_browser_workers() -> None:
    """Run all browser-worker seeds in a single SessionLocal transaction.

    Called by: main.py lifespan (after seed_api_sources)
    """
    db = SessionLocal()
    try:
        seed_browser_worker_sources(db)
        seed_ics_worker_status_singleton(db)
        seed_nc_worker_status_singleton(db)
        seed_tbf_worker_status_singleton(db)
        db.commit()
    except (SQLAlchemyError, DBAPIError) as e:
        logger.warning("Browser worker seed error: {}", e)
        db.rollback()
    finally:
        db.close()
