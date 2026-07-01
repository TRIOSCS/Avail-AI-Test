# Codebase Audit Remediation — 2026-06-30 → 2026-07-01

Full remediation of the 109-finding codebase audit ([`AUDIT_REPORT_2026-06-30.md`](./AUDIT_REPORT_2026-06-30.md)).
**Status: COMPLETE** — all findings fixed, merged to `main`, CI green, deployed to staging, live-verified on real Postgres. A follow-on go-live authz fix (#616) and a regression sweep on the merged diff (#617) also merged + deployed — see [Post-remediation](#post-remediation-beyond-the-original-audit).

## Method
- Read-only, adversarially-verified audit (19 units) → 109 findings (6 P0-cluster, 17 P1, 41 P2, 50 P3).
- Root-cause fixes only, TDD, **one PR per theme** for review.
- Built via parallel worktree workflows (7–13 agents) + **per-fix adversarial verification**; every flagged band-aid / no-commit was redone inline (quality gate caught ~8 flawed agent outputs + 1 self-caught near-regression).

## Findings → PRs

| Cluster | PRs |
|---|---|
| P0 (data-loss cron, XSS, read-IDOR, ownership-escalation, quote cascade, substitutes crash) | #598, #599 |
| P1 authz/scoping (IDOR/PII scoping, SSRF, connector key-leak, TRADER scoping, materials/resell gates) | #600 |
| P1 correctness (MPN normalization ×5, MOQ/lead-time, avail-score 0–100 + b6 payout, substitutes hardening) | #601 |
| P1 buy-plan PO approval bypass (`verify_po_sent` detection-only, `_complete_plan` cancels orphaned PO gate) | #602 |
| P1 transaction-safety ×4 (knowledge regen, company-merge fail-closed, teams watermark, email pending-parse) | #604 |
| P1 browser workers (persistent event loop, circuit-breaker reset ×3, **AI-gate poison ×4**, unguarded-index ×4, browser-leak ×2) | #605 |
| P1 template bugs ×5 (Alpine `var`→`let`, `\|tojson` in double-quoted attrs) | #606 |
| P1 sourcing ×4 (dedup MPN-scope, buyer-confidence preservation, Anthropic timeout, batch-JSON 400) | #607 |
| P1 CRM/offers ×3 (response-status vocab, CompanyCreate hq-normalization, phone-match perf) | #608 |
| P2 correctness ×7 (**encryption boot-canary**, OEMSecrets trust, email direction, worker kwargs, frozen result, knowledge IDOR+savepoint, unified status machine) | #609 |
| P3 simplify ×8 (**de-dup 4 `ai_gate.py` copies**, StrEnum, `db.get`, shared fuzzy, N+1) | #610 |
| P3 hygiene ×4 (implicit-Optional, dead code/config, `print`→loguru) | #611 |
| P3 docstring-rot | #612 |
| Buy-plan PO-INBOUND-stall follow-up (`confirm_po` in ACTIVE **or** INBOUND) | #613 |
| Phone-normalizer consolidation + `requirement_status`/`activity_service` StrEnum | #614 |

## Post-remediation (beyond the original audit)

These landed after the audit-findings remediation above, in the same push to multi-user readiness.

| Item | PR |
|---|---|
| Persisted the audit report + this remediation record to `docs/audit/` | #615 |
| **Go-live authz**: 2 sightings IDOR/enumeration gaps the audit missed — `sightings_detail` served any requirement's pricing/contacts ungated; `sightings_list` un-scoped enumeration for RESTRICTED_ROLES. Fixed with `require_requisition_access` + `created_by` scoping | #616 |
| **Regression sweep** on the merged diff (`b21e62d8..main`) — 4 self-inflicted regressions | #617 |

### Regression sweep (#617)

A fresh adversarially-verified review of the *remediation's own merged diff* found 4 real regressions it had introduced (2 further candidates were refuted and dropped). All fixed root-cause + TDD:

- 🔴 **HIGH — startup boot-hang.** The two new vendor-normalize backfills looped `while True` forever whenever any legacy `vendor_name` normalizes to `''` (`"LLC"`, `"Inc."`): the row never gets an `UPDATE`, so a `WHERE ... IS NULL` filter re-selects it endlessly and startup never completes. Fixed with `id`-cursor pagination so unupdatable rows are skipped. (An existing test had *masked* this with a stop-loop exception hack.)
- 🟠 **MED — dedup-drop.** The P1-tail dedup MPN-scope filter (#607) compared the raw packaging-suffix column, silently dropping same-part vendor offers whose typed PN differs from the search MPN by internal `-`/`.`. Fixed to compare the canonical `normalize_mpn_key`.
- 🟠 **MED — quote-fact loss.** The P2 `capture_quote_fact` savepoint change (#609) flushed the entry but never committed, and callers return without committing → the auto-captured price fact rolled back at session close. Fixed so `capture_quote_fact`/`capture_offer_fact` durably commit their own entry.
- 🟠 **MED — phone 422.** The phone-normalizer consolidation (#614) returns `None` for 7–9-digit partials, so `CompanyCreate/Update` 422'd and the htmx paths stored NULL. Fixed to preserve the raw input.

**Lesson:** after a large remediation, run a regression review on your *own* merged diff — consolidations, new backfills, and transaction-semantics changes are where self-inflicted regressions hide, and existing tests that encode the buggy behavior mask them.

## Deploy + verification
- Deployed to staging (`./deploy.sh`, build `65183f6f`), app healthy, encryption canary bootstrapped cleanly on first boot, workers active.
- Live-verified on real Postgres: phone-match `cast(JSONB).contains` (PG-only), avail-score scans, encryption round-trip, `is_obsolete` batch, sighting filters — all run clean.
- **Final re-deploy** carrying the go-live authz fix + regression sweep (`./deploy.sh`, build `3d09d4c5`): health `{"status":"ok","db":"ok","redis":"ok"}`, app + enrichment-worker build tags matched, host `nc`/`ics`/`tbf` workers restarted active, and startup completed cleanly on a real boot — confirming the backfills now terminate (boot-hang fix).

## Notable design decisions
- **avail-score b6**: Interaction Quality excluded from the sales `behavior_total` (per direction to score on real tradable activity) → both roles on one 0–100 scale, fair payout gates.
- **encryption**: a wrong `ENCRYPTION_SALT` raises `InvalidToken`, indistinguishable per-field from legit plaintext → detection is a boot-time decrypt **canary** in `system_config`, not a per-field except change.
- **phone normalizers**: the two *lenient* copies collapsed onto the canonical `phone_utils.format_phone_e164`; the strict `phone.normalize_e164` (phonenumbers-lib) is a genuinely different validator and kept separate.

## Known follow-ups / observations
- Staging `ENCRYPTION_SALT` is unset (legacy static salt) — set it for defense-in-depth.
- `AUDIT_REPORT_2026-06-30.md` is the point-in-time audit; some detailed P2s in its "P1 — likely bugs" section were addressed under the P1-tail PRs above.
