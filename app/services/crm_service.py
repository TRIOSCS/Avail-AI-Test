"""services/crm_service.py -- Shared CRM helpers (extracted from routers/crm.py).

Avoids circular imports: proactive_service needs next_quote_number but
should not import from routers.
"""

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from ..models import Quote


def next_quote_number(db: Session) -> str:
    """Generate next sequential quote number: Q-YYYY-NNNN."""
    year = datetime.now(timezone.utc).year
    prefix = f"Q-{year}-"
    last = (
        db.query(Quote)
        .filter(Quote.quote_number.like(f"{prefix}%"))
        .order_by(Quote.id.desc())
        .first()
    )
    if last:
        try:
            seq = int(last.quote_number.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    else:
        seq = 1
    return f"{prefix}{seq:04d}"
