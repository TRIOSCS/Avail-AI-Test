"""Restructure substitutes JSON from string arrays to object arrays.

Revision ID: restructure_substitutes_json
Revises: add_manufacturer_to_requirements
Create Date: 2026-03-22 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "restructure_substitutes_json"
down_revision: Union[str, None] = "add_manufacturer_to_requirements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Convert string arrays to object arrays
    op.execute("""
        UPDATE requirements
        SET substitutes = (
            SELECT jsonb_agg(jsonb_build_object('mpn', elem, 'manufacturer', ''))
            FROM jsonb_array_elements_text(substitutes::jsonb) AS elem
        )
        WHERE substitutes IS NOT NULL
          AND jsonb_typeof(substitutes::jsonb) = 'array'
          AND jsonb_array_length(substitutes::jsonb) > 0
          AND jsonb_typeof(substitutes::jsonb -> 0) = 'string'
    """)

    # substitutes_text generated column stays as substitutes::text — PG doesn't
    # allow subqueries in generated columns. The MPN strings are still present
    # in the JSON text so ILIKE search continues to work.


def downgrade() -> None:
    # Convert object arrays back to string arrays
    op.execute("""
        UPDATE requirements
        SET substitutes = (
            SELECT jsonb_agg(elem->>'mpn')
            FROM jsonb_array_elements(substitutes::jsonb) AS elem
        )
        WHERE substitutes IS NOT NULL
          AND jsonb_typeof(substitutes::jsonb) = 'array'
          AND jsonb_array_length(substitutes::jsonb) > 0
          AND jsonb_typeof(substitutes::jsonb -> 0) = 'object'
    """)
