# Browser_tab Troubleshooting

When the agent gets stuck in a loop on a real-world task, it's
almost always one of these patterns. Recognise it fast and switch
strategy — don't keep doing the same thing harder.

## "I clicked something and nothing seems to have happened"

Symptoms: clicked a menu/button, the next `read_page` looks the
same as before.

Causes + fixes (in order to try):

1. **The popover hasn't rendered yet.** Material Design and most
   modern UI frameworks animate menus over ~200-400ms. Reading
   immediately catches the pre-open DOM.
   → `wait(timeout_ms=400)` then `read_page` again.

2. **You clicked a different element than you thought.** read_page
   ref IDs can shift between scans on dynamic pages.
   → `screenshot` and visually verify what's open right now.

3. **The site is canvas-rendered.** Sheets, Figma, Miro, Excel
   Online — nothing inside the canvas appears in `read_page`.
   → See `references/canvas-apps.md` for the keyboard workflow.

4. **The element is in a Shadow DOM you can't pierce.** Some
   custom-element-heavy sites (YouTube, some payment widgets)
   put critical UI inside closed shadow roots.
   → Try `evaluate` to query the inner DOM directly, or instruct
     the user to disable the offending widget.

## "I'm in a localized UI and can't find the menu"

Symptoms: clicked something looking for "Data" / "Format" / "Tools"
and got the wrong thing.

Causes + fixes:

1. **The UI is in the user's language**, not English. "Data" might
   be "Veri" (Turkish), "Daten" (German), "Datos" (Spanish), etc.
   → `screenshot` first to see actual labels.
   → For Sheets specifically, the Turkish ↔ English table is in
     `references/sheets.md`.
   → For unknown locales: `evaluate({code: 'navigator.language'})`
     to confirm, then translate menu names yourself before clicking.

## "I'm reading the page but the cells/grid are empty"

Symptoms: `read_page` returns 5-10 elements when you expected 100s.

Cause: **canvas-rendered work area.** The grid/canvas/whiteboard
is drawn into a single `<canvas>` element that has no DOM children
visible to accessibility tree scanners.

Fix:
1. `screenshot` to actually see the data.
2. Switch to keyboard navigation. See `references/canvas-apps.md`.
3. For complex Sheets work, use Apps Script.

## "I'm in a loop — clicking, reading, clicking, reading, nothing changes"

This means your model has no idea what's happening visually. Stop
the loop NOW and:

1. `screenshot` — see what's on screen.
2. Compare to the screenshot from the previous cycle. If they look
   identical, your clicks aren't doing what you think. Try
   `find(query="...")` to locate the right element, then `click`
   that ref directly.
3. If everything looks the same screenshot to screenshot, the page
   may be frozen. `wait(network_idle=true, idle_ms=1500)` and try
   again, or tell the user the page seems stuck.
4. **Don't loop more than 3 times on the same step.** Pause and
   tell the user what you tried and what you'd need to make
   progress (e.g. "I see the menu but can't find the sort option,
   could you click it once so I can see what opens?").

## "Click landed but the page navigated somewhere I didn't expect"

Probably the user has a popup blocker, or the site uses
`window.open` which the Flowly extension auto-adopts into the tab
group.

Fix:
1. `tabs_context` to see all tabs in the group — was a new one added?
2. If yes, the new tab has the content you wanted.
3. If a navigation happened in the same tab, you'll get a
   `URL_DRIFT` error on the next mutating action — re-read_page
   and try again.

## "Permission denied" on a benign-looking action

The user has the action's permission category disabled for this
tab. Don't keep retrying. Tell the user:

- "Please enable the **Read** / **Interact** / **Navigate** /
  **Evaluate** toggle for this tab in the Flowly side panel."

## "EVALUATE_GUARDED_API"

Your `evaluate` snippet hit the deny-list (cookie / storage /
fetch / eval / dynamic Function / Worker / location reassignment).

If the snippet is genuinely benign — e.g. you legitimately need
to read `localStorage` for a debug tool — retry with
`unsafe: true`. The call is logged with the matched pattern names
in the audit trail; the user can review later.

If you're trying to do something the deny-list is right to block
(exfiltrate data, redirect the user, install a worker), don't
use `unsafe`. Tell the user the action is blocked and why.

## When to give up

Real talk: browser agents are not magic. Some tasks are genuinely
faster, more reliable, and safer for the human to do directly. If
you've spent 5+ tool calls on a task and you're not making clear
progress, stop and ask the user. They'd rather click once
themselves than watch you flail for another minute.
