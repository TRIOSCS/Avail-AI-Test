# Site Test Agent Instructions

You are testing the AvailAI application at https://app.availai.net.
You have access to the Playwright MCP tools to control a real browser.

## How to navigate
Use the Playwright browser tools:
1. browser_navigate to go to URLs
2. browser_snapshot to see the current page state (accessibility tree)
3. browser_click to click elements (use ref numbers from snapshot)
4. browser_fill_form to type into inputs
5. browser_take_screenshot to capture visual evidence
6. browser_console_messages to check for JS errors
7. browser_network_requests to check for failed API calls

## Authentication
The dispatcher has already set your session cookie — you should be logged in when you navigate to the site.

## When you find an issue
1. Take a screenshot with browser_take_screenshot
2. Check browser_console_messages for JS errors
3. Check browser_network_requests for failed requests
4. File a trouble ticket using Bash:

```bash
curl -s -X POST https://app.availai.net/api/trouble-tickets \
  -H "Content-Type: application/json" \
  -H "x-agent-key: Cmwq2kFDWnEbDO2fy4UF-UVf5QGgDDq-HDE6ZwYnkaU" \
  -d '{
    "source": "agent",
    "tested_area": "tickets",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "tickets"
  }'
```

## Before filing a ticket
Check for duplicates first:

```bash
curl -s "https://app.availai.net/api/trouble-tickets/similar?title=URL_ENCODED_TITLE&description=URL_ENCODED_DESC" \
  -H "x-agent-key: Cmwq2kFDWnEbDO2fy4UF-UVf5QGgDDq-HDE6ZwYnkaU"
```

If the response contains a match with similarity > 0.7, skip filing.

## When everything works
If all tests pass with no issues, just output: PASS: tickets

## Rules
- Do NOT click delete/remove/logout/destroy/purge buttons
- Take a screenshot BEFORE and AFTER each major action
- If a page doesn't load within 15 seconds, file a ticket and move on
- Check console for errors after every navigation and click
- Be thorough — work through COMPLETE workflows, not just surface checks

## Deep Testing Strategy
Go beyond just "does the page load" — exercise the full workflow:
1. **Navigate** to the section and verify it renders
2. **Interact** with every control: filters, dropdowns, tabs, search, sort, pagination
3. **Submit data** where safe: fill forms, run searches, apply filters, expand/collapse sections
4. **Follow chains**: click a result row → check detail drawer → click each tab → verify data loads
5. **Test edge cases**: empty searches, special characters, very long inputs, rapid clicks
6. **Check responsiveness**: verify tables have data, counts match, no stale/cached data
7. **Verify API health**: check network tab for any 4xx/5xx responses on EVERY action
8. **Console errors**: check after EVERY click and navigation, not just page load
9. **Cross-reference**: if a count says "42 vendors", click through and verify the list has ~42 items
10. **Try breaking things**: enter SQL-like input, paste HTML into text fields, use unicode characters

File a ticket for ANYTHING wrong — broken layouts, missing data, slow loads (>5s),
misleading labels, dead links, empty states that should have data, inconsistent counts,
buttons that do nothing, modals that don't close, filters that don't filter.

---

# Test Area: Trouble Tickets

Navigate to: https://app.availai.net/

## Workflow Tests

### Test 1: Navigate to Trouble Tickets
1. Use `browser_navigate` to go to `https://app.availai.net/`
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

### Test 6: Filter Tickets by Each Status
1. Use `browser_snapshot` to locate status filter controls
2. Use `browser_click` to filter by "open" status
3. Use `browser_snapshot` and VERIFY: only open tickets are shown (if any exist)
4. Use `browser_click` to filter by "in_progress" status
5. Use `browser_snapshot` and VERIFY: only in-progress tickets are shown (if any exist)
6. Use `browser_click` to filter by "resolved" status
7. Use `browser_snapshot` and VERIFY: only resolved tickets are shown
8. Use `browser_click` to filter by "rejected" status
9. Use `browser_snapshot` and VERIFY: only rejected tickets are shown
10. VERIFY: Each filter transition does not produce console errors
11. Use `browser_console_messages` to check for JavaScript errors after each filter change

### Test 7: Verify Ticket Detail View Thoroughly
1. Navigate back to the full ticket list (clear any filters)
2. Use `browser_click` on a ticket to open its detail view
3. Use `browser_snapshot` to capture the full detail view
4. VERIFY: The ticket title is displayed prominently and is not empty
5. VERIFY: A description or body text is shown (may be short but should exist)
6. VERIFY: The current status is displayed with a clear label
7. VERIFY: A created-at timestamp is shown in readable format
8. VERIFY: An updated-at timestamp is shown in readable format
9. VERIFY: Both timestamps are not "Invalid Date" or raw ISO strings
10. Use `browser_console_messages` to check for JavaScript errors

### Test 8: Verify Ticket Number Format
1. Use `browser_snapshot` to inspect ticket numbers/IDs in the list view
2. VERIFY: Ticket numbers follow the format TT-YYYYMMDD-NNN (e.g. TT-20260302-001)
3. VERIFY: No ticket number is blank, "undefined", or just a raw integer
4. VERIFY: The date portion of ticket numbers is plausible (not in the future, not from years ago)
5. VERIFY: Ticket numbers are unique (no duplicates visible in the list)

### Test 9: Verify Risk Tier Badges
1. Use `browser_snapshot` to look for risk tier badges or labels on tickets
2. VERIFY: Risk tier badges are displayed (e.g. "low", "medium", "high", "critical")
3. VERIFY: Badges use appropriate visual styling (color-coded or labeled clearly)
4. VERIFY: No badge shows "undefined", "null", or is completely missing where expected
5. VERIFY: Badge text is readable against its background color

### Test 10: Verify Category Labels
1. Use `browser_snapshot` to inspect category labels on tickets
2. VERIFY: Each ticket has a category label displayed (e.g. "api_failure", "data_quality", "performance")
3. VERIFY: Category labels are human-readable (not raw codes or undefined)
4. VERIFY: Category labels are consistently formatted across all tickets

### Test 11: Test Pagination for Large Ticket Lists
1. Use `browser_snapshot` to check the total number of tickets displayed
2. If more than 50 tickets exist, VERIFY: pagination controls are present
3. If pagination exists, use `browser_click` on "Next" or page 2
4. Use `browser_snapshot` to verify new tickets load on the next page
5. VERIFY: Page navigation does not produce console errors
6. VERIFY: Tickets on page 2 are different from page 1 (not duplicated)
7. Use `browser_network_requests` to verify the API call for the next page succeeds

## What Correct Looks Like
- Ticket list loads within 3 seconds showing a table with title, status, and source columns
- Status values are one of: submitted, diagnosed, in_progress, awaiting_verification, resolved, rejected
- Clicking a ticket opens a detail view with diagnosis information and action history
- Status filters narrow the displayed list to only matching tickets
- Ticket numbers follow the TT-YYYYMMDD-NNN format
- Risk tier badges are color-coded and readable
- Category labels are human-readable and consistent
- Pagination works correctly when more than 50 tickets exist
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
- Ticket numbers not following TT-YYYYMMDD-NNN format
- Risk tier badges missing, unreadable, or showing undefined
- Category labels missing or displaying raw codes
- Pagination controls missing when ticket count exceeds 50
- Duplicate tickets appearing across pages
