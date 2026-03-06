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
    "tested_area": "vendors",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "vendors"
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
If all tests pass with no issues, just output: PASS: vendors

## Rules
- Do NOT click delete/remove/logout/destroy/purge buttons
- Take a screenshot BEFORE and AFTER each major action
- If a page doesn't load within 15 seconds, file a ticket and move on
- Check console for errors after every navigation and click
- Be thorough but finish within 3 minutes

---

# Test Area: Vendors

Navigate to: https://app.availai.net/#vendors

## Workflow Tests

### Test 1: Vendor list loads
1. Navigate to https://app.availai.net/#vendors
2. Take a screenshot to capture the initial state
3. Use browser_snapshot to read the accessibility tree
4. VERIFY: A list or table of vendors is visible with vendor names
5. VERIFY: The list has multiple entries (not empty)
6. VERIFY: The page loaded in under 5 seconds
7. Check browser_console_messages for any JS errors
8. Check browser_network_requests for any failed API calls

### Test 2: Search for a vendor
1. Look for a search or filter input on the vendors page
2. If a search field exists, type "Digi" into it (partial match for DigiKey or similar)
3. Take a screenshot of the filtered results
4. VERIFY: The list filters to show matching vendors
5. VERIFY: Results update without a full page reload
6. Check browser_console_messages for any JS errors
7. If no search field exists, note this but do not file a ticket

### Test 3: Vendor detail view
1. Click on the first vendor in the list
2. Take a screenshot after clicking
3. Use browser_snapshot to read the detail content
4. VERIFY: A detail card or drawer opens showing vendor information
5. VERIFY: The vendor name is prominently displayed
6. VERIFY: Contact or description information is shown if available
7. Check browser_console_messages for any JS errors

### Test 4: Sighting counts display
1. In the vendor list or detail view, look for sighting counts or part counts
2. VERIFY: Sighting counts display as numeric values (not NaN, undefined, or blank)
3. VERIFY: If count badges or numbers are present, they are properly formatted
4. VERIFY: No negative counts or obviously wrong numbers (e.g., -1, 999999999)
5. Take a screenshot showing the sighting data

### Test 5: Return to list
1. Close the vendor detail (click back, close button, or outside the drawer)
2. VERIFY: The vendor list is still displayed and intact
3. VERIFY: The list did not reset or lose its position
4. Check browser_console_messages for any errors during navigation back

## What Correct Looks Like
- Vendor list loads promptly with readable vendor names
- Search filters the list dynamically
- Clicking a vendor opens a detail view with full information
- Sighting counts are formatted numeric values
- Navigation between list and detail is smooth
- No console errors during any interaction

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Vendor detail that fails to open or shows blank content
- Sighting counts that show NaN or incorrect values
