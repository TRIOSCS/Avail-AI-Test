You are building or repairing the React UI for the Avail sourcing engine.

Critical instruction:
Treat the uploaded React handoff files as the UI source of truth for this task.
These files are a design/spec reconstruction and may not match the current repo exactly.
If the current UI conflicts with this design, do not guess silently. Call out the mismatch and recommend the least risky path.

I am a beginner and not a developer, so explain everything in plain English.

Mission:
Implement or align the sourcing-engine React UI so buyers can:
- review ranked leads
- understand why each lead exists
- assess vendor safety
- act quickly
- record outcomes

Primary files to use:
- 00_react_ui_master_spec.md
- 01_react_component_tree.md
- 02_screen_flows.md
- 03_state_and_view_model.md
- 04_api_contract_expectations.md
- 05_interaction_and_visual_rules.md
- 06_react_acceptance_checklist.md

Required process:
PHASE 1 — audit only
- read the uploaded React handoff files
- inspect the current React or frontend sourcing UI
- compare the two
- identify what aligns, what conflicts, and what is missing
- stop and wait

PHASE 2 — implementation plan only
- create a small-slice implementation plan
- stop and wait

PHASE 3+ — implement one approved slice at a time
- smallest safe diff
- explain changes simply
- run checks
- stop and wait

Important UI outcomes:
- sourcing results list
- lead detail drawer/panel
- buyer follow-up queue
- separate confidence vs safety display
- visible source attribution
- visible reason summary
- visible suggested next action
- visible buyer status workflow
- caution-oriented safety language

Do not start coding until after the audit and plan are approved.
