# Test Area: Activity Feed

Navigate to: {{BASE_URL}}/

## Workflow Tests

### Test 1: Navigate to Activity Section
1. Use `browser_navigate` to go to `{{BASE_URL}}/`
2. Use `browser_snapshot` to capture the page state
3. Look for an "Activity", "Activity Log", or "Recent Activity" section in the sidebar or on the dashboard
4. If it is a sidebar item, use `browser_click` to navigate to the activity view
5. Use `browser_snapshot` to capture the activity feed
6. VERIFY: The activity feed loads and displays a list of entries
7. Use `browser_console_messages` to check for JavaScript errors on load

### Test 2: Verify Activity Feed Content
1. Use `browser_snapshot` to inspect the activity feed entries
2. VERIFY: Each entry shows a user name or identifier (who performed the action)
3. VERIFY: Each entry shows an action description (what was done, e.g. "created requisition", "updated contact")
4. VERIFY: Each entry shows a timestamp (when the action occurred)
5. VERIFY: No entries display "undefined", "null", "NaN", or "[object Object]"
6. VERIFY: Action descriptions are human-readable (not raw event codes)

### Test 3: Verify Chronological Order
1. Use `browser_snapshot` to inspect the timestamps of activity entries
2. VERIFY: Entries are in reverse chronological order (newest entry appears first at the top)
3. VERIFY: Timestamps decrease as you scroll down the list
4. VERIFY: No entries have future timestamps
5. VERIFY: Timestamps are in a readable format (relative like "2 hours ago" or absolute dates)

### Test 4: Verify Activity Entry Details
1. Use `browser_snapshot` to inspect individual activity entries
2. VERIFY: If entries are clickable, use `browser_click` on one to see if it expands or navigates
3. VERIFY: Entry details (if expandable) show additional context about the action
4. VERIFY: Links to related entities (requisitions, contacts, companies) work if present
5. Use `browser_console_messages` to check for errors during interaction

### Test 5: Verify Activity Feed Pagination or Scroll
1. Use `browser_snapshot` to check if the activity feed has pagination controls or infinite scroll
2. If pagination exists: VERIFY that "Next" or page number buttons are functional
3. If infinite scroll exists: VERIFY that more entries load when scrolling down
4. VERIFY: The feed does not show a permanent "Loading..." state
5. VERIFY: The total number of entries displayed is reasonable (not showing thousands at once)
6. Use `browser_network_requests` to verify API calls succeed when loading more entries

### Test 6: Navigate to Activity Log Section
1. Use `browser_navigate` to go to `{{BASE_URL}}/`
2. Use `browser_snapshot` to capture the page
3. Look for an "Activity" or "Activity Log" link in the sidebar navigation or on the dashboard
4. Use `browser_click` on the activity section link
5. Use `browser_snapshot` to verify the activity log view loads
6. VERIFY: The activity log displays a list or table of activity entries
7. VERIFY: The view has a clear heading indicating "Activity" or "Activity Log"
8. Use `browser_console_messages` to check for JavaScript errors

### Test 7: Verify Activity Entry Fields
1. Use `browser_snapshot` to inspect individual activity entries
2. VERIFY: Each entry shows a timestamp (when the action occurred)
3. VERIFY: Each entry shows a user name or identifier (who performed the action)
4. VERIFY: Each entry shows an action type (e.g. "email_sent", "call_logged", "contact_created", "requisition_updated")
5. VERIFY: Each entry shows a target entity or description (what was acted upon)
6. VERIFY: No entry has all four fields — it should have at least timestamp, user, and action
7. VERIFY: No fields show "undefined", "null", "NaN", or "[object Object]"

### Test 8: Test Filtering by Activity Type
1. Use `browser_snapshot` to look for activity type filter controls (dropdown, tabs, or checkboxes)
2. If a filter for activity type exists, use `browser_click` to open it
3. Use `browser_snapshot` to see available filter options (e.g. email_sent, call_logged, contact_created)
4. Use `browser_click` to select one activity type (e.g. "email_sent")
5. Use `browser_snapshot` to verify the list updates to show only matching activities
6. VERIFY: All displayed entries match the selected activity type
7. VERIFY: Filtering does not produce console errors
8. Use `browser_console_messages` to check for errors

### Test 9: Test Date Range Filter
1. Use `browser_snapshot` to look for date range filter controls (date pickers, "Last 7 days" dropdown, etc.)
2. If a date range filter exists, use `browser_click` to interact with it
3. Select a date range (e.g. "Last 7 days" or set a start/end date)
4. Use `browser_snapshot` to verify the activity list updates
5. VERIFY: All displayed entries have timestamps within the selected date range
6. VERIFY: No entries from outside the range are shown
7. Use `browser_console_messages` to check for errors

### Test 10: Verify Activity Links Navigate to Referenced Entity
1. Use `browser_snapshot` to look for clickable links within activity entries (e.g. a company name, requisition ID, or contact name)
2. If a linked entity exists, use `browser_click` on it
3. Use `browser_snapshot` to capture the destination
4. VERIFY: The click navigates to the referenced entity's detail view (not a blank page or error)
5. VERIFY: The destination page shows information related to the activity entry
6. Use `browser_console_messages` to check for JavaScript errors during navigation
7. Navigate back to the activity log and verify it reloads correctly

### Test 11: Verify Reverse Chronological Order
1. Use `browser_snapshot` to inspect the activity entries
2. VERIFY: The first (topmost) entry has the most recent timestamp
3. VERIFY: Each subsequent entry has an equal or older timestamp than the one above it
4. VERIFY: No entry is out of order (newer entries should never appear below older ones)
5. If timestamps are relative (e.g. "2 minutes ago", "1 hour ago"), verify they increase in age going down the list
6. VERIFY: No entries have future timestamps

## What Correct Looks Like
- Activity feed is accessible from the sidebar or visible on the main dashboard
- Each entry shows four key pieces of information: timestamp, user, action type, and target
- Entries are listed in reverse chronological order (newest first)
- Timestamps are human-readable and properly formatted
- Action descriptions are clear and descriptive
- Activity type filters narrow the list to matching entries
- Date range filters restrict entries to the selected period
- Clicking linked entities navigates to their detail views
- The feed loads within 3 seconds
- Pagination or scroll-to-load works if the feed has many entries
- No console errors or failed network requests

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Activity entries missing user, action, or timestamp fields
- Entries not in reverse chronological order
- Timestamps showing "Invalid Date" or raw ISO strings
- Feed stuck in a loading state
- Pagination controls that don't work
- Activity type filter not narrowing results correctly
- Date range filter showing entries outside the selected range
- Entity links navigating to blank pages or errors
- Future timestamps on activity entries
