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
      (c) "Demo · Awarded FPGA lot"          — ``awarded`` DERIVED through the real
          ``excess_service.award_offer`` chokepoint (a genuine WON offer from the demo
          broker, buyer-attributed, with real rollups/line statuses).

Offers land via the real ``excess_service.submit_offer`` (so rollups + the unmatched
queue behave exactly as in production); per-line lists are mirrored via
``excess_mirror.sync_list_mirror`` so the Sighting live-mirror is exercised too.

Idempotency model: a fixed title per list + a fixed offerer/owner email. A re-run
finds the existing rows and (for offers) skips creation when the list already has
offers — so re-seeding does not stack duplicate offers. A re-run also re-arms the
collecting demo when the nightly expiry has decayed it (expired / lapsed close_at):
status back to ``collecting``, ``close_at`` pushed forward, mirror re-synced.

Called by: an operator (manually, post-deploy). NOT cron — it's a demo fixture.
Depends on: app.database.SessionLocal, models, excess_service, excess_mirror.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from loguru import logger
from sqlalchemy.orm import Session

from ..constants import ExcessLineItemStatus, ExcessListStatus, ExcessOfferScope, UserRole
from ..models import Company, User
from ..models.excess import ExcessLineItem, ExcessList, ExcessOffer
from ..models.vendors import VendorCard
from ..services import excess_mirror, excess_service
from ..utils.normalization import normalize_mpn_key
from ..vendor_utils import normalize_vendor_name

# Stable identity keys — the whole seed keys off these so it is re-run safe.
_TRADER_EMAIL = "demo.trader@trioscs.com"
_BROKER_EMAIL = "demo.broker@trioscs.com"
_CUSTOMER_NAME = "Demo Excess Customer Inc."
# The broker's own company — the BUYER counterparty the awarded offer is attributed to
# (canonicalized to a VendorCard via submit_offer's buyer_company_id, exactly like the
# module's manual-log / outreach flows).
_BROKER_COMPANY_NAME = "Demo Broker Trading Co."

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
    return datetime.now(UTC)


# ── find-or-create helpers ───────────────────────────────────────────


def _get_or_create_user(db: Session, email: str, name: str, role: str) -> User:
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None:
        user = User(email=email, name=name, role=role, created_at=_now())
        db.add(user)
        db.flush()
        logger.info("seed-resell: created user {} ({})", email, role)
    return user


def _get_or_create_company(db: Session, name: str, account_type: str = "Customer") -> Company:
    co = db.query(Company).filter(Company.name == name).one_or_none()
    if co is None:
        co = Company(name=name, account_type=account_type, is_active=True, created_at=_now())
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
        el.close_at = _now() + timedelta(days=close_in_days)  # type: ignore[assignment]  # legacy Column-model ORM noise
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


def _refresh_demo_window(db: Session, el: ExcessList, *, close_in_days: int) -> None:
    """Re-arm a decayed demo posting window on an idempotent re-seed (finding #62).

    The nightly expiry job (``expire_overdue_lists``) flips any open/collecting list past
    ``close_at`` to terminal ``expired`` and retires its Sighting mirror — so the flagship
    collecting demo silently dies a few nights after seeding, and find-by-title used to
    return early without restoring it. On a found (not created) list, when the demo has
    decayed — status ``expired``, or a still-unresolved (open/collecting) window whose
    ``close_at`` has lapsed — re-stamp what a fresh seed would set: status back to
    ``COLLECTING``, ``close_at`` pushed ``close_in_days`` forward, ``updated_at`` now, and
    the mirror re-synced so the lines advertise as live supply again. Deliberately does
    NOT touch awarded/bid_out/closed lists: those record a user's own actions on the demo
    (an award, a sent bid) and a re-seed must never undo them. Commits.
    """
    now = _now()
    close_at = el.close_at
    if close_at is not None and close_at.tzinfo is None:  # SQLite strips tzinfo
        close_at = close_at.replace(tzinfo=UTC)
    expired = el.status == ExcessListStatus.EXPIRED
    lapsed = (
        el.status in (ExcessListStatus.OPEN, ExcessListStatus.COLLECTING) and close_at is not None and close_at <= now
    )
    if not (expired or lapsed):
        return
    el.status = ExcessListStatus.COLLECTING  # type: ignore[assignment]  # legacy Column-model ORM noise
    el.close_at = now + timedelta(days=close_in_days)  # type: ignore[assignment]  # legacy Column-model ORM noise
    el.updated_at = now  # type: ignore[assignment]  # legacy Column-model ORM noise
    db.flush()
    excess_mirror.sync_list_mirror(db, el)
    db.commit()
    logger.info("seed-resell: refreshed decayed demo window on {!r} (collecting, +{}d)", el.title, close_in_days)


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
        el.total_line_items = len(_BULK_MPNS)  # type: ignore[assignment]  # legacy Column-model ORM noise
        db.commit()
        # Mirror so the Sighting live-mirror is exercised (and matchers see supply).
        excess_mirror.sync_list_mirror(db, el)
        db.commit()
    else:
        # The nightly expiry decays this demo after close_in_days nights; a re-seed
        # must re-arm the window instead of returning early (finding #62).
        _refresh_demo_window(db, el, close_in_days=3)

    if _list_has_offers(db, el):
        return  # already seeded offers — don't stack duplicates

    lines = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    if not lines:
        return

    # Several per-line offers across the first few lines, with varied prices so the
    # rollup picks a clear best + the spread bar has range.
    excess_service.submit_offer(
        db,
        list_id=el.id,  # type: ignore[arg-type]  # legacy Column-model ORM noise
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
        list_id=el.id,  # type: ignore[arg-type]  # legacy Column-model ORM noise
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
        list_id=el.id,  # type: ignore[arg-type]  # legacy Column-model ORM noise
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
        el.total_line_items = 1  # type: ignore[assignment]  # legacy Column-model ORM noise
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
        list_id=el.id,  # type: ignore[arg-type]  # legacy Column-model ORM noise
        user=broker,
        scope=ExcessOfferScope.PER_LINE,
        notes="Standard price.",
        lines=[{"mpn_raw": line.part_number, "quantity": 24, "unit_price": Decimal("28.0000"), "lead_time_days": 5}],
    )
    excess_service.submit_offer(
        db,
        list_id=el.id,  # type: ignore[arg-type]  # legacy Column-model ORM noise
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


def _build_awarded(db: Session, company: Company, owner: User, broker: User) -> None:
    """Seed the awarded shape through the REAL award chain (finding #60).

    The list is seeded like the others (``open`` + available lines + mirror), the demo
    broker submits a genuine full-coverage per-line offer via
    ``excess_service.submit_offer`` — attributed to the broker's company via
    ``buyer_company_id``, as the module's manual-log flow does — and the award is driven
    through ``excess_service.award_offer`` (the single chokepoint where an ExcessOffer
    becomes ``won``). So the WON offer, per-line rollups, awarded line statuses, the
    list's derived ``awarded`` status, the retired mirror, and the buyer's BuyerScore are
    all the service's own output — the Offers tab renders the emerald won card and
    Unaward is demonstrable, instead of the old hand-stamped zero-offer state.
    """
    el, created = _get_or_create_list(
        db,
        title=_LIST_AWARDED,
        company=company,
        owner=owner,
        status=ExcessListStatus.OPEN,
        close_in_days=None,
    )
    if created:
        for mpn, mfr in _BULK_MPNS[:6]:
            _add_line(db, el, mpn=mpn, mfr=mfr, qty=200)
        el.total_line_items = 6  # type: ignore[assignment]  # legacy Column-model ORM noise
        db.commit()
        excess_mirror.sync_list_mirror(db, el)
        db.commit()

    if _list_has_offers(db, el):
        logger.info("seed-resell: ensured awarded list {!r}", el.title)
        return

    lines = db.query(ExcessLineItem).filter_by(excess_list_id=el.id).order_by(ExcessLineItem.id).all()
    if not lines:
        return

    # Legacy decay (finding #60 follow-up): the OLD seeder hand-stamped this demo
    # awarded with ZERO offers — a shape unreachable through the app. Left as-is,
    # submit_offer would commit a dangling late offer and award_offer would then 409
    # on the already-awarded lines, aborting the seed mid-run (and the NEXT run's
    # offers-exist short-circuit would falsely report success). Mirroring the #62
    # refresh philosophy: reset to what a fresh seed writes (open list, available
    # lines, live mirror) and re-derive the award through the real chain below.
    if not created and (
        el.status == ExcessListStatus.AWARDED or any(ln.status == ExcessLineItemStatus.AWARDED for ln in lines)
    ):
        el.status = ExcessListStatus.OPEN  # type: ignore[assignment]  # legacy Column-model ORM noise
        el.updated_at = _now()  # type: ignore[assignment]  # legacy Column-model ORM noise
        for ln in lines:
            ln.status = ExcessLineItemStatus.AVAILABLE  # type: ignore[assignment]  # legacy Column-model ORM noise
            ln.best_offer_id = None  # hand-stamped legacy rollup, no offer behind it
        db.flush()
        excess_mirror.sync_list_mirror(db, el)
        db.commit()
        logger.info("seed-resell: healed legacy hand-stamped awarded demo {!r} (reset to open, re-deriving)", el.title)

    buyer_co = _get_or_create_company(db, _BROKER_COMPANY_NAME, account_type="Partner")
    offer = excess_service.submit_offer(
        db,
        list_id=el.id,  # type: ignore[arg-type]  # legacy Column-model ORM noise
        user=broker,
        scope=ExcessOfferScope.PER_LINE,
        notes="Full-lot FPGA buyer — takes every line.",
        buyer_company_id=buyer_co.id,  # type: ignore[arg-type]  # legacy Column-model ORM noise
        lines=[
            {
                "mpn_raw": line.part_number,
                "quantity": line.quantity,
                "unit_price": Decimal("18.0000") + Decimal(idx) * Decimal("3.2500"),
                "lead_time_days": 7 + idx,
            }
            for idx, line in enumerate(lines)
        ],
    )
    excess_service.award_offer(db, offer.id, owner)  # type: ignore[arg-type]  # legacy Column-model ORM noise
    db.commit()
    logger.info("seed-resell: awarded {!r} through award_offer (won offer + rollups derived)", el.title)


# ── reset ────────────────────────────────────────────────────────────


def _reset(db: Session) -> None:
    """Delete the demo lists (cascades to lines + offers), the demo users/companies, and
    the awarded demo's buyer VendorCard (its BuyerScore rows cascade at the DB
    level)."""
    lists = db.query(ExcessList).filter(ExcessList.title.in_(_DEMO_TITLES)).all()
    for el in lists:
        # Delete the Sighting mirror + virtual scratch req before the list, else its
        # customer_excess sightings would strand (advertising deleted demo supply).
        excess_mirror.teardown_list_mirror(db, el)
        db.delete(el)
    db.commit()
    for email in (_TRADER_EMAIL, _BROKER_EMAIL):
        u = db.query(User).filter(User.email == email).one_or_none()
        if u:
            db.delete(u)
    for name in (_CUSTOMER_NAME, _BROKER_COMPANY_NAME):
        co = db.query(Company).filter(Company.name == name).one_or_none()
        if co:
            db.delete(co)
    # The awarded build attributes its offer to the broker company, which counterparty_card
    # canonicalized to a VendorCard (+ BuyerScore via the win-hook) — remove it too, or a
    # reset would strand a demo buyer card advertising wins.
    card = (
        db.query(VendorCard)
        .filter(VendorCard.normalized_name == normalize_vendor_name(_BROKER_COMPANY_NAME))
        .one_or_none()
    )
    if card:
        db.delete(card)  # buyer_scores.vendor_card_id is ondelete=CASCADE
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
    _build_awarded(db, company, trader, broker)
    logger.info("seed-resell: done — open /v2/resell as the Demo Trader to view all three shapes.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the Trading workspace demo data (idempotent).")
    parser.add_argument("--reset", action="store_true", help="Delete the demo data, then exit.")
    args = parser.parse_args(argv)

    # Hard production guard: refuse to seed synthetic demo data unless the operator has
    # explicitly opted in (same ALLOW_SAMPLE_DATA_SEED flag as the AVSAMPLE seeder). --reset
    # only deletes tagged demo rows, so it's allowed without the flag. Checked BEFORE opening
    # a DB session so a refused run never touches whatever database SessionLocal resolves to.
    if not args.reset and os.getenv("ALLOW_SAMPLE_DATA_SEED", "").strip().lower() not in {"1", "true", "yes", "on"}:
        print(
            "REFUSED: resell demo seeding is disabled unless ALLOW_SAMPLE_DATA_SEED=true is set "
            "(guards against injecting demo data into production). Set it explicitly to seed.",
            file=sys.stderr,
        )
        raise SystemExit(2)

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
