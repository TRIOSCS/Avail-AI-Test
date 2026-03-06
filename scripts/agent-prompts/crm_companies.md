# Test Area: CRM Companies

Navigate to: {{BASE_URL}}/#customers

## Workflow Tests

### Test 1: Companies list loads
1. Navigate to {{BASE_URL}}/#customers
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
