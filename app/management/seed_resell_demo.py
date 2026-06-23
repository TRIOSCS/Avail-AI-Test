"""seed_resell_demo.py — Idempotent demo seed for the Resell workspace (Chunk F).

Run after deploy so the user can open ``/v2/resell`` on staging and judge the look
across all three deal shapes:

    python -m app.management.seed_resell_demo          # seed / re-seed (idempotent)
    python -m app.management.seed_resell_demo --reset  # delete the demo data first

Creates (all find-or-create by a stable key — safe to re-run; never duplicates):
  • a trader USER (the list owner) and a buyer USER (the offering broker — passes the
    self-offer guard since broker_id != owner_id),
  • a customer COMPANY,
  • three demo lists owned by the trader:
      (a) "Demo · Q1 surplus (collecting)"  — ~40 lines, status ``collecting``, with
          several per-line offers, one UNMATCHED queue row, and one TAKE-ALL offer,
      (b) "Demo · One-off RU heatsink"       — single line, status ``open``, 2 offers,
      (c) "Demo · Awarded FPGA lot"          — status ``awarded``.

Offers land via the real ``excess_service.submit_offer`` (so rollups + the unmatched
queue behave exactly as in production); per-line lists are mirrored via
``excess_mirror.sync_list_mirror`` so the Sighting live-mirror is exercised too.

Idempotency model: a fixed title per list + a fixed offerer/owner email. A re-run
finds the existing rows and (for offers) skips creation when the list already has
offers — so re-seeding does not stack duplicate offers.

Called by: an operator (manually, post-deploy). NOT cron — it's a demo fixture.
Depends on: app.database.SessionLocal, models, excess_service, excess_mirror.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ExcessLineItemStatus, ExcessListStatus, ExcessOfferScope, UserRole
from ..models import Company, User
from ..models.excess import ExcessLineItem, ExcessList, ExcessOffer
from ..services import excess_mirror, excess_service
from ..utils.normalization import normalize_mpn_key

# Stable identity keys — the whole seed keys off these so it is re-run safe.
_TRADER_EMAIL = "demo.trader@trioscs.com"
_BROKER_EMAIL = "demo.broker@trioscs.com"
_CUSTOMER_NAME = "Demo Excess Customer Inc."

_LIST_COLLECTING = "Demo · Q1 surplus (collecting)"
_LIST_ONEOFF = "Demo · One-off RU heatsink"
_LIST_AWARDED = "Demo · Awarded FPGA lot"

_DEMO_TITLES = (_LIST_COLLECTING, _LIST_ONEOFF, _LIST_AWARDED)

# 40 realistic-ish electronic-component MPNs for the big collecting list.
_BULK_MPNS = [
    ("XCVU9P-2FLGA2104I", "AMD Xilinx"),
    ("EP4CE10F17C8N", "Intel Altera"),
    ("LM358N", "Texas Instruments"),
    ("STM32F407VGT6", "STMicroelectronics"),
    ("ATMEGA328P-PU", "Microchip"),
    ("MAX232CPE", "Maxim"),
    ("NE555P", "Texas Instruments"),
    ("LM7805CT", "ON Semiconductor"),
    ("TL072CP", "Texas Instruments"),
    ("MCP2515-I/SO", "Microchip"),
    ("CY7C68013A-56PVXC", "Cypress"),
    ("FT232RL", "FTDI"),
    ("ESP32-WROOM-32E", "Espressif"),
    ("BC547B", "NXP"),
    ("IRF540N", "Infineon"),
    ("1N4148", "Vishay"),
    ("DS3231SN", "Maxim"),
    ("PCF8574T", "NXP"),
    ("ULN2003A", "Texas Instruments"),
    ("L7812CV", "STMicroelectronics"),
    ("AD8232ACPZ", "Analog Devices"),
    ("TPS54331DR", "Texas Instruments"),
    ("MCP3008-I/P", "Microchip"),
    ("SN74HC595N", "Texas Instruments"),
    ("CD4017BE", "Texas Instruments"),
    ("LM393P", "Texas Instruments"),
    ("AMS1117-3.3", "AMS"),
    ("W25Q128JVSIQ", "Winbond"),
    ("24LC256-I/P", "Microchip"),
    ("MAX7219CNG", "Maxim"),
    ("PIC16F877A-I/P", "Microchip"),
    ("LM2596S-ADJ", "ON Semiconductor"),
    ("HC-SR04", "Generic"),
    ("DHT22", "Aosong"),
    ("MPU-6050", "TDK InvenSense"),
    ("BME280", "Bosch"),
    ("nRF24L01+", "Nordic"),
    ("CH340G", "WCH"),
    ("XC7A35T-1FTG256C", "AMD Xilinx"),
    ("MAX31855KASA+", "Maxim"),
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── find-or-create helpers ───────────────────────────────────────────


def _get_or_create_user(db: Session, email: str, name: str, role: str) -> User:
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None:
        user = User(email=email, name=name, role=role, created_at=_now())
        db.add(user)
        db.flush()
        logger.info("seed-resell: created user {} ({})", email, role)
    return user


def _get_or_create_company(db: Session, name: str) -> Company:
    co = db.query(Company).filter(Company.name == name).one_or_none()
    if co is None:
        co = Company(name=name, account_type="Customer", is_active=True, created_at=_now())
        db.add(co)
        db.flush()
        logger.info("seed-resell: created company {}", name)
    return co


def _get_or_create_list(
    db: Session, *, title: str, company: Company, owner: User, status: str, close_in_days: int | None
) -> tuple[ExcessList, bool]:
    """Return (list, created).

    Sets status + close_at on create only.
    """
    el = db.query(ExcessList).filter(ExcessList.title == title).one_or_none()
    if el is not None:
        return el, False
    el = ExcessList(
        title=title,
        company_id=company.id,
        owner_id=owner.id,
        status=status,
        notes="Seeded demo data for the Trading workspace.",
        created_at=_now(),
    )
    # close_at is set if the column exists (additive-friendly).
    if close_in_days is not None and hasattr(el, "close_at"):
        el.close_at = _now() + timedelta(days=close_in_days)
    db.add(el)
    db.flush()
    logger.info("seed-resell: created list {!r} (status={})", title, status)
    return el, True


def _add_line(db: Session, el: ExcessList, *, mpn: str, mfr: str, qty: int, condition: str = "New") -> ExcessLineItem:
    item = ExcessLineItem(
        excess_list_id=el.id,
        part_number=mpn,
        normalized_part_number=normalize_mpn_key(mpn) or None,
        manufacturer=mfr,
        quantity=qty,
        condition=condition,
        status=ExcessLineItemStatus.AVAILABLE,
        created_at=_now(),
    )
    db.add(item)
    excess_service._resolve_line_material_card(db, item)
    return item


def _list_has_offers(db: Session, el: ExcessList) -> bool:
    return db.query(ExcessOffer.id).filter(ExcessOffer.excess_list_id == el.id).first() is not None


# ── per-list builders ────────────────────────────────────────────────


def _build_collecting(db: Session, company: Company, owner: User, broker: User) -> None:
    el, created = _get_or_create_list(
        db,
        title=_LIST_COLLECTING,
        company=company,
        owner=owner,
        status=ExcessListStatus.COLLECTING,
        close_in_days=3,
    )
    if created:
        for mpn, mfr in _BULK_MPNS:
            _add_line(db, el, mpn=mpn, mfr=mfr, qty=50 + (hash(mpn) % 950))
        el.total_line_items = len(_BULK_MPNS)
        db.commit()
        # Mirror so the Sighting live-mirror is exercised (and matchers see supply).
        excess_mirror.sync_list_mirror(db, el)
        db.commit()

    if _list_has_offers(db, el):
        return  # already seeded offers — don't stack duplicates

    lines = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    if not lines:
        return

    # Several per-line offers across the first few lines, with varied prices so the
    # rollup picks a clear best + the spread bar has range.
    excess_service.submit_offer(
        db,
        list_id=el.id,
        user=broker,
        scope=ExcessOfferScope.PER_LINE,
        notes="Bulk reseller — strong on FPGAs.",
        lines=[
            {
                "mpn_raw": lines[0].part_number,
                "quantity": 40,
                "unit_price": Decimal("142.5000"),
                "lead_time_days": 7,
                "terms_text": "DC 2023+",
            },
            {"mpn_raw": lines[1].part_number, "quantity": 120, "unit_price": Decimal("3.2000"), "lead_time_days": 5},
            {"mpn_raw": lines[2].part_number, "quantity": 500, "unit_price": Decimal("0.1800")},
            # An MPN that is NOT on the posting → unmatched queue (never dropped).
            {
                "mpn_raw": "TOTALLY-UNKNOWN-PN-999",
                "quantity": 25,
                "unit_price": Decimal("9.9900"),
                "terms_text": "as-is",
            },
        ],
    )
    excess_service.submit_offer(
        db,
        list_id=el.id,
        user=broker,
        scope=ExcessOfferScope.PER_LINE,
        notes="Competing bid — cheaper on the lead FPGA.",
        lines=[
            {"mpn_raw": lines[0].part_number, "quantity": 40, "unit_price": Decimal("138.0000"), "lead_time_days": 14},
            {"mpn_raw": lines[1].part_number, "quantity": 120, "unit_price": Decimal("3.4500"), "lead_time_days": 3},
        ],
    )
    # A take-all bundle for the whole list (pins as the headline banner).
    excess_service.submit_offer(
        db,
        list_id=el.id,
        user=broker,
        scope=ExcessOfferScope.TAKE_ALL,
        notes="Will take the entire lot, sight-unseen.",
        take_all_total_price=Decimal("48500.00"),
    )
    db.commit()
    logger.info("seed-resell: seeded offers (per-line + unmatched + take-all) on {!r}", el.title)


def _build_oneoff(db: Session, company: Company, owner: User, broker: User) -> None:
    el, created = _get_or_create_list(
        db,
        title=_LIST_ONEOFF,
        company=company,
        owner=owner,
        status=ExcessListStatus.OPEN,
        close_in_days=10,
    )
    if created:
        _add_line(db, el, mpn="DELL-412-AAVE", mfr="Dell", qty=24, condition="Refurbished")
        el.total_line_items = 1
        db.commit()
        excess_mirror.sync_list_mirror(db, el)
        db.commit()

    if _list_has_offers(db, el):
        return
    line = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).first()
    if line is None:
        return
    excess_service.submit_offer(
        db,
        list_id=el.id,
        user=broker,
        scope=ExcessOfferScope.PER_LINE,
        notes="Standard price.",
        lines=[{"mpn_raw": line.part_number, "quantity": 24, "unit_price": Decimal("28.0000"), "lead_time_days": 5}],
    )
    excess_service.submit_offer(
        db,
        list_id=el.id,
        user=broker,
        scope=ExcessOfferScope.PER_LINE,
        notes="Best price, longer lead.",
        lines=[
            {
                "mpn_raw": line.part_number,
                "quantity": 24,
                "unit_price": Decimal("24.5000"),
                "lead_time_days": 21,
                "terms_text": "tray pack",
            }
        ],
    )
    db.commit()
    logger.info("seed-resell: seeded 2 offers on the one-off {!r}", el.title)


def _build_awarded(db: Session, company: Company, owner: User) -> None:
    el, created = _get_or_create_list(
        db,
        title=_LIST_AWARDED,
        company=company,
        owner=owner,
        status=ExcessListStatus.AWARDED,
        close_in_days=None,
    )
    if created:
        for mpn, mfr in _BULK_MPNS[:6]:
            item = _add_line(db, el, mpn=mpn, mfr=mfr, qty=200)
            item.status = ExcessLineItemStatus.AWARDED
        el.total_line_items = 6
        db.commit()
    logger.info("seed-resell: ensured awarded list {!r}", el.title)


# ── reset ────────────────────────────────────────────────────────────


def _reset(db: Session) -> None:
    """Delete the demo lists (cascades to lines + offers) and the demo users/company."""
    lists = db.query(ExcessList).filter(ExcessList.title.in_(_DEMO_TITLES)).all()
    for el in lists:
        db.delete(el)
    db.commit()
    for email in (_TRADER_EMAIL, _BROKER_EMAIL):
        u = db.query(User).filter(User.email == email).one_or_none()
        if u:
            db.delete(u)
    co = db.query(Company).filter(Company.name == _CUSTOMER_NAME).one_or_none()
    if co:
        db.delete(co)
    db.commit()
    logger.info("seed-resell: reset complete ({} demo lists removed)", len(lists))


# ── entry point ──────────────────────────────────────────────────────


def seed(db: Session) -> None:
    """Idempotently seed the three demo deal shapes."""
    trader = _get_or_create_user(db, _TRADER_EMAIL, "Demo Trader", UserRole.TRADER)
    broker = _get_or_create_user(db, _BROKER_EMAIL, "Demo Broker", UserRole.BUYER)
    company = _get_or_create_company(db, _CUSTOMER_NAME)
    db.commit()

    _build_collecting(db, company, trader, broker)
    _build_oneoff(db, company, trader, broker)
    _build_awarded(db, company, trader)
    logger.info("seed-resell: done — open /v2/resell as the Demo Trader to view all three shapes.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the Trading workspace demo data (idempotent).")
    parser.add_argument("--reset", action="store_true", help="Delete the demo data, then exit.")
    args = parser.parse_args(argv)

    from ..database import SessionLocal

    db = SessionLocal()
    try:
        if args.reset:
            _reset(db)
        else:
            seed(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
