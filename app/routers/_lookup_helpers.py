"""Shared model lookup helpers for router endpoints.

Called by: all router files that need model-by-id lookups
Depends on: SQLAlchemy Session, HTTPException
"""

from fastapi import HTTPException
from sqlalchemy.orm import Session


def get_requisition_or_404(db: Session, req_id: int):
    from ..models.sourcing import Requisition

    req = db.get(Requisition, req_id)
    if not req:
        raise HTTPException(404, "Requisition not found")
    return req


def get_requirement_or_404(db: Session, req_id: int):
    from ..models.sourcing import Requirement

    req = db.get(Requirement, req_id)
    if not req:
        raise HTTPException(404, "Requirement not found")
    return req


def get_offer_or_404(db: Session, offer_id: int):
    from ..models.offers import Offer

    offer = db.get(Offer, offer_id)
    if not offer:
        raise HTTPException(404, "Offer not found")
    return offer


def get_vendor_card_or_404(db: Session, card_id: int):
    from ..models.vendors import VendorCard

    card = db.get(VendorCard, card_id)
    if not card:
        raise HTTPException(404, "Vendor not found")
    return card
