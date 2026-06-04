"""Vocabulary-normalization contract tests (Phase B2).

Guards the activity_log categorical enums against column-width drift and pins the
canonical Direction vocabulary (no 'sent'/'received'/'unknown' sentinels stored).

Depends on: app/constants.py enums; column widths from app/models/intelligence.py.
"""

from app.constants import ActivityType, Channel, Direction, EventType

# Column widths declared on ActivityLog (app/models/intelligence.py).
_COLUMN_WIDTHS = {
    ActivityType: 20,  # activity_type String(20)
    Channel: 20,  # channel String(20)
    EventType: 30,  # event_type String(30)
    Direction: 20,  # direction String(20)
}


def test_enum_values_fit_their_columns():
    """Every canonical value fits its activity_log column (overflow rolls back on
    PG)."""
    for enum, width in _COLUMN_WIDTHS.items():
        for member in enum:
            assert len(member.value) <= width, f"{enum.__name__}.{member.name}={member.value!r} > {width}"


def test_direction_is_only_inbound_outbound():
    """Stored direction is canonical — input synonyms (sent/received) are normalized by
    the log helpers and genuinely-unknown direction is stored as NULL, never a
    string."""
    assert {d.value for d in Direction} == {"inbound", "outbound"}
