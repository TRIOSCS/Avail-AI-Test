"""Deterministic re-classification of the polluted `cpu` catch-all bucket.

What: prefix-based classifier (classifier.py) + rule table (prefix_map.py) that map a
    definitively-non-CPU manufacturer MPN to its correct commodity, guarding real Intel/AMD
    CPUs. Applied by app/management/fix_cpu_pollution.py at source="cpu_pollution_fix".
Depends on: app.services.commodity_registry (vocab), stdlib re.
"""
