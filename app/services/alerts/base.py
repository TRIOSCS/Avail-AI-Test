"""AlertSource — reusable cross-app alert/badge primitive.

An AlertSource answers, for the current user, two questions: how many items need
attention (drives the nav badge) and which specific items (drives the in-tab
spotlight). Two temperaments:

  - FYI: the count EXCLUDES items the user has already seen (alert_seen rows).
         Seeing an item drains the badge. Used for new offers / inbound comms.
  - ACTION: the count derives purely from work-state (e.g. an open buy-plan step).
         alert_seen does NOT change the count — it only records the one-time
         spotlight pulse so a row you've looked at stops pulsing. The item leaves
         the count when the underlying work is done.

Called by: services/alerts/registry.py, the badge/seen routers, concrete sources.
Depends on: models/alert_seen.py, constants.AlertKind, config.settings.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import AlertKind
from app.models.alert_seen import AlertSeen
from app.models.auth import User


class Temperament(StrEnum):
    """How a source's badge count clears."""

    FYI = "fyi"  # clears on see — count excludes seen items
    ACTION = "action"  # clears on act — count from work-state; seen gates the pulse only


@dataclass(frozen=True)
class AlertItem:
    """A single item the in-tab spotlight should highlight."""

    ref_id: int
    anchor: str = ""  # optional DOM anchor/data value the tab template stamps on the row


def _parse_epoch() -> datetime | None:
    raw = (settings.alerts_epoch or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def recency_floor(now: datetime | None = None) -> datetime:
    """Earliest 'new' timestamp an FYI item may carry to still count.

    = max(now - ALERT_RECENCY_DAYS, ALERTS_EPOCH). The epoch (set at feature launch)
    keeps the badge from lighting up for the pre-launch backlog; the rolling window
    keeps stale items from nagging forever.
    """
    now = now or datetime.now(UTC)
    floor = now - timedelta(days=settings.alert_recency_days)
    epoch = _parse_epoch()
    return epoch if epoch and epoch > floor else floor


def record_seen(db: Session, user: User, kind: str, ref_id: int) -> None:
    """Idempotently record that ``user`` has seen ``(kind, ref_id)``.

    Safe to call repeatedly: the unique constraint plus a check-then-insert (with an
    IntegrityError fallback for the concurrent case) keeps exactly one row.
    """
    exists = (
        db.query(AlertSeen.id)
        .filter(
            AlertSeen.user_id == user.id,
            AlertSeen.alert_kind == kind,
            AlertSeen.ref_id == ref_id,
        )
        .first()
    )
    if exists:
        return
    db.add(AlertSeen(user_id=user.id, alert_kind=kind, ref_id=ref_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # inserted concurrently — the desired end-state already holds


class AlertSource(abc.ABC):
    """Base class for a single tab alert.

    Concrete sources set ``key``, ``kind``, ``temperament`` and implement
    ``count_for_user`` + ``new_items_for_user``.
    """

    key: str
    kind: AlertKind
    temperament: Temperament

    @abc.abstractmethod
    def count_for_user(self, db: Session, user: User) -> int:
        """How many items currently need this user's attention (drives the badge)."""

    @abc.abstractmethod
    def new_items_for_user(self, db: Session, user: User) -> list[AlertItem]:
        """The specific items to spotlight on tab entry (drives the glide/rail)."""

    # --- shared helpers ---------------------------------------------------

    def seen_ref_ids(self, db: Session, user: User) -> set[int]:
        """The set of ref_ids this user has already seen for this source's kind."""
        rows = db.query(AlertSeen.ref_id).filter(AlertSeen.user_id == user.id, AlertSeen.alert_kind == self.kind).all()
        return {r[0] for r in rows}

    def recency_floor(self, now: datetime | None = None) -> datetime:
        """Convenience wrapper over the module-level :func:`recency_floor`."""
        return recency_floor(now)
