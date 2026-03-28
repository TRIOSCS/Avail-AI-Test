"""Drop 7 dead tables and legacy_error_report_id column.

Revision ID: a3f9c1d82e47
Revises: restructure_substitutes_json
Create Date: 2026-03-28
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f9c1d82e47"
down_revision: Union[str, None] = "restructure_substitutes_json"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("error_reports")
    op.drop_table("reactivation_signals")
    op.drop_table("ics_classification_cache")
    op.drop_table("nc_classification_cache")
    op.drop_table("teams_notification_log")
    op.drop_table("teams_alert_config")
    op.drop_table("risk_flags")
    op.drop_column("trouble_tickets", "legacy_error_report_id")


def downgrade() -> None:
    # Dead tables — no downgrade needed for cleanup migration
    pass
