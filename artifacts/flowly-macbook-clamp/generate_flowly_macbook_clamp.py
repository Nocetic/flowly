#!/usr/bin/env python3
"""Generate the dimension-calibrated Flowly Triangle Clip as a watertight STL.

The generator intentionally uses only NumPy so the design remains reproducible
without a desktop CAD installation.  Geometry is built as one implicit solid
(spring clamp + screen saddle + logo relief), then polygonized with marching
tetrahedra.

Coordinate system / units:
    X = width, Y = depth, Z = height, all in millimetres.
    The logo is on the front face (negative Y).
"""

from __future__ import annotations

import argparse
import math
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np


SVG_TOKEN = re.compile(r"[A-Za-z]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")


def _cubic(p0: np.ndarray, p1: np.ndarray, p2: np.ndarray, p3: np.ndarray, scale: float) -> list[np.ndarray]:
    control_length = (
        np.linalg.norm(p1 - p0)
        + np.linalg.norm(p2 - p1)
        + np.linalg.norm(p3 - p2)
    )
    subdivisions = int(np.clip(math.ceil(control_length * scale / 0.30), 5, 30))
    points: list[np.ndarray] = []
    for index in range(1, subdivisions + 1):
        t = index / subdivisions
        q = (
            ((1.0 - t) ** 3) * p0
            + 3.0 * ((1.0 - t) ** 2) * t * p1
            + 3.0 * (1.0 - t) * (t**2) * p2
            + (t**3) * p3
        )
        points.append(q)
    return points


def parse_svg_path(path_data: str, scale_hint: float) -> np.ndarray:
    """Flatten one absolute SVG path containing M/L/H/V/C/Z commands."""
    tokens = SVG_TOKEN.findall(path_data)
    cursor = np.array([0.0, 0.0], dtype=float)
    start = cursor.copy()
    points: list[np.ndarray] = []
    command: str | None = None
    index = 0

    arity = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6}
    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command in "Zz":
                if points and np.linalg.norm(points[-1] - start) > 1e-9:
                    points.append(start.copy())
                cursor = start.copy()
                continue
        if command is None:
            raise ValueError("SVG path begins without a command")
        upper = command.upper()
        if upper not in arity:
            raise ValueError(f"Unsupported SVG path command: {command}")
        count = arity[upper]
        if index + count > len(tokens):
            raise ValueError(f"Incomplete SVG path command: {command}")
        values = [float(value) for value in tokens[index : index + count]]
        index += count
        relative = command.islower()

        if upper == "M":
            target = np.array(values, dtype=float)
            if relative:
                target += cursor
            cursor = target
            start = target.copy()
            points.append(target.copy())
            command = "l" if relative else "L"
        elif upper == "L":
            target = np.array(values, dtype=float)
            if relative:
                target += cursor
            cursor = target
            points.append(target.copy())
        elif upper == "H":
            target_x = values[0] + (cursor[0] if relative else 0.0)
            cursor = np.array([target_x, cursor[1]])
            points.append(cursor.copy())
        elif upper == "V":
            target_y = values[0] + (cursor[1] if relative else 0.0)
            cursor = np.array([cursor[0], target_y])
            points.append(cursor.copy())
        elif upper == "C":
            p1 = np.array(values[0:2], dtype=float)
            p2 = np.array(values[2:4], dtype=float)
            p3 = np.array(values[4:6], dtype=float)
            if relative:
                p1 += cursor
                p2 += cursor
                p3 += cursor
            points.extend(_cubic(cursor, p1, p2, p3, scale_hint))
            cursor = p3

    polygon = np.asarray(points, dtype=float)
    if len(polygon) > 1 and np.linalg.norm(polygon[0] - polygon[-1]) < 1e-9:
        polygon = polygon[:-1]
    if len(polygon) < 3:
        raise ValueError("SVG path did not produce a polygon")
    return polygon


def load_logo_polygons(svg_path: Path, target_height: float, center_z: float) -> list[np.ndarray]:
    root = ET.parse(svg_path).getroot()
    view_box = root.attrib.get("viewBox", "0 0 162 162").split()
    min_x, min_y, width, height = map(float, view_box)
    scale = target_height / height

    raw_polygons: list[np.ndarray] = []
    for element in root.iter():
        if element.tag.endswith("path") and element.attrib.get("d"):
            raw_polygons.append(parse_svg_path(element.attrib["d"], scale))
    if not raw_polygons:
        raise ValueError(f"No paths found in {svg_path}")

    # Convert SVG's Y-down coordinates to the model's Z-up coordinates.
    result: list[np.ndarray] = []
    for polygon in raw_polygons:
        x = (polygon[:, 0] - (min_x + width / 2.0)) * scale
        z = ((min_y + height / 2.0) - polygon[:, 1]) * scale + center_z
        result.append(np.column_stack((x, z)))
    return result


def polygon_signed_distance(x: np.ndarray, z: np.ndarray, polygon: np.ndarray) -> np.ndarray:
    """Signed distance to a simple 2D polygon; negative values are inside."""
    distance_squared = np.full(np.broadcast(x, z).shape, np.inf, dtype=float)
    inside = np.zeros(distance_squared.shape, dtype=bool)

    for first, second in zip(polygon, np.roll(polygon, -1, axis=0)):
        ax, az = first
        bx, bz = second
        dx, dz = bx - ax, bz - az
        length_squared = dx * dx + dz * dz
        if length_squared > 1e-14:
            projection = np.clip(((x - ax) * dx + (z - az) * dz) / length_squared, 0.0, 1.0)
            closest_x = ax + projection * dx
            closest_z = az + projection * dz
            candidate = (x - closest_x) ** 2 + (z - closest_z) ** 2
            distance_squared = np.minimum(distance_squared, candidate)

        crosses = (az > z) != (bz > z)
        crossing_x = ax + (z - az) * dx / (dz + np.where(abs(dz) < 1e-15, 1e-15, 0.0))
        inside ^= crosses & (x < crossing_x)

    distance = np.sqrt(distance_squared)
    return np.where(inside, -distance, distance)


def logo_signed_distance(x: np.ndarray, z: np.ndarray, polygons: list[np.ndarray]) -> np.ndarray:
    result = np.full(np.broadcast(x, z).shape, np.inf, dtype=float)
    for polygon in polygons:
        result = np.minimum(result, polygon_signed_distance(x, z, polygon))
    return result


def rounded_box_sdf(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    width: float,
    depth: float,
    height: float,
    radius: float,
) -> np.ndarray:
    center_z = height / 2.0
    qx = np.abs(x) - (width / 2.0 - radius)
    qy = np.abs(y) - (depth / 2.0 - radius)
    qz = np.abs(z - center_z) - (height / 2.0 - radius)
    outside = np.sqrt(np.maximum(qx, 0.0) ** 2 + np.maximum(qy, 0.0) ** 2 + np.maximum(qz, 0.0) ** 2)
    inside = np.minimum(np.maximum(np.maximum(qx, qy), qz), 0.0)
    return outside + inside - radius


def centered_rounded_box_sdf(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    center: tuple[float, float, float],
    size: tuple[float, float, float],
    radius: float,
) -> np.ndarray:
    """Signed distance to a rounded box positioned by its centre."""
    cx, cy, cz = center
    width, depth, height = size
    qx = np.abs(x - cx) - (width / 2.0 - radius)
    qy = np.abs(y - cy) - (depth / 2.0 - radius)
    qz = np.abs(z - cz) - (height / 2.0 - radius)
    outside = np.sqrt(np.maximum(qx, 0.0) ** 2 + np.maximum(qy, 0.0) ** 2 + np.maximum(qz, 0.0) ** 2)
    inside = np.minimum(np.maximum(np.maximum(qx, qy), qz), 0.0)
    return outside + inside - radius


def ellipsoid_sdf(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    center: tuple[float, float, float],
    radii: tuple[float, float, float],
) -> np.ndarray:
    """Approximate ellipsoid SDF; its zero surface is exact."""
    cx, cy, cz = center
    rx, ry, rz = radii
    normalized = np.sqrt(((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 + ((z - cz) / rz) ** 2)
    return (normalized - 1.0) * min(radii)


def rounded_rect_2d_sdf(
    x: np.ndarray,
    y: np.ndarray,
    center: tuple[float, float],
    size: tuple[float, float],
    radius: float,
) -> np.ndarray:
    """Signed distance to a rounded rectangle in the XY plane."""
    cx, cy = center
    width, depth = size
    qx = np.abs(x - cx) - (width / 2.0 - radius)
    qy = np.abs(y - cy) - (depth / 2.0 - radius)
    outside = np.hypot(np.maximum(qx, 0.0), np.maximum(qy, 0.0))
    inside = np.minimum(np.maximum(qx, qy), 0.0)
    return outside + inside - radius


def capsule_sdf(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    start: tuple[float, float, float],
    end: tuple[float, float, float],
    radius: float,
) -> np.ndarray:
    """Signed distance to a capsule between two 3D points."""
    a = np.asarray(start, dtype=float)
    b = np.asarray(end, dtype=float)
    direction = b - a
    length_squared = float(np.dot(direction, direction))
    px = x - a[0]
    py = y - a[1]
    pz = z - a[2]
    projection = np.clip((px * direction[0] + py * direction[1] + pz * direction[2]) / length_squared, 0.0, 1.0)
    dx = px - projection * direction[0]
    dy = py - projection * direction[1]
    dz = pz - projection * direction[2]
    return np.sqrt(dx * dx + dy * dy + dz * dz) - radius


def extruded_logo_sdf(
    x: np.ndarray,
    y: np.ndarray,
    z: np.ndarray,
    polygons: list[np.ndarray],
    front_y: float,
    relief: float,
    overlap: float,
) -> np.ndarray:
    planar = logo_signed_distance(x, z, polygons)
    y_min = front_y - relief
    y_max = front_y + overlap
    center_y = (y_min + y_max) / 2.0
    half_depth = (y_max - y_min) / 2.0
    dy = np.abs(y - center_y) - half_depth
    outside = np.hypot(np.maximum(planar, 0.0), np.maximum(dy, 0.0))
    inside = np.minimum(np.maximum(planar, dy), 0.0)
    return outside + inside


CUBE_CORNERS = np.asarray(
    [
        [0, 0, 0],
        [1, 0, 0],
        [1, 1, 0],
        [0, 1, 0],
        [0, 0, 1],
        [1, 0, 1],
        [1, 1, 1],
        [0, 1, 1],
    ],
    dtype=int,
)

TETRAHEDRA = (
    (0, 1, 2, 6),
    (0, 2, 3, 6),
    (0, 3, 7, 6),
    (0, 7, 4, 6),
    (0, 4, 5, 6),
    (0, 5, 1, 6),
)


def interpolate_iso(a: np.ndarray, b: np.ndarray, fa: float, fb: float) -> np.ndarray:
    denominator = fa - fb
    t = 0.5 if abs(denominator) < 1e-15 else fa / denominator
    return a + np.clip(t, 0.0, 1.0) * (b - a)


def orient_triangle(points: list[np.ndarray], gradient: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a, b, c = points
    if np.dot(np.cross(b - a, c - a), gradient) < 0.0:
        b, c = c, b
    return a, b, c


def tetra_triangles(points: np.ndarray, values: np.ndarray, gradient: np.ndarray) -> list[tuple[np.ndarray, np.ndarray, np.ndarray]]:
    inside = np.flatnonzero(values <= 0.0).tolist()
    outside = np.flatnonzero(values > 0.0).tolist()
    if len(inside) in (0, 4):
        return []

    if len(inside) == 1:
        source = inside[0]
        intersections = [
            interpolate_iso(points[source], points[target], values[source], values[target])
            for target in outside
        ]
        return [orient_triangle(intersections, gradient)]

    if len(inside) == 3:
        source = outside[0]
        intersections = [
            interpolate_iso(points[source], points[target], values[source], values[target])
            for target in inside
        ]
        return [orient_triangle(intersections, gradient)]

    intersections = [
        interpolate_iso(points[i], points[o], values[i], values[o])
        for i in inside
        for o in outside
    ]
    center = np.mean(intersections, axis=0)
    normal = gradient / (np.linalg.norm(gradient) + 1e-15)
    u = intersections[0] - center
    u /= np.linalg.norm(u) + 1e-15
    v = np.cross(normal, u)
    angles = [math.atan2(np.dot(point - center, v), np.dot(point - center, u)) for point in intersections]
    ordered = [intersections[i] for i in np.argsort(angles)]
    return [
        orient_triangle([ordered[0], ordered[1], ordered[2]], gradient),
        orient_triangle([ordered[0], ordered[2], ordered[3]], gradient),
    ]


def polygonize(field: np.ndarray, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray) -> np.ndarray:
    triangles: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
    spacing = np.array([xs[1] - xs[0], ys[1] - ys[0], zs[1] - zs[0]])
    corner_offsets = CUBE_CORNERS * spacing

    corner_fields = [
        field[
            corner[0] : field.shape[0] - 1 + corner[0],
            corner[1] : field.shape[1] - 1 + corner[1],
            corner[2] : field.shape[2] - 1 + corner[2],
        ]
        for corner in CUBE_CORNERS
    ]
    cube_min = np.minimum.reduce(corner_fields)
    cube_max = np.maximum.reduce(corner_fields)
    active_cubes = np.argwhere((cube_min <= 0.0) & (cube_max > 0.0))

    inverse_matrices: list[np.ndarray] = []
    for tetrahedron in TETRAHEDRA:
        tetra_points = corner_offsets[list(tetrahedron)]
        matrix = tetra_points[1:] - tetra_points[0]
        inverse_matrices.append(np.linalg.inv(matrix))

    for i, j, k in active_cubes:
        origin = np.array([xs[i], ys[j], zs[k]])
        cube_points = origin + corner_offsets
        cube_values = np.asarray([field[i + dx, j + dy, k + dz] for dx, dy, dz in CUBE_CORNERS])
        for tetrahedron, inverse in zip(TETRAHEDRA, inverse_matrices):
            ids = list(tetrahedron)
            points = cube_points[ids]
            values = cube_values[ids]
            if np.all(values <= 0.0) or np.all(values > 0.0):
                continue
            gradient = inverse @ (values[1:] - values[0])
            triangles.extend(tetra_triangles(points, values, gradient))

    if not triangles:
        raise RuntimeError("Polygonization produced no triangles")
    return np.asarray(triangles, dtype=np.float32)


def write_binary_stl(path: Path, triangles: np.ndarray) -> None:
    header = b"Flowly Fit Clip V2 - slim vertical face and open truss"
    header = header[:80].ljust(80, b" ")
    with path.open("wb") as stream:
        stream.write(header)
        stream.write(struct.pack("<I", len(triangles)))
        for triangle in triangles:
            normal = np.cross(triangle[1] - triangle[0], triangle[2] - triangle[0])
            length = np.linalg.norm(normal)
            if length > 1e-15:
                normal /= length
            stream.write(struct.pack("<12fH", *(normal.tolist() + triangle.reshape(-1).tolist()), 0))


def clean_triangles(triangles: np.ndarray) -> np.ndarray:
    """Weld coincident vertices and remove zero-area or duplicate facets."""
    flat = triangles.reshape(-1, 3).astype(np.float64)
    quantized = np.round(flat, 6)
    unique_vertices, inverse = np.unique(quantized, axis=0, return_inverse=True)
    faces = inverse.reshape(-1, 3)

    distinct = (
        (faces[:, 0] != faces[:, 1])
        & (faces[:, 1] != faces[:, 2])
        & (faces[:, 2] != faces[:, 0])
    )
    faces = faces[distinct]

    candidate = unique_vertices[faces]
    cross = np.cross(candidate[:, 1] - candidate[:, 0], candidate[:, 2] - candidate[:, 0])
    faces = faces[np.linalg.norm(cross, axis=1) > 1e-9]

    canonical = np.sort(faces, axis=1)
    _, first = np.unique(canonical, axis=0, return_index=True)
    faces = faces[np.sort(first)]
    return unique_vertices[faces].astype(np.float32)


def mesh_report(triangles: np.ndarray) -> dict[str, object]:
    flat = triangles.reshape(-1, 3)
    minimum = flat.min(axis=0)
    maximum = flat.max(axis=0)
    cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    areas = np.linalg.norm(cross, axis=1) / 2.0
    signed_volume = np.einsum("ij,ij->i", triangles[:, 0], np.cross(triangles[:, 1], triangles[:, 2])).sum() / 6.0

    # Quantized edge incidence is a practical watertightness check for STL output.
    quantized = np.round(flat.astype(np.float64), 5)
    _, inverse = np.unique(quantized, axis=0, return_inverse=True)
    indexed = inverse.reshape(-1, 3)
    edges = np.concatenate((indexed[:, [0, 1]], indexed[:, [1, 2]], indexed[:, [2, 0]]), axis=0)
    edges.sort(axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)

    return {
        "triangles": int(len(triangles)),
        "bounds_min_mm": minimum.tolist(),
        "bounds_max_mm": maximum.tolist(),
        "size_mm": (maximum - minimum).tolist(),
        "signed_volume_mm3": float(signed_volume),
        "degenerate_triangles": int(np.count_nonzero(areas < 1e-12)),
        "micro_triangles_below_1e-8_mm2": int(np.count_nonzero(areas < 1e-8)),
        "boundary_edges": int(np.count_nonzero(counts == 1)),
        "nonmanifold_edges": int(np.count_nonzero(counts > 2)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logo", type=Path, default=Path(__file__).with_name("flowly-logo.svg"))
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("flowly-fit-clip.stl"))
    parser.add_argument("--voxel", type=float, default=0.40, help="surface sampling pitch in mm")
    arguments = parser.parse_args()

    width = 10.0
    overall_height = 18.8
    logo_height = 6.8
    logo_relief = 0.20
    logo_overlap = 0.55

    polygons = load_logo_polygons(arguments.logo, logo_height, 9.0)
    margin = 1.8

    def axis(start: float, stop: float) -> np.ndarray:
        count = math.ceil((stop - start) / arguments.voxel) + 1
        return np.linspace(start, stop, count)

    xs = axis(-width / 2.0 - margin, width / 2.0 + margin)
    ys = axis(-4.20 - margin, 25.0 + margin)
    zs = axis(-margin, overall_height + margin)

    x = xs[:, None, None]
    y = ys[None, :, None]
    z = zs[None, None, :]
    # The only exterior element is a slim vertical capsule carrying the logo.
    front_core = centered_rounded_box_sdf(
        x, y, z,
        center=(0.0, -1.5, 9.0),
        size=(width, 5.0, 18.0),
        radius=2.2,
    )
    body = front_core

    # A short jaw grips only the lower-case front taper.  It deliberately does
    # not reach toward the trackpad or resemble a long office-stapler arm.
    lower_jaw = centered_rounded_box_sdf(
        x, y, z,
        center=(0.0, 12.5, 0.5),
        size=(8.5, 25.0, 1.0),
        radius=0.42,
    )
    # Apple does not publish lower-lip thickness separately.  This compliant
    # TPU tongue opens from 4.0 to 5.8 mm across the short insert.
    jaw_slope = 1.8 / 23.0
    upper_jaw = centered_rounded_box_sdf(
        x, y, z - jaw_slope * (y - 11.5),
        center=(0.0, 11.5, 6.7),
        size=(6.0, 23.0, 1.6),
        radius=0.62,
    )
    body = np.minimum(body, np.minimum(lower_jaw, upper_jaw))

    # Two almost invisible internal grip lines provide preload.
    lower_grip = capsule_sdf(x, y, z, (-3.0, 9.5, 1.13), (3.0, 9.5, 1.13), 0.18)
    upper_grip = capsule_sdf(x, y, z, (-2.0, 9.5, 5.72), (2.0, 9.5, 5.72), 0.20)
    body = np.minimum(body, np.minimum(lower_grip, upper_grip))

    # There is no long screen roof.  The lid touches only the soft bead beside
    # the logo.  Two slim diagonal rails run inward and downward to the jaw,
    # producing the requested open right-triangle architecture.
    rail_slope = -8.4 / 23.0
    for rail_x in (-3.1, 3.1):
        rail = centered_rounded_box_sdf(
            x, y, z - rail_slope * (y - 11.5),
            center=(rail_x, 11.5, 12.75),
            size=(1.2, 23.0, 1.7),
            radius=0.48,
        )
        body = np.minimum(body, rail)

    screen_bead = capsule_sdf(
        x, y, z,
        (-2.8, -0.2, 17.85),
        (2.8, -0.2, 17.85),
        0.50,
    )
    body = np.minimum(body, screen_bead)

    # A shallow debossed mark is quieter and more durable than a protruding logo.
    logo_2d = logo_signed_distance(xs[:, None], zs[None, :], polygons)
    logo_front_y = -4.00
    y_min = logo_front_y - logo_relief
    y_max = logo_front_y + logo_overlap
    center_y = (y_min + y_max) / 2.0
    half_depth = (y_max - y_min) / 2.0
    dy = np.abs(y - center_y) - half_depth
    planar = logo_2d[:, None, :]
    logo = np.hypot(np.maximum(planar, 0.0), np.maximum(dy, 0.0)) + np.minimum(np.maximum(planar, dy), 0.0)
    body = np.maximum(body, -logo)

    # Offset the isovalue away from planar grid coincidences.  The resulting
    # 0.055 mm inward shift at the default resolution is far below FDM process
    # tolerance and avoids zero-area marching-tetrahedra facets.
    field = body + arguments.voxel * 0.137

    triangles = polygonize(field, xs, ys, zs)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    write_binary_stl(arguments.output, triangles)
    report = mesh_report(triangles)

    print(f"Wrote: {arguments.output}")
    for key, value in report.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
