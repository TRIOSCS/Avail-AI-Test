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
- Be thorough but finish within 3 minutes

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
