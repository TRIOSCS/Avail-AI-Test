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
    "tested_area": "tagging",
    "title": "SHORT TITLE",
    "description": "DETAILED DESCRIPTION WITH STEPS",
    "current_page": "URL WHERE ISSUE OCCURRED",
    "console_errors": "ANY JS ERRORS FROM CONSOLE",
    "current_view": "tagging"
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
If all tests pass with no issues, just output: PASS: tagging

## Rules
- Do NOT click delete/remove/logout/destroy/purge buttons
- Take a screenshot BEFORE and AFTER each major action
- If a page doesn't load within 15 seconds, file a ticket and move on
- Check console for errors after every navigation and click
- Be thorough but finish within 3 minutes

---

# Test Area: Tagging

Navigate to: https://app.availai.net/#vendors

## Workflow Tests

### Test 1: Navigate to tagging/material view
1. Navigate to https://app.availai.net/#vendors
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
