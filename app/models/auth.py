"""Auth & user models."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import relationship, validates

from ..utils.encrypted_type import EncryptedText
from .base import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(255))
    role = Column(String(20), default="buyer")  # buyer | sales | trader | manager | admin
    is_active = Column(Boolean, default=True)
    azure_id = Column(String(255), unique=True)
    refresh_token = Column(EncryptedText)
    access_token = Column(EncryptedText)
    # PBKDF2 password hash stored as "<salt_b64>$<hash_b64>", encrypted at rest
    password_hash = Column(EncryptedText)
    token_expires_at = Column(DateTime)
    email_signature = Column(Text)
    last_email_scan = Column(DateTime)
    last_inbox_scan = Column(DateTime)
    last_contacts_sync = Column(DateTime)
    m365_connected = Column(Boolean, default=False)
    m365_error_reason = Column(String(255))
    m365_last_healthy = Column(DateTime)
    commodity_tags = Column(JSON, default=list)

    # Parts workspace — which columns the user wants visible in the split-panel view
    parts_column_prefs = Column(JSON, default=list)

    # Requisition detail — column visibility prefs for requirements and offers tables
    requirements_column_prefs = Column(JSON)
    offers_column_prefs = Column(JSON)

    # Mailbox settings (from Graph /me/mailboxSettings)
    timezone = Column(String(100))
    working_hours_start = Column(String(10))  # e.g. "08:00"
    working_hours_end = Column(String(10))  # e.g. "17:00"

    # 8x8 Work Analytics
    eight_by_eight_extension = Column(String(20))
    eight_by_eight_enabled = Column(Boolean, default=False)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

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
