# Phase 3 Activity & Tasks — Human Testing Checklist

Use this checklist after the Phase 3 activity/task simplification changes. Test in a browser while logged in as a user who can see requisitions and the task board.

---

## 1. Requisition task board (main flow)

### What to click

1. Go to the requisition list (RFQs / Reqs view).
2. Click a requisition row to open the drill-down panel.
3. Click the **Tasks** sub-tab (next to Workspace, Quote, Activity, Offers, etc.).
4. Click **Add task** (or the control that opens the new-task form).
5. Fill in: **Title**, **Assign to** (a user), **Due date** (at least 24 hours from now). Save.
6. Drag the new task card from **To Do** into **In Progress**, then into **Done** (or use the column headers as drop targets).
7. Refresh the page and open the same requisition again; go back to the Tasks tab.
8. Click **Edit** on a task, change the title, save.
9. Click **Delete** on a task and confirm in the dialog.

### What should happen

- The Tasks tab loads and shows three columns: To Do, In Progress, Done.
- The new task appears in To Do after you save.
- When you drag the task to In Progress, it moves there and stays there (no error message).
- When you drag it to Done, it moves to the Done column.
- After refresh, the task is still in Done (or wherever you left it).
- Edit saves the new title and the card updates.
- Delete removes the task and the card disappears; after refresh the task is still gone.

### What bad result would mean

- **404 or “Failed to move task”** when dragging → backend status-update route is missing or wrong.
- **Task doesn’t move** when you drop it → drag-and-drop or API call is broken.
- **Task reappears in To Do after refresh** → status wasn’t saved.
- **“Due date must be at least 24 hours from now”** when you pick a date sooner → validation is working (expected); pick a later date.
- **Edit or Delete does nothing or errors** → check browser console and network tab for failed requests.

### Edge cases to try

- Create a task with **due date in the past** (if the UI allows). It should either be rejected or show as overdue (e.g. red or “Overdue”).
- Create a task **without** assignee or due date. The app may block save; if it doesn’t, confirm the task still appears and can be moved.
- **Two tabs:** Open the same requisition’s Tasks tab in two browser tabs. Move a task in one tab, refresh the other. The other tab should show the updated state after refresh.
- **Empty board:** Requisition with no tasks. You should see an empty board with clear “no tasks” or “add a task” message, not a spinner forever or a broken layout.

---

## 2. Requisition Activity tab (timeline)

### What to click

1. With a requisition open in the drill-down, click the **Activity** sub-tab.
2. If there is activity, use the filter pills (All, Email, Phone, Notes) and switch between them.
3. If there is no activity, check that you see an empty state and a **Check for Replies** (or similar) control.

### What should happen

- The Activity tab loads and shows a timeline of events (emails sent/received, calls, notes) for this requisition, or a clear “No activity yet” message.
- Filter pills narrow the list (e.g. only email, or only notes). Switching back to “All” shows everything again.
- No JavaScript errors in the console; the panel doesn’t stay on “Loading…” forever.

### What bad result would mean

- **Blank panel or endless “Loading…”** → API or render failed; check network for `/api/requisitions/{id}/activity`.
- **Activity shows in wrong requisition** → wrong req ID or cache; try another req.
- **Filters do nothing or break the list** → filter logic or re-render is broken.

### Edge cases to try

- **Requisition with no activity.** You should see a clear empty state, not a broken or blank area.
- **Requisition with lots of activity.** Scroll and use filters; list should stay usable and not freeze.

---

## 3. Part-level (per part) Offers and Activity

### What to click

1. With a requisition open, go to the **Workspace** (or parts) sub-tab so you see the list of parts/requirements.
2. Expand a part row (click the row or the expand control) so the detail panel opens below it.
3. In that panel you should see sub-tabs such as **Offers** and **Activity**. Click **Offers**, then **Activity**.

### What should happen

- **Offers:** List of offers for that part (or “No offers yet for this part”).
- **Activity:** Combined notes/tasks for that part, or a clear empty state like “No activity for this part.”
- Switching between Offers and Activity updates the content; no crash or endless loading.

### What bad result would mean

- **Panel doesn’t expand or stays empty** → expand or load logic broken.
- **Offers tab shows wrong part’s offers** → requirement ID mix-up.
- **Activity tab errors or never loads** → check for `/api/requirements/{id}/notes` or `/api/requirements/{id}/tasks` and console errors.
- **Tabs missing or mislabeled** → UI wiring or labels wrong.

### Edge cases to try

- **Part with no offers and no activity.** Both tabs should show clear empty messages.
- **Several parts:** Expand one part, then another. Each panel should show data for the correct part.

---

## 4. My Tasks sidebar

### What to click

1. Find the **My Tasks** sidebar (often a tab or button on the right that opens a panel).
2. Open it and check the two tabs: **Assigned to Me** and **Waiting On**.
3. In **Assigned to Me**, you should see tasks assigned to you (or “No tasks assigned to you”).
4. Click a task that has a **Complete** (or similar) action; enter a short resolution note and submit.
5. Go to a requisition’s Tasks tab and move a task (e.g. To Do → Done) or add/delete a task. Then look at the My Tasks sidebar again.

### What should happen

- **Assigned to Me** lists your tasks; **Waiting On** lists tasks you created that someone else is doing.
- The badge or count on the sidebar reflects how many tasks you have (e.g. assigned to you).
- When you complete a task from the sidebar, it disappears from the list (or moves to a “done” area) and the count updates.
- When you change a task from the requisition Tasks tab (move status, add, delete), the My Tasks sidebar updates when you next open it or after a refresh (depending on implementation).

### What bad result would mean

- **Sidebar empty when you have tasks** → wrong user ID or API; check `/api/tasks/mine` and `/api/tasks/mine/summary`.
- **Complete does nothing or returns 403** → only the assignee can complete; confirm you’re the assignee, then check network/console.
- **Sidebar count never changes** after you complete or move a task → sidebar not refreshing; may need to refresh the panel or the page.
- **Waiting On shows tasks assigned to you** (or vice versa) → API or filter logic wrong.

### Edge cases to try

- **No tasks.** Both tabs should show clear “No tasks assigned to you” / “No tasks waiting on others” (or similar).
- **Complete as non-assignee.** If you can open a task you didn’t create and try to complete it, you should get a clear error (e.g. “Only the assignee can complete”) and the task should not be marked done.
- **Overdue task.** A task with due date in the past should show as overdue (e.g. red or “Overdue” label) in the sidebar and on the task board.

---

## 5. Quick smoke summary

Use this for a fast pass:

| Step | Action | Expected |
|------|--------|----------|
| 1 | Open a req → Tasks tab | Board with To Do / In Progress / Done loads. |
| 2 | Add a task (title, assignee, due date) | Task appears in To Do. |
| 3 | Drag that task to Done | Task moves to Done; no error. |
| 4 | Refresh page, open same req → Tasks | Task still in Done. |
| 5 | Open Activity tab | Timeline or “No activity yet.” |
| 6 | Expand a part → Offers then Activity | Each tab shows data or empty state. |
| 7 | Open My Tasks sidebar → Assigned to Me | Your tasks or “No tasks” message. |
| 8 | Complete one task from sidebar (with note) | Task completes; list/count updates. |

If any step fails, note which step and what you saw (message, blank screen, console error) and treat that as a bug.

---

## 6. When to stop and report

- Any **404** or **500** in the network tab for task or activity endpoints.
- **Drag-and-drop never moves the task** or shows “Failed to move task.”
- **Tasks or activity for the wrong requisition or part.**
- **Sidebar and task board out of sync** after you change a task (and refresh if the design says “refresh to see updates”).
- **JavaScript errors in the console** that appear when you use Tasks or Activity.

Report with: what you clicked, what you expected, what actually happened, and (if possible) a screenshot or the exact error text.
