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
    "tested_area": "crm_companies",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "crm_companies"
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
If all tests pass with no issues, just output: PASS: crm_companies

## Rules
- Do NOT click delete/remove/logout/destroy/purge buttons
- Take a screenshot BEFORE and AFTER each major action
- If a page doesn't load within 15 seconds, file a ticket and move on
- Check console for errors after every navigation and click
- Be thorough but finish within 3 minutes

---

# Test Area: CRM Companies

Navigate to: https://app.availai.net/#customers

## Workflow Tests

### Test 1: Companies list loads
1. Navigate to https://app.availai.net/#customers
2. Take a screenshot to capture the initial state
3. Use browser_snapshot to read the accessibility tree
4. VERIFY: A list of companies is visible with company names
5. VERIFY: Each row shows site count and open requisition count
6. VERIFY: The list has multiple entries (not empty)
7. Check browser_console_messages for any JS errors
8. Check browser_network_requests for any failed API calls (especially /api/companies)

### Test 2: Company detail drawer
1. Click on the first company in the list
2. Take a screenshot after clicking
3. Use browser_snapshot to read the drawer content
4. VERIFY: A detail drawer opens showing the company name prominently
5. VERIFY: The drawer contains tabs (Sites, Contacts, Requisitions, or similar)
6. Check browser_console_messages for any JS errors

### Test 3: Drawer tabs load content
1. With the company detail drawer open, look for the Sites tab and click it
2. Take a screenshot and verify site data loads
3. Click the Contacts tab
4. Take a screenshot and verify contacts load (names, emails, phone numbers)
5. Click the Requisitions tab
6. Take a screenshot and verify requisition data loads
7. VERIFY: Each tab transitions smoothly without blank screens
8. Check browser_console_messages after each tab switch

### Test 4: Load More pagination
1. Close the drawer by clicking outside it or pressing Escape
2. Scroll to the bottom of the companies list
3. Look for a "Load More" button or infinite scroll trigger
4. If a "Load More" button exists, click it
5. Take a screenshot after loading more
6. VERIFY: Additional companies appear in the list
7. VERIFY: The previously loaded companies are still visible
8. Check browser_console_messages for any errors during pagination

### Test 5: Data formatting
1. Examine the companies list for data formatting
2. VERIFY: Site counts are numeric (not NaN, undefined, or [object Object])
3. VERIFY: Requisition counts are numeric
4. VERIFY: Company names are readable text
5. VERIFY: No raw JSON or ciphertext is displayed anywhere

## What Correct Looks Like
- Companies list loads promptly (under 2 seconds) with names and counts
- Clicking a company opens a detail drawer with multiple tabs
- Each tab (Sites, Contacts, Requisitions) loads its own content
- Load More pagination adds rows without losing existing ones
- All numeric values display as formatted numbers
- No console errors during any interaction

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Drawer that fails to open or shows blank content
- Tabs that don't load their content
- Pagination that breaks the list or duplicates entries
