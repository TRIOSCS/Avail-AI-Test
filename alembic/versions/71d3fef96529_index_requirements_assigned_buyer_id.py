"""Index requirements assigned buyer id.

Revision ID: 71d3fef96529
Revises: a431c202afa4
Create Date: 2026-07-09 06:35:13.652911

P3.1 — `requirements.assigned_buyer_id` had no index anywhere despite being filtered on
every buyer's default sightings board (routers/sightings.py:413,585) and the offers alert
source (services/alerts/sources/offers.py:58-60). Autogenerate against the dev DB also
picked up ~15 unrelated pre-existing drift ops (dead-table drops, UTCDateTime type
reconciliations, unrelated index drops on material_cards/vendor_cards) — all stripped
here; this migration carries ONLY the new index.
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "71d3fef96529"
down_revision: Union[str, None] = "a431c202afa4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index("ix_requirements_assigned_buyer", "requirements", ["assigned_buyer_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_requirements_assigned_buyer", table_name="requirements")
