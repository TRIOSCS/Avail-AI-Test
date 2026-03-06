# Test Area: Requisitions

Navigate to: {{BASE_URL}}/#rfqs

## Workflow Tests

### Test 1: Requisition list loads
1. Navigate to {{BASE_URL}}/#rfqs
2. Take a screenshot to capture the initial state
3. Use browser_snapshot to read the accessibility tree
4. VERIFY: A list or table of requisitions is visible with multiple rows
5. VERIFY: Each row shows meaningful data (not empty placeholders)
6. Check browser_console_messages for any JS errors
7. Check browser_network_requests for any failed API calls

### Test 2: Open requisition detail drawer
1. From the requisition list, click on the first requisition row
2. Take a screenshot after clicking
3. Use browser_snapshot to read the drawer content
4. VERIFY: A detail drawer or panel opens on the right side
5. VERIFY: The drawer shows the requisition's parts list
6. VERIFY: A status badge is visible (e.g., Open, In Progress, Quoted)
7. VERIFY: An assignee or owner field is displayed
8. Check browser_console_messages for any JS errors

### Test 3: Drawer tabs work
1. With the requisition detail drawer open, look for tabs (e.g., Parts, Offers, Quotes)
2. Take a screenshot showing the current tab
3. Click on each available tab one by one
4. After clicking each tab, use browser_snapshot to verify content loaded
5. Take a screenshot after switching to each tab
6. VERIFY: Each tab loads its content without errors
7. VERIFY: No blank or stuck-loading states
8. Check browser_console_messages after each tab click

### Test 4: Requisition data integrity
1. With any requisition detail open, examine the displayed data
2. VERIFY: Part numbers display as readable text (not ciphertext or encoded data)
3. VERIFY: Quantities are numeric values (not NaN or undefined)
4. VERIFY: Dates, if shown, are formatted properly (not raw timestamps or "Invalid Date")
5. VERIFY: Currency values, if shown, are formatted (not NaN or overflow numbers)

### Test 5: Status filter
1. Navigate to {{BASE_URL}}/#rfqs
2. Take a screenshot to capture the initial requisition list
3. Use browser_snapshot to look for a status filter dropdown or filter buttons (e.g., "All", "Open", "Quoted", "Closed")
4. If a status filter exists, click on "Open" (or the first filter option)
5. Take a screenshot after filtering
6. VERIFY: The list updated — only requisitions with "Open" status are shown
7. Click on "Quoted" filter
8. Take a screenshot
9. VERIFY: The list now shows only "Quoted" requisitions (different set from "Open")
10. Click on "Closed" filter
11. Take a screenshot
12. VERIFY: The list shows only "Closed" requisitions
13. Click "All" or clear the filter to restore the full list
14. VERIFY: The full list returns with all statuses visible
15. Check browser_console_messages for any JS errors during filter changes

### Test 6: Sorting columns
1. From the requisition list, take a screenshot to note the initial row order
2. Look for clickable column headers (e.g., Age, Customer, Status, Date)
3. Click the "Age" or "Date" column header
4. Take a screenshot after clicking
5. VERIFY: The row order has changed — rows are now sorted by that column
6. Click the same column header again
7. Take a screenshot
8. VERIFY: The sort direction toggled (ascending to descending or vice versa)
9. Click the "Customer" column header
10. Take a screenshot
11. VERIFY: Rows reordered by customer name alphabetically
12. Check browser_console_messages for any JS errors during sorting

### Test 7: Part detail within requisition
1. Open a requisition detail drawer by clicking a row
2. Click the "Parts" tab
3. Take a screenshot of the parts list
4. Use browser_snapshot to read the parts data
5. Click on the first part row (if clickable)
6. Take a screenshot after clicking
7. VERIFY: Part details show the MPN (manufacturer part number) as readable text
8. VERIFY: Quantity is displayed as a numeric value, not "NaN" or "undefined"
9. VERIFY: Target price (if shown) is formatted as currency (e.g., "$1.50"), not a raw float
10. VERIFY: Manufacturer name is displayed (if available) as readable text
11. VERIFY: No fields show "[object Object]", "null", or "undefined"
12. Check browser_console_messages for any JS errors

### Test 8: Offers tab deep dive
1. With a requisition detail drawer open, click the "Offers" tab
2. Take a screenshot of the offers list
3. Use browser_snapshot to read all offer rows in the accessibility tree
4. VERIFY: Each offer row shows a vendor name as readable text (not an ID or UUID)
5. VERIFY: Each offer shows an MPN that matches or relates to the requisition's parts
6. VERIFY: Quantities are formatted as integers (e.g., "1,000" or "1000"), not "NaN"
7. VERIFY: Unit prices are formatted as currency (e.g., "$0.45"), not raw floats like "0.4500000001"
8. VERIFY: If total price is shown, it equals unit price multiplied by quantity (spot-check at least one row)
9. VERIFY: No offer rows show completely blank or missing data in critical columns
10. Check browser_console_messages for any JS errors

### Test 9: Bid due dates formatting
1. From the requisition list, look for a "Bid Due" or "Due Date" column
2. Take a screenshot of the list showing date columns
3. Use browser_snapshot to read the date values
4. VERIFY: Dates display in human-readable format (e.g., "Mar 6, 2026" or "03/06/2026")
5. VERIFY: No dates appear as raw ISO timestamps (e.g., "2026-03-06T00:00:00.000Z")
6. VERIFY: No dates appear as Unix timestamps (e.g., "1741219200")
7. VERIFY: No dates display as "Invalid Date" or "NaN"
8. Open a requisition detail drawer and check dates there as well
9. VERIFY: Detail view dates are also human-readable and consistent with the list format

### Test 10: Pagination and scrolling
1. Navigate to {{BASE_URL}}/#rfqs
2. Take a screenshot and count the number of visible requisition rows
3. Use browser_snapshot to count all rows in the accessibility tree
4. If there are more than 20 rows, look for pagination controls (page numbers, "Next" button, "Load More")
5. If pagination exists, click "Next" or "Load More"
6. Take a screenshot after loading more
7. VERIFY: New rows appeared that were not in the initial set
8. VERIFY: No duplicate rows between pages
9. If there is no pagination, scroll to the bottom of the list
10. VERIFY: All rows are accessible either through pagination or scrolling
11. VERIFY: No "infinite loading" spinner stuck at the bottom
12. Check browser_console_messages for any JS errors during pagination/scrolling

### Test 11: Cross-reference part/offer/quote counts
1. Navigate to {{BASE_URL}}/#rfqs
2. Take a screenshot of the requisition list
3. Use browser_snapshot to find a row that shows counts (e.g., "Parts: 3", "Offers: 5", "Quotes: 2")
4. Note the counts displayed in the list row for the first requisition
5. Click that requisition row to open the detail drawer
6. Click the "Parts" tab and count the number of part rows
7. Take a screenshot — VERIFY: The count matches what was shown in the list row
8. Click the "Offers" tab and count the number of offer rows
9. Take a screenshot — VERIFY: The count matches what was shown in the list row
10. Click the "Quotes" tab and count the number of quote rows
11. Take a screenshot — VERIFY: The count matches what was shown in the list row
12. If any counts do not match, report the discrepancy with exact numbers (list said X, tab shows Y)
13. Check browser_console_messages for any JS errors

## What Correct Looks Like
- Requisition list renders with multiple rows showing summary data
- Clicking a row opens a detail drawer with full requisition info
- Status badges display with proper styling and text
- Tabs switch smoothly and load their content
- All data fields show properly formatted values
- No console errors during navigation
- Status filters narrow the list to only matching requisitions
- Column headers sort rows when clicked, toggling between ascending and descending
- Part details show MPN, quantity, and target price in proper formats
- Offers show vendor names, MPNs, quantities, and formatted currency prices
- All dates are human-readable, never raw ISO or Unix timestamps
- Pagination or scroll loading works to access all requisitions
- Part/Offer/Quote counts in the list row match the actual count of items in each tab

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- Drawer that fails to open or shows blank content
- Tabs that don't switch or load content
- Status filters that don't update the list or show wrong results
- Sort not working or rows not reordering on column header click
- Prices displayed as raw floats instead of formatted currency
- Dates shown as ISO timestamps, Unix timestamps, or "Invalid Date"
- Count mismatches between list row summaries and actual tab contents
- Pagination controls missing when there are many rows, or duplicate rows across pages
- Vendor names showing as IDs or UUIDs instead of readable names
