# Test Area: Search

Navigate to: {{BASE_URL}}/#rfqs

## Workflow Tests

### Test 1: Basic part number search
1. Navigate to {{BASE_URL}}/#rfqs
2. Take a screenshot to capture the initial state
3. Look for a search input field in the page snapshot
4. Type "LM358" into the search field using browser_fill_form
5. Submit the search by pressing Enter or clicking the search button
6. Take a screenshot of the results
7. VERIFY: Results appear with columns for vendor, price, and quantity
8. VERIFY: At least one result row is visible
9. Check browser_console_messages for any JS errors

### Test 2: Result detail expansion
1. From the search results for "LM358", click on the first result row
2. Take a screenshot after clicking
3. VERIFY: A detail view or drawer expands showing additional information about the part
4. VERIFY: No console errors from browser_console_messages
5. Check browser_network_requests for any failed API calls (4xx or 5xx)

### Test 3: Empty search result handling
1. Clear the search field
2. Type "ZZZZNOTREAL999" into the search field
3. Submit the search
4. Take a screenshot of the result
5. VERIFY: An empty state message displays (e.g., "No results found") rather than a crash or blank page
6. VERIFY: No console errors from browser_console_messages
7. VERIFY: No network request failures (check browser_network_requests)

### Test 4: Special characters in search
1. Clear the search field
2. Type "LM358-N/NOPB" into the search field (contains dash and slash)
3. Submit the search
4. Take a screenshot
5. VERIFY: The page does not crash or show an error
6. VERIFY: Results display normally (may be empty, but no errors)
7. Check browser_console_messages for any JS errors
8. Check browser_network_requests for any 4xx/5xx responses

### Test 5: Sorting columns
1. Navigate to {{BASE_URL}}/#rfqs
2. Perform a search for "LM358" to populate results
3. Take a screenshot of the initial results order
4. Look for clickable column headers (e.g., Price, Quantity, Vendor)
5. Click the "Price" column header (or whichever sortable column is available)
6. Take a screenshot after clicking
7. VERIFY: The row order has changed compared to the initial screenshot
8. Click the same column header again to reverse sort
9. Take a screenshot after the second click
10. VERIFY: The order reversed again (ascending vs descending toggle)
11. Check browser_console_messages for any JS errors during sorting

### Test 6: Multi-part search without page reload
1. From the search results page, clear the search field
2. Type "LM358" into the search field and submit
3. Take a screenshot and note the number of results
4. WITHOUT navigating away or reloading, clear the search field
5. Type "STM32F103" into the search field and submit
6. Take a screenshot of the new results
7. VERIFY: Results have changed — they now relate to "STM32F103", not "LM358"
8. VERIFY: No stale results from the previous search remain visible
9. Clear the search field again and type "TPS54302" and submit
10. VERIFY: Results update cleanly a third time with no residual data
11. Check browser_console_messages for any JS errors across all three searches
12. Check browser_network_requests — each search should have triggered a new API call

### Test 7: Detail drawer tabs deep dive
1. Search for "LM358" and click on the first result row to open a detail view
2. Take a screenshot of the detail drawer/panel
3. Use browser_snapshot to identify all available tabs (e.g., Parts, Offers, Quotes, Files, Sourcing)
4. Click the "Parts" tab (or first tab)
5. Take a screenshot — VERIFY: parts list is visible with MPN, quantity, and target price columns
6. Click the "Offers" tab
7. Take a screenshot — VERIFY: offers list shows vendor name, price, quantity, and lead time
8. Click the "Quotes" tab
9. Take a screenshot — VERIFY: quotes content loads (may be empty but should not error)
10. Click the "Files" tab if present
11. Take a screenshot — VERIFY: file attachments section loads without errors
12. Click the "Sourcing" tab if present
13. Take a screenshot — VERIFY: sourcing results or connector data loads
14. VERIFY: No tab shows a stuck loading spinner or blank white content area
15. Check browser_console_messages after each tab switch for any JS errors

### Test 8: Offer data integrity
1. With a requisition detail open, click the "Offers" tab
2. Take a screenshot of the offers list
3. Use browser_snapshot to read all offer data in the accessibility tree
4. VERIFY: Prices are formatted as currency (e.g., "$1.23" or "1.23 USD"), not raw floats like "1.2300000001"
5. VERIFY: Quantities are whole numbers or properly formatted integers, not "NaN" or "undefined"
6. VERIFY: Vendor names are human-readable strings, not IDs, UUIDs, or encoded values
7. VERIFY: No fields display "[object Object]", "null", "undefined", or "NaN"
8. VERIFY: If lead times are shown, they display in readable format (e.g., "4-6 weeks", "14 days"), not raw numbers
9. VERIFY: Dates (if any) are formatted as human-readable dates, not ISO 8601 strings or Unix timestamps
10. Check browser_console_messages for any JS errors

### Test 9: XSS security probe
1. Clear the search field
2. Type `<script>alert(1)</script>` into the search field
3. Submit the search
4. Take a screenshot of the result
5. VERIFY: No JavaScript alert dialog appeared
6. VERIFY: The literal text "<script>alert(1)</script>" is either escaped/displayed as text or sanitized away entirely
7. Use browser_snapshot to verify the script tag is NOT present as executable HTML in the DOM
8. Clear the search field and try `"><img src=x onerror=alert(1)>`
9. Submit the search
10. Take a screenshot
11. VERIFY: No alert dialog, no broken HTML layout — the input is safely escaped
12. Check browser_console_messages for any JS errors (security-related errors are acceptable)

### Test 10: API response validation
1. Navigate to {{BASE_URL}}/#rfqs fresh
2. Open browser_network_requests monitoring
3. Perform a search for "LM358"
4. Wait for results to load, then check browser_network_requests
5. VERIFY: All /api/ requests returned HTTP 200 status codes
6. VERIFY: No /api/ requests returned 4xx or 5xx status codes
7. VERIFY: All /api/ response times are under 3 seconds (note any that exceed this)
8. Click on a result row to open the detail drawer
9. Check browser_network_requests again for the detail-loading API calls
10. VERIFY: Detail API calls also return 200 and complete within 3 seconds
11. Switch between tabs in the detail drawer
12. After each tab switch, check browser_network_requests for new API calls
13. VERIFY: All tab-loading API calls return 200 with response times under 3 seconds
14. VERIFY: No CORS errors or blocked requests appear in browser_console_messages

## What Correct Looks Like
- Search input is clearly visible and accepts text
- Results render in a table or list with vendor, price, and quantity columns
- Clicking a result row expands or opens a detail view
- Nonsense searches show an empty state, not a crash
- Special characters in part numbers do not break the search
- No console errors during any search operation
- Column headers are clickable and toggle sort order (ascending/descending)
- Multiple searches in sequence produce correct, non-stale results each time
- All drawer tabs load content without errors or blank states
- Offer data shows properly formatted currency, quantities, and vendor names
- XSS payloads are escaped or sanitized, never executed
- All API calls return 200 with response times under 3 seconds

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Search field not accepting input
- Results not rendering after a valid search
- Stale results remaining after a new search query
- Sort not toggling or rows not reordering when column headers are clicked
- Tabs that show blank content, stuck spinners, or console errors
- Prices displayed as raw floats instead of formatted currency
- Vendor names showing as IDs or UUIDs instead of readable names
- XSS payloads executing (alert dialogs appearing)
- API calls returning 4xx/5xx or taking longer than 3 seconds
- CORS errors or blocked network requests
