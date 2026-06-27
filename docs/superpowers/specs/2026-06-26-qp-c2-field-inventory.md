<!-- PRESERVED 2026-06-27 from an ephemeral session scratchpad. Original: session
46711e5f .../scratchpad/qp-field-inventory-c2.md (2026-06-26). -->

> **Preserved reference.** QP field inventory for the C2 native view + the vendor-share
> **redaction whitelist** (the C2c SHOW/HIDE field list, Mike-locked). C2a/C2b shipped; the
> §vendor-facing-share-link portion is HELD with C2c (see `2026-06-26-qp-c2-blueprint.md`).

# QP field inventory — for the C2 native QP view (extracted from the SharePoint QP template, 2026-06-26)

Source: `Shared Documents/Quality Plans/QP Template/Quality Plan - Template - Copy.xlsx` (blank template).
The native QP view (Phase C2) replaces this spreadsheet field-for-field. Five sections; four approval gates.

## § SALES — "Quality Questions" (gated by the SALES ORDER approval; owner = Account Manager)
- Sales Order # (TSO… — the SO record key)
- Product ID (primary) + Product ID (2nd) + Additional Product ID (×2)
- Condition
- Quantity
- FW / HW / REV / Date & Week Codes
- Product Commodity
- Testing Required / Option
- Testing Specifics / Test Location
- Serial Preapproval Required (Y/N)
- Authorized to Ship Early? (Y/N)
- Authorized to Ship Partial? (Y/N)
- Routing / Prescreening WHS
- Vendor Rating
- Is third-party packaging acceptable? (Y/N)
- List packaging requirements
- BOM / Matrix Links / Acceptable SUBS / TSO
- Notes

## § PURCHASING — "Quality Questions" (gated by the PURCHASE ORDER approval; owner = Buyer)
- Purchase Order #
- Product ID (primary) + Product ID (2nd) + Additional Product ID (×2)
- Condition
- FW / HW / REV / Date & Week Codes
- Product Commodity
- Testing Required / Option
- Routing / Prescreening WHS
- Packaging
- Will TPO ship complete? (Y/N)
- TPO Notes / Shipping Schedule

## § BUY PLAN — the material sourcing table (gated by the BUY PLAN approval; owner = Account Manager)
**Already modeled** by the existing `BuyPlan` + `Offer` (the spec notes Offer carries ~16 of these). Columns:
- Material (Part Number) | Vendor / Location | Sourcing Type | Back-up Offers | Resource Required
- Condition | Specifics | Warranty | Lead Time | Qty | Unit Price | Pkg. & Pkg. Condition
- Created Date | Owner | Vendor | Part Desc.
- Notes
(Prepayment gate attaches here per buy-plan line / vendor.)

## § FRU — crosswalk block
- FRU Part Number | Model(s) | Manufactured by | Carrier | Alternate Carrier | Series
(Trio already has an FRU crosswalk — reuse it; see project_materials_wave / FRU crosswalk.)

## § SERIAL — serial preapproval tracking
- Buyer | Submitted by | Buyer Date
- Has SN Previously Been Received? (Y/N)
- Purchase Order | Part Number
- Serial Number | Seagate SN (if applicable) | TSO | Customer PO
- Submitted to Customer Date | Did Customer Approve? (Y/N) | Customer Approved Date | OPS Received (Y/N)

## Mapping to the engine (C2)
- The QP is **buy-plan-centric** already (QualityPlan.buy_plan_id NOT NULL); Sales/Purchasing/Serial are NEW sections.
- Gates → engine gate_types: `sales_order` (Sales §), `purchase_order` (Purchasing §), `buy_plan` (Buy Plan §, wired in C1),
  `prepayment` (already shipped). Approvers = per-user toggles (`can_approve_sales_orders`, `can_approve_pos`,
  `can_approve_buy_plans`, `can_approve_prepayments`) — NO hardwired names (Mike's locked rule).
- Sales Order / Purchase Order are **record-only** (no Acctivate write-back, per the locked decision) — capture the
  SO#/PO# + the section fields; the gate approves the section.
- Completeness gate: required fields blank → block submit (the QualityPlanService.validate_complete pattern, already
  in P1 for the shell).
- Sample filled QPs available in SharePoint (TSO0190738 etc.) if field formats/options are needed at build time.

## C2 sub-feature — vendor-facing QP share link (Mike, 2026-06-26)
Buyers need to share the QP with vendors; sales want a link to paste into the Acctivate SO notes. So:
- **Unguessable, no-login share link** `/qp/share/<random-token>` → a **read-only redacted** QP render. (Public-by-token;
  security = unguessable token + revocation + pre-redacted content. Token is a long random string, NOT the QP id.)
- **Revocable + regenerable** by the buyer/sales (a "Share with vendor" action on the QP); **non-expiring by default**
  (so an Acctivate note keeps working) with optional expiry. Store the token on the QP (or a qp_share_token table).
- **Redaction = "sourcing requirements only" (Mike's locked choice):**
  - SHOW (vendor-safe): part #s / Product IDs, condition, quantity, FW/HW/REV/date codes, product commodity,
    testing required + specifics, serial-preapproval flag, packaging requirements, the technical Quality Questions.
  - HIDE (always): ALL customer identity — customer name/company, customer PO, end-customer refs, the Serial-section
    customer fields (Submitted to Customer Date, Did Customer Approve?, Customer Approved Date); internal pricing/
    margin/unit-price; internal owner/rep names; BOM/internal matrix links. (Customer-identifying info MUST be protected.)
  - Build the redacted view as a SEPARATE template/serializer that whitelists vendor-safe fields (never a "hide list"
    on the full view — whitelist so a future field isn't accidentally leaked). Add a test asserting NO customer field
    appears in the share render.
