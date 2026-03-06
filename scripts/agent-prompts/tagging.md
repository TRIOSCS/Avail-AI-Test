# Test Area: Tagging

Navigate to: {{BASE_URL}}/#vendors

## Workflow Tests

### Test 1: Navigate to tagging/material view
1. Navigate to {{BASE_URL}}/#vendors
2. Take a screenshot to capture the initial state
3. Use browser_snapshot to read the accessibility tree
4. Look for any tag statistics section, AI tagging panel, or material classification area
5. VERIFY: The page loads without errors
6. Check browser_console_messages for any JS errors
7. Check browser_network_requests for any failed API calls

### Test 2: Tag statistics display
1. Look for tag coverage percentage, tag counts, or classification statistics on the page
2. If a tag statistics section exists, take a screenshot of it
3. VERIFY: Coverage percentage displays as a readable number (e.g., "85.3%"), not NaN or undefined
4. VERIFY: Tag counts are numeric values
5. VERIFY: If a confidence distribution is shown, values sum to something reasonable
6. Check browser_console_messages for any JS errors

### Test 3: Tag visibility on materials
1. Look for material cards, parts, or vendor items that display tags
2. If tags are visible on items, examine their rendering
3. VERIFY: Tags show readable text labels (e.g., "Resistor", "Capacitor", "IC")
4. VERIFY: Tags are not showing raw IDs or "[object Object]"
5. VERIFY: Tag badges or chips render properly (not broken layout)
6. Take a screenshot showing tags on items

### Test 4: Console error check across views
1. If the tagging section has sub-views or tabs, click through each one
2. After each click, check browser_console_messages
3. Take a screenshot of each sub-view
4. VERIFY: No JavaScript errors appear in the console during navigation
5. VERIFY: No network requests fail (check browser_network_requests)
6. VERIFY: All views render content without blank screens

## What Correct Looks Like
- Tag statistics section shows coverage percentage and counts as formatted numbers
- Material items display tag labels as readable text chips/badges
- Confidence values (if shown) are between 0 and 1 or 0% and 100%
- No raw IDs, [object Object], or undefined values in tag displays
- No console errors during any interaction

## What to Report
- Console errors on any page load or click
- Network request failures (4xx, 5xx)
- Pages that take >5 seconds to load
- Missing data (tag stats showing 0% when data should exist)
- Broken formatting (NaN, undefined, [object Object])
- Tags displaying as raw data instead of readable labels
- Coverage percentages that are negative or >100%
- Layout issues with tag badges or chips
