# Test Area: Notifications

Navigate to: {{BASE_URL}}/

## Workflow Tests

### Test 1: Locate Bell Icon in Top Navigation
1. Use `browser_navigate` to go to `{{BASE_URL}}/`
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

## What Correct Looks Like
- A bell icon is visible in the top navigation bar on every page
- An unread count badge appears when there are unread notifications
- Clicking the bell opens a dropdown listing notifications
- Each notification shows a descriptive message and a timestamp
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
