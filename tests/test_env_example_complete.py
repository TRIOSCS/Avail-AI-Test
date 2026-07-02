"""CFG-6 — .env.example must document every Settings field (no silent drift).

The audit found .env.example documented ~40 of ~143 settings, so operators had no
reference for the majority of feature flags and tunables. This guard fails the moment a
new Settings field is added without a matching .env.example entry, keeping the reference
complete going forward.

Called by: pytest
Depends on: app.config.Settings, .env.example (repo root).
"""

import re
from pathlib import Path

from app.config import Settings

# Fields that PR #670 removes from Settings — intentionally NOT documented. Once #670
# merges these no longer exist on Settings, so this set becomes a harmless no-op; drop it.
_PENDING_REMOVAL = {
    "min_tag_confidence",
    "email_mining_lookback_days",
    "offer_attribution_days",
    "vendor_protection_drop_days",
    "routing_window_hours",
    "collision_lookback_days",
    "buyplan_escalate_manager_hours",
    "hunter_cooldown_minutes",
    "on_demand_enrichment_enabled",
    "prospecting_resurface_days",
}


def _documented_keys() -> set[str]:
    env_example = Path(__file__).resolve().parent.parent / ".env.example"
    keys = set()
    for line in env_example.read_text().splitlines():
        m = re.match(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", line)
        if m:
            keys.add(m.group(1).lower())
    return keys


def test_env_example_documents_every_settings_field():
    fields = set(Settings.model_fields)
    documented = _documented_keys()
    missing = sorted(f for f in fields if f not in documented and f not in _PENDING_REMOVAL)
    assert not missing, (
        "These app/config.py Settings fields are undocumented in .env.example: "
        f"{missing}. Add each as `UPPERCASE_NAME=<default>` under the matching section."
    )
