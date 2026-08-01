# CLAUDE.md — AVAIL

Loaded every session. Do not restate this back to me.

## What this is

AVAIL is a FastAPI/PostgreSQL sourcing platform and CRM for TRIO,
an electronic component brokerage and ITAD operation. Single
developer. Hosted on DigitalOcean at app.availai.net.

**AVAIL is the command center, not the ERP.** We do not cut POs in
AVAIL. We issue buy instructions to buyers, who cut the POs in the
ERP and send them.

## Systems of record

| Lives in Acctivate (ERP) | Lives in AVAIL |
|---|---|
| Sales orders | Everything else |
| Purchase orders | Requisitions, buy plans, quality plans |
| Inventory | Resell, CRM, contacts, approvals, audit |

## Hard constraints

- **Nothing integrates with Acctivate.** No sync, no read, no
  write. It is manual by design. Do not propose otherwise.
- **ERP-neutral field naming.** We move to Dynamics 365 Business
  Central in 2027. Never hardcode a vendor name into a field,
  model, or column. `erp_so_number`, not `acctivate_so_number`.
- AVAIL stores external ERP document numbers as references only.
  It never owns the SO/PO lifecycle.

## Business context

**The Quality Plan is the hub document.** One QP per TSO, today an
Excel workbook in a SharePoint library. It contains four sections
in one sheet:

```
QUALITY PLAN (per TSO)
├── SALES section        → gated by Sales Order approval
├── PURCHASING section   → gated by PO approval
├── BUY PLAN section     → gated by Buy Plan approval
└── SERIAL / FRU section → ops / receiving tracking
    + Vendor Prepayment approval (standalone, tied to a PO)
```

The Buy Plan already in AVAIL is a section inside the QP, not a
sibling of it. Approved QPs render back into the same SharePoint
library path so existing approval-card attachment links keep
working.

**Order types:** New, Revision, Testing Service, Comps Program,
Stock Sale. Some order types have no buy plan.

**Approvals Workspace:** four tabs — Sales Orders, Buy Plans,
Purchase Orders, Prepayments. All four are lenses on one pipeline
rooted at the sales order, not four separate sign-offs. The buy
plan shares the sales order's single manager approval. The detail
pane is a working editor at every stage; every change is
audit-logged.

**Two-screen model:** the Approvals workspace is the approval desk
(all decide actions). A full-page Deal view is the home for
build/fix work — draft, submit, line edits, issues,
request-prepayment.

**Approval routing:** SO, buy plan, and PO run day to day through
one manager, with two others as any-of-3 first-responder-wins
backup. Prepayments route to two accounting approvers, plus the
owner on higher value.

**Auto-approve rule:** deals under $5K revenue auto-approve.
Everything else is a one-click approval with drill-in to edit.

**Roles:** sales, buyer, manager, admin. No sub-roles. Internally,
sales staff only sell, buyers only buy, traders do both.

**Resell module:** the inverse of sourcing. Post customer excess,
internal traders submit offers, owner builds a bid back to the
customer. ExcessList/ExcessLine mirrors the
Requisition/Requirement pattern. Posted lines dual-write into the
existing Sighting table so the matcher picks them up. Offers are
scoped per-line or take-all. Customer identity is hidden on
postings. Match on part number only — no price ranking, no margin
math.

## Code conventions

- Structure: `routers/` (thin), `services/` (fat), `models/`,
  `schemas/`, `specs/`, `test_fixtures/`, `tests/`
- Routers call services. Business logic never lives in a router.
- Loguru, never `print()`
- Header docstring on every new file
- Tests alongside code, same commit
- Exact file paths. No placeholders, no `path/to/file.py`
- Alembic: check the current migration head before writing one

## How to work with me

- I am usually on a phone in Termius. I cannot read code to check
  your work. Show me output, not claims.
- Simple over clever, every time.
- ~150 lines per response.
- Warn before any destructive operation and wait.
- If something is missing that materially changes the answer, ask.
  Do not guess and do not present an assumption as a fact.

## Do not

- Invent file paths, model names, or column names. Verify or ask.
- Add phasing plans, role-permission tiers, or financial logic I
  did not ask for.
- Expand scope past the one feature named in the session prompt.
- Answer with a plan when I asked for the concept.
- Report a test as passing without pasting the actual output.
- Leave a TBD anywhere.
