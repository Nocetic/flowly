#!/usr/bin/env python3
"""Render a product preview and an explanatory MacBook centre section."""

from __future__ import annotations

import importlib.util
import struct
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.collections import PolyCollection
from matplotlib.patches import Circle, FancyBboxPatch, Polygon


HERE = Path(__file__).resolve().parent
STL = HERE / "flowly-fit-clip.stl"
OUTPUT = HERE / "flowly-fit-clip-preview.png"


def read_binary_stl(path: Path) -> np.ndarray:
    record = np.dtype([
        ("normal", "<f4", (3,)),
        ("vertices", "<f4", (3, 3)),
        ("attribute", "<u2"),
    ])
    with path.open("rb") as stream:
        stream.read(80)
        count = struct.unpack("<I", stream.read(4))[0]
        raw = np.frombuffer(stream.read(count * 50), dtype=record, count=count)
    return raw["vertices"].copy()


def load_logo() -> list[np.ndarray]:
    source = HERE / "generate_flowly_macbook_clamp.py"
    spec = importlib.util.spec_from_file_location("flowly_generator", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.load_logo_polygons(HERE / "flowly-logo.svg", 10.5, 10.0)


def orthographic_project(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Project XYZ points from a front-right elevated product-camera view."""
    view = np.array([0.72, -1.25, 0.62], dtype=float)
    view /= np.linalg.norm(view)
    right = np.cross(np.array([0.0, 0.0, 1.0]), view)
    right /= np.linalg.norm(right)
    up = np.cross(view, right)
    centre = np.array([0.0, 18.0, 9.5])
    relative = points - centre
    projected = np.stack((relative @ right, relative @ up), axis=-1)
    depth = relative @ view
    return projected, depth


def render() -> None:
    triangles = read_binary_stl(STL)
    logo = load_logo()

    background = "#F7F5F0"
    ink = "#123A36"
    mint = np.array([0.25, 0.78, 0.63])
    metal = "#D5D8D9"
    metal_edge = "#A6ADAF"

    figure = plt.figure(figsize=(16, 9), dpi=180, facecolor=background)
    grid = figure.add_gridspec(1, 2, width_ratios=[1.32, 1.0], wspace=0.015)

    product = figure.add_subplot(grid[0], facecolor=background)
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    normals /= np.linalg.norm(normals, axis=1, keepdims=True) + 1e-12
    light = np.array([-0.35, -0.65, 0.68])
    light /= np.linalg.norm(light)
    light_response = np.einsum("ij,j->i", normals.astype(np.float64), light)
    shade = np.clip(0.68 + 0.28 * light_response, 0.48, 1.0)
    colors = np.clip(mint[None, :] * shade[:, None] + (1 - shade[:, None]) * 0.08, 0, 1)
    colors = np.column_stack((colors, np.ones(len(colors))))
    projected, depth = orthographic_project(triangles)
    order = np.argsort(depth.mean(axis=1))
    mesh = PolyCollection(projected[order], facecolors=colors[order], edgecolors="none",
                          antialiased=False, rasterized=True)
    product.add_collection(mesh)

    logo_faces: list[np.ndarray] = []
    for polygon in logo:
        # Place the dark tint just above the actual recessed front face; it is
        # a debossed mark, not a separate protruding badge.
        points = np.column_stack((polygon[:, 0], np.full(len(polygon), -3.97), polygon[:, 1]))
        logo_faces.append(orthographic_project(points)[0])
    product.add_collection(PolyCollection(logo_faces, facecolor=ink, edgecolor="none", alpha=0.96))

    all_projected = projected.reshape(-1, 2)
    minimum = all_projected.min(axis=0)
    maximum = all_projected.max(axis=0)
    centre_2d = (minimum + maximum) / 2
    half_extent = np.max(maximum - minimum) * 0.72
    product.set_xlim(centre_2d[0] - half_extent, centre_2d[0] + half_extent)
    product.set_ylim(centre_2d[1] - half_extent * 0.78, centre_2d[1] + half_extent * 0.78)
    product.set_aspect("equal", adjustable="box")
    product.axis("off")
    product.text(0.055, 0.92, "FLOWLY FIT CLIP", transform=product.transAxes,
                 fontsize=24, fontweight="bold", color=ink)
    product.text(0.058, 0.875, "İnce dikey yüz  •  kısa mandal  •  açık üçgen",
                 transform=product.transAxes, fontsize=11.5, color="#57706C")
    product.text(0.06, 0.09, "TPU 95A / 98A", transform=product.transAxes,
                 fontsize=10.5, color=ink,
                 bbox=dict(boxstyle="round,pad=0.55", facecolor="#E6F4EE", edgecolor="none"))

    section = figure.add_subplot(grid[1], facecolor=background)
    section.set_aspect("equal", adjustable="box")
    section.set_xlim(-9, 56)
    section.set_ylim(-2, 25)
    section.axis("off")

    # Representative MacBook lower-case taper placed inside the short TPU jaw.
    base = Polygon(
        [(0, 1.12), (53, 1.12), (53, 8.5), (23, 6.65), (0, 4.92)],
        closed=True, facecolor=metal, edgecolor=metal_edge, linewidth=1.2, zorder=2,
    )
    section.add_patch(base)

    lid_bottom = lambda depth: 18.38 - 0.028 * depth
    lid = Polygon(
        [(-0.5, lid_bottom(-0.5)), (53, lid_bottom(53)),
         (53, lid_bottom(53) + 1.65), (-0.5, lid_bottom(-0.5) + 1.65)],
        closed=True, facecolor="#BEC4C5", edgecolor="#929A9C", linewidth=1.2, zorder=2,
    )
    section.add_patch(lid)

    # Side profile: one of the two diagonal rails is visible.
    section.add_patch(FancyBboxPatch(
        (-4.0, 0), 5.0, 18.0, boxstyle="round,pad=0,rounding_size=2.2",
        facecolor="#40C8A1", edgecolor=ink, linewidth=1.0, zorder=5,
    ))
    section.add_patch(Polygon(
        [(0, 0), (25, 0), (25, 1.0), (0, 1.0)], closed=True,
        facecolor="#40C8A1", edgecolor=ink, linewidth=1.0, zorder=5,
    ))
    section.add_patch(Polygon(
        [(0, 5.0), (23, 6.8), (23, 8.4), (0, 6.6)], closed=True,
        facecolor="#40C8A1", edgecolor=ink, linewidth=1.0, zorder=6,
    ))
    section.add_patch(Polygon(
        [(0, 16.1), (23, 7.7), (23, 9.4), (0, 17.8)], closed=True,
        facecolor="#40C8A1", edgecolor=ink, linewidth=1.0, zorder=6,
    ))
    section.add_patch(Circle((-0.2, 17.85), 0.55, facecolor="#40C8A1",
                             edgecolor=ink, linewidth=1.0, zorder=7))

    # Simple mark on the logo face in section view.
    section.text(-1.5, 9.0, "f", ha="center", va="center", fontsize=16,
                 fontweight="bold", color=ink, zorder=7)

    section.annotate("", xy=(0, 22.0), xytext=(25, 22.0),
                     arrowprops=dict(arrowstyle="<->", color=ink, linewidth=1.1))
    section.text(12.5, 22.3, "25 mm kısa mandal", ha="center", va="bottom",
                 fontsize=10.5, color=ink)
    section.annotate("", xy=(-6.2, 0), xytext=(-6.2, 18.35),
                     arrowprops=dict(arrowstyle="<->", color=ink, linewidth=1.1))
    section.text(-6.7, 9.2, "18,3 mm", ha="right", va="center", rotation=90,
                 fontsize=10.5, color=ink)
    section.annotate("içi boş dik üçgen", xy=(11, 11.1), xytext=(25, 13.0),
                     ha="left", va="center", fontsize=10.5, color=ink,
                     arrowprops=dict(arrowstyle="->", color=ink, linewidth=1.0))
    section.annotate("ekran yalnız burada temas eder", xy=(-0.2, 18.4), xytext=(13, 20.0),
                     ha="left", va="center", fontsize=9.8, color=ink,
                     arrowprops=dict(arrowstyle="->", color=ink, linewidth=1.0))
    section.text(41, 17.6, "MacBook kapağı", fontsize=9.2, color="#697376", rotation=-2)
    section.text(38, 5.0, "alt kasa", fontsize=9.5, color="#697376")
    section.text(-4.0, -1.15, "yalnız alt kasaya mandallanır", fontsize=9.8, color=ink)
    section.text(-4.0, 24.3, "YAN PROFİL  /  KAPALI DURUM", fontsize=12,
                 fontweight="bold", color=ink, va="bottom")

    figure.savefig(OUTPUT, dpi=180, facecolor=background, bbox_inches="tight", pad_inches=0.25)
    plt.close(figure)
    print(f"Wrote: {OUTPUT}")


if __name__ == "__main__":
    render()
