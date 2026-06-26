"""Auth & user models."""

from datetime import datetime, timezone

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

    # Mailbox settings (from Graph /me/mailboxSettings)
    timezone = Column(String(100))
    working_hours_start = Column(String(10))  # e.g. "08:00"
    working_hours_end = Column(String(10))  # e.g. "17:00"

    # 8x8 Work Analytics
    eight_by_eight_extension = Column(String(20))
    eight_by_eight_enabled = Column(Boolean, default=False)

    # Notification preferences (Profile tab toggles — Tasks 7-9 wire the UI)
    notify_buyplan_email_enabled = Column(Boolean, default=True, nullable=False)
    notify_new_offer_alert_enabled = Column(Boolean, default=True, nullable=False)

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

    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc))

    requisitions = relationship("Requisition", back_populates="creator", foreign_keys="[Requisition.created_by]")
    contacts = relationship("Contact", back_populates="user")
    strategic_vendors = relationship("StrategicVendor", back_populates="user")

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
    created_at = Column(UTCDateTime, default=lambda: datetime.now(timezone.utc), index=True)
