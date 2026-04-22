"""Ad-hoc verification: dump /requisitions2 HTML under both AVAIL_OPP_TABLE_V2 states.

Uses the same dep-override pattern as tests/conftest.py so auth and DB point at
in-memory test fixtures. Dumps the table-fragment HTML + marker counts so the
reviewer can eyeball the difference between v2 and legacy rendering.

Usage: TESTING=1 PYTHONPATH=/root/availai python scripts/verify_v2_flag.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("TESTING", "1")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
sys.path.insert(0, "/root/availai")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import settings as app_settings  # noqa: E402
from app.database import get_db  # noqa: E402
from app.dependencies import (  # noqa: E402
    require_admin,
    require_buyer,
    require_fresh_token,
    require_user,
)
from app.main import app  # noqa: E402
from app.models import Requirement, Requisition, User  # noqa: E402
from tests.conftest import engine  # noqa: E402 — in-memory SQLite engine


def _marker_counts(html: str) -> dict[str, int]:
    return {
        "opp-col-header": html.count("opp-col-header"),
        "opp-status-dot": html.count("opp-status-dot"),
        "opp-coverage-seg": html.count("opp-coverage-seg"),
        "opp-action-rail": html.count("opp-action-rail"),
        "opp-chip-row": html.count("opp-chip-row"),
        "status_badge legacy (bg-sky-50)": html.count("bg-sky-50 text-sky-600"),
    }


def run(flag_value: bool) -> None:
    app_settings.avail_opp_table_v2 = flag_value
    label = "true" if flag_value else "false"
    print("\n" + "=" * 72)
    print(f"  AVAIL_OPP_TABLE_V2 = {label}")
    print("=" * 72)

    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    db = SessionLocal()

    # Seed a single user if the shared in-memory DB is empty.
    from app.models.base import Base

    Base.metadata.create_all(bind=engine)
    user = db.query(User).first()
    if user is None:
        user = User(email="verifier@example.com", name="Verifier", role="buyer", is_active=True)
        db.add(user)
        db.commit()
        db.refresh(user)
    # Seed a requisition with two requirements so the table renders rows
    # (not the empty-state card). Deadline in 6h → exercises urgency accent.
    if db.query(Requisition).count() == 0:
        from datetime import datetime, timedelta, timezone

        deadline = (datetime.now(timezone.utc) + timedelta(hours=6)).strftime("%Y-%m-%d")
        req = Requisition(
            name="Acme Q3 — power mgmt",
            customer_name="Acme Corp",
            status="sourcing",
            created_by=user.id,
            deadline=deadline,
            urgency="normal",
            opportunity_value=None,
        )
        db.add(req)
        db.commit()
        db.refresh(req)
        db.add(Requirement(requisition_id=req.id, primary_mpn="LM317", target_qty=100, target_price=0.50))
        db.add(Requirement(requisition_id=req.id, primary_mpn="NE555", target_qty=500, target_price=None))
        db.commit()

    def _override_db():
        yield db

    def _override_user():
        return user

    async def _override_fresh_token():
        return "mock-token"

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_user] = _override_user
    app.dependency_overrides[require_admin] = _override_user
    app.dependency_overrides[require_buyer] = _override_user
    app.dependency_overrides[require_fresh_token] = _override_fresh_token

    try:
        with TestClient(app) as client:
            resp = client.get("/requisitions2/table?status=all")
        print(f"HTTP {resp.status_code}")
        html = resp.text

        print("\nMarker counts:")
        for marker, count in _marker_counts(html).items():
            print(f"  {marker!r:40s} → {count}")

        idx_thead = html.find("<thead>")
        if idx_thead >= 0:
            end = html.find("</thead>", idx_thead) + len("</thead>")
            print("\n<thead> fragment (first 600 chars):")
            print(html[idx_thead : min(end, idx_thead + 600)])

        idx_tr = html.find('id="rq2-row-')
        if idx_tr >= 0:
            tr_start = html.rfind("<tr", 0, idx_tr)
            tr_end = html.find("</tr>", idx_tr) + len("</tr>")
            print("\nFirst <tr> (first 800 chars):")
            print(html[tr_start : min(tr_end, tr_start + 800)])
        else:
            print("\n(no <tr> rows rendered — fixture has no requisitions matching the filter)")
    finally:
        for dep in (get_db, require_user, require_admin, require_buyer, require_fresh_token):
            app.dependency_overrides.pop(dep, None)
        db.close()


if __name__ == "__main__":
    run(True)
    run(False)
