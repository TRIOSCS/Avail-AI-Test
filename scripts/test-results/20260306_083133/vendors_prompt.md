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

### Test 6: Vendor drawer tabs
1. Click on a vendor to open its detail drawer or view
2. Look for tabs in the vendor detail (e.g. Overview, Contacts, Scorecard, Parts, Comms)
3. Click each tab one at a time, taking a screenshot after each click
4. VERIFY: Each tab loads its own content (not blank or stuck on a spinner)
5. VERIFY: Tab transitions are smooth with no flicker or full-page reload
6. VERIFY: The active tab is visually highlighted or indicated
7. VERIFY: Switching back to a previously visited tab still shows its content
8. Check browser_console_messages after each tab switch for JS errors
9. Check browser_network_requests for any failed API calls on tab switch

### Test 7: Scorecard data
1. In the vendor detail drawer, click the Scorecard tab (if it exists)
2. Take a screenshot of the scorecard content
3. Use browser_snapshot to read all values in the scorecard
4. VERIFY: Scorecard shows numeric scores, ratings, or metrics
5. VERIFY: No values display as "NaN", "undefined", "null", or "[object Object]"
6. VERIFY: Percentage values (if any) are between 0% and 100%
7. VERIFY: Rating labels are meaningful (e.g. "Excellent", "Good", "Fair", not blank)
8. VERIFY: If a reliability or quality score is shown, it is a reasonable number (not negative, not impossibly large)
9. If no Scorecard tab exists, note this as an observation

### Test 8: Parts inventory
1. In the vendor detail drawer, click the Parts tab (if it exists)
2. Take a screenshot of the parts list
3. Use browser_snapshot to read the parts data
4. VERIFY: Part numbers (MPNs) display as readable alphanumeric strings (e.g. "LM358DR", "STM32F103C8T6")
5. VERIFY: Part numbers are NOT displayed as raw IDs, UUIDs, or numeric database keys
6. VERIFY: If quantities are shown, they are formatted numbers (not NaN or negative)
7. VERIFY: If prices are shown, they use proper currency formatting (e.g. "$1.23")
8. VERIFY: The parts list has at least one entry (if the vendor has sightings)
9. If no Parts tab exists, note this as an observation

### Test 9: Contact info in vendor
1. In the vendor detail drawer, click the Contacts tab (if it exists)
2. Take a screenshot of the contacts list
3. Use browser_snapshot to read all contact fields
4. VERIFY: Contact names display as readable text (first name, last name)
5. VERIFY: Email addresses are displayed as readable emails (not ciphertext or base64)
6. VERIFY: Phone numbers are displayed in a readable format (not raw digit strings or encrypted blobs)
7. VERIFY: Each contact has at least a name and one communication channel (email or phone)
8. VERIFY: No contact rows show "[object Object]", "undefined", or empty where data should exist
9. If no Contacts tab exists, note this as an observation

### Test 10: Tier/rating display
1. In the vendor list or vendor detail, look for a tier indicator, reliability rating, or trust score
2. Take a screenshot showing the tier or rating
3. VERIFY: If a tier is shown (e.g. "Gold", "Silver", "Tier 1"), it is a meaningful label
4. VERIFY: If a numeric rating is shown, it is within a sensible range (e.g. 0-5 stars, 0-100%)
5. VERIFY: The tier/rating is NOT displayed as "null", "undefined", or a raw database value
6. VERIFY: If a color-coded badge is used, the color corresponds to the tier level
7. If no tier or rating is shown, note this as an observation

### Test 11: Sort vendors
1. Look for sort controls on the vendor list (column headers, sort dropdown, or sort buttons)
2. If sort controls exist, click on a sortable column (e.g. vendor name or sighting count)
3. Take a screenshot after sorting
4. VERIFY: The list order changes visibly after clicking sort
5. VERIFY: If sorted by name, vendors are in alphabetical order (A-Z or Z-A)
6. VERIFY: If sorted by sighting count, numbers are in ascending or descending order
7. Click the same sort control again to reverse the order
8. VERIFY: The order reverses correctly
9. Check browser_console_messages for any JS errors during sorting
10. If no sort controls exist, note this as an observation

### Test 12: Large list scroll
1. Navigate to the full vendor list at https://app.availai.net/#vendors
2. Scroll slowly to the bottom of the vendor list
3. Take a screenshot at the bottom of the list
4. VERIFY: All vendor rows render completely (no blank rows, missing text, or placeholder content)
5. VERIFY: No visual glitches like overlapping rows or misaligned columns
6. VERIFY: If a "Load More" or pagination control exists at the bottom, it is functional
7. Scroll back to the top of the list
8. VERIFY: The top of the list still renders correctly after scrolling back
9. Check browser_console_messages for any JS errors during scrolling

## What Correct Looks Like
- Vendor list loads promptly with readable vendor names
- Search filters the list dynamically
- Clicking a vendor opens a detail view with full information
- All drawer tabs (Overview, Contacts, Scorecard, Parts, Comms) load their content
- Scorecard shows numeric scores and meaningful ratings (no NaN)
- Parts tab shows readable MPNs with proper formatting
- Contacts tab shows decrypted names, emails, and phone numbers
- Vendor tier/rating displays as a meaningful label or score
- Sorting reorders the list correctly
- Scrolling through large lists renders all rows without blanks
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
- Drawer tabs that fail to load or show blank content
- Scorecard values showing NaN, undefined, or impossible numbers
- Part numbers displayed as IDs instead of readable MPNs
- Contacts showing ciphertext instead of readable data
- Tier/rating showing null or raw database values
- Sorting that does not change list order or throws errors
- Blank rows or missing data when scrolling through the list
