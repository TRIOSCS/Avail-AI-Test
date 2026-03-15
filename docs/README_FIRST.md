# Avail Sourcing Rebuild — Claude Code Handoff

This package is a clean rebuild-oriented handoff to help Claude Code implement the sourcing engine
to the intended product vision.

## What this package is for
Use this when the existing implementation is incomplete, off-spec, or unreliable.
It is meant to anchor Claude Code to the intended product/design direction.

## How to use
1. Upload this package (or extracted files) to Claude Code.
2. Tell Claude Code to treat these files as the primary product and UI source of truth.
3. Require Claude to:
   - audit first
   - plan second
   - implement one slice at a time
   - stop after every phase

## Key product decisions already made
- No resizable split-pane sourcing layout.
- No Buyer Follow-Up Queue.
- Confidence and Safety are separate concepts.
- One lead per vendor per part.
- AI/web search is augmentation, not source of truth.
- Use caution language for vendor risk, not accusation language.
