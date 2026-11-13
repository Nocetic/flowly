---
name: astronomy
description: "Compute practical astronomy — sunrise/sunset/solar-noon for any place and date, Moon phase and illumination, Julian Date, sidereal time, converting between equatorial (RA/Dec) and horizon (altitude/azimuth) coordinates, and the angular separation between two objects. Includes a stdlib ephemeris calculator using low-precision analytic formulae (good to ~1 minute for the Sun, arcminutes for coordinates). Use when the user asks when the sun rises/sets, tonight's moon phase, where/whether an object is up right now, an RA/Dec-to-alt-az conversion, sidereal or Julian time, or how far apart two stars are on the sky."
metadata: {"flowly":{"emoji":"🔭","tags":["science","astronomy","ephemeris","sunrise","sunset","moon-phase","coordinates","observing","sidereal"],"requires":{"bins":["python3"]},"category":"science","related_skills":["physics-solver","symbolic-math","maps","weather","engineering-units"]}}
---

# Astronomy — Where and When Things Are in the Sky

Observing math is mostly bookkeeping in the right frames: convert the clock to
**Julian Date**, spin the Earth to get **sidereal time**, then trade **equatorial**
(RA/Dec — fixed to the stars) for **horizon** (alt/az — fixed to you). Get the
frames and the sign conventions right and the rest is trigonometry. The
`astro.py` helper does it with standard low-precision formulae — precise enough
for planning a session, not for pointing a telescope blind.

## What this skill produces

**Chat-first.** Default: the direct answer — the rise/set times, tonight's phase,
whether an object is above the horizon and how high — with the one convention that
matters called out (timezone, east-positive longitude, RA in hours). Note the
precision when it's relevant (≈1 min for Sun times).

## When to use

- "When does the sun rise/set in \<place\> on \<date\>?" / "How long is the day?"
- "What's the moon phase tonight?" / "How illuminated is the Moon?"
- "Is \<star/planet\> up right now from my location? How high?"
- "Convert RA/Dec to altitude/azimuth." / "What's on the meridian now?"
- "What's the Julian Date / sidereal time?"
- "How far apart are \<object A\> and \<object B\> on the sky?"

## Conventions (state these, they cause most errors)

- **Longitude east-positive**, latitude north-positive (Istanbul = lat 41.01,
  lon +28.98). Western longitudes are negative.
- **RA in hours** (0–24); Dec, Alt, Az in **degrees**.
- Times are **UTC** unless you pass `--tz` (offset in hours); Sun rise/set is then
  reported in that local zone.

## The calculator

`scripts/astro.py` (stdlib — `math` + `datetime`, no install):
```bash
python3 scripts/astro.py sun --lat 41.01 --lon 28.98 --date 2026-07-06 --tz 3
python3 scripts/astro.py moon --date 2026-07-06
python3 scripts/astro.py jd --date 2026-07-06 --time 12:00
python3 scripts/astro.py sidereal --lon 28.98 --date 2026-07-06 --time 21:00
python3 scripts/astro.py altaz --ra 5.919 --dec 7.407 --lat 41.01 --lon 28.98 \
        --date 2026-07-06 --time 21:00                    # Betelgeuse
python3 scripts/astro.py sep --ra1 5.919 --dec1 7.407 --ra2 5.242 --dec2 -8.202
```

## Chat output format

```
**Sun** — 2026-07-06, Istanbul (41.01°N, 28.98°E), UTC+3

Sunrise = 05:35   Solar noon = 13:11   Sunset = 20:47
Day length ≈ 15:12   (times ±~1 min; refraction-standard −0.833° horizon)
```

## Workflow

1. **Pin location + date/time** and the timezone; restate them east-positive with
   RA in hours so the input is unambiguous.
2. **Compute** with `astro.py`; for "is it up?" run `altaz` and read altitude
   (>0° = above horizon; higher = better, lower = more atmosphere/extinction).
3. **Report** the answer with units and the timezone; flag the precision when it
   matters (Sun ≈1 min; low-precision positions drift arcminutes).
4. **Interpret** for the user: meridian = highest/best, negative altitude = not
   observable, thin crescent near the Sun = hard to see.
5. **Route out:** place → coordinates via `maps`; will it be *clear*? → `weather`;
   the physics of orbits/gravity → `physics-solver`; the trig itself →
   `symbolic-math`; unit conversions → `engineering-units`.

## Key pitfalls

- **Longitude sign.** West is negative. A flipped sign throws rise/set by hours.
- **RA units.** RA is in **hours** here (multiply by 15 for degrees); feeding
  degrees as hours puts objects in the wrong place.
- **Timezone vs UTC.** `jd`/`sidereal`/`altaz` take UTC; only `sun` applies `--tz`.
  Convert local clock time to UTC before `altaz`, or the sky will be rotated.
- **Precision overreach.** These are low-precision formulae — great for planning,
  wrong for occultation timing or precise pointing. Say so; use a real ephemeris
  (Skyfield/JPL Horizons) when arcsecond accuracy is needed.
- **Polar cases.** Above the Arctic/Antarctic circles the Sun may never rise or
  set; the tool reports polar night / midnight sun instead of a fake time.
- **Refraction & horizon.** Rise/set uses the standard −0.833° (refraction + solar
  radius); a hill or a sea horizon shifts the real time.

## Quick reference

- JD → sidereal time → (RA/Dec ↔ Alt/Az). Lon east-+, RA in hours, degrees else.
- `astro.py sun|moon|jd|sidereal|altaz|sep`. `sun` takes `--tz`; others are UTC.
- Altitude >0° = up; on the meridian (LST≈RA) = highest. cos(H0) from the sunrise eq.
- ~1-min Sun precision; use Skyfield/Horizons for arcsecond work.
- Place → maps · clear skies? → weather · orbital physics → physics-solver.
