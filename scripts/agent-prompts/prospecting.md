# Test Area: Prospecting

Navigate to: {{BASE_URL}}/#suggested

## Workflow Tests

### Test 1: Discovery pool loads
1. Navigate to {{BASE_URL}}/#suggested
2. Take a screenshot to capture the initial state
3. Use browser_snapshot to read the accessibility tree
4. VERIFY: The page loads the discovery pool view (card-based layout)
5. VERIFY: Company name cards are visible with readable names
6. VERIFY: The page does not show a blank screen or loading spinner stuck indefinitely
7. Check browser_console_messages for any JS errors
8. Check browser_network_requests for any failed API calls

### Test 2: Discovery cards display data
1. Examine the discovery pool cards visible on the page
2. Take a screenshot of the cards
3. VERIFY: Each card shows a company name
4. VERIFY: Cards show additional info like industry, location, or revenue range where available
5. VERIFY: No cards display "[object Object]", "undefined", or "null" as content
6. VERIFY: Card layout is not broken (no overlapping or truncated text)

### Test 3: Filter controls
1. Look for filter controls on the page (industry, region, revenue dropdowns or inputs)
2. If filter controls exist, take a screenshot showing them
3. Click on an industry filter dropdown if available
4. VERIFY: Filter options load and are selectable
5. Select a filter value and verify the card list updates
6. Check browser_console_messages for any JS errors after filtering
7. If no filters exist, note this but do not file a ticket (filters may not be implemented yet)

### Test 4: Card detail interaction
1. Click on one of the discovery pool cards
2. Take a screenshot after clicking
3. VERIFY: A detail view, drawer, or expanded card opens with more information about the company
4. VERIFY: The detail shows company details (website, industry, revenue, etc.)
5. VERIFY: No console errors from browser_console_messages
6. Check browser_network_requests for any failed API calls

## What Correct Looks Like
- Discovery pool shows a grid or list of company cards
- Each card displays a company name and summary info
- Filter controls (if present) update the displayed cards
- Clicking a card opens a detail view with enriched company data
- No console errors during any interaction
- Page loads in under 5 seconds

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (empty card grid when data should exist)
- Broken formatting (NaN, undefined, [object Object])
- Cards that don't respond to clicks
- Filters that don't update the displayed results
- Layout issues (overlapping cards, broken grid)
