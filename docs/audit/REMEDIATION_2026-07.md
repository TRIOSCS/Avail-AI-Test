# Codebase Audit Remediation — 2026-06-30 → 2026-07-01

Full remediation of the 109-finding codebase audit ([`AUDIT_REPORT_2026-06-30.md`](./AUDIT_REPORT_2026-06-30.md)).
**Status: COMPLETE** — all findings fixed, merged to `main`, CI green, deployed to staging, live-verified on real Postgres.

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

## Deploy + verification
- Deployed to staging (`./deploy.sh`, build `65183f6f`), app healthy, encryption canary bootstrapped cleanly on first boot, workers active.
- Live-verified on real Postgres: phone-match `cast(JSONB).contains` (PG-only), avail-score scans, encryption round-trip, `is_obsolete` batch, sighting filters — all run clean.

## Notable design decisions
- **avail-score b6**: Interaction Quality excluded from the sales `behavior_total` (per direction to score on real tradable activity) → both roles on one 0–100 scale, fair payout gates.
- **encryption**: a wrong `ENCRYPTION_SALT` raises `InvalidToken`, indistinguishable per-field from legit plaintext → detection is a boot-time decrypt **canary** in `system_config`, not a per-field except change.
- **phone normalizers**: the two *lenient* copies collapsed onto the canonical `phone_utils.format_phone_e164`; the strict `phone.normalize_e164` (phonenumbers-lib) is a genuinely different validator and kept separate.

## Known follow-ups / observations
- Staging `ENCRYPTION_SALT` is unset (legacy static salt) — set it for defense-in-depth.
- `AUDIT_REPORT_2026-06-30.md` is the point-in-time audit; some detailed P2s in its "P1 — likely bugs" section were addressed under the P1-tail PRs above.
