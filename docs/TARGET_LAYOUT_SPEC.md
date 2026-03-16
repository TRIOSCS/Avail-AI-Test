# Target Layout Spec

## Correct interaction model
Use a standard list → detail sourcing workflow.

### Left
- global app sidebar/navigation only
- should not crowd or overlap the sourcing page content

### Main working area
The sourcing/requisitions screen should prioritize:
1. page header / actions
2. filters and search controls
3. primary results/list area
4. detail view when an item is selected

## Important non-feature
Do NOT introduce resizable split panes.
The goal is a stable, well-proportioned layout, not user-resizable panels.

## Layout rules
- Sidebar should have a fixed, reasonable width.
- Main content should use remaining width cleanly.
- Results/list area should be readable without feeling squeezed.
- Detail view should appear naturally within the main content flow.
- Avoid nested scroll traps where possible.
- Use responsive grid/flex behavior that degrades cleanly on smaller widths.

## Detail behavior
- Default empty state is okay when nothing is selected.
- Once selected, detail should replace the blank placeholder with real structured content.
- Detail should support readable sections, not a giant blank card.
