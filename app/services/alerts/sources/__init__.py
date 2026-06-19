"""Concrete AlertSource implementations + central registration.

Each module defines a single AlertSource subclass; this package wires them to their nav
tabs. Importing this package (the badge/seen routers do) registers every source, so the
registry is fully populated. Tab keys match the nav item ids in mobile_nav.html.
"""

from ..registry import register
from .buyplan import BuyplanActionSource
from .inbound_customer import InboundCustomerSource
from .offers import OfferConfirmedSource

register("requisitions", OfferConfirmedSource())  # Sales Hub
register("buy-plans", BuyplanActionSource())  # Buy Plans is its own primary nav tab
register("crm", InboundCustomerSource())  # CRM — inbound from a customer

__all__ = ["OfferConfirmedSource", "BuyplanActionSource", "InboundCustomerSource"]
