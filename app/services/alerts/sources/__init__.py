"""Concrete AlertSource implementations + central registration.

Each module defines a single AlertSource subclass; this package wires them to their nav
tabs. Importing this package (the badge/seen routers do) registers every source, so the
registry is fully populated. Tab keys match the nav item ids in mobile_nav.html.
"""

from ..registry import register
from .approvals import ApprovalRequestActionSource
from .buyplan import BuyplanActionSource
from .inbound_customer import InboundCustomerSource
from .offers import OfferConfirmedSource
from .resourcing import BuyplanResourcingSource
from .tasks import TasksActionSource

register("requisitions", OfferConfirmedSource())  # Sales Hub
register("buy-plans", BuyplanActionSource())  # Buy Plans is its own primary nav tab
register("buy-plans", BuyplanResourcingSource())  # open re-sourcing pool (adds to the tab badge)
register("crm", InboundCustomerSource())  # CRM — inbound from a customer
register("my-day", TasksActionSource())  # My Day — open tasks assigned to me
register(
    "buy-plans", ApprovalRequestActionSource()
)  # Approvals folded into the Buy Plans hub — count merges onto its badge

__all__ = [
    "OfferConfirmedSource",
    "BuyplanActionSource",
    "BuyplanResourcingSource",
    "InboundCustomerSource",
    "TasksActionSource",
    "ApprovalRequestActionSource",
]
