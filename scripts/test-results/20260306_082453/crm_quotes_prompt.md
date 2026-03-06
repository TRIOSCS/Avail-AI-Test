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
    "tested_area": "crm_quotes",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "crm_quotes"
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
If all tests pass with no issues, just output: PASS: crm_quotes

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

# Test Area: CRM Quotes

Navigate to: https://app.availai.net/#customers

## Workflow Tests

### Test 1: Navigate to quotes via company
1. Navigate to https://app.availai.net/#customers
2. Take a screenshot to capture the initial state
3. Wait for the companies list to load
4. Click on the first company to open the detail drawer
5. Use browser_snapshot to find a Quotes tab inside the drawer
6. Click the Quotes tab
7. Take a screenshot after the tab loads
8. VERIFY: The Quotes tab content loads (may be a list of quotes or an empty state)
9. Check browser_console_messages for any JS errors

### Test 2: Quote list displays correctly
1. With the Quotes tab open, examine the displayed data
2. VERIFY: If quotes exist, rows show customer name, total amount, and status
3. VERIFY: Status values are readable labels (e.g., "Draft", "Sent", "Accepted", "Expired")
4. VERIFY: Total amounts are formatted as currency (e.g., "$1,234.56"), not raw numbers or NaN
5. Take a screenshot of the quotes list

### Test 3: Quote detail view
1. If quote rows are present, click on the first quote row
2. Take a screenshot after clicking
3. Use browser_snapshot to read the detail content
4. VERIFY: Quote detail shows line items with part numbers, quantities, and unit prices
5. VERIFY: All currency values are properly formatted (no NaN, no scientific notation, no overflow)
6. VERIFY: Quantities are displayed as whole numbers
7. Check browser_console_messages for any JS errors
8. Check browser_network_requests for any failed API calls

### Test 4: Currency formatting validation
1. In any quote view (list or detail), examine all monetary values
2. VERIFY: No values display as "NaN", "$NaN", "undefined", or "$undefined"
3. VERIFY: Large values are formatted with commas (e.g., "$1,234,567.89" not "1234567.89")
4. VERIFY: No values overflow their container or get truncated
5. VERIFY: No negative values display incorrectly (e.g., "-$100" or "($100)" are both acceptable)

### Test 5: Quote line item calculations
1. In any quote detail view, look at line items
2. VERIFY: unit_price * quantity = line total for each row
3. VERIFY: Sum of line totals = quote subtotal
4. VERIFY: Margin percentage is between 0% and 100% (not negative or >100%)
5. Take a screenshot of any inconsistent calculations

### Test 6: Quote status transitions
1. Look at quotes in different statuses (Draft, Sent, Accepted, Expired)
2. VERIFY: Status badges use distinct colors or styles
3. VERIFY: Status labels are human-readable
4. If status change buttons exist (Send, Accept), verify they're present but DO NOT click them

### Test 7: Quote navigation chain
1. From the companies list, click a company → open drawer → Quotes tab
2. Click a quote to see its detail
3. VERIFY: The quote shows which requisition it's linked to
4. VERIFY: Line items show part numbers matching the requisition's parts
5. Navigate back to the company list
6. VERIFY: The list is still intact after the deep navigation

### Test 8: Multiple company quotes
1. Check quotes across 2-3 different companies
2. VERIFY: Each company's quotes are distinct (not showing the same data)
3. VERIFY: Quote numbers follow a consistent format (e.g., Q-YYYY-NNNN)
4. VERIFY: No duplicate quote numbers across companies

## What Correct Looks Like
- Quotes tab is accessible within the company detail drawer
- Quote rows show customer, total, and status in a readable table
- Currency values use proper formatting with dollar sign and commas
- Quote detail view shows individual line items with pricing
- No NaN, undefined, or overflow in any numeric field
- No console errors during navigation

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Currency values that display as NaN or overflow
- Buttons that don't respond to clicks
- Quotes tab that fails to load or shows blank content
