# Google Sheets — Operating Playbook

The Sheets grid is rendered into a `<canvas>`. `read_page` will show
the toolbar, menu bar, and sidebars — but NOT individual cells. You
cannot click cells by ref. Drive the grid with the keyboard. Reach
for Apps Script the moment a task needs more than a few cell edits.

## Step 0 — Always do these in order

1. **`screenshot` first.** You're walking into a localized, partly
   canvas UI. The image tells you immediately what locale the menus
   are in, what cells contain, what's selected, where the sidebar is.
   Skipping this and going straight to `read_page` is the #1 reason
   agents get stuck on Sheets.
2. **`read_page` second.** With the visual context from step 1,
   matching ref IDs to UI elements is fast.
3. After ANY click that opens a menu, dropdown, or sidebar:
   `wait(timeout_ms=400)` then `read_page` again. The popover
   animation takes ~300ms; reading too early returns the pre-open
   DOM.

## Localisation — Turkish ↔ English

Menu items use the user's UI language. Most common Turkish ↔ English
mapping:

| English | Türkçe |
|---|---|
| File | **Dosya** |
| Edit | **Düzenle** |
| View | **Görünüm** |
| Insert | **Ekle** |
| Format | **Biçim** |
| Data | **Veri** |
| Tools | **Araçlar** |
| Extensions | **Uzantılar** |
| Help | **Yardım** |
| Sort range | Aralığı sırala |
| Sort sheet by column X | Sayfayı sütuna göre sırala |
| Conditional formatting | Koşullu biçimlendirme |
| Apps Script (under Extensions) | Apps Script (Uzantılar altında — ARTIK Tools altında DEĞİL) |
| Script editor (legacy) | Komut Dosyası Düzenleyici |
| Pivot table | Pivot tablo |
| Filter | Filtre |
| Freeze | Dondur |
| Find and replace | Bul ve değiştir |
| Add a sheet | Sayfa ekle |
| Hide column | Sütunu gizle |
| Insert row above | Üstüne satır ekle |
| Done | Bitti |
| Cancel | İptal |
| Apply | Uygula |
| Save | Kaydet |
| Run | Çalıştır |

Other locales: German "Daten/Format/Werkzeuge", Spanish "Datos/Formato/Herramientas",
French "Données/Format/Outils". Same idea — read the actual labels
from `read_page` and translate them in your head, don't search for
English strings.

**IMPORTANT** — Apps Script lives under **Extensions** (Uzantılar),
not Tools, in modern Google Sheets. Older docs still say "Tools →
Script editor" but that's wrong as of 2022.

## Recipes

### Basic cell editing

```
key("Ctrl+Home")        → cursor to A1
key("ArrowDown")×N      → move to row
key("ArrowRight")×N     → move to column
key("F2")               → start editing focused cell (or just type())
type("hello")           → enter content
key("Enter")            → commit + move down
key("Tab")              → commit + move right
key("Escape")           → cancel edit
key("Ctrl+G")           → Go-To dialog → type("B5") + key("Enter") jumps to B5
```

### Header row in one batch (the Claude pattern)

```python
batch(actions=[
    {"action": "key",  "params": {"key": "Ctrl+Home"}},
    {"action": "type", "params": {"text": "Task ID"}},
    {"action": "key",  "params": {"key": "Tab"}},
    {"action": "type", "params": {"text": "Task Name"}},
    {"action": "key",  "params": {"key": "Tab"}},
    {"action": "type", "params": {"text": "Assignee"}},
    {"action": "key",  "params": {"key": "Tab"}},
    {"action": "type", "params": {"text": "Priority"}},
    {"action": "key",  "params": {"key": "Tab"}},
    {"action": "type", "params": {"text": "Status"}},
    {"action": "key",  "params": {"key": "Enter"}},
])
```

### Sort a column ascending

Two reliable paths:

**A) Right-click on the column letter:**
1. `screenshot` → see column letters at top
2. `read_page` → find the column letter element ref (look for role="columnheader" or text matching "A", "B", "G" etc.)
3. `click(ref=column_letter_ref, button="right")` → context menu opens
4. `wait(timeout_ms=400)` then `read_page`
5. Find item whose label is "Sort sheet by column X, A→Z" (TR: "Sayfayı sütuna göre sırala (A→Z)")
6. `click(ref=that_item)`

**B) Data menu:**
1. Click Data menu (Veri)
2. `wait(400)` + `read_page`
3. Click "Sort range" / "Aralığı sırala"
4. Modal opens — `wait(400)` + `read_page`
5. Configure sort column + direction
6. Click "Sort" / "Sırala"

### Coloring cells — pick the SIMPLEST approach that works

**DON'T jump to Apps Script.** Apps Script is the LAST resort, not
the first. Try cheap approaches first; escalate only if they
genuinely fail.

**Approach 1 — toolbar Fill color (simplest, for ad-hoc highlighting)**

Best when the user asks "make these specific cells / this row red".
You're not creating a rule, you're just painting cells. ~5-8 actions
per range:

1. `screenshot` → see the grid + which cells need it
2. Navigate to the cell or range with arrow keys / `Ctrl+G` / clicking
   the Name Box on the toolbar (the box at top-left that shows current
   cell, e.g. "A1") and typing the range
3. Select the range: `key("Shift+ArrowDown")` to extend, or type the
   range like "A2:H2" into the Name Box
4. Click the toolbar's Fill color button (paint bucket icon, hover to
   confirm "Dolgu rengi"/"Fill color"), pick a color from the swatch
5. `screenshot` to verify

Works for "make High priority rows red" if the user just wants
THESE rows colored right now. Doesn't auto-apply to new rows. Fast.
**Use this first for ad-hoc coloring.**

**Approach 2 — Conditional formatting via Format menu (for rules)**

Use ONLY when the user explicitly wants the formatting to auto-apply
to future data ("any row where Priority='High' should ALWAYS be red,
even when I add new rows"). It's ~15-20 actions per rule:

1. `screenshot` + `read_page`
2. Click Format (Biçim) → "Conditional formatting" (Koşullu biçimlendirme)
3. `wait(400)` + `screenshot` — sidebar opens on the right
4. Click "Apply to range" input, type range (e.g. "A2:H11"), `key("Tab")`
5. Open the "Format cells if..." dropdown → pick "Custom formula is" (Özel formül)
6. Type formula (e.g. `=$D2="High"`)
7. Click the fill-color swatch under "Formatting style", pick red
8. Click "Done" / "Bitti"
9. For more rules: "+ Add another rule" and repeat

The color picker is a DOM preset palette (no canvas issues). The
sidebar is DOM. This DOES work if you screenshot between steps and
don't lose patience.

**Approach 3 — Apps Script (LAST resort, RARELY)**

ONLY when:
- Approaches 1 + 2 BOTH genuinely failed (you tried them, the UI
  didn't cooperate, you have screenshots showing what went wrong), OR
- The task involves 5+ separate rules that would be tedious by UI, OR
- The task needs cross-sheet references / computed ranges / loops

**Don't open Apps Script reflexively for "color these 3 rows".** It
is *more* total actions than just painting the cells (8+ vs 5), AND
requires the user to grant OAuth permissions on first run. The user
is watching; they prefer the simpler path. Apps Script is for the
rare case where UI literally can't express the operation.

### Apps Script — when you really do need it

**Step 1 — Open the script editor**

Modern Sheets puts it under Extensions, not Tools:
1. `screenshot` → confirm the menu bar is what you expect
2. Click "Extensions" / "Uzantılar"
3. `wait(300)` + `read_page`
4. Click "Apps Script"
5. **A new tab opens.** The Flowly extension auto-adopts spawned
   tabs (the openerTabId points at our managed tab) — the new tab
   joins the Flowly group automatically.
6. `tabs_context` to confirm the new tab is in the group, then
   `read_page` against the new tab to see the editor.

**Step 2 — Write the script**

The editor is Monaco-based, real DOM. Find the code area's ref via
`read_page`, click it to focus, and `type()` your function. The
default `myFunction()` placeholder can be selected (Cmd+A) and
replaced.

Example for the user's "highlight high-priority red, sort by due
date" task:

```js
function flowlyTask() {
  const sh = SpreadsheetApp.getActiveSheet();
  const range = sh.getDataRange();
  const lastRow = range.getLastRow();

  // Conditional rule: high priority → red row tint.
  const highRule = SpreadsheetApp.newConditionalFormatRule()
    .whenFormulaSatisfied('=$D2="High"')
    .setBackground('#ffcdd2')
    .setRanges([sh.getRange(2, 1, lastRow - 1, range.getLastColumn())])
    .build();

  // Status column tints (green=Done, blue=In Progress).
  const statusRange = sh.getRange('E2:E' + lastRow);
  const doneRule = SpreadsheetApp.newConditionalFormatRule()
    .whenTextEqualTo('Done').setBackground('#c8e6c9')
    .setRanges([statusRange]).build();
  const wipRule = SpreadsheetApp.newConditionalFormatRule()
    .whenTextEqualTo('In Progress').setBackground('#bbdefb')
    .setRanges([statusRange]).build();

  sh.setConditionalFormatRules([highRule, doneRule, wipRule]);

  // Sort by Due Date (column G = 7).
  sh.getRange(2, 1, lastRow - 1, range.getLastColumn()).sort({column: 7, ascending: true});
}
```

**Step 3 — Save and run**

1. `key("Cmd+S")` (Mac) or `key("Ctrl+S")` (Windows/Linux) to save.
2. The first save shows a "Project name" prompt — `type` something
   like "FlowlyAutomation" + `key("Enter")`.
3. `key("Cmd+Enter")` or click the "Run" button (▶) at the top.
4. **First run shows a permissions consent screen.** This is OAuth —
   the agent CANNOT click through it on the user's behalf. Tell the
   user to grant permissions once. Future runs skip this.
5. After the user grants, the script runs. `screenshot` to confirm
   "Execution finished" appears in the bottom panel.

**Step 4 — Verify the result**

1. Switch back to the spreadsheet tab (`tabs_list` to find its
   tabId, then `tabs_context` shows which is focused).
2. `screenshot` — the sheet should now show your formatting + sort.
3. If it didn't apply, the script may have errored — go back to
   the editor tab, `read_page`, look for the error message in the
   "Execution log" / "Yürütme günlüğü".

## When all else fails

If the user really needs an interactive Sheets workflow that Apps
Script can't handle, ask them to do it manually. Most non-trivial
spreadsheet operations are faster for a human than for any browser
agent — Sheets is genuinely a hard automation target. Be honest
about the limits; don't loop forever.
