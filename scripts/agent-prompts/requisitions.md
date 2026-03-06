# Test Area: Requisitions

Navigate to: {{BASE_URL}}/#rfqs

## Workflow Tests

### Test 1: Requisition list loads
1. Navigate to {{BASE_URL}}/#rfqs
2. Take a screenshot to capture the initial state
3. Use browser_snapshot to read the accessibility tree
4. VERIFY: A list or table of requisitions is visible with multiple rows
5. VERIFY: Each row shows meaningful data (not empty placeholders)
6. Check browser_console_messages for any JS errors
7. Check browser_network_requests for any failed API calls

### Test 2: Open requisition detail drawer
1. From the requisition list, click on the first requisition row
2. Take a screenshot after clicking
3. Use browser_snapshot to read the drawer content
4. VERIFY: A detail drawer or panel opens on the right side
5. VERIFY: The drawer shows the requisition's parts list
6. VERIFY: A status badge is visible (e.g., Open, In Progress, Quoted)
7. VERIFY: An assignee or owner field is displayed
8. Check browser_console_messages for any JS errors

### Test 3: Drawer tabs work
1. With the requisition detail drawer open, look for tabs (e.g., Parts, Offers, Quotes)
2. Take a screenshot showing the current tab
3. Click on each available tab one by one
4. After clicking each tab, use browser_snapshot to verify content loaded
5. Take a screenshot after switching to each tab
6. VERIFY: Each tab loads its content without errors
7. VERIFY: No blank or stuck-loading states
8. Check browser_console_messages after each tab click

### Test 4: Requisition data integrity
1. With any requisition detail open, examine the displayed data
2. VERIFY: Part numbers display as readable text (not ciphertext or encoded data)
3. VERIFY: Quantities are numeric values (not NaN or undefined)
4. VERIFY: Dates, if shown, are formatted properly (not raw timestamps or "Invalid Date")
5. VERIFY: Currency values, if shown, are formatted (not NaN or overflow numbers)

## What Correct Looks Like
- Requisition list renders with multiple rows showing summary data
- Clicking a row opens a detail drawer with full requisition info
- Status badges display with proper styling and text
- Tabs switch smoothly and load their content
- All data fields show properly formatted values
- No console errors during navigation

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Drawer that fails to open or shows blank content
- Tabs that don't switch or load content
