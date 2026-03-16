# Sourcing UI Recovery Handoff

This package is for getting the sourcing/requisitions UI back on track after an off-spec implementation.

## What appears wrong from the latest UI state
- The left sidebar visually overlaps or crowds the working content area.
- The center list/filter area appears too narrow and compressed.
- The right detail area is mostly empty and not being used effectively.
- The overall page does not match the intended list → detail sourcing workflow.
- The layout feels stuck between old shell behavior and new sourcing UI expectations.

## Product truths to preserve
- No resizable split-pane layout.
- No Buyer Follow-Up Queue.
- Use list → detail navigation.
- Keep Confidence separate from Safety.
- Keep source attribution visible.
- Keep suggested next action visible.
- Use caution-oriented vendor safety language.

## Required process
1. Audit the current implementation against the uploaded docs and the screenshot.
2. Identify layout/root-cause issues before changing code.
3. Propose a small-slice recovery plan.
4. Implement one slice at a time.
5. Stop after each slice.
