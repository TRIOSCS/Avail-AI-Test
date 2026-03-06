# Test Area: CRM Quotes

Navigate to: {{BASE_URL}}/#customers

## Workflow Tests

### Test 1: Navigate to quotes via company
1. Navigate to {{BASE_URL}}/#customers
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
