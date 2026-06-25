# README assets

Visuals referenced by the top-level `README.md`. All are **final**: the
banner and stack diagram are designed in Figma; the TUI/Desktop/Mobile
demos are real screenshots and screen recordings.

| File | Used for | Replace with | Suggested size |
|---|---|---|---|
| `banner.png` | Top hero banner — **final** (designed in Figma, serif title + antique current-chart art) | — | 1300×300 |
| `diagram.png` | Channels → core → provider stack — **final** (Figma, brand turquoise) | — | 1300×440 |
| `architecture.png` | Detailed architecture (clients → sandboxed gateway → provider) — **final** (Figma) | — | 1300×760 |
| `demo-tui.png` | Terminal TUI demo — **real screenshot** (token + emails redacted) | a GIF if you want motion | 1139×814 |
| `demo-desktop.gif` | Desktop app demo — **real recording** (email already masked) | — | ~720×520 |
| `demo-mobile.gif` | iOS + Android apps — **real recording** (light + dark) | — | 920×688, 2.8 MB |

## Swapping a placeholder for a GIF/MP4

1. Drop the real file in `assets/`, e.g. `assets/demo-desktop.gif`.
2. Update the matching `<img src="assets/demo-desktop.svg" …>` in `README.md`
   to point at the new file (`.gif`/`.png`). For motion, an MP4 referenced as a
   plain link also auto-embeds a player on GitHub.
3. Keep widths set in the README so the layout stays stable.

Recording tips: keep clips ≤15 s and loopable; trim to the single action the
caption describes (one task end-to-end reads better than a feature tour).
