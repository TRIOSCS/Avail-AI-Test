"""htmx/_constants.py — Column picker constants for requirements, offers, and parts.

Called by: requisitions.py, parts.py
Depends on: nothing
"""

# ── Requirements Tab Column Picker ───────────────────────────────────────────

_ALL_REQ_COLUMNS = [
    ("mpn", "MPN"),
    ("brand", "Brand"),
    ("qty", "Qty"),
    ("target_price", "Target Price"),
    ("customer_pn", "Customer PN"),
    ("need_by_date", "Need-by Date"),
    ("condition", "Condition"),
    ("date_codes", "Date Codes"),
    ("firmware", "Firmware"),
    ("hardware_codes", "Hardware Codes"),
    ("packaging", "Packaging"),
    ("notes", "Notes"),
    ("substitutes", "Substitutes"),
    ("status", "Status"),
    ("sightings", "Sightings"),
]

_DEFAULT_REQ_COLUMNS = [
    "mpn",
    "brand",
    "qty",
    "target_price",
    "customer_pn",
    "need_by_date",
    "status",
    "sightings",
]

# ── Offers Tab Column Picker ────────────────────────────────────────────────

_ALL_OFFER_COLUMNS = [
    ("vendor", "Vendor"),
    ("mpn", "MPN"),
    ("qty", "Qty"),
    ("price", "Price"),
    ("condition", "Condition"),
    ("date_code", "Date Code"),
    ("lead_time", "Lead Time"),
    ("manufacturer", "Manufacturer"),
    ("moq", "MOQ"),
    ("spq", "SPQ"),
    ("packaging", "Packaging"),
    ("firmware", "Firmware"),
    ("hardware_code", "Hardware Code"),
    ("warranty", "Warranty"),
    ("country", "Country"),
    ("valid_until", "Valid Until"),
    ("notes", "Notes"),
    ("status", "Status"),
]

_DEFAULT_OFFER_COLUMNS = [
    "vendor",
    "mpn",
    "qty",
    "price",
    "condition",
    "date_code",
    "lead_time",
    "status",
]

# ── Parts Workspace Column Picker ────────────────────────────────────────────

# Default columns shown when user has no saved preference
_DEFAULT_PARTS_COLUMNS = [
    "mpn",
    "brand",
    "qty",
    "target_price",
    "status",
    "requisition",
    "customer",
    "offers",
    "best_price",
    "owner",
    "created",
]

# All available columns for the column picker
_ALL_PARTS_COLUMNS = [
    ("mpn", "MPN"),
    ("brand", "Brand"),
    ("qty", "Qty Needed"),
    ("target_price", "Target Price"),
    ("status", "Status"),
    ("requisition", "Requisition"),
    ("customer", "Customer"),
    ("offers", "Offers"),
    ("best_price", "Best Price"),
    ("owner", "Owner"),
    ("created", "Created"),
    ("date_codes", "Date Codes"),
    ("condition", "Condition"),
    ("packaging", "Packaging"),
    ("customer_pn", "Customer PN"),
    ("substitutes", "Substitutes"),
    ("firmware", "Firmware"),
    ("hardware_codes", "HW Codes"),
    ("need_by_date", "Need By"),
    ("notes", "Notes"),
]
