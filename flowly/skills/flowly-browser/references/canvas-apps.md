# Canvas-Rendered Apps — Keyboard-Driven Operation

Apps that render their main work area into a `<canvas>` element are
invisible to `read_page`'s accessibility tree scan. The toolbar and
menus are normal DOM, but the grid / shapes / cells / nodes inside
the canvas have no refs. You must drive these apps with the
keyboard, using `key` action.

Affected apps:
- **Google Sheets** — see `references/sheets.md` for the deep dive
- **Excel Online** — same patterns as Sheets but Microsoft locale
- **Google Docs** — body is partially canvas (slow path) or HTML
  (fast path) depending on settings; assume canvas
- **Figma** — entire canvas is a literal canvas
- **Miro / Mural** — whiteboard canvases
- **Linear graph view** — issue dependency graphs are canvas
- **Notion's database "Calendar" + "Timeline" views** — canvas

## The cardinal rule

**Always `screenshot` before doing anything in a canvas app.** It's
the only way you'll know what's on screen, what's selected, what
state the app is in. `read_page` alone is insufficient.

## Sheets / Excel keyboard reference

| Key | Effect |
|---|---|
| `Ctrl+Home` | Cursor to A1 |
| `Ctrl+End` | Last filled cell |
| `Arrow keys` | Move between cells |
| `Ctrl+Arrow` | Jump to edge of data block |
| `Shift+Arrow` | Extend selection |
| `Ctrl+Shift+Arrow` | Extend selection to data edge |
| `F2` (or just type) | Edit focused cell |
| `Enter` | Commit + move down |
| `Tab` | Commit + move right |
| `Shift+Tab` | Commit + move left |
| `Esc` | Cancel edit |
| `Ctrl+Z` / `Ctrl+Y` | Undo / Redo |
| `Ctrl+C` / `Ctrl+V` | Copy / Paste |
| `Ctrl+A` | Select all (twice = whole sheet) |
| `Ctrl+G` (Sheets) | Go-To dialog |
| `Ctrl+/` | Keyboard shortcuts overlay |
| `Ctrl+F` | Find (also acts as cell search) |
| `Ctrl+Alt+M` | Insert comment |

## Figma keyboard reference

| Key | Effect |
|---|---|
| `V` | Move tool |
| `T` | Text tool |
| `R` | Rectangle |
| `O` | Ellipse |
| `F` | Frame |
| `Ctrl+/` (or Cmd) | Quick actions search bar — type any command |
| `Ctrl+\\` | Toggle UI |
| `Ctrl+G` | Group selection |
| `Ctrl+Shift+E` | Export selection |

For Figma, the **Quick Actions search bar (Ctrl+/)** is your best
friend. It accepts text commands ("create rectangle", "auto layout",
"export") that bypass menu navigation entirely.

## Pattern: navigate, edit, commit

Every canvas app shares the same edit cycle:

1. `screenshot` to see current state and what's selected
2. Use `key` for navigation (arrows, Tab, etc.)
3. Confirm position with `screenshot`
4. `key("F2")` or just `type` to edit
5. `key("Enter")` or `key("Tab")` to commit
6. `screenshot` to verify

Don't skip the screenshots. Three screenshots per cycle costs you
some tokens but saves you from 10 wasted clicks down the line.

## When canvas-driving fails

If you've spent more than 5 keyboard actions and you're still not
sure what's happening, escalate:

- **For Sheets/Docs/Forms**: switch to Apps Script. See
  `references/sheets.md` Apps Script section.
- **For Figma/Miro**: ask the user to do the operation. These apps
  weren't designed for non-Figma-Plugin automation. The user will
  do it in 5 seconds vs. you flailing for 5 minutes.
- **For Excel Online**: try the Office Scripts equivalent of
  Apps Script (Automate tab → All scripts → New script).
