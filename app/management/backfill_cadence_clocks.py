"""One-time backfill of CRM cadence clocks from historical ActivityLog.

Also seeds tier='key' for accounts already flagged is_strategic (idempotent).
Run: python -m app.management.backfill_cadence_clocks
"""

from sqlalchemy.orm import Session

from ..models.crm import Company
from ..services.cadence_service import materialize_all_clocks


def backfill_for_session(db: Session) -> int:
    db.query(Company).filter(Company.is_strategic.is_(True), Company.tier.is_(None)).update(
        {"tier": "key"}, synchronize_session=False
    )
    return materialize_all_clocks(db)


def backfill_cadence_clocks() -> int:
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        n = backfill_for_session(db)
        db.commit()
        return n
    finally:
        db.close()


if __name__ == "__main__":
    print(f"Backfilled cadence clocks for {backfill_cadence_clocks()} companies")
