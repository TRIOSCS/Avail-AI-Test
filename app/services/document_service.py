"""PDF document generation using WeasyPrint."""

from datetime import datetime, timezone

from jinja2 import Environment, FileSystemLoader
from sqlalchemy.orm import Session

_jinja_env = Environment(
    loader=FileSystemLoader("app/templates/documents"),
    autoescape=True,
)


def _render_pdf(template_name: str, **context) -> bytes:
    """Render a document template to PDF, injecting the shared ``generated_at``
    stamp."""
    from weasyprint import HTML

    template = _jinja_env.get_template(template_name)
    html = template.render(
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        **context,
    )
    return HTML(string=html).write_pdf()


def generate_rfq_summary_pdf(requisition_id: int, db: Session) -> bytes:
    """Generate a PDF summary of a requisition with its requirements and offers."""
    from app.models import Offer, Requirement, Requisition

    requisition = db.get(Requisition, requisition_id)
    if not requisition:
        raise ValueError(f"Requisition {requisition_id} not found")

    requirements = db.query(Requirement).filter_by(requisition_id=requisition_id).order_by(Requirement.id).all()

    offers = db.query(Offer).filter_by(requisition_id=requisition_id).order_by(Offer.created_at.desc()).all()

    return _render_pdf(
        "rfq_summary.html",
        requisition=requisition,
        requirements=requirements,
        offers=offers,
    )


def generate_quote_report_pdf(quote_id: int, db: Session) -> bytes:
    """Generate a PDF report for a quote."""
    from app.models import Company, CustomerSite, Quote

    quote = db.get(Quote, quote_id)
    if not quote:
        raise ValueError(f"Quote {quote_id} not found")

    customer_site = db.get(CustomerSite, quote.customer_site_id) if quote.customer_site_id else None
    company = db.get(Company, customer_site.company_id) if customer_site else None

    line_items = quote.line_items or []

    return _render_pdf(
        "quote_report.html",
        quote=quote,
        customer_site=customer_site,
        company=company,
        line_items=line_items,
    )


def generate_bid_report_pdf(bid_id: int, db: Session) -> bytes:
    """Generate the CLEAN customer-facing bid-back PDF (Chunk E).

    Cloned from the Quote report path (``quote_report.html`` → WeasyPrint). The template
    renders ONLY the whitelisted payload from ``bid_back_service.bid_back_export_context``
    — no Vendor / trader column, no seller-company identity. Cleanliness is enforced at
    assembly (the context strips every leaky field), so the template cannot accidentally
    surface one.
    """
    from app.models.excess import CustomerBid
    from app.services.bid_back_service import bid_back_export_context

    bid = db.get(CustomerBid, bid_id)
    if not bid:
        raise ValueError(f"CustomerBid {bid_id} not found")

    ctx = bid_back_export_context(bid)
    return _render_pdf("bid_report.html", **ctx)
