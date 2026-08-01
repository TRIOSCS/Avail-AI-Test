# Session prompt template

Save as `specs/session-prompt-template.md`. Copy, fill the three
brackets, paste into the CLI. Everything else is already in
`CLAUDE.md` — do not repeat it here.

---

## TASK

<one feature. one sentence. e.g. "Build the Quality Plan model and
the completeness gate that blocks routing on blank fields.">

**In scope:** <list>
**Out of scope:** <list — be specific, this is the guardrail>

---

## GROUND RULE

You have the repo. I don't. Anything I describe below is business
truth, not code truth. Where my description conflicts with what is
actually in the codebase, **the code wins** — flag the conflict
and stop. Do not silently reconcile it.

---

## PHASE 0 — DISCOVERY (write no code)

Map the following and write your findings to
`specs/discovery-<feature>.md`:

1. <models / routers / services you expect to be involved>
2. Existing patterns this feature must match
3. Current Alembic migration head
4. Anything I described above that does not exist in the code

Rules for Phase 0:
- Cite `file:line` for every claim. No claim without a citation.
- If you cannot find something, say "not found" — do not infer it
  from a similar name.
- End with an **Open Questions** list. No TBDs anywhere else.

**Then stop and wait for my go.** Do not proceed to build.

---

## BEFORE YOU BUILD

Restate in one paragraph, in your own words, what you are about to
build and why. If your restatement and my task don't match, we fix
it now instead of three files in.

---

## BEFORE YOU WRITE

Show me:
- Files touched (created / modified / deleted)
- Approximate lines added and removed
- Anything destructive, called out explicitly

Wait for confirmation on anything destructive.

---

## AFTER YOU BUILD

- Run the tests. Paste the actual terminal output.
- A test is not passing until I have seen the output.
- Summarize what changed in five lines or fewer.

---

## SESSION HYGIENE

One feature per session. At ~15–20 exchanges, stop and tell me to
start fresh — the next session reads
`specs/discovery-<feature>.md` instead of re-mapping cold.
