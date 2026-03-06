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
    "tested_area": "rfq",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "rfq"
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
If all tests pass with no issues, just output: PASS: rfq

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

# Test Area: RFQ

Navigate to: https://app.availai.net/#rfqs

## Workflow Tests

### Test 1: RFQ section loads
1. Navigate to https://app.availai.net/#rfqs
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

### Test 5: RFQ status tracking and badges
1. From the RFQ list, take a screenshot
2. Use browser_snapshot to identify all visible status values across rows
3. Look for RFQs in different statuses: "Sent", "Responded", "Expired", "Draft", "Pending"
4. VERIFY: Each status displays as a styled badge or label (colored background, not plain text)
5. VERIFY: "Sent" badges use a distinct color (e.g., blue or yellow) different from "Responded" (e.g., green)
6. VERIFY: "Expired" badges use a warning or muted color (e.g., red or gray)
7. Take a screenshot showing multiple status badge colors for comparison
8. VERIFY: No status shows as raw text like "sent" (lowercase) without styling
9. VERIFY: No status shows as a numeric value or code instead of a readable label
10. Check browser_console_messages for any JS errors

### Test 6: Response data for answered RFQs
1. Look for an RFQ with "Responded" status in the list
2. Click on it to open the detail view
3. Take a screenshot of the detail
4. VERIFY: Response data is visible, including pricing information (formatted as currency, e.g., "$1.50")
5. VERIFY: Lead time is shown in a human-readable format (e.g., "4-6 weeks", "14 days")
6. VERIFY: Vendor details are displayed — vendor name as readable text, not an ID or UUID
7. VERIFY: If multiple responses exist, each is listed separately with its own pricing
8. VERIFY: No response fields show "NaN", "undefined", "[object Object]", or "null"
9. Use browser_snapshot to read all response data in the accessibility tree
10. Check browser_console_messages for any JS errors

### Test 7: Email trail / communications
1. With an RFQ detail view open (preferably one with "Responded" status), look for an email or communications section
2. Take a screenshot
3. Use browser_snapshot to find any email-related UI elements (e.g., "Emails", "Communications", "Messages" tab or section)
4. If an email section exists, click on it
5. Take a screenshot of the email trail
6. VERIFY: Sent emails show the date, recipient, and subject line
7. VERIFY: Received responses show the date, sender, and content or summary
8. VERIFY: Email dates are in human-readable format, not raw timestamps
9. VERIFY: No email content displays as ciphertext, encoded data, or raw HTML tags
10. If no email section exists, report that RFQ email trail is not exposed in the UI
11. Check browser_console_messages for any JS errors

### Test 8: Date formatting across RFQ views
1. Navigate to the RFQ list view
2. Take a screenshot showing date columns (e.g., "Sent Date", "Due Date", "Created")
3. Use browser_snapshot to read all date values in the list
4. VERIFY: All dates are in human-readable format (e.g., "Mar 6, 2026" or "03/06/2026")
5. VERIFY: No dates appear as ISO 8601 (e.g., "2026-03-06T14:30:00.000Z")
6. VERIFY: No dates appear as Unix timestamps (e.g., "1741219200")
7. VERIFY: No dates show as "Invalid Date", "NaN", or empty strings where a date should be
8. Open an RFQ detail view and check dates there
9. VERIFY: Detail view dates are also human-readable
10. VERIFY: Dates in the detail view are consistent in format with the list view dates
11. If relative dates are used (e.g., "3 days ago"), verify they seem accurate relative to today's date

### Test 9: Bulk actions
1. Navigate to the RFQ list view
2. Take a screenshot
3. Use browser_snapshot to look for checkboxes on each row or a "Select All" checkbox
4. If checkboxes exist, click the first row's checkbox
5. Take a screenshot — VERIFY: The row is visually selected (highlighted, checked)
6. Click two more row checkboxes to select multiple RFQs
7. Look for bulk action buttons or a dropdown that appeared (e.g., "Delete Selected", "Resend", "Export")
8. Take a screenshot showing the bulk action UI
9. VERIFY: Bulk action buttons are visible and labeled clearly
10. If a "Select All" checkbox exists, click it
11. VERIFY: All rows become selected
12. Click "Select All" again to deselect
13. VERIFY: All rows become deselected
14. If no checkboxes or bulk actions exist, report that bulk actions are not available in the RFQ UI
15. Check browser_console_messages for any JS errors during selection

### Test 10: Network validation across interactions
1. Navigate to https://app.availai.net/#rfqs fresh
2. Monitor browser_network_requests from the start
3. Wait for the page to fully load
4. VERIFY: All initial /api/ requests returned HTTP 200
5. Click on the first RFQ row to open details
6. Check browser_network_requests — VERIFY: Detail API call returned 200
7. If tabs exist in the detail view, click each tab
8. After each tab click, check browser_network_requests
9. VERIFY: Every tab-loading API call returned 200, no 4xx or 5xx
10. Close the detail view and click on a different RFQ
11. Check browser_network_requests again
12. VERIFY: No 4xx or 5xx errors across any interaction
13. VERIFY: All API response times are under 3 seconds
14. VERIFY: No CORS errors appear in browser_console_messages
15. Take a final screenshot summarizing the network health

## What Correct Looks Like
- RFQ section is accessible from the rfqs hash route
- Tables show vendor, part, and status columns with real data
- Tab switching between views is smooth with no errors
- Detail views show properly formatted vendor and pricing info
- No console errors during navigation or interaction
- Status badges render with distinct colors for each status (Sent, Responded, Expired, etc.)
- RFQ responses show formatted pricing, lead times, and vendor details
- Email trail (if present) shows dated sent/received communications
- All dates are human-readable across list and detail views, never raw ISO or Unix
- Bulk actions (if present) allow multi-select with visible action buttons
- All API calls return 200 with response times under 3 seconds

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Tabs that cause console errors when clicked
- Status badges missing styling or showing raw/numeric values instead of labels
- Response data missing pricing, lead time, or vendor details
- Dates displayed as ISO 8601, Unix timestamps, or "Invalid Date"
- Email trail showing raw HTML, ciphertext, or encoded content
- Bulk action checkboxes not responding or bulk action buttons missing after selection
- API calls returning 4xx/5xx or taking longer than 3 seconds
- CORS errors or blocked network requests
