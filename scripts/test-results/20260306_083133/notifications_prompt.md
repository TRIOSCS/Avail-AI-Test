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
    "tested_area": "notifications",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "notifications"
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
If all tests pass with no issues, just output: PASS: notifications

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

# Test Area: Notifications

Navigate to: https://app.availai.net/

## Workflow Tests

### Test 1: Locate Bell Icon in Top Navigation
1. Use `browser_navigate` to go to `https://app.availai.net/`
2. Use `browser_snapshot` to capture the page state
3. VERIFY: A bell icon is visible in the top navigation bar
4. VERIFY: The bell icon is clearly identifiable (bell shape, notification icon)
5. Use `browser_console_messages` to check for JavaScript errors on load

### Test 2: Check Unread Badge Count
1. Use `browser_snapshot` to inspect the bell icon area
2. VERIFY: If there are unread notifications, a badge with a number is displayed on or near the bell icon
3. VERIFY: The badge number is a positive integer (not "0", "NaN", "undefined", or negative)
4. VERIFY: If there are no unread notifications, either no badge is shown or the badge shows "0" gracefully

### Test 3: Open Notifications Dropdown
1. Use `browser_click` on the bell icon to open the notifications dropdown or panel
2. Use `browser_snapshot` to capture the dropdown content
3. VERIFY: A dropdown or panel opens showing a list of notifications
4. VERIFY: The dropdown does not appear empty if the badge showed unread count > 0
5. VERIFY: The dropdown renders without JavaScript errors
6. Use `browser_console_messages` to check for errors triggered by opening the dropdown

### Test 4: Verify Notification List Content
1. Use `browser_snapshot` to inspect the notification entries
2. VERIFY: Each notification shows a message or title describing the event
3. VERIFY: Each notification shows a timestamp (relative like "2 hours ago" or absolute date)
4. VERIFY: Notifications are listed in reverse chronological order (newest first)
5. VERIFY: No notification entries show "undefined", "null", or "[object Object]"
6. VERIFY: Notification text is readable and not truncated to the point of being meaningless

### Test 5: Verify Unread vs Read Distinction
1. Use `browser_snapshot` to check if unread and read notifications are visually distinct
2. VERIFY: Unread notifications have a different style (bold text, highlight, dot indicator, or background color)
3. VERIFY: The visual distinction is clear and consistent across all entries
4. VERIFY: The unread count badge matches the number of visually-unread items in the list

### Test 6: Close Notifications Dropdown
1. Use `browser_click` outside the dropdown area or on a close button to dismiss it
2. Use `browser_snapshot` to verify the dropdown is closed
3. VERIFY: The dropdown closes cleanly without leaving artifacts on the page
4. VERIFY: The bell icon is still visible and accessible after closing
5. Use `browser_console_messages` to check for errors on close

### Test 7: Verify Bell Icon Presence in Header
1. Use `browser_navigate` to go to `https://app.availai.net/`
2. Use `browser_snapshot` to inspect the top header/navigation bar
3. VERIFY: A bell icon (or notification icon) is present in the header area
4. VERIFY: The bell icon is visually distinct and clickable (not hidden or overlapped)
5. Navigate to `https://app.availai.net/#admin` and use `browser_snapshot`
6. VERIFY: The bell icon remains visible on the admin page as well (persistent across views)

### Test 8: Verify Bell Click Opens Dropdown with Content
1. Use `browser_click` on the bell icon
2. Use `browser_snapshot` to capture the dropdown
3. VERIFY: The dropdown opens and shows either a list of notifications OR a "no new notifications" message
4. VERIFY: The dropdown does not appear as an empty container with no text at all
5. VERIFY: If the message says "no new" or "all caught up", it is grammatically correct and styled
6. Use `browser_console_messages` to check for JavaScript errors

### Test 9: Verify Notification Timestamps Are Human-Readable
1. If notifications exist in the dropdown, use `browser_snapshot` to inspect their timestamps
2. VERIFY: Timestamps use relative format ("2 hours ago", "yesterday") or a readable absolute date
3. VERIFY: No timestamp shows raw ISO format (e.g. "2026-03-06T14:30:00Z" without formatting)
4. VERIFY: No timestamp shows "Invalid Date", "NaN", or "undefined"
5. VERIFY: Timestamps are plausible (not in the future, not from years ago)

### Test 10: Click a Notification and Verify Navigation
1. If notifications exist in the dropdown, use `browser_click` on the first notification
2. Use `browser_snapshot` to capture the resulting page
3. VERIFY: Clicking the notification navigates to a meaningful location (e.g. a ticket, a company, a requisition)
4. VERIFY: The destination page loads content related to the notification (not a blank page or error)
5. VERIFY: The URL hash changed to reflect the navigation target
6. Use `browser_console_messages` to check for JavaScript errors during navigation

### Test 11: Verify Notification Count Badge Updates
1. Use `browser_navigate` to go to `https://app.availai.net/`
2. Use `browser_snapshot` to note the current notification badge count (if any)
3. Use `browser_click` on the bell icon to open the dropdown
4. If notifications exist, use `browser_click` on a notification to mark it as read
5. Use `browser_navigate` to go to `https://app.availai.net/` (reload)
6. Use `browser_snapshot` to check the badge count
7. VERIFY: The badge count decreased by 1 (or the badge disappeared if count reached 0)
8. VERIFY: The badge count is never negative
9. Use `browser_console_messages` to check for errors

## What Correct Looks Like
- A bell icon is visible in the top navigation bar on every page
- An unread count badge appears when there are unread notifications
- Clicking the bell opens a dropdown listing notifications or a "no new" message
- Each notification shows a descriptive message and a human-readable timestamp
- Timestamps use relative or formatted absolute dates (never raw ISO or "Invalid Date")
- Clicking a notification navigates to the related entity (ticket, requisition, etc.)
- The notification count badge updates when notifications are read
- Notifications appear in reverse chronological order (newest first)
- Unread notifications are visually distinct from read ones
- The dropdown closes cleanly when dismissed
- No console errors during any interaction

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Bell icon missing from the navigation bar
- Badge count showing incorrect or malformed numbers
- Dropdown failing to open or close
- Notifications not in chronological order
- Timestamps showing raw ISO format or "Invalid Date"
- Clicking a notification navigates to a blank page or produces an error
- Badge count not updating after reading a notification
- Bell icon disappearing on certain pages
