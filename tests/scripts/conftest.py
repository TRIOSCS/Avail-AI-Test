"""tests/scripts/conftest.py — pytest conftest for tests/scripts/.

Under importlib mode, tests/scripts/__init__.py redirects sys.modules["scripts"]
to the real /root/availai/scripts/ package. This causes a path-mismatch when
pytest tries to load this file as "scripts.conftest" while scripts/conftest.py
is already registered under that name.

Fix: pre-remove any stale "scripts.conftest" entry from sys.modules so pytest
can register this file cleanly.

Called by: pytest (auto-loaded as conftest during collection)
Depends on: nothing
"""

import sys

# Remove any stale mapping so pytest can register THIS file as scripts.conftest.
sys.modules.pop("scripts.conftest", None)
