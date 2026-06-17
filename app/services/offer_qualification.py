"""Standardized offer qualification logic.

What: condition-driven validation, standardized-note composition, qualification
      status/meter computation, vendor-memory prefill, and RFQ-back request templates
      for the sighting->offer conversion flow.
Called by: app/routers/crm/offers.py, app/routers/sightings.py, app/routers/htmx_views.py,
      app/models/offers.py (qualification_summary property).
Depends on: app.models.offers.Offer (lazy import inside DB-touching functions only).
"""

from __future__ import annotations

from typing import Any

PACKAGING_CHIPS = ("Tape & Reel", "Reels", "Trays", "Tubes", "Antistatic bags", "Boxes")
USAGE_OPTIONS = ("boards", "systems")
REFURB_BY_OPTIONS = ("supplier", "third_party")
REQUEST_KINDS = ("images", "fpq", "cert", "pkg_qty")

_LEGACY_CONDITION = {
    "used": "pulls",
    "pull": "pulls",
    "pulls": "pulls",
    "pulled": "pulls",
    "refurbished": "refurb",
    "recertified": "refurb",
    "refurb": "refurb",
    "new": "new",
    "new_no_pkg": "new_no_pkg",
    "new_no_packaging": "new_no_pkg",
}
_VALID_CONDITIONS = {"new", "new_no_pkg", "pulls", "refurb"}

_USAGE_HUMAN = {"boards": "boards", "systems": "systems"}
_WHO_HUMAN = {"supplier": "the supplier", "third_party": "a third party"}

_REQUEST_TEMPLATES = {
    "images": "Please provide images of all angles, markings, contact points, and packaging for {mpn}.",
    "fpq": "Please confirm the factory package quantity (FPQ) for {mpn}.",
    "cert": "Please provide the third-party refurbishment certification document for {mpn}.",
    "pkg_qty": "Please confirm the package quantity and how the parts are packaged for {mpn}.",
}


class QualificationError(Exception):
    """Raised when an offer is missing a per-condition essential.

    Carries `.errors`.
    """

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def normalize_offer_condition(raw: str | None) -> str | None:
    if not raw:
        return None
    v = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    v = _LEGACY_CONDITION.get(v, v)
    return v if v in _VALID_CONDITIONS else None


def _s(data: dict, key: str) -> str:
    return str(data.get(key) or "").strip()


def _require_packaging(data: dict, errors: list[str]) -> None:
    pkg = _s(data, "packaging")
    if not pkg:
        errors.append("Packaging is required; 'bulk' is not acceptable.")
    elif pkg.lower() in ("bulk", "loose") or pkg not in PACKAGING_CHIPS:
        errors.append(f"Packaging must be one of {', '.join(PACKAGING_CHIPS)} — 'bulk' is not acceptable.")


def validate_essentials(condition: str | None, data: dict) -> list[str]:
    errors: list[str] = []
    if not condition:
        return errors  # unset is allowed to save
    if condition == "new":
        if not _s(data, "manufacturer"):
            errors.append("Manufacturer is required for New (original packaging) offers.")
    elif condition == "new_no_pkg":
        _require_packaging(data, errors)
    elif condition == "pulls":
        _require_packaging(data, errors)
        if data.get("usage") not in USAGE_OPTIONS:
            errors.append("Usage (pulled from boards or systems) is required for Pulls.")
    elif condition == "refurb":
        if data.get("refurbished_by") not in REFURB_BY_OPTIONS:
            errors.append("Refurbished-by (supplier or 3rd-party) is required for Refurbs.")
        if not _s(data, "refurb_process"):
            errors.append("Refurbishment process is required for Refurbs.")
    return errors


def compose_note(condition: str | None, data: dict) -> str:
    pkg = _s(data, "packaging")
    if condition == "new":
        return "New — parts are in the original manufacturer's packaging."
    if condition == "new_no_pkg":
        note = (
            f"New, no original manufacturer packaging. Packaged in {pkg}."
            if pkg
            else "New, no original manufacturer packaging."
        )
        pc = _s(data, "part_condition")
        return f"{note} {pc}" if pc else note
    if condition == "pulls":
        usage = _USAGE_HUMAN.get(data.get("usage"), "")
        if pkg and usage:
            note = f"Pulls — packaged in {pkg}, pulled from {usage}."
        elif pkg:
            note = f"Pulls — packaged in {pkg}."
        elif usage:
            note = f"Pulls — pulled from {usage}."
        else:
            note = "Pulls."
        pc = _s(data, "part_condition")
        return f"{note} Condition: {pc}." if pc else note
    if condition == "refurb":
        who = _WHO_HUMAN.get(data.get("refurbished_by"), "")
        note = f"Refurbished by {who}." if who else "Refurbished."
        proc = _s(data, "refurb_process")
        if proc:
            note += f" Process: {proc}."
        if data.get("refurbished_by") == "third_party":
            cert = data.get("cert_doc")
            if cert == "yes":
                note += " Certifying document on file."
            elif cert == "no":
                note += " No certifying document."
        return note
    return ""


def _items_for(condition: str, data: dict, has_images: bool) -> dict[str, bool]:
    pkg = bool(_s(data, "packaging"))
    dc = bool(_s(data, "date_code"))
    if condition == "new":
        return {"manufacturer": bool(_s(data, "manufacturer")), "package_type": pkg, "date_code": dc}
    if condition == "new_no_pkg":
        return {"packaging": pkg, "images": has_images, "date_code": dc}
    if condition == "pulls":
        return {
            "packaging": pkg,
            "usage": data.get("usage") in USAGE_OPTIONS,
            "images": has_images,
            "part_condition": bool(_s(data, "part_condition")),
        }
    if condition == "refurb":
        items = {
            "refurbished_by": data.get("refurbished_by") in REFURB_BY_OPTIONS,
            "refurb_process": bool(_s(data, "refurb_process")),
            "images": has_images,
        }
        if data.get("refurbished_by") == "third_party":
            items["cert_doc"] = data.get("cert_doc") in ("yes", "no")
        return items
    return {}


def meter(condition: str | None, data: dict, has_images: bool) -> tuple[int, int]:
    if not condition:
        return (0, 0)
    items = _items_for(condition, data, has_images)
    return (sum(1 for ok in items.values() if ok), len(items))


def compute_status(condition: str | None, data: dict, has_images: bool) -> str:
    if not condition:
        return "unset"
    if validate_essentials(condition, data):
        return "incomplete"
    filled, total = meter(condition, data, has_images)
    return "complete" if filled >= total else "essentials"


def _data_from_offer(offer: Any) -> dict:
    q = dict(offer.qualification or {})
    return {
        "manufacturer": offer.manufacturer,
        "packaging": offer.packaging,
        "date_code": offer.date_code,
        "usage": q.get("usage"),
        "refurbished_by": q.get("refurbished_by"),
        "refurb_process": q.get("refurb_process"),
        "cert_doc": q.get("cert_doc"),
        "part_condition": q.get("part_condition"),
    }


def apply_qualification(offer: Any) -> None:
    """Validate essentials, compose the standardized note, compute status; set the
    columns.

    Raises QualificationError(list[str]) when a per-condition essential is missing.
    """
    data = _data_from_offer(offer)
    errors = validate_essentials(offer.condition, data)
    if errors:
        raise QualificationError(errors)
    has_images = bool(getattr(offer, "attachments", None))
    offer.qualification_note = compose_note(offer.condition, data)
    offer.qualification_status = compute_status(offer.condition, data, has_images)


def request_template(kind: str, mpn: str | None) -> str:
    tpl = _REQUEST_TEMPLATES.get(kind)
    if not tpl:
        raise ValueError(f"Unknown request kind: {kind}")
    return tpl.format(mpn=mpn or "this part")


def prefill_from_vendor(db, vendor_name_normalized: str | None) -> dict:
    """Vendor-memory: pull stable answers from this vendor's most-recent offer (#8)."""
    if not vendor_name_normalized:
        return {}
    from app.models.offers import Offer

    prev = (
        db.query(Offer)
        .filter(Offer.vendor_name_normalized == vendor_name_normalized)
        .order_by(Offer.created_at.desc())
        .first()
    )
    if not prev:
        return {}
    out: dict = {}
    if prev.country_of_origin:
        out["country_of_origin"] = prev.country_of_origin
    pq = prev.qualification or {}
    if pq.get("refurbished_by"):
        out["refurbished_by"] = pq["refurbished_by"]
    if pq.get("terms"):
        out["terms"] = pq["terms"]
    return out
