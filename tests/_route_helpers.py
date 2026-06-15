"""Tree-aware route iterator for tests asserting on FastAPI route registration.

Purpose: Flatten ``app.routes`` into the individual ``APIRoute`` objects, transparently
    descending into the ``_IncludedRouter`` wrappers that fastapi 0.137 introduced for
    ``app.include_router()``'d routes (PR #15745 turned ``app.routes`` into a tree whose
    wrapper nodes expose only ``.original_router``, not ``.path``/``.methods``). Degrades
    to a flat walk when ``original_router`` is absent, so it is correct on fastapi 0.136.x
    too (where ``app.routes`` is already flat).
Called by: tests/test_security_fixes.py, tests/test_no_auto_search.py.
Depends on: nothing beyond the route objects passed in (no fastapi import — duck-typed).
"""

from __future__ import annotations


def iter_routes(routes):
    """Yield the flat route objects, descending into included-router wrappers."""
    for r in routes:
        orig = getattr(r, "original_router", None)
        if orig is not None and hasattr(orig, "routes"):
            yield from iter_routes(orig.routes)
            continue
        yield r
