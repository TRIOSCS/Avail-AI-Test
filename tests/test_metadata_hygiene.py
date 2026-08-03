"""test_metadata_hygiene.py — Declarative-metadata FK-graph hygiene.

Guards that ``Base.metadata.sorted_tables`` can topologically order EVERY
table without dropping FK edges: an unresolvable cycle makes SQLAlchemy emit
``SAWarning: Cannot correctly sort tables`` and silently ignore those FK
constraints in the sort — noise in every test run and a trap for anything
that relies on create/delete ordering. The known companies → site_contacts →
customer_sites → companies cycle must stay broken (back-edge FK declared with
``use_alter=True``).

Called by: pytest
Depends on: app/models (Base + every model module's table metadata)
"""

import warnings

from sqlalchemy.exc import SAWarning

from app.models import Base


def test_sorted_tables_emits_no_unresolvable_cycle_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        order = Base.metadata.sorted_tables

    cycle_warnings = [
        w for w in caught if issubclass(w.category, SAWarning) and "cannot correctly sort" in str(w.message).lower()
    ]
    assert cycle_warnings == [], f"unresolvable FK cycle(s): {[str(w.message) for w in cycle_warnings]}"
    assert len(order) == len(Base.metadata.tables)
