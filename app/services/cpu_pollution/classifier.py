"""Pure prefix classifier for the cpu-bucket cleanup.

What: classify_polluted_mpn(mpn) -> canonical commodity key | None. Precision-first — returns
    None for any real Intel/AMD CPU identifier and for any MPN without a definitive non-CPU
    manufacturer prefix. Called by: app/management/fix_cpu_pollution.py.
Depends on: prefix_map.PREFIX_RULES + CPU_GUARD.
"""

from __future__ import annotations

from app.services.cpu_pollution.prefix_map import CPU_GUARD, PREFIX_RULES


def classify_polluted_mpn(mpn: str | None) -> str | None:
    """Return the correct commodity for a definitively-non-CPU `cpu`-bucket MPN, else
    None."""
    if not mpn:
        return None
    s = mpn.strip().upper()
    if not s:
        return None
    for guard in CPU_GUARD:
        if guard.search(s):
            return None
    for pattern, commodity in PREFIX_RULES:
        if pattern.search(s):
            return commodity
    return None
