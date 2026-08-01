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
  model, or column. Existing columns are neutral by omission
  (`sales_order_number`, `customer_po_number`, `po_number`);
  keep new ones the same way.
- AVAIL stores external ERP document numbers as references only.
  It never owns the SO/PO lifecycle.

## Business context

**The Quality Plan is the hub document.** One QP per TSO, native
in AVAIL (`app/models/quality_plan.py`, migration 161) — it
replaced the per-TSO Excel workbook on SharePoint. Sections:

```
QUALITY PLAN (per TSO)
├── SALES section        → QP_SALES approval gate
├── PURCHASING section   → QP_PURCHASING approval gate
└── SERIAL / FRU section → ops / receiving tracking
    + Vendor Prepayment (standalone gate, tied to buy plan + line)
```

In the data model the Buy Plan (`buy_plans_v3`) is the root and
the QP hangs off it (get-or-create at /v2/qp/for-buy-plan/{bp_id});
the buy plan has its own BUY_PLAN gate. Rendering approved QPs
back to the SharePoint library is planned, NOT built.

**Order types:** New, Revision, Testing Service, Comps Program,
Stock Sale. Some order types have no buy plan.

**Approvals Workspace:** four tabs — Sales Orders, Buy Plans,
Purchase Orders, Prepayments. All four are lenses on one pipeline
rooted at the sales order, not four separate sign-offs. The buy
plan shares the sales order's single manager approval. The detail
pane is a working editor at every stage; every change is
audit-logged.

**One-screen model:** the Approvals workspace is both the
approval desk and the working editor — draft, submit, line edits,
and prepayment requests all happen in its panes. The old full-page
Deal view is retired; /v2/buy-plans 308-redirects into the
workspace.

**Approval routing:** eligibility is per-user toggles, not roles
(`can_approve_buy_plans` / `_purchase_orders` / `_prepayments`,
each with an optional per-user amount limit). A request opens ONE
any-of step over everyone eligible; first responder wins. One
manager day-to-day with two backups is practice (data), not code.
Prepayments: managers approve in AVAIL; accounting confirms
payment via a tokenized pay link (no login). The owner is
notified, never a required approver.

**No auto-approve (frozen scope):** every plan goes to the one
manager approval regardless of value; the old sub-$5K rule was
removed (`auto_approved` column is vestigial). Approval is one
click with drill-in to edit.

**Roles:** buyer, sales, trader, manager, admin (an `agent` role
also exists in constants). Sales staff only sell, buyers only
buy, traders do both — practice, not enforced in code.

**Resell module:** the inverse of sourcing. Post customer excess,
internal traders submit offers, owner builds a bid back to the
customer. ExcessList/ExcessLineItem mirrors the
Requisition/Requirement pattern. Posted lines dual-write into the
existing Sighting table so the matcher picks them up. Offers are
scoped per-line or take-all. Customer identity is hidden on
postings. Match on part number only — price never affects matching; no
margin math (a display-only best-bid rollup ranks by highest
unit price).

## Code conventions

- Structure: `routers/` (thin), `services/` (fat), `models/`,
  `schemas/`, `specs/`, `tests/` (fixtures in `tests/fixtures/`)
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
