"""Search worker configuration factory.

Creates worker config objects from environment variables using a prefix.
Each attribute is named {PREFIX}_{SETTING} to match existing usage patterns.

Called by: all worker modules via IcsConfig / NcConfig wrappers
Depends on: environment variables
"""

import os

# Common config fields: (suffix, default_value, type)
# Credential fields are worker-specific and handled by subclasses.
_COMMON_FIELDS = [
    ("MAX_DAILY_SEARCHES", "50", int),
    ("MAX_HOURLY_SEARCHES", "10", int),
    ("MIN_DELAY_SECONDS", "150", int),
    ("MAX_DELAY_SECONDS", "420", int),
    ("TYPICAL_DELAY_SECONDS", "270", int),
    ("DEDUP_WINDOW_DAYS", "7", int),
    ("BUSINESS_HOURS_START", "8", int),
    ("BUSINESS_HOURS_END", "18", int),
    ("BROWSER_PROFILE_DIR", "/root/worker_browser_profile", str),
]


def build_worker_config(prefix: str, defaults: dict | None = None):
    """Populate config attributes as {PREFIX}_{FIELD} from env vars.

    Args:
        prefix: Environment variable prefix (e.g. "ICS", "NC").
        defaults: Optional dict of {FIELD_SUFFIX: default_value} overrides
                  for the common fields.

    Returns a new object with all fields set as attributes.
    """
    merged_defaults = {f[0]: f[1] for f in _COMMON_FIELDS}
    if defaults:
        merged_defaults.update(defaults)

    obj_dict = {}
    for suffix, default, typ in _COMMON_FIELDS:
        env_key = f"{prefix}_{suffix}"
        raw = os.environ.get(env_key, merged_defaults.get(suffix, default))
        obj_dict[env_key] = typ(raw)

    return obj_dict
