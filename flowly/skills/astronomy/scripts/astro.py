#!/usr/bin/env python3
"""Astronomy — ephemeris and observing math. Stdlib only (math + datetime).

Sun rise/set/transit, Moon phase, Julian Date, sidereal time, equatorial↔horizon
coordinate transforms, and angular separation. Low-precision analytic formulae
(Meeus / the standard sunrise equation): good to ~1 minute for the Sun and a few
arcminutes for coordinates — right for planning and teaching, not spacecraft nav.

Conventions: longitude EAST-positive, latitude north-positive, RA in HOURS
(0–24), Dec/Alt/Az in degrees, times UTC unless a --tz offset (hours) is given.

Usage:
    astro.py sun --lat 41.01 --lon 28.98 --date 2026-07-06 --tz 3
    astro.py moon --date 2026-07-06
    astro.py jd --date 2026-07-06 --time 12:00
    astro.py sidereal --lon 28.98 --date 2026-07-06 --time 21:00
    astro.py altaz --ra 5.919 --dec 7.407 --lat 41.01 --lon 28.98 \
                   --date 2026-07-06 --time 21:00           # Betelgeuse
    astro.py sep --ra1 5.919 --dec1 7.407 --ra2 5.242 --dec2 -8.202
"""
from __future__ import annotations

import argparse
import math
from datetime import datetime

SIN = lambda d: math.sin(math.radians(d))
COS = lambda d: math.cos(math.radians(d))
ATAN2 = lambda y, x: math.degrees(math.atan2(y, x))
ASIN = lambda x: math.degrees(math.asin(max(-1.0, min(1.0, x))))


def julian_date(dt: datetime) -> float:
    """UTC datetime → Julian Date (Gregorian calendar)."""
    y, m = dt.year, dt.month
    d = dt.day + (dt.hour + dt.minute / 60 + dt.second / 3600) / 24
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return math.floor(365.25 * (y + 4716)) + math.floor(30.6001 * (m + 1)) + d + b - 1524.5


def _hm(hours: float) -> str:
    hours %= 24
    h = int(hours)
    mnt = int(round((hours - h) * 60))
    if mnt == 60:
        h, mnt = (h + 1) % 24, 0
    return f"{h:02d}:{mnt:02d}"


def _parse_dt(datestr, timestr="00:00"):
    y, m, d = (int(x) for x in datestr.split("-"))
    parts = [int(x) for x in timestr.split(":")]
    hh, mm = parts[0], (parts[1] if len(parts) > 1 else 0)
    return datetime(y, m, d, hh, mm)


def gmst_deg(jd: float) -> float:
    """Greenwich mean sidereal time in degrees."""
    return (280.46061837 + 360.98564736629 * (jd - 2451545.0)) % 360


def cmd_jd(a):
    dt = _parse_dt(a.date, a.time)
    jd = julian_date(dt)
    print(f"**Julian Date** — {a.date} {a.time} UTC\n")
    print(f"JD = {jd:.5f}")
    print(f"J2000 epoch offset = {jd - 2451545.0:+.5f} days")


def cmd_sidereal(a):
    dt = _parse_dt(a.date, a.time)
    jd = julian_date(dt)
    gmst = gmst_deg(jd)
    lst = (gmst + a.lon) % 360
    print(f"**Sidereal time** — {a.date} {a.time} UTC, lon {a.lon:+.3f}°\n")
    print(f"GMST = {gmst:.3f}° = {_hm(gmst / 15)}")
    print(f"LST  = {lst:.3f}° = {_hm(lst / 15)}  (objects with RA≈LST are on the meridian)")


def _sun_ra_dec(jd):
    """Low-precision Sun apparent RA (deg), Dec (deg) — Meeus ch. 25."""
    n = jd - 2451545.0
    L = (280.460 + 0.9856474 * n) % 360
    g = (357.528 + 0.9856003 * n) % 360
    lam = L + 1.915 * SIN(g) + 0.020 * SIN(2 * g)
    eps = 23.439 - 0.0000004 * n
    ra = ATAN2(COS(eps) * SIN(lam), COS(lam)) % 360
    dec = ASIN(SIN(eps) * SIN(lam))
    return ra, dec, lam


def cmd_sun(a):
    """Sunrise / transit / sunset via the standard sunrise equation."""
    # n must be the whole day count since J2000 noon; derive it from local noon JD
    jd_noon = julian_date(_parse_dt(a.date, "12:00"))
    n = round(jd_noon - 2451545.0 + 0.0008)
    Jstar = n - a.lon / 360.0
    M = (357.5291 + 0.98560028 * Jstar) % 360
    C = 1.9148 * SIN(M) + 0.0200 * SIN(2 * M) + 0.0003 * SIN(3 * M)
    lam = (M + C + 180 + 102.9372) % 360
    Jtransit = 2451545.0 + Jstar + 0.0053 * SIN(M) - 0.0069 * SIN(2 * lam)
    dec = ASIN(SIN(lam) * SIN(23.4397))
    cos_w0 = (SIN(-0.833) - SIN(a.lat) * SIN(dec)) / (COS(a.lat) * COS(dec))

    def _local(jd):
        ut = ((jd + 0.5) % 1.0) * 24
        return _hm(ut + a.tz)

    print(f"**Sun** — {a.date}, lat {a.lat:+.3f}°, lon {a.lon:+.3f}°, UTC{a.tz:+g}\n")
    print(f"Solar declination ≈ {dec:+.2f}°")
    print(f"Solar transit (noon) = {_local(Jtransit)}")
    if cos_w0 > 1:
        print("Polar night — the Sun stays below the horizon all day.")
        return
    if cos_w0 < -1:
        print("Midnight sun — the Sun stays above the horizon all day.")
        return
    w0 = math.degrees(math.acos(cos_w0))
    print(f"Sunrise = {_local(Jtransit - w0 / 360)}")
    print(f"Sunset  = {_local(Jtransit + w0 / 360)}")
    print(f"Day length ≈ {_hm(2 * w0 / 15)}")


PHASES = [
    (0.02, "New Moon 🌑"), (0.24, "Waxing Crescent 🌒"), (0.26, "First Quarter 🌓"),
    (0.49, "Waxing Gibbous 🌔"), (0.51, "Full Moon 🌕"), (0.74, "Waning Gibbous 🌖"),
    (0.76, "Last Quarter 🌗"), (0.98, "Waning Crescent 🌘"), (1.01, "New Moon 🌑"),
]


def cmd_moon(a):
    dt = _parse_dt(a.date, a.time)
    jd = julian_date(dt)
    synodic = 29.530588853
    jd_new = 2451550.1  # reference new moon, 2000-01-06
    age = (jd - jd_new) % synodic
    frac_cycle = age / synodic
    illum = (1 - math.cos(2 * math.pi * frac_cycle)) / 2
    name = next(n for thresh, n in PHASES if frac_cycle <= thresh)
    print(f"**Moon phase** — {a.date}\n")
    print(f"Age = {age:.1f} days into the {synodic:.2f}-day cycle")
    print(f"Illumination ≈ {illum * 100:.0f}%  ·  {name}")
    print(f"({'waxing — rises during the day, sets after sunset' if frac_cycle < 0.5 else 'waning — rises late, visible toward morning'})")


def cmd_altaz(a):
    dt = _parse_dt(a.date, a.time)
    jd = julian_date(dt)
    lst = (gmst_deg(jd) + a.lon) % 360
    ra_deg = a.ra * 15
    H = (lst - ra_deg) % 360  # hour angle, degrees
    alt = ASIN(SIN(a.lat) * SIN(a.dec) + COS(a.lat) * COS(a.dec) * COS(H))
    az = ATAN2(-COS(a.dec) * SIN(H),
               SIN(a.dec) * COS(a.lat) - COS(a.dec) * SIN(a.lat) * COS(H)) % 360
    print(f"**Horizon coordinates** — {a.date} {a.time} UTC\n")
    print(f"RA {a.ra:.4f}h  Dec {a.dec:+.3f}°  from lat {a.lat:+.3f}°, lon {a.lon:+.3f}°")
    print(f"Altitude = {alt:+.2f}°  ({'above' if alt > 0 else 'below'} the horizon)")
    print(f"Azimuth  = {az:.2f}°  (0°=N, 90°=E, 180°=S, 270°=W)")
    if alt > 0:
        print(f"Airmass ≈ {1 / (SIN(alt) + 0.0001):.2f}" if alt > 3 else "Very low — heavy extinction.")


def cmd_sep(a):
    ra1, ra2 = a.ra1 * 15, a.ra2 * 15
    cosd = SIN(a.dec1) * SIN(a.dec2) + COS(a.dec1) * COS(a.dec2) * COS(ra1 - ra2)
    sep = math.degrees(math.acos(max(-1.0, min(1.0, cosd))))
    print("**Angular separation**\n")
    print(f"({a.ra1:.4f}h, {a.dec1:+.3f}°) ↔ ({a.ra2:.4f}h, {a.dec2:+.3f}°)")
    if sep < 1:
        print(f"= {sep * 60:.1f} arcmin ({sep:.4f}°)")
    else:
        print(f"= {sep:.3f}°")


def main():
    ap = argparse.ArgumentParser(description="Astronomy / ephemeris (stdlib)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sun"); p.add_argument("--lat", type=float, required=True); p.add_argument("--lon", type=float, required=True); p.add_argument("--date", required=True); p.add_argument("--tz", type=float, default=0.0); p.set_defaults(fn=cmd_sun)
    p = sub.add_parser("moon"); p.add_argument("--date", required=True); p.add_argument("--time", default="00:00"); p.set_defaults(fn=cmd_moon)
    p = sub.add_parser("jd"); p.add_argument("--date", required=True); p.add_argument("--time", default="00:00"); p.set_defaults(fn=cmd_jd)
    p = sub.add_parser("sidereal"); p.add_argument("--lon", type=float, required=True); p.add_argument("--date", required=True); p.add_argument("--time", default="00:00"); p.set_defaults(fn=cmd_sidereal)
    p = sub.add_parser("altaz"); p.add_argument("--ra", type=float, required=True); p.add_argument("--dec", type=float, required=True); p.add_argument("--lat", type=float, required=True); p.add_argument("--lon", type=float, required=True); p.add_argument("--date", required=True); p.add_argument("--time", default="00:00"); p.set_defaults(fn=cmd_altaz)
    p = sub.add_parser("sep"); [p.add_argument(f"--{x}", type=float, required=True) for x in ("ra1", "dec1", "ra2", "dec2")]; p.set_defaults(fn=cmd_sep)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
