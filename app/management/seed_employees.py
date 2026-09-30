"""seed_employees.py — Idempotent TRIO employee → AVAIL user seeder + 8x8 mapping.

Seeds the users table from the company Employee Contact List (2026-09) and maps
the Trading desk (sales / buyers / traders) to their 8x8 phone extensions so the
8x8 CDR poller (app/jobs/eight_by_eight_jobs.py) attributes their calls.

    scripts/mgmt.sh seed_employees            # dry-run: print the plan, write nothing
    scripts/mgmt.sh seed_employees --apply    # create missing users / fill extensions

Behavior (idempotent, additive, never destructive):
  * Lookup by lowercased email. A missing user is created (name, role, extension,
    8x8 flag) and an INVITE audit row is written with actor NULL ("system").
  * An EXISTING user is never modified except: an empty eight_by_eight_extension
    is filled from the sheet, and eight_by_eight_enabled is switched on only in
    that same first-fill moment for Trading-desk rows of ACTIVE users (a
    deactivated employee gets the extension but never re-enters the poller).
    Role, name, active state, and any admin-edited phone mapping are left
    untouched.
  * eight_by_eight_enabled is seeded True ONLY for the Trading department, whose
    extensions are unique. Warehouse/Operations share lines (e.g. x1022), which
    would misattribute CDRs, so their extensions are stored for reference but the
    poller flag stays False.
  * The poller builds one extension -> user dict, so two enabled users on one
    extension silently misattribute calls. Before enabling (create or first-fill),
    the seeder checks the database for a DIFFERENT user already enabled on the
    same extension; if one exists the user and extension are still written, the
    flag stays False, and a warning names both emails and the extension. The
    pre-existing user is never touched.

Role mapping (all interactive roles share the same ROLE_ACCESS_DEFAULTS, so this
is labeling + manager-assignability, not access): Trading titles map to
buyer/sales/trader; CEO + COO map to manager; Finance/Operations/Warehouse staff
get the neutral interactive default "buyer".

Called by: an operator (manually, post-deploy). NOT cron.
Depends on: app.database.SessionLocal, models.User / UserAdminAudit,
    constants.UserRole / UserAuditAction, services.user_admin.record_user_audit.
"""

from __future__ import annotations

import argparse
from typing import TypedDict

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.constants import UserAuditAction, UserRole
from app.models import User
from app.services.user_admin import record_user_audit


class _Employee(TypedDict):
    email: str
    name: str
    title: str
    department: str
    role: UserRole
    extension: str
    enable_8x8: bool


def _emp(
    email: str,
    name: str,
    title: str,
    department: str,
    role: UserRole,
    extension: str,
    *,
    enable_8x8: bool = False,
) -> _Employee:
    return {
        "email": email,
        "name": name,
        "title": title,
        "department": department,
        "role": role,
        "extension": extension,
        "enable_8x8": enable_8x8,
    }


# Source: Employee_Contact_List.xlsx (2026-09, provided by the owner). Emails are the
# sheet's mailbox values verbatim (incl. celedridge@ for Christy Eldridge). Extensions
# are the 8x8 4-digit extensions; Trading rows are unique, several NC warehouse rows
# share x1022 (site line) so they stay enable_8x8=False.
EMPLOYEES: list[_Employee] = [
    # ── Trading desk (8x8 CDR mapping ON) ────────────────────────────────
    _emp("datmojo@trioscs.com", "Derrick Atmojo", "Senior Buyer", "Trading", UserRole.BUYER, "1012", enable_8x8=True),
    _emp("jfu@trioscs.com", "Jenny Fu", "Buyer", "Trading", UserRole.BUYER, "1023", enable_8x8=True),
    _emp(
        "asharma@trioscs.com",
        "Aniket Sharma",
        "Sourcing Supervisor",
        "Trading",
        UserRole.BUYER,
        "1010",
        enable_8x8=True,
    ),
    _emp(
        "pcash@trioscs.com",
        "Patricia Cash",
        "Sales Account Executive",
        "Trading",
        UserRole.SALES,
        "1004",
        enable_8x8=True,
    ),
    _emp(
        "sgarcia@trioscs.com",
        "Sally Garcia",
        "Sales Account Executive",
        "Trading",
        UserRole.SALES,
        "1032",
        enable_8x8=True,
    ),
    _emp(
        "tkanev@trioscs.com",
        "Tsvetan Kanev",
        "Sales Account Executive",
        "Trading",
        UserRole.SALES,
        "1020",
        enable_8x8=True,
    ),
    _emp(
        "mtewes@trioscs.com",
        "Martina Tewes",
        "Sales Account Executive",
        "Trading",
        UserRole.SALES,
        "1009",
        enable_8x8=True,
    ),
    _emp("ggreco@trioscs.com", "Gerry Greco", "Sales Engineer", "Trading", UserRole.SALES, "1019", enable_8x8=True),
    _emp(
        "msondergaard@trioscs.com",
        "Mikkel Sondergaard",
        "IBM Trading Manager",
        "Trading",
        UserRole.TRADER,
        "1016",
        enable_8x8=True,
    ),
    _emp("jsheridan@trioscs.com", "Jason Sheridan", "ITAD Trader", "Trading", UserRole.TRADER, "1017", enable_8x8=True),
    _emp("jshui@trioscs.com", "Joyce Shui", "Trading Specialist", "Trading", UserRole.TRADER, "1008", enable_8x8=True),
    _emp(
        "dtuckman@trioscs.com",
        "David Tuckman",
        "Trading Specialist",
        "Trading",
        UserRole.TRADER,
        "1006",
        enable_8x8=True,
    ),
    # ── Executives (extension stored; CDR flag left for an explicit opt-in) ──
    _emp("mkhoury@trioscs.com", "Michael Khoury", "CEO/Director of Global Trading", "", UserRole.MANAGER, "1001"),
    _emp("mmoawad@trioscs.com", "Marcus Moawad", "COO", "", UserRole.MANAGER, "1002"),
    # ── Finance ──────────────────────────────────────────────────────────
    _emp("kcienfuegos@trioscs.com", "Katy Cienfuegos", "Staff Accountant", "Finance", UserRole.BUYER, "1003"),
    _emp("mholliday@trioscs.com", "Myrna Holliday", "Accounting Manager", "Finance", UserRole.BUYER, "1018"),
    _emp("cstevens@trioscs.com", "Charlene Stevens", "Office Manager", "Finance", UserRole.BUYER, "1007"),
    # ── Operations ───────────────────────────────────────────────────────
    _emp("eadjei@trioscs.com", "Emmanuel Adjei", "Technical Specialist", "Operations", UserRole.BUYER, "1022"),
    _emp(
        "aalvarez@trioscs.com", "Angel Alvarez", "Logistics/Testing Coordinator", "Operations", UserRole.BUYER, "1029"
    ),
    _emp(
        "celedridge@trioscs.com",
        "Christy Eldridge",
        "Order Fulfillment Administrator",
        "Operations",
        UserRole.BUYER,
        "1028",
    ),
    _emp("lmeadows@trioscs.com", "Laura Meadows", "Order Fulfilment Manager", "Operations", UserRole.BUYER, "1013"),
    _emp("jmolina@trioscs.com", "Jimmy Molina", "Warehouse Manager NC", "Operations", UserRole.BUYER, "1099"),
    _emp("dobbey@trioscs.com", "Darrel Obbey", "Technical Manager", "Operations", UserRole.BUYER, "1025"),
    _emp("erodarte@trioscs.com", "Eric Rodarte", "Operations Manager", "Operations", UserRole.BUYER, "1005"),
    _emp("pstukes@trioscs.com", "Phil Stukes", "Site Warehouse Manager", "Operations", UserRole.BUYER, "1022"),
    # ── Warehouse ────────────────────────────────────────────────────────
    _emp("abazemore@trioscs.com", "Anfrenee Bazemore", "Warehouse Clerk", "Warehouse", UserRole.BUYER, "1022"),
    _emp(
        "cgrant@trioscs.com",
        "Christopher Grant",
        "Warehouse Associate/ITAD Receiver",
        "Warehouse",
        UserRole.BUYER,
        "1022",
    ),
    _emp("jintriago@trioscs.com", "Jackson Intriago", "Warehouse Clerk", "Warehouse", UserRole.BUYER, "1022"),
    _emp("ejimenez@trioscs.com", "Ernesto Jimenez", "Warehouse Clerk", "Warehouse", UserRole.BUYER, "1022"),
    _emp("jlattimore@trioscs.com", "Justin Lattimore", "Receiving Clerk", "Warehouse", UserRole.BUYER, "1024"),
    _emp(
        "phstukes@trioscs.com",
        "Phil Stukes Jr.",
        "Warehouse Associate/ITAD Receiver",
        "Warehouse",
        UserRole.BUYER,
        "1022",
    ),
]


def _extension_is_free(db: Session, email: str, extension: str) -> bool:
    """True when no OTHER user is already 8x8-enabled on `extension`.

    The CDR poller maps extension -> user in one dict, so a second enabled user on the
    same extension would silently misattribute calls. Logs a warning naming both emails
    and the extension when a conflict exists.
    """
    owner = (
        db.execute(
            select(User).where(
                User.eight_by_eight_enabled.is_(True),
                User.eight_by_eight_extension == extension,
                User.email != email,
            )
        )
        .scalars()
        .first()
    )
    if owner is None:
        return True
    logger.warning(
        "8x8 CONFLICT: {} is already enabled on ext={}; leaving 8x8 disabled for {}",
        owner.email,
        extension,
        email,
    )
    return False


def seed(db: Session, *, apply: bool = False) -> dict[str, int]:
    """Upsert EMPLOYEES into users; returns {created, extension_filled, skipped}.

    Dry-run (apply=False) computes and logs the same plan but rolls back instead of
    committing. Existing users are never modified beyond first-fill of an empty
    eight_by_eight_extension (see module docstring).
    """
    created = extension_filled = skipped = 0
    for emp in EMPLOYEES:
        email = emp["email"].strip().lower()
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            enable_8x8 = emp["enable_8x8"] and _extension_is_free(db, email, emp["extension"])
            user = User(
                email=email,
                name=emp["name"],
                role=str(emp["role"]),
                is_active=True,
                eight_by_eight_extension=emp["extension"],
                eight_by_eight_enabled=enable_8x8,
            )
            db.add(user)
            db.flush()  # assign user.id for the audit row
            record_user_audit(
                db,
                actor_id=None,  # system / seed — survives as "system" in the audit view
                target_user_id=user.id,
                action=UserAuditAction.INVITE,
                detail={"email": email, "role": str(emp["role"]), "source": "seed_employees"},
            )
            created += 1
            logger.info("CREATE {} role={} ext={} 8x8={}", email, emp["role"], emp["extension"], enable_8x8)
        elif not user.eight_by_eight_extension:
            user.eight_by_eight_extension = emp["extension"]  # type: ignore[assignment]  # legacy Column-model ORM noise
            if (
                emp["enable_8x8"]
                and user.is_active
                and not user.eight_by_eight_enabled
                and _extension_is_free(db, email, emp["extension"])
            ):
                user.eight_by_eight_enabled = True  # type: ignore[assignment]  # legacy Column-model ORM noise
            extension_filled += 1
            logger.info("FILL-EXT {} ext={} 8x8={}", email, emp["extension"], bool(user.eight_by_eight_enabled))
        else:
            skipped += 1
            logger.info("SKIP {} (exists, extension already set)", email)

    if apply:
        db.commit()
    else:
        db.rollback()
    return {"created": created, "extension_filled": extension_filled, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed TRIO employees as AVAIL users + 8x8 extensions")
    parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry-run, no writes)")
    args = parser.parse_args()

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        counts = seed(db, apply=args.apply)
    finally:
        db.close()
    mode = "APPLIED" if args.apply else "DRY-RUN (no writes; re-run with --apply)"
    logger.info("seed_employees {}: {}", mode, counts)


if __name__ == "__main__":
    main()
