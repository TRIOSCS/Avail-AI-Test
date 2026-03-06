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

## What Correct Looks Like
- Search input is clearly visible and accepts text
- Results render in a table or list with vendor, price, and quantity columns
- Clicking a result row expands or opens a detail view
- Nonsense searches show an empty state, not a crash
- Special characters in part numbers do not break the search
- No console errors during any search operation

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Search field not accepting input
- Results not rendering after a valid search
