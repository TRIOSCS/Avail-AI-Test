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
    "tested_area": "pipeline",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "pipeline"
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
If all tests pass with no issues, just output: PASS: pipeline

## Rules
- Do NOT click delete/remove/logout/destroy/purge buttons
- Take a screenshot BEFORE and AFTER each major action
- If a page doesn't load within 15 seconds, file a ticket and move on
- Check console for errors after every navigation and click
- Be thorough but finish within 3 minutes

---

# Test Area: Pipeline / Dashboard

Navigate to: https://app.availai.net/#dashboard

## Workflow Tests

### Test 1: Navigate to Pipeline Dashboard
1. Use `browser_navigate` to go to `https://app.availai.net/#dashboard`
2. Use `browser_snapshot` to capture the page state
3. VERIFY: The pipeline or dashboard view loads and displays content
4. VERIFY: The page has a recognizable heading or title for the dashboard
5. Use `browser_console_messages` to check for JavaScript errors on load
6. Use `browser_network_requests` to check for failed API calls

### Test 2: Verify KPI Cards Display
1. Use `browser_snapshot` to inspect the KPI or metric cards at the top of the dashboard
2. VERIFY: KPI cards are visible showing key metrics (e.g. total requisitions, active sites, pipeline value)
3. VERIFY: Each card shows a numeric value that is a valid number (not "NaN", "undefined", or "null")
4. VERIFY: Card labels are descriptive and readable
5. VERIFY: Cards are properly aligned and not overlapping

### Test 3: Verify Currency Formatting
1. Use `browser_snapshot` to look for currency values on the dashboard
2. VERIFY: Dollar amounts use proper formatting with "$" prefix and comma separators (e.g. "$1,234,567")
3. VERIFY: No currency values display as raw numbers without formatting (e.g. "1234567.89")
4. VERIFY: No currency values show "NaN", "$NaN", "$undefined", or "$null"
5. VERIFY: Large values display correctly (no scientific notation like "1.23e+6")

### Test 4: Verify Pipeline Chart or Table
1. Use `browser_snapshot` to look for pipeline visualization (chart, table, or funnel)
2. VERIFY: If a chart exists, it renders visually (not a blank canvas or broken image)
3. VERIFY: If a table exists, it has headers and data rows
4. VERIFY: Pipeline stages or categories are labeled clearly
5. VERIFY: Data values in the pipeline view are consistent (no negative counts, no impossible values)

### Test 5: Check for JavaScript Errors Across Interactions
1. Use `browser_console_messages` to collect all console messages
2. VERIFY: No uncaught exceptions or JavaScript errors are present
3. VERIFY: No "TypeError", "ReferenceError", or "SyntaxError" messages in the console
4. If there are interactive elements (tabs, filters, date pickers), use `browser_click` on each
5. After each interaction, use `browser_console_messages` to check for new errors
6. Use `browser_snapshot` after interactions to verify the page updates correctly

### Test 6: Verify Dashboard Load Performance
1. Use `browser_network_requests` to review all API calls made during dashboard load
2. VERIFY: No API requests return 4xx or 5xx status codes
3. VERIFY: The dashboard is fully rendered within 5 seconds of navigation
4. VERIFY: No loading spinners are stuck in a perpetual loading state
5. VERIFY: All sections of the dashboard have populated with data (no empty placeholder sections)

## What Correct Looks Like
- Dashboard loads within 5 seconds at the #dashboard hash route
- KPI cards display valid numeric values with proper formatting
- Currency values use "$" prefix with comma separators (e.g. "$1,234,567.00")
- Pipeline visualization (chart or table) renders with real data
- All pipeline stages are labeled and show reasonable values
- No JavaScript errors in the console
- No failed API requests
- No stuck loading spinners or empty sections

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- KPI cards showing NaN, undefined, or malformed numbers
- Currency values without proper formatting
- Charts that fail to render or show blank
- Pipeline stages with negative or impossible values
