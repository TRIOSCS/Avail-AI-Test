"""Backfill: uppercase all MPN fields and normalize substitutes JSON.

Called by: alembic upgrade head
Depends on: requirements table (primary_mpn, customer_pn, oem_pn, substitutes columns)

Revision ID: f2ee82c7b17d
Revises: a2a095f252db
Create Date: 2026-03-29 23:37:54.516804
"""

from typing import Sequence, Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2ee82c7b17d"
down_revision: Union[str, None] = "a2a095f252db"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Uppercase all MPN-like fields and normalize substitute JSON format."""
    conn = op.get_bind()

    # 1. Uppercase string columns
    conn.execute(
        text("""
        UPDATE requirements SET primary_mpn = UPPER(TRIM(primary_mpn))
        WHERE primary_mpn IS NOT NULL AND primary_mpn != UPPER(TRIM(primary_mpn))
    """)
    )
    conn.execute(
        text("""
        UPDATE requirements SET customer_pn = UPPER(TRIM(customer_pn))
        WHERE customer_pn IS NOT NULL AND customer_pn != UPPER(TRIM(customer_pn))
    """)
    )
    conn.execute(
        text("""
        UPDATE requirements SET oem_pn = UPPER(TRIM(oem_pn))
        WHERE oem_pn IS NOT NULL AND oem_pn != UPPER(TRIM(oem_pn))
    """)
    )

    # 2. Uppercase MPNs inside JSON substitutes column
    # Handles both string-format and dict-format subs in a single pass
    conn.execute(
        text("""
        UPDATE requirements
        SET substitutes = (
            SELECT jsonb_agg(
                CASE
                    WHEN jsonb_typeof(elem) = 'string'
                    THEN to_jsonb(UPPER(TRIM(elem #>> '{}')))
                    WHEN jsonb_typeof(elem) = 'object'
                    THEN jsonb_set(elem, '{mpn}', to_jsonb(UPPER(TRIM(elem ->> 'mpn'))))
                    ELSE elem
                END
            )
            FROM jsonb_array_elements(substitutes::jsonb) AS elem
        )
        WHERE substitutes IS NOT NULL
          AND substitutes::text != '[]'
    """)
    )


def downgrade() -> None:
    """No-op — uppercasing is non-destructive and cannot be reversed."""
    pass
