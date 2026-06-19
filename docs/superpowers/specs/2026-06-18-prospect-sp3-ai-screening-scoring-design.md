# Real Prospect Enrichment — SP3: AI Account Screening + Match/Opportunity Scoring

**Date:** 2026-06-18
**Status:** Architecture approved; detailed spec + plan finalized when reached in sequence
(after SP1).
**Owner:** prospecting
**Program:** Sub-project 3 of 4. Build order: SP1 → **SP3** → SP4 → SP2. Consumes the real
data from **SP1** (`2026-06-18-prospect-real-enrichment-design.md`); screens every account
that enters prospecting (broker-discovered and SP4-parked/swept).

## Goal

Turn prospecting from "a list of accounts" into "a vetted, ranked list of likely TRIO
opportunities." An AI judges each account, **procurement-first**, and produces a
**TRIO-match score** and an **opportunity score** that gate and rank the queue. Low-match
accounts are soft-bucketed (recoverable), not deleted. The verdict is **grounded in real
signals and cites its evidence** — when data is too thin it routes to enrichment rather
than guessing (consistent with TRIO's no-hallucination / high-confidence enrichment bar).

## Two-sided, procurement-first

TRIO is two-sided: a buyer-side opportunity (the account has procurement that sources the
electronic components TRIO supplies) and an excess-side opportunity (the account has surplus
electronic inventory TRIO can buy/broker). **Procurement is primary**; excess is a secondary
signal that can still lift an account but is not the main surface reason.

## AI screen output (per account)

The screen calls Claude with grounded context and returns a validated structure:

```json
{
  "trio_match_score": 0-100,        // procurement-first fit: how likely + strongly this
                                    //   account needs TRIO's components
  "opportunity_score": 0-100,       // estimated opportunity size/value (spend potential
                                    //   from size/industry; secondary excess volume)
  "excess_likelihood": 0-100,       // secondary: likely surplus inventory to sell
  "verdict": "pass" | "screened_out" | "insufficient_data",
  "rationale": "<= 2 sentences, grounded, cites the evidence used",
  "evidence": ["industry=...", "naics=...", "size=...", "news=...", "history=..."],
  "confidence": 0-100,
  "model": "<anthropic model id>",
  "screened_at": "<iso>"
}
```

- **`insufficient_data`** when grounding is too thin to judge at the confidence bar → the
  account is routed back to enrichment (SP1) and re-screened later; it is **not**
  screened-out and **not** guessed.
- Evidence must reference real fields (firmographics, enrichment data, news, TRIO history,
  contacts). No ungrounded claims.

## Grounding inputs

Industry / NAICS / "what they make"; SP1 firmographics + contacts; news/event signals;
**TRIO history** (quotes, POs, buy-plans — the purchase record; SFDC when it lands);
`historical_context`. The screen reasons: does this company design/build/repair products
that consume the components TRIO supplies, and is there evidence of a sourcing function?

## Storage + ranking (migration)

SP3 adds sortable columns to `ProspectAccount` (Alembic migration, with rollback):

- `trio_match_score: Integer, default=0, index`
- `opportunity_score: Integer, default=0, index`

Full verdict (rationale, evidence, verdict, confidence, model, screened_at) lives in
`enrichment_data['ai_screen']` (JSONB, no migration). The two scalar columns exist so the
queue **sorts and filters in SQL** at scale.

**Default queue ranking:** `trio_match_score` desc → `opportunity_score` desc →
buyer-ready/readiness as the "act-now" overlay (existing `build_priority_snapshot`). AI
match = *should we pursue this account*; opportunity = *how big*; readiness = *is it
actionable now*. The three are complementary; the existing deterministic fit/readiness are
retained.

## Gate (soft-bucket)

`verdict == "screened_out"` (match below `ai_screen_min_match`, default 40) → the account is
hidden from the default queue and shown in a collapsed **"Screened out / low fit"** bucket
with its rationale. Threshold is config; a buyer can override (claim anyway). Nothing is
deleted.

## When it runs + cost control

- Runs as the **final step of `run_enrichment_job`** (after SP1 enrichment) and after SP4
  park/sweep inflow enrichment.
- **Cache** the verdict; **re-screen only on material new data** (new contacts, new
  firmographics, new events) — not on every render.
- **Daily cap** (`ai_screen_daily_cap`, default 200) + spend metering, mirroring the
  existing enrichment-throughput controls. The screen prefers already-gathered enrichment
  data; optional `web_search` only when a verdict would otherwise be `insufficient_data`
  and the account is worth it (config-gated) — same credit philosophy as the broker router.

## UI (needs approval at build time)

- Prospect card + detail: AI **match** and **opportunity** scores, the one-line rationale,
  and an evidence tooltip. The "Screened out / low fit" collapsed bucket. The default sort
  becomes AI-match.
- All new elements; per the UI guardrail they get explicit approval when SP3 is built.

## Testing strategy

- Screen service: grounded context assembled from real fields; output schema validated;
  `insufficient_data` path routes to enrich (no guess); evidence references real fields.
- Scoring/rank: columns populated; queue sorts by match→opportunity→readiness; soft-bucket
  threshold gates correctly; buyer override surfaces a screened-out account.
- Cost: cache hit avoids re-call; re-screen only on material change; daily cap respected.
- Migration: upgrade → downgrade → upgrade clean.

## Out of scope (SP3)

- Excess-primary screening (procurement-first per decision; excess is a secondary lift).
- Replacing the deterministic fit/readiness scores (they remain alongside the AI scores).
