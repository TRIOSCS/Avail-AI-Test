# Test Area: Pipeline / Dashboard

Navigate to: {{BASE_URL}}/#dashboard

## Workflow Tests

### Test 1: Navigate to Pipeline Dashboard
1. Use `browser_navigate` to go to `{{BASE_URL}}/#dashboard`
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
