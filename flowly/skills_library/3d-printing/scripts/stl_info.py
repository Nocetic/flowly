#!/usr/bin/env python3
"""STL analyzer — bounding box, volume, area, triangle/mesh sanity.

Stdlib only. Handles binary and ASCII STL. Prints chat-ready markdown.

Volume via the signed-tetrahedron method (correct for a closed manifold mesh;
for an open/non-manifold mesh it's approximate — flagged).

Usage:
    stl_info.py model.stl
    stl_info.py model.stl --bed 220x220x250
    stl_info.py model.stl --density 1.24 --price 22   # PLA g/cm^3 and $/kg
"""
from __future__ import annotations

import argparse
import struct
import sys


def parse_binary(data):
    # 80-byte header, 4-byte uint32 count, then 50 bytes/triangle
    n = struct.unpack_from("<I", data, 80)[0]
    tris = []
    off = 84
    rec = struct.Struct("<12fH")
    for _ in range(n):
        if off + 50 > len(data):
            break
        vals = rec.unpack_from(data, off)
        # vals[0:3] normal, 3:6 v1, 6:9 v2, 9:12 v3
        tris.append((vals[3:6], vals[6:9], vals[9:12]))
        off += 50
    return tris


def parse_ascii(text):
    tris = []
    verts = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            verts.append((float(parts[1]), float(parts[2]), float(parts[3])))
            if len(verts) == 3:
                tris.append((verts[0], verts[1], verts[2]))
                verts = []
    return tris


def load(path):
    with open(path, "rb") as f:
        data = f.read()
    # Heuristic: ASCII STL starts with "solid" AND contains "facet"
    head = data[:512].lstrip()
    if head[:5].lower() == b"solid" and b"facet" in data[:2048].lower():
        try:
            return parse_ascii(data.decode("utf-8", "replace"))
        except Exception:
            pass
    return parse_binary(data)


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def sub(a, b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])


def dot(a, b):
    return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]


def norm(a):
    return (a[0]*a[0]+a[1]*a[1]+a[2]*a[2]) ** 0.5


def analyze(tris):
    if not tris:
        sys.exit("no triangles parsed — not a valid STL?")
    mn = [float("inf")]*3
    mx = [float("-inf")]*3
    vol6 = 0.0  # 6x signed volume
    area = 0.0
    degenerate = 0
    for v1, v2, v3 in tris:
        for v in (v1, v2, v3):
            for i in range(3):
                mn[i] = min(mn[i], v[i])
                mx[i] = max(mx[i], v[i])
        # signed volume of tetra (origin, v1, v2, v3)
        vol6 += dot(v1, cross(v2, v3))
        cr = cross(sub(v2, v1), sub(v3, v1))
        a = 0.5 * norm(cr)
        area += a
        if a < 1e-9:
            degenerate += 1
    dims = tuple(mx[i]-mn[i] for i in range(3))
    volume = abs(vol6) / 6.0
    return {"n": len(tris), "min": mn, "max": mx, "dims": dims,
            "volume": volume, "area": area, "degenerate": degenerate}


def main():
    ap = argparse.ArgumentParser(description="STL analyzer")
    ap.add_argument("stl")
    ap.add_argument("--bed", help="bed size WxDxH in mm, e.g. 220x220x250")
    ap.add_argument("--density", type=float, default=None, help="material g/cm^3 (PLA~1.24, PETG~1.27, ABS~1.04)")
    ap.add_argument("--price", type=float, default=None, help="filament price per kg")
    a = ap.parse_args()

    tris = load(a.stl)
    r = analyze(tris)
    dx, dy, dz = r["dims"]
    vol_cm3 = r["volume"] / 1000.0  # mm^3 -> cm^3

    print(f"**STL: {a.stl}**\n")
    print(f"Triangles: {r['n']:,}")
    print(f"Bounding box: {dx:.1f} × {dy:.1f} × {dz:.1f} mm")
    print(f"Volume: {vol_cm3:.2f} cm³ · Surface area: {r['area']/100.0:.1f} cm²")

    if a.bed:
        try:
            bw, bd, bh = (float(x) for x in a.bed.lower().split("x"))
            fits = dx <= bw and dy <= bd and dz <= bh
            # also try rotating footprint 90°
            fits_rot = dy <= bw and dx <= bd and dz <= bh
            if fits:
                print(f"Bed {a.bed}: fits ✅")
            elif fits_rot:
                print(f"Bed {a.bed}: fits if rotated 90° in XY ✅")
            else:
                print(f"Bed {a.bed}: does NOT fit ❌ (needs splitting or a bigger printer)")
        except ValueError:
            print(f"(could not parse --bed '{a.bed}', expected WxDxH)")

    if a.density:
        grams = vol_cm3 * a.density
        line = f"Est. mass: {grams:.1f} g {a.density} g/cm³"
        if a.price:
            line += f" ≈ ${grams/1000.0*a.price:.2f}"
        line += "  (solid; multiply by infill fraction for real usage)"
        print(line)

    if r["degenerate"]:
        print(f"\n⚠️ {r['degenerate']} degenerate (zero-area) triangle(s) — mesh may be malformed; "
              f"clean/repair in CAD before slicing.")
    else:
        print("\n✅ No degenerate triangles detected.")
    print("_Note: volume assumes a closed manifold mesh; open meshes give an approximate value._")


if __name__ == "__main__":
    main()
