---
name: findmy
description: "Track Apple devices and AirTags via the FindMy.app on macOS using AppleScript + screenshot."
metadata: {"flowly":{"emoji":"📍","platforms":["macos"],"tags":["FindMy","AirTag","location","tracking","macOS","Apple"],"requires":{"bins":["osascript","screencapture"]},"install":[{"id":"brew","kind":"brew","formula":"steipete/tap/peekaboo","tap":"steipete/tap","bins":["peekaboo"],"label":"Install peekaboo (optional UI automation)"}]}}
---

# Find My (Apple)

Track Apple devices and AirTags via FindMy.app on macOS. Apple does not provide
a CLI for FindMy, so this skill drives the app via AppleScript and reads
location data from screenshots (Flowly's model is multimodal — attached
screenshots are read directly).

## Prerequisites

- **macOS** with Find My app and iCloud signed in
- Devices / AirTags already registered in Find My
- **Screen Recording** permission for the terminal running Flowly
  (System Settings → Privacy & Security → Screen Recording)
- **Optional but recommended:** install `peekaboo` for reliable UI automation:
  `brew install steipete/tap/peekaboo`

## When to Use

- User asks "where is my [device / cat / keys / bag]?"
- Tracking AirTag locations
- Checking device locations (iPhone, iPad, Mac, AirPods)
- Monitoring pet or item movement over time (AirTag patrol routes)

## Method 1: AppleScript + screenshot (basic)

### Open FindMy and navigate

```bash
# Open Find My app
osascript -e 'tell application "FindMy" to activate'

# Wait for it to load
sleep 3
```

Then take a screenshot via Flowly's `screenshot` tool (or `screencapture`) and
read the result — Flowly's model can see the captured image directly:

```bash
screencapture -w -o /tmp/findmy.png
```

After capture, the agent reads the PNG to extract device names and locations.

### Switch between tabs

```bash
# Switch to Devices tab
osascript -e '
tell application "System Events"
    tell process "FindMy"
        click button "Devices" of toolbar 1 of window 1
    end tell
end tell'

# Switch to Items tab (AirTags)
osascript -e '
tell application "System Events"
    tell process "FindMy"
        click button "Items" of toolbar 1 of window 1
    end tell
end tell'
```

## Method 2: Peekaboo UI automation (recommended)

If `peekaboo` is installed, use it for more reliable UI interaction:

```bash
# Open Find My
osascript -e 'tell application "FindMy" to activate'
sleep 3

# Capture and annotate the UI
peekaboo see --app "FindMy" --annotate --path /tmp/findmy-ui.png

# Click on a specific device/item by element ID
peekaboo click --on B3 --app "FindMy"

# Capture the detail view
peekaboo image --app "FindMy" --path /tmp/findmy-detail.png
```

Then read the captured images to extract address/coordinates.

## Workflow: track an AirTag location over time

For monitoring an AirTag (e.g., tracking a cat's patrol route), use Flowly's
`cron` tool to schedule periodic captures rather than a busy `while true` loop:

1. Open FindMy and click the AirTag item once (FindMy only refreshes location
   while the item's page is actively displayed).
2. Schedule a cronjob (every 5 minutes) that runs `screencapture -w -o
   /tmp/findmy-$(date +%H%M%S).png`.
3. After collection, read each screenshot and compile a timeline / route.

## Limitations

- FindMy has **no CLI or API** — must use UI automation
- AirTags only update location while their page is actively displayed
- Location accuracy depends on nearby Apple devices in the FindMy network
- Screen Recording permission required for screenshots
- AppleScript UI automation may break across macOS versions

## Rules

1. Keep the FindMy app in the foreground when tracking AirTags (updates stop when minimized).
2. Use Flowly's vision capability to read screenshot content — don't try to parse pixels manually.
3. For ongoing tracking, schedule with the `cron` tool instead of blocking loops.
4. Respect privacy — only track devices / items the user owns or has explicit permission to track.
