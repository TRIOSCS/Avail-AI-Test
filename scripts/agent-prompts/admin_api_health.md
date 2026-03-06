# Test Area: Admin API Health Dashboard

Navigate to: {{BASE_URL}}/#alerts

## Workflow Tests

### Test 1: Navigate to API Health Dashboard
1. Use `browser_navigate` to go to `{{BASE_URL}}/#alerts`
2. Use `browser_snapshot` to capture the page state
3. VERIFY: The API Health dashboard loads and is visible
4. VERIFY: A status grid or table of API sources is displayed
5. Use `browser_console_messages` to check for JavaScript errors on load
6. Use `browser_network_requests` to check for failed API calls

### Test 2: Verify Status Grid Content
1. Use `browser_snapshot` to inspect the status grid
2. VERIFY: Each API source has a name label (e.g. Lusha, Hunter, Apollo, DigiKey, Mouser, Nexar, etc.)
3. VERIFY: Each source shows a status indicator (green/healthy, yellow/degraded, red/down, or similar)
4. VERIFY: Status values are not empty, "undefined", or "null"
5. VERIFY: No source rows show broken formatting or missing data

### Test 3: Verify Last-Check Timestamps
1. Use `browser_snapshot` to look for last-check or last-ping timestamps on each source
2. VERIFY: Each source displays a last-check timestamp
3. VERIFY: Timestamps are in a readable format (not raw ISO, not "Invalid Date")
4. VERIFY: Timestamps are recent (within the last few hours, not from weeks ago)

### Test 4: Verify Warning Banner Behavior
1. Use `browser_navigate` to go to `{{BASE_URL}}/`
2. Use `browser_snapshot` to check for a warning banner at the top of the page
3. If any API sources are degraded or down, VERIFY: an amber or red warning banner is visible
4. If all API sources are healthy, VERIFY: no warning banner is shown (or it is hidden)
5. VERIFY: The banner text describes which sources have issues (if applicable)
6. Use `browser_console_messages` to check for errors related to the banner

### Test 5: Verify Dashboard Data Sections
1. Use `browser_navigate` to go to `{{BASE_URL}}/#alerts`
2. Use `browser_snapshot` to look for usage statistics or additional health metrics
3. VERIFY: If a usage overview section exists, it shows request counts or usage data
4. VERIFY: Numeric values are properly formatted (not NaN or undefined)
5. VERIFY: The page does not show any loading spinners stuck indefinitely

### Test 6: Click API Source Row for Details
1. Use `browser_navigate` to go to `{{BASE_URL}}/#alerts`
2. Use `browser_snapshot` to identify clickable API source rows in the status grid
3. Use `browser_click` on the first API source row to open its details
4. Use `browser_snapshot` to capture the detail view
5. VERIFY: The detail view shows the source name prominently
6. VERIFY: An error count is displayed and is a valid number (not NaN or undefined)
7. VERIFY: A last-check time is shown in human-readable format
8. VERIFY: A response time metric is displayed (e.g. "120ms" or similar)
9. Use `browser_console_messages` to check for JavaScript errors on interaction
10. Repeat for at least one more API source row to confirm consistency

### Test 7: Verify Status Indicator Color Consistency
1. Use `browser_navigate` to go to `{{BASE_URL}}/#alerts`
2. Use `browser_snapshot` to inspect all status indicators across every API source
3. VERIFY: All "live" or "healthy" sources use green indicators (consistent shade/style)
4. VERIFY: All "degraded" or "warning" sources use yellow/amber indicators
5. VERIFY: All "error" or "down" sources use red indicators
6. VERIFY: No source uses an unexpected color (e.g. blue, gray for active status)
7. VERIFY: The color scheme is consistent — no mix of different green shades for the same status

### Test 8: Verify Error Counts Are Valid Numbers
1. Use `browser_snapshot` to inspect error count values across all API source rows
2. VERIFY: Every error count displayed is a valid integer (0 or positive number)
3. VERIFY: No error count shows "NaN", "undefined", "null", or blank
4. VERIFY: Error counts of zero are displayed as "0" (not hidden or empty)
5. VERIFY: Large error counts are formatted readably (e.g. "1,234" not "1234" for thousands)

### Test 9: Verify Last-Checked Timestamps Are Recent
1. Use `browser_snapshot` to inspect last-checked timestamps on all API sources
2. VERIFY: Every source has a last-checked timestamp displayed
3. VERIFY: No timestamp is older than 24 hours (sources should be checked regularly)
4. VERIFY: No timestamp shows a date from a previous month or year (stale data)
5. VERIFY: Timestamps use a consistent format across all sources
6. VERIFY: No timestamp displays "Never" or is completely missing for an active source

### Test 10: Verify Usage Stats and Call Counts
1. Use `browser_snapshot` to look for API usage statistics or call count sections
2. VERIFY: If a usage overview section exists, API call counts are displayed as formatted numbers
3. VERIFY: Call counts use proper number formatting (e.g. "1,234" not "1234")
4. VERIFY: Usage percentages (if shown) are between 0% and 100%
5. VERIFY: No usage stat shows "NaN", "undefined", or negative numbers
6. VERIFY: If monthly limits are shown, current usage does not exceed the limit without a warning

## What Correct Looks Like
- The API Health page loads within 3 seconds at the #alerts hash route
- A grid or table lists all configured API sources by name
- Each source has a colored status indicator (green for healthy, amber for degraded, red for down)
- Each source shows a last-check timestamp in human-readable format
- Clicking a source row reveals details: error count, last check time, response time
- Error counts are always valid numbers, never NaN
- Last-checked timestamps are recent (within 24 hours) and consistently formatted
- Usage statistics show properly formatted call counts and percentages
- A warning banner appears on all pages when any source is degraded or down
- The banner is hidden when all sources are healthy
- Status indicator colors are consistent across all sources

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Status indicators that are missing or show incorrect states
- Timestamps showing "Invalid Date" or missing entirely
- Warning banner stuck visible when all sources are healthy
- Error counts displaying NaN or non-numeric values
- Last-checked timestamps older than 24 hours (stale monitoring data)
- Inconsistent status indicator colors for the same status type
- Usage stats with negative numbers or percentages above 100%
- Source detail view failing to open on click
