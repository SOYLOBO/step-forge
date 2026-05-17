"""Hex-honeycomb truss cutter for build123d parts.

The algorithm:
  1. Compute the part's XY bounding box and Z range.
  2. Walk a hex grid covering the XY box. For each candidate cell:
     a. Build an "enlarged" test prism (the hex inflated by safety_margin).
     b. The test prism is exactly as tall as the part's Z extent.
     c. If the test prism is fully inside the part, the actual hex cut
        (smaller in XY, taller in Z so it cuts cleanly through) is safe.
  3. Union all safe hex prisms and subtract from the part.

The containment test guarantees we never cut closer than `safety_margin`
to an existing edge or hole, so the fork bores, central bore, and outer
silhouette are all preserved automatically.
"""
from __future__ import annotations

import math
from typing import Optional

from build123d import RegularPolygon, Pos, extrude, Shape


def _safe_volume(shape) -> float:
    try:
        return float(shape.volume)
    except Exception:
        return 0.0


def trussify(
    part: Shape,
    cell_size: float = 15.0,
    wall_thickness: float = 3.0,
    safety_margin: float = 3.0,
    progress: Optional[callable] = None,
) -> Shape:
    """Cut a hex-honeycomb truss pattern through `part` along the Z axis.

    Args:
        part: A build123d Shape (typically a Part/Solid from extrude(...)).
        cell_size: Hex across-flats in mm. Larger = lighter but weaker.
        wall_thickness: Wall between adjacent hex cells in mm.
        safety_margin: Minimum material to leave around existing edges/holes.
        progress: Optional callable(cells_tested, cells_total) for status.

    Returns:
        A new Shape with the truss pattern removed.
    """
    W = float(cell_size)
    t = float(wall_thickness)
    m = float(safety_margin)
    if W <= 0 or t < 0 or m < 0:
        raise ValueError("cell_size > 0, wall_thickness >= 0, safety_margin >= 0")

    bbox = part.bounding_box()
    xmin, xmax = bbox.min.X, bbox.max.X
    ymin, ymax = bbox.min.Y, bbox.max.Y
    zmin, zmax = bbox.min.Z, bbox.max.Z
    z_height = zmax - zmin

    # Hex geometry (flat-top: across-flats W, circumradius r)
    r_cut = W / math.sqrt(3)
    r_test = (W + 2 * m) / math.sqrt(3)

    # Tiling pitches for flat-top hexes
    pitch_x = (W + t) * math.sqrt(3) / 2
    pitch_y = W + t

    # Template prisms at origin
    cut_template = Pos(0, 0, zmin - 1) * extrude(RegularPolygon(r_cut, 6), z_height + 2)
    test_template = Pos(0, 0, zmin) * extrude(RegularPolygon(r_test, 6), z_height)

    # Pre-count cells for progress reporting
    cols = int((xmax - xmin + W) / pitch_x) + 2
    rows = int((ymax - ymin + W) / pitch_y) + 2
    cells_total = cols * rows

    cuts = []
    cells_tested = 0

    i = 0
    x = xmin
    while x <= xmax + W:
        y_offset = pitch_y / 2 if i % 2 == 1 else 0.0
        y = ymin + y_offset
        while y <= ymax + W:
            cells_tested += 1
            if progress is not None and cells_tested % 10 == 0:
                progress(cells_tested, cells_total)

            test = Pos(x, y, 0) * test_template
            try:
                outside = test - part
                vol_out = _safe_volume(outside)
            except Exception:
                vol_out = float("inf")

            # If the inflated hex is entirely inside the part, it's safe to cut.
            if vol_out < 0.01:
                cuts.append(Pos(x, y, 0) * cut_template)
            y += pitch_y
        x += pitch_x
        i += 1

    if progress is not None:
        progress(cells_total, cells_total)

    if not cuts:
        return part

    cut_union = cuts[0]
    for c in cuts[1:]:
        cut_union = cut_union + c
    return part - cut_union


def analyze(part: Shape, density_g_per_cm3: float = 1.25) -> dict:
    """Return bbox + volume + mass stats for a Shape."""
    bbox = part.bounding_box()
    vol_mm3 = float(part.volume)
    mass_g = vol_mm3 * float(density_g_per_cm3) / 1000.0
    return {
        "bbox_mm": {"x": bbox.size.X, "y": bbox.size.Y, "z": bbox.size.Z},
        "volume_mm3": vol_mm3,
        "volume_cm3": vol_mm3 / 1000.0,
        "mass_g": mass_g,
        "density_g_per_cm3": float(density_g_per_cm3),
    }
