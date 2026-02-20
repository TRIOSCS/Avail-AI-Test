"""Auth & user models."""

from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from ..utils.encrypted_type import EncryptedText
from .base import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(255))
    role = Column(
        String(20), default="buyer"
    )  # buyer | sales | trader | manager | admin | dev_assistant
    is_active = Column(Boolean, default=True)
    azure_id = Column(String(255), unique=True)
    refresh_token = Column(EncryptedText)
    access_token = Column(EncryptedText)
    token_expires_at = Column(DateTime)
    email_signature = Column(Text)
    last_email_scan = Column(DateTime)
    last_inbox_scan = Column(DateTime)
    last_contacts_sync = Column(DateTime)
    m365_connected = Column(Boolean, default=False)
    m365_error_reason = Column(String(255))
    m365_last_healthy = Column(DateTime)
    last_deep_email_scan = Column(DateTime)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    requisitions = relationship("Requisition", back_populates="creator")
    contacts = relationship("Contact", back_populates="user")
