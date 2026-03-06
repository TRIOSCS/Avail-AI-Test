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
    "tested_area": "prospecting",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "prospecting"
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
If all tests pass with no issues, just output: PASS: prospecting

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

# Test Area: Prospecting

Navigate to: https://app.availai.net/#suggested

## Workflow Tests

### Test 1: Discovery pool loads
1. Navigate to https://app.availai.net/#suggested
2. Take a screenshot to capture the initial state
3. Use browser_snapshot to read the accessibility tree
4. VERIFY: The page loads the discovery pool view (card-based layout)
5. VERIFY: Company name cards are visible with readable names
6. VERIFY: The page does not show a blank screen or loading spinner stuck indefinitely
7. Check browser_console_messages for any JS errors
8. Check browser_network_requests for any failed API calls

### Test 2: Discovery cards display data
1. Examine the discovery pool cards visible on the page
2. Take a screenshot of the cards
3. VERIFY: Each card shows a company name
4. VERIFY: Cards show additional info like industry, location, or revenue range where available
5. VERIFY: No cards display "[object Object]", "undefined", or "null" as content
6. VERIFY: Card layout is not broken (no overlapping or truncated text)

### Test 3: Filter controls
1. Look for filter controls on the page (industry, region, revenue dropdowns or inputs)
2. If filter controls exist, take a screenshot showing them
3. Click on an industry filter dropdown if available
4. VERIFY: Filter options load and are selectable
5. Select a filter value and verify the card list updates
6. Check browser_console_messages for any JS errors after filtering
7. If no filters exist, note this but do not file a ticket (filters may not be implemented yet)

### Test 4: Card detail interaction
1. Click on one of the discovery pool cards
2. Take a screenshot after clicking
3. VERIFY: A detail view, drawer, or expanded card opens with more information about the company
4. VERIFY: The detail shows company details (website, industry, revenue, etc.)
5. VERIFY: No console errors from browser_console_messages
6. Check browser_network_requests for any failed API calls

### Test 5: Navigate to Discovery Pool via Hash Route
1. Use `browser_navigate` to go to `https://app.availai.net/#view-suggested`
2. Use `browser_snapshot` to capture the page state
3. VERIFY: The discovery pool view loads (same view as #suggested)
4. VERIFY: Company cards are visible with data
5. VERIFY: The page does not redirect to a different view unexpectedly
6. Use `browser_console_messages` to check for JavaScript errors

### Test 6: Verify Company Card Data Fields
1. Use `browser_snapshot` to inspect individual company cards closely
2. VERIFY: Each card displays a company name that is not empty or "undefined"
3. VERIFY: Cards show an industry label where available (e.g. "Electronics", "Manufacturing")
4. VERIFY: Cards show a revenue range where available (e.g. "$1M-$10M", "Enterprise")
5. VERIFY: No card shows "[object Object]" or "null" in any field
6. VERIFY: Cards without certain data show graceful fallbacks (e.g. "N/A" or simply omit the field)

### Test 7: Test Industry Filter
1. Use `browser_snapshot` to locate the industry filter control (dropdown or multi-select)
2. If an industry filter exists, use `browser_click` to open it
3. Use `browser_snapshot` to verify filter options are listed
4. Use `browser_click` to select an industry option
5. Use `browser_snapshot` to verify the card grid updates to show only matching companies
6. VERIFY: The displayed cards match the selected industry
7. Use `browser_console_messages` to check for JavaScript errors after filtering

### Test 8: Test Revenue Range Filter
1. Use `browser_snapshot` to locate a revenue range filter control
2. If a revenue filter exists, use `browser_click` to open it
3. Use `browser_click` to select a revenue range option
4. Use `browser_snapshot` to verify the card grid updates
5. VERIFY: Displayed cards match the selected revenue range
6. Use `browser_console_messages` to check for errors

### Test 9: Test Region Filter
1. Use `browser_snapshot` to locate a region filter control
2. If a region filter exists, use `browser_click` to open it
3. Use `browser_click` to select a region option
4. Use `browser_snapshot` to verify the card grid updates
5. VERIFY: Displayed cards are filtered by the selected region
6. Use `browser_console_messages` to check for errors

### Test 10: Click Company Card and Verify Detail View
1. Use `browser_snapshot` to identify a company card
2. Use `browser_click` on the card to open the detail view
3. Use `browser_snapshot` to capture the detail/drawer view
4. VERIFY: The detail view shows the company name matching the card clicked
5. VERIFY: Enrichment data is displayed (website, industry, employee count, revenue, location, etc.)
6. VERIFY: No enrichment fields show "undefined", "null", or "[object Object]"
7. VERIFY: If a "Discover" or "Add to CRM" button is present, it is visible and labeled clearly
8. Do NOT click the Add/Discover button without confirming what it does first
9. Use `browser_console_messages` to check for errors

### Test 11: Test Sorting Options
1. Use `browser_snapshot` to look for sorting controls (dropdown, toggle, or sort buttons)
2. If sorting exists, use `browser_click` to change the sort order (e.g. by recency, by name, by revenue)
3. Use `browser_snapshot` to verify the card order changes
4. VERIFY: Cards reorder according to the selected sort criterion
5. VERIFY: Sorting does not produce duplicate cards or empty results
6. Use `browser_console_messages` to check for errors

### Test 12: Verify Discover/Add Buttons Open Confirmation
1. Use `browser_snapshot` to locate "Discover", "Add", or "Import" buttons on a company card or detail view
2. If such a button exists, use `browser_click` on it
3. Use `browser_snapshot` to capture the result
4. VERIFY: A confirmation dialog or modal appears before the action is executed
5. VERIFY: The confirmation shows what will happen (e.g. "Add company to CRM?")
6. Use `browser_click` to dismiss/cancel the confirmation without proceeding
7. VERIFY: Canceling returns to the previous state without side effects
8. Use `browser_console_messages` to check for errors

## What Correct Looks Like
- Discovery pool shows a grid or list of company cards
- Each card displays a company name, industry, and revenue range where available
- Filter controls for industry, revenue, and region update the displayed cards
- Clicking a card opens a detail view with enriched company data
- Sorting options reorder cards correctly
- Discover/Add buttons show a confirmation before executing
- No console errors during any interaction
- Page loads in under 5 seconds

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty card grid when data should exist)
- Broken formatting (NaN, undefined, [object Object])
- Cards that don't respond to clicks
- Filters that don't update the displayed results
- Layout issues (overlapping cards, broken grid)
- Company cards missing name, industry, or revenue fields
- Detail view failing to show enrichment data
- Discover/Add buttons executing without confirmation
- Sorting controls not changing card order
- Filters producing empty results when data should match
