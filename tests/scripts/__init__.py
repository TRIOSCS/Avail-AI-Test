"""Tests/scripts/ — Tests for scripts/ utilities.

pytest (prepend import mode) inserts /root/availai/tests/ into sys.path
when this package is loaded (because tests/__init__.py does not exist).
This causes `import scripts` to resolve to tests/scripts/ rather than
the real /root/availai/scripts/ package — breaking the test imports.

Fix: immediately re-register sys.modules["scripts"] to point at the real
package so that `from scripts.check_schema_matches_models import ...`
resolves correctly before any test module is imported.

Called by: pytest (auto-loaded as package __init__ during collection)
Depends on: nothing (must be import-order-safe)
"""

import importlib
import importlib.util
import sys
import types

_REPO_ROOT = "/root/availai"
_REAL_SCRIPTS_PATH = _REPO_ROOT + "/scripts"

# Ensure /root/availai is at the front of sys.path so the real scripts
# package takes priority over tests/scripts/ on subsequent imports.
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
elif sys.path.index(_REPO_ROOT) != 0:
    sys.path.remove(_REPO_ROOT)
    sys.path.insert(0, _REPO_ROOT)

# Re-register the real scripts package in sys.modules.
# At this point sys.modules["scripts"] points to THIS file (tests/scripts/).
# We need to replace it with the real /root/availai/scripts/__init__.py.
_spec = importlib.util.spec_from_file_location(
    "scripts",
    _REAL_SCRIPTS_PATH + "/__init__.py",
    submodule_search_locations=[_REAL_SCRIPTS_PATH],
)
if _spec is not None:
    _real_scripts = types.ModuleType("scripts")
    _real_scripts.__spec__ = _spec
    _real_scripts.__path__ = [_REAL_SCRIPTS_PATH]  # type: ignore[attr-defined]
    _real_scripts.__package__ = "scripts"
    _real_scripts.__file__ = _REAL_SCRIPTS_PATH + "/__init__.py"
    sys.modules["scripts"] = _real_scripts
    # Execute the real __init__.py in the new module's namespace.
    _spec.loader.exec_module(_real_scripts)  # type: ignore[union-attr]
