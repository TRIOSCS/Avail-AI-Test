"""Backfill vendor_sighting_summary pre-aggregated fields.

Revision ID: 70b4dce3cf67
Revises: 838cd7ddccf1
Create Date: 2026-03-25 04:29:29.407212
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "70b4dce3cf67"
down_revision: Union[str, None] = "838cd7ddccf1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    # Backfill vendor_card_id
    conn.execute(
        sa.text("""
        UPDATE vendor_sighting_summary vss
        SET vendor_card_id = vc.id
        FROM vendor_cards vc
        WHERE vss.vendor_name = vc.normalized_name
    """)
    )
    # Backfill vendor_phone from VendorCard where NULL
    conn.execute(
        sa.text("""
        UPDATE vendor_sighting_summary vss
        SET vendor_phone = (vc.phones->>0)
        FROM vendor_cards vc
        WHERE vss.vendor_name = vc.normalized_name
          AND vss.vendor_phone IS NULL
          AND vc.phones IS NOT NULL
          AND jsonb_array_length(vc.phones::jsonb) > 0
    """)
    )
    # Backfill aggregated fields from sightings
    conn.execute(
        sa.text("""
        UPDATE vendor_sighting_summary vss SET
          newest_sighting_at = sub.newest,
          best_lead_time_days = sub.best_lt,
          min_moq = sub.min_moq,
          has_contact_info = sub.has_contact
        FROM (
          SELECT requirement_id, LOWER(TRIM(vendor_name)) as vn,
            MAX(created_at) AS newest,
            MIN(lead_time_days) FILTER (WHERE lead_time_days IS NOT NULL) AS best_lt,
            MIN(moq) FILTER (WHERE moq IS NOT NULL) AS min_moq,
            BOOL_OR(vendor_email IS NOT NULL OR vendor_phone IS NOT NULL) AS has_contact
          FROM sightings WHERE NOT is_unavailable
          GROUP BY requirement_id, LOWER(TRIM(vendor_name))
        ) sub
        WHERE sub.vn = vss.vendor_name
          AND sub.requirement_id = vss.requirement_id
    """)
    )


def downgrade() -> None:
    pass  # data-only migration
