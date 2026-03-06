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
    "tested_area": "activity",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "activity"
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
If all tests pass with no issues, just output: PASS: activity

## Rules
- Do NOT click delete/remove/logout/destroy/purge buttons
- Take a screenshot BEFORE and AFTER each major action
- If a page doesn't load within 15 seconds, file a ticket and move on
- Check console for errors after every navigation and click
- Be thorough but finish within 3 minutes

---

# Test Area: Activity Feed

Navigate to: https://app.availai.net/

## Workflow Tests

### Test 1: Navigate to Activity Section
1. Use `browser_navigate` to go to `https://app.availai.net/`
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

## What Correct Looks Like
- Activity feed is accessible from the sidebar or visible on the main dashboard
- Each entry shows three key pieces of information: user, action, and timestamp
- Entries are listed in reverse chronological order (newest first)
- Timestamps are human-readable and properly formatted
- Action descriptions are clear and descriptive
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
