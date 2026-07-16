"""Auth & user models."""

from datetime import UTC, datetime

from sqlalchemy import JSON, Boolean, Column, ForeignKey, Integer, Numeric, String, Text, text
from sqlalchemy.orm import relationship, validates

from ..database import UTCDateTime
from ..utils.encrypted_type import EncryptedText
from .base import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(255))
    role = Column(String(20), default="buyer")  # buyer | sales | trader | manager | admin
    is_active = Column(Boolean, nullable=False, default=True, server_default=text("true"))
    azure_id = Column(String(255), unique=True)
    refresh_token = Column(EncryptedText)
    access_token = Column(EncryptedText)
    # PBKDF2 password hash stored as "<salt_b64>$<hash_b64>", encrypted at rest
    password_hash = Column(EncryptedText)
    token_expires_at = Column(UTCDateTime)
    email_signature = Column(Text)
    last_email_scan = Column(UTCDateTime)
    last_inbox_scan = Column(UTCDateTime)
    last_contacts_sync = Column(UTCDateTime)
    m365_connected = Column(Boolean, default=False)
    m365_error_reason = Column(String(255))
    m365_last_healthy = Column(UTCDateTime)
    commodity_tags = Column(JSON, default=list)

    # User-management foundation (Phase 1)
    last_login_at = Column(UTCDateTime, nullable=True)
    # Explicit per-user access overrides ONLY: {access_key_str: bool}. An absent key
    # means "use the role default" (constants.ROLE_ACCESS_DEFAULTS). Read by
    # dependencies.user_has_access — override wins over the role default.
    access_overrides = Column(JSON, default=dict)
    invited_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Latches True when an ADMIN_EMAILS bootstrap admin is explicitly demoted via the
    # admin Users tab (change_user_role). The login bootstrap in routers/auth.py skips
    # re-promotion while this is set, so a demoted admin stays demoted across logins.
    # Cleared when an admin re-promotes them to admin.
    admin_bootstrap_opted_out = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # Per-rep manager / supervisor. When set, account-park alerts (and future manager-
    # routed notifications) target THIS user's specific manager instead of fanning out to
    # every MANAGER/ADMIN; when NULL the all-managers fallback is preserved. Self-
    # referential FK to users.id with ondelete=SET NULL (a manager's deletion detaches
    # their reports, never cascades). Distinct from invited_by_id (the other users.id
    # self-FK), so the `manager` relationship below pins foreign_keys explicitly. Set via
    # the admin Users tab (app/routers/admin/users.py); read by
    # services.prospect_reclamation._sweep_notification_recipients.
    reports_to_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    # Mailbox settings (from Graph /me/mailboxSettings). NOTE: `timezone` holds the Graph
    # mailbox zone (Windows format, e.g. "Pacific Standard Time") used for RFQ send-window
    # scheduling — it is NOT a valid IANA name. For rendering timestamps in the viewer's
    # zone use `display_timezone` (below), never this column.
    timezone = Column(String(100))
    working_hours_start = Column(String(10))  # e.g. "08:00"
    working_hours_end = Column(String(10))  # e.g. "17:00"

    # Per-user DISPLAY timezone — an IANA zone name (e.g. "America/New_York", "Asia/Tokyo")
    # used to render stored-UTC timestamps in this viewer's own timezone. Auto-detected
    # from the browser (Intl.DateTimeFormat().resolvedOptions().timeZone) and overridable
    # in the profile page. NULL → fall back to app.utils.timezones.DEFAULT_DISPLAY_TZ.
    display_timezone = Column(String(64), nullable=True)

    # 8x8 Work Analytics
    eight_by_eight_extension = Column(String(20))
    eight_by_eight_enabled = Column(Boolean, default=False)

    # Notification preferences (Profile tab toggles — Tasks 7-9 wire the UI)
    notify_buyplan_email_enabled = Column(Boolean, default=True, nullable=False)
    notify_new_offer_alert_enabled = Column(Boolean, default=True, nullable=False)
    # Urgent "a deal needs re-sourcing" broadcast — honored only on the intrusive
    # personal pushes (email + Teams DM); the in-app row + Teams channel card always
    # fire regardless, so an opted-out buyer can still see and claim the line.
    notify_resource_alert_enabled = Column(Boolean, nullable=False, default=True, server_default=text("true"))

    # Profile photo — stored basename of the file under avatars.AVATARS_DIR
    # (e.g. "user_12_a1b2c3d4.png"); NULL falls back to the initials avatar.
    avatar_path = Column(String(255), nullable=True)
    # ── Approval rights (per-gate per-user toggles, admin-managed) ────────────────
    # Independent of role: admins do NOT auto-qualify — these columns are the single
    # source of truth, so the toggle UI reflects exactly who can approve each gate.

    # Buy-plan gate: no dollar limit — approves any amount.
    can_approve_buy_plans = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # Prepayment gate: requires both the toggle AND an amount check.
    # prepayment_approval_limit=NULL means unlimited (applies to any amount).
    # e.g. limit=1000 → only routes prepayments ≤ $1,000 to this user.
    can_approve_prepayments = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    prepayment_approval_limit = Column(Numeric(12, 2), nullable=True)

    # QP Sales-Order gate (Sales section): no dollar limit — approves any amount.
    can_approve_qp_sales = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # QP Purchasing-section gate (qp_purchasing): no dollar limit — approves any amount.
    # Renamed from can_approve_pos in SP-3 when the deal-level PO gate de-collided.
    can_approve_qp_purchasing = Column(Boolean, nullable=False, default=False, server_default=text("false"))

    # Deal-level Purchase-Order gate (SP-3): requires the toggle AND an amount check.
    # purchase_order_approval_limit=NULL means unlimited; e.g. limit=10000 routes only
    # PO spends ≤ $10,000 to this user (mirrors the prepayment gate's money guard).
    can_approve_purchase_orders = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    purchase_order_approval_limit = Column(Numeric(12, 2), nullable=True)

    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC))

    requisitions = relationship("Requisition", back_populates="creator", foreign_keys="[Requisition.created_by]")
    contacts = relationship("Contact", back_populates="user")
    strategic_vendors = relationship("StrategicVendor", back_populates="user")

    # Self-referential manager link (uses reports_to_id, NOT invited_by_id). remote_side
    # marks the parent (manager) row; foreign_keys disambiguates the two users.id self-FKs.
    manager = relationship("User", remote_side=[id], foreign_keys=[reports_to_id])

    @validates("email")
    def _validate_email(self, _key, value):
        if value and "@" not in value:
            raise ValueError(f"Invalid email: {value}")
        return value

    @validates("role")
    def _validate_role(self, _key, value):
        from ..constants import UserRole

        valid = {e.value for e in UserRole}
        if value and value not in valid:
            from loguru import logger

            logger.warning("Unexpected role: {}. Expected one of {}", value, valid)
        return value


class UserAdminAudit(Base):
    """Append-only audit trail for admin actions against users.

    Records who (actor) did what (action, see constants.UserAuditAction) to whom
    (target_user_id) plus a JSON detail blob (e.g. {"from": "buyer", "to": "manager"}).
    actor_id is nullable + SET NULL so the trail survives the admin's deletion;
    target_user_id CASCADEs so a user's audit rows are removed with the user.
    """

    __tablename__ = "user_admin_audit"

    id = Column(Integer, primary_key=True)
    actor_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    target_user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    action = Column(String(32), nullable=False)
    detail = Column(JSON, default=dict)
    created_at = Column(UTCDateTime, default=lambda: datetime.now(UTC), index=True)
