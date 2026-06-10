"""Merge the 092_spec_provenance and 094_fru_links heads into a single head.

092_spec_provenance (SP2 provenance columns) and 093/094 (taxonomy normalization +
fru_links) both chained off 091_cleanup_vague_descs on concurrent branches — this
no-op merge revision rejoins the graph so ``alembic upgrade head`` has exactly one
head (enforced by tests/test_migration_chain.py).

Revision ID: merge_092_094
Revises: 092_spec_provenance, 094_fru_links
Create Date: 2026-06-10
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "merge_092_094"
down_revision: Union[str, None] = ("092_spec_provenance", "094_fru_links")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
