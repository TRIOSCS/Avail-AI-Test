"""PDF document generation using WeasyPrint."""

import logging
from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

_jinja_env = Environment(
    loader=FileSystemLoader("app/templates/documents"),
    autoescape=True,
)


def generate_rfq_summary_pdf(requisition_id: int, db: Session) -> bytes:
    """Generate a PDF summary of a requisition with its requirements and offers."""
    from app.models import Requisition, Requirement, Offer

    requisition = db.get(Requisition, requisition_id)
    if not requisition:
        raise ValueError(f"Requisition {requisition_id} not found")

    requirements = (
        db.query(Requirement)
        .filter_by(requisition_id=requisition_id)
        .order_by(Requirement.id)
        .all()
    )

    offers = (
        db.query(Offer)
        .filter_by(requisition_id=requisition_id)
        .order_by(Offer.created_at.desc())
        .all()
    )

    template = _jinja_env.get_template("rfq_summary.html")
    html = template.render(
        requisition=requisition,
        requirements=requirements,
        offers=offers,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    from weasyprint import HTML
    return HTML(string=html).write_pdf()


def generate_quote_report_pdf(quote_id: int, db: Session) -> bytes:
    """Generate a PDF report for a quote."""
    from app.models import Quote, CustomerSite, Company

    quote = db.get(Quote, quote_id)
    if not quote:
        raise ValueError(f"Quote {quote_id} not found")

    customer_site = db.get(CustomerSite, quote.customer_site_id) if quote.customer_site_id else None
    company = db.get(Company, customer_site.company_id) if customer_site else None

    line_items = quote.line_items or []

    template = _jinja_env.get_template("quote_report.html")
    html = template.render(
        quote=quote,
        customer_site=customer_site,
        company=company,
        line_items=line_items,
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    )

    from weasyprint import HTML
    return HTML(string=html).write_pdf()
