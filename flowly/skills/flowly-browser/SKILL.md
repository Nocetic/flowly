---
name: flowly-browser
description: Browser automation playbook — Sheets/Notion/GitHub recipes + canvas-app keyboard shortcuts. ALWAYS read before browser_tab.
metadata: {"flowly":{"always":true,"requires_tools":["browser_tab"],"category":"browser","tags":["browser","extension","automation","sheets","notion","github","figma"]}}
---

# Flowly Browser Playbook

You are operating the user's visible browser through `browser_tab`. The active
provider may be Flowly Desktop's embedded browser or the Flowly Chrome
extension. The user can see what you do in real time. Chrome-provider tabs are
the ones the user added to the Flowly tab group; its cyan page-edge glow shows
when the agent is acting.

## ⚠️ ALWAYS PLAN FIRST — `browser_plan(action="create", ...)`

Before any browser_tab work that involves more than a single click, your
**FIRST tool call must be `browser_plan(action="create", ...)`**. Even
"small" tasks get a plan — the discipline of writing down what each step
should produce keeps you from drifting through 60 tool calls and then
reporting success when the screenshot shows the task isn't done.

The plan tool is ALWAYS available alongside browser_tab. Three reasons it
exists:

1. **External memory.** After 30+ tool calls your attention is dominated
   by the latest screenshot. The plan is auto-injected into every
   browser_tab result tail (`_planContext`) so you always see your
   position without re-reading scrollback.

2. **Evidence requirement.** `update_step(status="done")` REQUIRES an
   `evidence` string describing what you actually observed (screenshot
   contents, DOM, URL change). A separate validator LLM checks the
   evidence against the step's `successCriteria` — if they don't match,
   the call returns `VALIDATOR_REJECTED` with a specific suggested fix
   and the step stays `in_progress` until you provide real evidence.

3. **End-turn guard.** When you try to end your turn with an active plan
   that has unfinished steps, the system injects a hard reminder. You
   must either complete remaining steps, mark them `blocked` with a
   reason, or call `complete(final_evidence=...)` with partial-completion
   evidence and tell the user what input you need.

### How to write a good plan

```
browser_plan(action="create",
  goal="highlight overdue rows red and sort by due date",
  steps=[
    {"id":1, "content":"identify Due Date column",
     "successCriteria":"read_page output shows column G named 'Due Date'"},
    {"id":2, "content":"select range A2:H11 via Ctrl+G",
     "successCriteria":"Name Box shows 'A2:H11' after Enter"},
    {"id":3, "content":"open Format → Conditional formatting",
     "successCriteria":"sidebar 'Conditional format rules' visible on right"},
    {"id":4, "content":"set Custom formula =$G2<TODAY() with red fill",
     "successCriteria":"sidebar shows formula entered + red color swatch selected"},
    {"id":5, "content":"click Done to apply",
     "successCriteria":"sidebar closes; screenshot shows ONLY past-due rows red, others unchanged"},
    {"id":6, "content":"Data → Sort range by Due Date ascending",
     "successCriteria":"row 2 has the earliest Due Date"},
    {"id":7, "content":"VERIFY final state matches user's request",
     "successCriteria":"screenshot: only past-due rows red, sorted by Due Date asc, header row intact"}
  ])
```

**Good `successCriteria`** are specific and observable: "sidebar with X
visible", "rows N-M have red background", "URL contains /confirm".
**Bad** ones can't be verified: "menu opens", "looks right", "works".

**Last step is ALWAYS** "verify final state matches user's request" — a
screenshot + comparison to the original ask. Without it, you'll claim
done when the page state diverged silently.

### Per-step workflow

For each step in your plan:
1. Optionally `update_step(id=N, status="in_progress")` before starting
   (lets the user see what you're working on).
2. Take the actions (click, type, key, screenshot, etc.).
3. **Verify** by screenshotting or reading_page.
4. `update_step(id=N, status="done", evidence="screenshot shows ...")`
   — describe what you actually saw, not what you intended.

If `VALIDATOR_REJECTED`: read the `suggested_fix`, do whatever extra
verification it suggests, then retry with better evidence. After 3
rejections the system accepts anyway with a logged warning so you're
not blocked forever — but if that happens, your plan probably had an
unverifiable successCriteria.

If genuinely blocked: `update_step(id=N, status="blocked", evidence=
"reason — what would unblock")`. Then revise plan or escalate.

When all steps verified: `browser_plan(action="complete",
final_evidence="screenshot shows ROW BY ROW that goal achieved: ...")`.
The final_evidence is the user's lie-detector — describe the actual
screenshot, not what you intended to do.

## ⚠️ CRITICAL FACTS — read these BEFORE acting on Google Sheets

These contradict your training data. Trust them.

1. **Apps Script lives under the EXTENSIONS menu (Uzantılar / Erweiterungen
   / Extensions), NOT under Tools (Araçlar / Werkzeuge / Outils).** Google
   moved it in 2022. If you click Tools / Araçlar looking for "Script
   editor", you'll get translation, name range manager, and other unrelated
   things — you will loop forever. The CORRECT path is:
   **Extensions menu → Apps Script** (sometimes labeled "Apps Script editor").
   In Turkish UI: **Uzantılar → Apps Script**.

2. **Don't reach for Apps Script reflexively.** For "color these specific
   cells/rows red", use the **toolbar Fill color button** (paint bucket
   icon, hover to see "Dolgu rengi"/"Fill color"). Workflow:
   `screenshot` → identify cells → `key("Ctrl+G")` → type the range like
   "A3:H7" → `key("Enter")` → click toolbar Fill color → pick color from
   swatch → done. ~6 actions. Apps Script is 30+ actions WITH an OAuth
   prompt the agent can't click through.
   
   Three-tier hierarchy:
   - **Ad-hoc coloring** ("color THESE rows") → toolbar Fill color
   - **Permanent rule** ("ALWAYS color rows where Priority=High") →
     Format menu (Biçim) → Conditional formatting (Koşullu biçimlendirme)
   - **Apps Script** → only when 1+2 both genuinely failed OR 5+ rules

3. **Sheets cells are CANVAS-rendered. read_page CANNOT see them.** It
   sees the toolbar and menus, returns ~5-10 elements. That's expected
   on Sheets. To know what's in a cell, `screenshot`. To navigate to a
   cell, use keyboard shortcuts (next section), not click-by-ref.

4. **Apps Script: VERIFY EXECUTION before claiming success.** Hitting
   the Run button is not the same as the script working. After
   `Ctrl+S` then Run:
   - Watch the **bottom "Execution log" panel** (it slides up from
     the bottom of the editor). It shows: timestamp, "Execution
     started", then either "Execution completed" (success — usually
     in green/grey) or "Execution failed: <error message>" (in red).
     Syntax errors show as `SyntaxError: <details>` BEFORE the
     "Execution started" line — the script never ran.
     `screenshot` the execution log panel before saying "done".
   - **`screenshot(ref=...)`** the Run/Save button after you click
     it — Apps Script disables them while running, then re-enables
     when finished. If they're still disabled, the script is still
     running.
   - If you closed/navigated away and a "Leave site? Changes you
     made may not be saved" dialog appeared — Flowly auto-accepts
     `beforeunload` so the navigation goes through. The next
     `read_page` will include `recentDialogs` in the result so you
     know what was auto-handled.
   - Then switch back to the spreadsheet tab and `screenshot` /
     `read_page` to confirm the changes actually landed in the sheet.
     Don't trust "Execution completed" alone — verify the visible
     side effect.

## Google Sheets / Excel Online — keyboard shortcuts (memorize these)

Spreadsheets are CANVAS-rendered. read_page sees the toolbar but NOT
cells. You drive the grid with the keyboard, full stop. These work
in any locale (Turkish/German/etc.) — they are platform shortcuts,
not menu items.

| Action | Shortcut |
|---|---|
| Jump to A1 | `key("Ctrl+Home")` |
| Jump to last filled cell | `key("Ctrl+End")` |
| Move between cells | `key("ArrowUp/Down/Left/Right")` |
| Jump to data-block edge | `key("Ctrl+ArrowDown")` etc. |
| Extend selection | `key("Shift+ArrowDown")` |
| Extend to data edge | `key("Ctrl+Shift+ArrowDown")` |
| Select column | `key("Ctrl+Space")` |
| Select row | `key("Shift+Space")` |
| Select all (twice = whole sheet) | `key("Ctrl+A")` |
| Edit focused cell | `key("F2")` (or just `type()`) |
| Commit + move down | `key("Enter")` |
| Commit + move right | `key("Tab")` |
| Cancel edit | `key("Escape")` |
| Go-To dialog (jump to cell ref) | `key("Ctrl+G")` then `type("B5")` + `key("Enter")` |
| Find / Find-and-replace | `key("Ctrl+F")` / `key("Ctrl+H")` |
| Undo / Redo | `key("Ctrl+Z")` / `key("Ctrl+Y")` |
| Copy / Cut / Paste | `key("Ctrl+C")` / `key("Ctrl+X")` / `key("Ctrl+V")` |
| Insert row above | `key("Ctrl+Alt+=")` then choose row |
| Bold / Italic / Underline | `key("Ctrl+B")` / `key("Ctrl+I")` / `key("Ctrl+U")` |
| Fill color (Sheets) | NO universal shortcut — see references/sheets.md for the toolbar/menu workflow |
| Show all keyboard shortcuts | `key("Ctrl+/")` (Mac: `key("Cmd+/")`) |

Mac users: swap `Ctrl` → `Cmd` for most. The agent should detect
platform via `navigator.platform` if needed; default to Ctrl.

For coloring cells, sorting, conditional formatting, or anything
beyond cell editing, see `references/sheets.md` (loaded on demand).

## Read these first, then act

1. **`screenshot` BEFORE diving in to ANY non-trivial task.** Especially
   on apps with localized menus (Turkish/German/etc.) or canvas-rendered
   work areas (Sheets/Figma/Miro). The image tells you what's actually
   on screen — labels, layout, cell contents, what's selected, where
   sidebars are. Skipping this and going straight to `read_page` is
   the #1 reason agents loop forever on Sheets / Notion / Linear.
2. **`read_page` second.** With the visual context from step 1, matching
   ref IDs to UI elements is fast.
3. **After EVERY click that opens a menu/dropdown/modal/sidebar:**
   `wait(timeout_ms=400)` then `read_page` again. Material Design
   popovers animate over ~300ms; reading immediately catches the
   pre-open DOM and you'll think the click did nothing.
4. **Verify mutating actions with a follow-up `read_page` (or screenshot
   on canvas apps)** before claiming success. The extension reports
   DOM-level success for clicks, not the user-visible side effect.
5. **Don't confuse the user with raw output.** `console_log`, `evaluate`,
   `read_network_requests` results are debug-only; summarise instead of
   pasting verbatim.

## Choosing your tools

| Need to… | Use |
|---|---|
| Find an element by name/intent | `read_page` (a11y tree) → use the ref. `find` for fuzzy matches. |
| Click / type / scroll | `click(ref=)`, `type(ref=, text=)`, `scroll(direction=)`. CDP-backed, isTrusted=true. |
| Press a key (Enter, Tab, Escape, Ctrl+a) | `key(key=)`. Handles modifiers + special keys. |
| Fill a form with N known values | `batch(actions=[...])` — one round-trip, ~80ms between steps. |
| Upload a file from disk | `upload_file(selector=, paths=)` — never opens the OS picker. |
| Drop an inline image into a chat composer | `upload_image(selector=, dataUrl=)`. |
| Look at network calls the page made | `read_network_requests` (filter by url_contains/method/status). |
| Check what scripts logged | `console_log`. |
| Run page-context JS (escape hatch) | `evaluate` — guarded, see below. |
| Wait for a UI condition | `wait(selector=, timeout_ms=)` — never a sleep loop. |

## Batch — when to use, when NOT to

USE batch for **deterministic** sequences where the next action does NOT
depend on observing the previous result. Examples:
- Filling a form's known fields: `[type "name", key Tab, type "email", key Tab, ...]`
- Spreadsheet header row: `[type "Task ID", key Tab, type "Task Name", key Tab, ...]`
- Pressing the same key N times: `[key ArrowDown, key ArrowDown, ...]`

DON'T batch when you need to read state between steps. If the next click
depends on what a dropdown shows, do those as individual calls.

`stop_on_error: true` (default) aborts the batch on the first failure;
the response tells you `executed`, `total`, `stoppedAt`.

## Common failure modes — STOP THE LOOP, switch strategy

If you've done the same action 3+ times and the page state isn't
changing, you are not going to succeed by trying harder. Stop and
diagnose. See `references/troubleshooting.md` for the full playbook.
The fastest mental check:

- **`read_page` returned 5-10 elements when you expected 100s?** →
  Canvas-rendered. See `references/canvas-apps.md`.
- **Clicked a menu, next read_page looks the same?** → Popover hadn't
  finished animating. `wait(400)` + read again. If still nothing,
  `screenshot` to confirm what's actually on screen.
- **Can't find "Data" / "Format" / "Tools" labels?** → UI is in user's
  language. `screenshot` to see actual labels. For Sheets see
  `references/sheets.md` Turkish ↔ English table.
- **Element ref is "stale".** DOM was rebuilt. `read_page` to refresh.
- **`URL_DRIFT` error.** Tab navigated since last `read_page`. Re-read.
- **`PERMISSION_DENIED`.** User disabled that action's category for
  this tab. Tell them which toggle to flip — don't retry blindly.
- **`EVALUATE_GUARDED_API`.** Snippet hit the deny-list. Retry with
  `unsafe: true` if benign (audited).
- **Looped 3+ times, not making progress?** STOP. `screenshot`, tell
  the user what you tried and what's blocking. They'll either click
  it themselves or pick a different approach.

## Site-specific recipes

Load on demand:

- `references/sheets.md` — Google Sheets editing, sorting, conditional
  formatting, **Apps Script escape hatch** for anything non-trivial.
- `references/canvas-apps.md` — Sheets, Excel Online, Figma, Miro,
  Linear graph view. Keyboard navigation patterns.
- `references/forms.md` — Form filling patterns including batch.
- `references/troubleshooting.md` — When the agent gets stuck.

Read the relevant reference BEFORE attempting the task. They're short
(~few hundred lines each) and shape your strategy correctly from the
first action — saves you 10+ failed attempts.

## Localisation

Menu labels vary by user UI language. "Data" in English is "Veri" in
Turkish, "Daten" in German, "Datos" in Spanish, "Données" in French.
Read the actual labels from `read_page` instead of searching for
English text. The user's locale is typically discoverable from
`navigator.language` via `evaluate` if you need to confirm.

## Safety floor

- Sensitive sites (banking, government auth, healthcare, password
  managers) are blocked by default. If the user wants you to act on
  one, they have to flip the per-host override in the side panel —
  don't try to talk them into it.
- Per-tab permission flags (read/interact/navigate/evaluate) are real
  user-controlled gates. Don't suggest bypasses; explain which flag
  to flip if a permission denial blocks the task.
- The user can revoke any tab's group membership instantly. If the
  cyan glow disappears mid-task, stop and ask before retrying — the
  user may have stepped away from this task on purpose.
