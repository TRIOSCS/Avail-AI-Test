# Test Area: Pipeline / Dashboard

Navigate to: {{BASE_URL}}/#dashboard

## Workflow Tests

### Test 1: Navigate to Pipeline Dashboard
1. Use `browser_navigate` to go to `{{BASE_URL}}/#dashboard`
2. Use `browser_snapshot` to capture the page state
3. VERIFY: The pipeline or dashboard view loads and displays content
4. VERIFY: The page has a recognizable heading or title for the dashboard
5. Use `browser_console_messages` to check for JavaScript errors on load
6. Use `browser_network_requests` to check for failed API calls

### Test 2: Verify KPI Cards Display
1. Use `browser_snapshot` to inspect the KPI or metric cards at the top of the dashboard
2. VERIFY: KPI cards are visible showing key metrics (e.g. total requisitions, active sites, pipeline value)
3. VERIFY: Each card shows a numeric value that is a valid number (not "NaN", "undefined", or "null")
4. VERIFY: Card labels are descriptive and readable
5. VERIFY: Cards are properly aligned and not overlapping

### Test 3: Verify Currency Formatting
1. Use `browser_snapshot` to look for currency values on the dashboard
2. VERIFY: Dollar amounts use proper formatting with "$" prefix and comma separators (e.g. "$1,234,567")
3. VERIFY: No currency values display as raw numbers without formatting (e.g. "1234567.89")
4. VERIFY: No currency values show "NaN", "$NaN", "$undefined", or "$null"
5. VERIFY: Large values display correctly (no scientific notation like "1.23e+6")

### Test 4: Verify Pipeline Chart or Table
1. Use `browser_snapshot` to look for pipeline visualization (chart, table, or funnel)
2. VERIFY: If a chart exists, it renders visually (not a blank canvas or broken image)
3. VERIFY: If a table exists, it has headers and data rows
4. VERIFY: Pipeline stages or categories are labeled clearly
5. VERIFY: Data values in the pipeline view are consistent (no negative counts, no impossible values)

### Test 5: Check for JavaScript Errors Across Interactions
1. Use `browser_console_messages` to collect all console messages
2. VERIFY: No uncaught exceptions or JavaScript errors are present
3. VERIFY: No "TypeError", "ReferenceError", or "SyntaxError" messages in the console
4. If there are interactive elements (tabs, filters, date pickers), use `browser_click` on each
5. After each interaction, use `browser_console_messages` to check for new errors
6. Use `browser_snapshot` after interactions to verify the page updates correctly

### Test 6: Verify Dashboard Load Performance
1. Use `browser_network_requests` to review all API calls made during dashboard load
2. VERIFY: No API requests return 4xx or 5xx status codes
3. VERIFY: The dashboard is fully rendered within 5 seconds of navigation
4. VERIFY: No loading spinners are stuck in a perpetual loading state
5. VERIFY: All sections of the dashboard have populated with data (no empty placeholder sections)

### Test 7: Filter by date range
1. Look for date range filters, date pickers, or time period selectors on the dashboard
2. If date filters exist, take a screenshot showing the current filter state
3. Change the date range (e.g. select "Last 7 days", "Last 30 days", or pick a custom range)
4. Take a screenshot after changing the filter
5. VERIFY: The dashboard KPI values update after the date change (numbers should differ)
6. VERIFY: Any charts or tables refresh to reflect the new date range
7. VERIFY: No loading spinners get stuck during the filter change
8. Check browser_console_messages for any JS errors during filtering
9. Check browser_network_requests for any failed API calls
10. If no date filters exist, note this as an observation

### Test 8: Drill down from KPI cards
1. Click on a KPI card or a pipeline stage element (e.g. a stage in a funnel or a metric card)
2. Take a screenshot after clicking
3. VERIFY: Clicking navigates to a detailed view, opens a drawer, or expands a section with more information
4. VERIFY: The detailed view shows a list or breakdown related to the KPI (e.g. clicking "Open Requisitions" shows a list of open requisitions)
5. VERIFY: The detailed data is consistent with the summary number on the KPI card
6. VERIFY: Navigation back to the dashboard is possible (back button, breadcrumb, or close)
7. Navigate back and VERIFY the dashboard is still intact
8. Check browser_console_messages for any JS errors
9. If clicking a KPI card does nothing, note this as an observation

### Test 9: Refresh data
1. Look for a refresh button, reload icon, or "Update" button on the dashboard
2. If found, note the current KPI values, then click the refresh button
3. Take a screenshot after clicking refresh
4. VERIFY: The dashboard reloads its data (loading indicators may briefly appear)
5. VERIFY: After refresh, all KPI cards still show valid data (not NaN or blank)
6. VERIFY: Charts and tables re-render correctly after refresh
7. VERIFY: No stuck loading spinners after refresh completes
8. Check browser_console_messages for any JS errors during refresh
9. If no refresh button exists, note this as an observation

### Test 10: Sidebar navigation round-trip
1. From the dashboard at {{BASE_URL}}/#dashboard, look for the sidebar navigation menu
2. Click on a different section (e.g. "RFQs" or "Vendors" or "Customers")
3. Take a screenshot to verify the new section loads
4. VERIFY: The new section loads its content correctly
5. Click back on the "Dashboard" or "Pipeline" sidebar item to return
6. Take a screenshot of the returned dashboard
7. VERIFY: The dashboard loads completely again with all KPI cards and charts
8. VERIFY: No data is missing or stuck from the previous navigation
9. VERIFY: The URL hash is back to #dashboard
10. Check browser_console_messages for any JS errors across the round-trip

### Test 11: Buy plan section
1. Look for a "Buy Plan" section, tab, or link on the dashboard or in the sidebar
2. If accessible, navigate to it and take a screenshot
3. VERIFY: The buy plan area loads with content (not a blank page)
4. VERIFY: If pending approvals are shown, each has an identifiable requisition or part reference
5. VERIFY: Approval status labels are meaningful (e.g. "Pending", "Approved", "Rejected")
6. VERIFY: Dollar amounts in buy plans use proper currency formatting (e.g. "$1,234.56")
7. VERIFY: No entries show NaN, undefined, or [object Object]
8. Check browser_console_messages for any JS errors
9. If no buy plan section exists, note this as an observation

### Test 12: Proactive offers
1. Look for a "Proactive Offers" section, tab, or link on the dashboard or in the sidebar
2. If accessible, navigate to it and take a screenshot
3. Use browser_snapshot to read the proactive offers content
4. VERIFY: The section loads with data (offers, part numbers, vendor names)
5. VERIFY: Part numbers display as readable MPNs (not database IDs)
6. VERIFY: Vendor names are readable text
7. VERIFY: If prices are shown, they use proper currency formatting
8. VERIFY: If dates are shown, they are in a readable format (not raw timestamps)
9. VERIFY: No entries show NaN, undefined, or [object Object]
10. Check browser_console_messages for any JS errors
11. If no proactive offers section exists, note this as an observation

## What Correct Looks Like
- Dashboard loads within 5 seconds at the #dashboard hash route
- KPI cards display valid numeric values with proper formatting
- Currency values use "$" prefix with comma separators (e.g. "$1,234,567.00")
- Pipeline visualization (chart or table) renders with real data
- All pipeline stages are labeled and show reasonable values
- Date filters update the dashboard when changed
- Clicking KPI cards drills down to relevant detail views
- Refresh button reloads data without breaking the display
- Sidebar navigation works round-trip without losing dashboard state
- Buy plan shows pending approvals with proper formatting
- Proactive offers show readable part numbers, vendors, and prices
- No JavaScript errors in the console
- No failed API requests
- No stuck loading spinners or empty sections

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty tables that should have rows)
- Broken formatting (NaN, undefined, [object Object])
- Buttons that don't respond to clicks
- KPI cards showing NaN, undefined, or malformed numbers
- Currency values without proper formatting
- Charts that fail to render or show blank
- Pipeline stages with negative or impossible values
- Date filters that do not update the dashboard
- KPI drill-down that leads to a blank or error page
- Refresh that breaks the dashboard or gets stuck loading
- Sidebar navigation that causes dashboard data loss on return
- Buy plan entries with missing or malformed data
- Proactive offers showing database IDs instead of MPNs
