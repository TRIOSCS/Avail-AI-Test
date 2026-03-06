# Test Area: Vendors

Navigate to: {{BASE_URL}}/#vendors

## Workflow Tests

### Test 1: Vendor list loads
1. Navigate to {{BASE_URL}}/#vendors
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
