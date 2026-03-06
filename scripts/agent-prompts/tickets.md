# Test Area: Trouble Tickets

Navigate to: {{BASE_URL}}/

## Workflow Tests

### Test 1: Navigate to Trouble Tickets
1. Use `browser_navigate` to go to `{{BASE_URL}}/`
2. Use `browser_snapshot` to capture the current page state
3. Look for a "Trouble Tickets" or "Tickets" link in the sidebar navigation
4. Use `browser_click` on the Trouble Tickets sidebar item
5. Use `browser_snapshot` to capture the ticket list view
6. VERIFY: The ticket list page loads and displays a table or list of tickets
7. VERIFY: The list includes columns for title, status, and source

### Test 2: Verify Ticket List Content
1. Use `browser_snapshot` to inspect the ticket list
2. VERIFY: Each ticket row shows a title (not empty, not "undefined")
3. VERIFY: Each ticket row shows a status value (e.g. "submitted", "diagnosed", "resolved", "rejected")
4. VERIFY: Each ticket row shows a source identifier
5. VERIFY: No rows display "NaN", "undefined", or "[object Object]"
6. Use `browser_console_messages` to check for JavaScript errors
7. Use `browser_network_requests` to check for failed API calls

### Test 3: Filter Tickets by Status
1. Use `browser_snapshot` to look for filter controls (dropdown, tabs, or buttons for status filtering)
2. If a status filter exists, use `browser_click` to select "submitted" status
3. Use `browser_snapshot` to verify the list updates to show only submitted tickets
4. Use `browser_click` to select "diagnosed" status
5. Use `browser_snapshot` to verify the list updates to show only diagnosed tickets
6. Use `browser_click` to select "resolved" status
7. Use `browser_snapshot` to verify the list updates to show only resolved tickets
8. VERIFY: Filtering does not produce console errors or failed network requests

### Test 4: View Ticket Detail
1. Use `browser_snapshot` to identify a clickable ticket row or title link
2. Use `browser_click` on a ticket title or row to open the detail view
3. Use `browser_snapshot` to capture the detail view
4. VERIFY: The detail view shows the ticket title prominently
5. VERIFY: The detail view shows the current status
6. VERIFY: A diagnosis section is present (may be empty if ticket is new)
7. VERIFY: An action history or log section is present showing past actions/state changes
8. Use `browser_console_messages` to check for JavaScript errors on the detail view

### Test 5: Check Ticket Timestamps and Formatting
1. From the ticket detail view, use `browser_snapshot`
2. VERIFY: Created-at and updated-at timestamps are displayed in a readable format
3. VERIFY: No timestamps show as "Invalid Date" or raw ISO strings without formatting
4. VERIFY: Cost values (if displayed) use proper currency formatting (not raw floats)

## What Correct Looks Like
- Ticket list loads within 3 seconds showing a table with title, status, and source columns
- Status values are one of: submitted, diagnosed, in_progress, awaiting_verification, resolved, rejected
- Clicking a ticket opens a detail view with diagnosis information and action history
- Status filters narrow the displayed list to only matching tickets
- All text values are properly rendered (no undefined, NaN, or object references)
- Timestamps are human-readable
- Cost values use currency formatting (e.g. "$2.00" not "2")

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Filter controls that don't update the list
- Detail view that fails to load ticket information
