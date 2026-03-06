# Test Area: RFQ

Navigate to: {{BASE_URL}}/#rfqs

## Workflow Tests

### Test 1: RFQ section loads
1. Navigate to {{BASE_URL}}/#rfqs
2. Take a screenshot to capture the initial state
3. Use browser_snapshot to read the accessibility tree
4. Look for any RFQ-related tabs, sections, or buttons (e.g., "RFQs", "Quotes", "Send RFQ")
5. VERIFY: The page loads without errors
6. Check browser_console_messages for any JS errors
7. Check browser_network_requests for any failed API calls

### Test 2: RFQ list displays columns
1. If an RFQ list or table is visible, examine its columns
2. Take a screenshot of the RFQ list
3. VERIFY: Columns include vendor/supplier name, part number, and status
4. VERIFY: Rows contain actual data (not empty or placeholder text)
5. VERIFY: Status values are readable labels (e.g., "Sent", "Received", "Pending")

### Test 3: Tab switching stability
1. Look for tabs or section switches on the RFQ page (e.g., between Requisitions, RFQs, Pipeline views)
2. Click each available tab
3. After each tab click, check browser_console_messages for errors
4. Take a screenshot after switching to verify the new view renders
5. VERIFY: No console errors from tab switching
6. VERIFY: Each tab's content loads and displays

### Test 4: RFQ detail view
1. If RFQ rows are present, click on the first row
2. Take a screenshot of the detail view
3. VERIFY: Detail shows vendor information, part details, and pricing
4. VERIFY: No ciphertext or raw data is visible where formatted values should be
5. Check browser_console_messages for any JS errors

## What Correct Looks Like
- RFQ section is accessible from the rfqs hash route
- Tables show vendor, part, and status columns with real data
- Tab switching between views is smooth with no errors
- Detail views show properly formatted vendor and pricing info
- No console errors during navigation or interaction

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Tabs that cause console errors when clicked
