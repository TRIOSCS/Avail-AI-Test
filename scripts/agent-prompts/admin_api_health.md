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

## What Correct Looks Like
- The API Health page loads within 3 seconds at the #alerts hash route
- A grid or table lists all configured API sources by name
- Each source has a colored status indicator (green for healthy, amber for degraded, red for down)
- Each source shows a last-check timestamp in human-readable format
- A warning banner appears on all pages when any source is degraded or down
- The banner is hidden when all sources are healthy
- Usage statistics (if present) show properly formatted numbers

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
