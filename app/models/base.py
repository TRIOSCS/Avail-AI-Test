"""Shared declarative base for all models, plus cross-model helpers.

Holds:
- Base — the single DeclarativeBase every model inherits.
- AttachmentColumnsMixin — the 9-column OneDrive/SharePoint attachment payload
  shared byte-for-byte by every *Attachment model.
- validate_enum_member / warn_enum_member — the standard @validates bodies for
  string columns backed by a constants enum (raise vs. loguru-warn variants).
- validate_custom_fields_dict — the shared custom_fields JSON cap validator.

Called by: every module in app/models/, app/schemas (via model validators)
Depends on: app.database (UTCDateTime)
"""

from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import Column, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, declared_attr, relationship

from ..database import UTCDateTime


class Base(DeclarativeBase):
    pass


class AttachmentColumnsMixin:
    """Shared OneDrive / company-SharePoint attachment payload.

    library_drive_id NULL  → OneDrive fallback row (user token, item in /me/drive)
    library_drive_id set   → company SharePoint library row (app token)

    Subclasses keep their own id, subject FK(s), subject relationship, __tablename__ and
    __table_args__. Every column here is a declared_attr so each subclass gets fresh
    Column objects created AFTER its class-body columns — preserving the historical
    column order (id, subject FKs, then this block).
    """

    @declared_attr
    def file_name(cls):
        return Column(String(500), nullable=False)

    @declared_attr
    def library_item_id(cls):
        return Column(String(500))

    @declared_attr
    def library_drive_id(cls):
        return Column(String(200))

    @declared_attr
    def library_web_url(cls):
        return Column(Text)

    @declared_attr
    def thumbnail_url(cls):
        return Column(Text)

    @declared_attr
    def content_type(cls):
        return Column(String(100))

    @declared_attr
    def size_bytes(cls):
        return Column(Integer)

    @declared_attr
    def uploaded_by_id(cls):
        return Column(Integer, ForeignKey("users.id", ondelete="SET NULL"))

    @declared_attr
    def created_at(cls):
        return Column(UTCDateTime, default=lambda: datetime.now(UTC))

    @declared_attr
    def uploaded_by(cls):
        # cls is the concrete mapped class at runtime (declared_attr semantics).
        name = cls.__name__  # type: ignore[attr-defined]
        return relationship("User", foreign_keys=f"{name}.uploaded_by_id")


def validate_enum_member(enum_cls, value, label):
    """Shared @validates body: raise ValueError unless value is a member of enum_cls.

    Falsy values (None, "") pass through untouched — column nullability owns that
    decision. `label` names the field in the error, e.g. "buy plan status".
    """
    valid = {e.value for e in enum_cls}
    if value and value not in valid:
        raise ValueError(f"Invalid {label}: {value!r}. Valid: {valid}")
    return value


def warn_enum_member(enum_cls, value, label):
    """Shared @validates body: loguru-warn (never raise) on a non-member value.

    For columns where a write must never crash over vocabulary drift (User.role,
    Requisition.status, Offer.status).
    """
    valid = {e.value for e in enum_cls}
    if value and value not in valid:
        logger.warning("Unexpected {}: {}. Expected one of {}", label, value, valid)
    return value


def validate_custom_fields_dict(value):
    """Shared @validates body for JSON custom_fields columns.

    Cap: max 30 keys, key max 60 chars, value max 500 chars. None collapses to {}.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("custom_fields must be a dict")
    if len(value) > 30:
        raise ValueError("custom_fields: max 30 keys")
    for k, v in value.items():
        if len(str(k)) > 60:
            raise ValueError(f"custom_fields key too long (max 60 chars): {k!r}")
        if len(str(v)) > 500:
            raise ValueError(f"custom_fields value too long (max 500 chars) for key {k!r}")
    return value
