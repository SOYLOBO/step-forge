"""Hex-honeycomb truss cutter for build123d parts.

The algorithm cuts hex tubes through a Shape along a chosen axis (X/Y/Z).
For each candidate cell:
  1. Build an inflated "test prism" along the cut axis at the cell center.
     The test prism is larger than the actual cut by `safety_margin` on every
     side in the plane perpendicular to the cut axis.
  2. Intersect with the part to get the local material in that column.
  3. Skip if the material is missing, broken into pieces (a tube here would
     punch through air gaps), or if the test hex isn't fully solid (the
     inflated hex would clip an existing hole, edge, or boundary).
  4. Cut a hex tube through the actual material extent along the cut axis.

This per-cell local thickness analysis is what lets a single axis-aligned
cut work on cross-shaped or otherwise non-prismatic parts. Cells that
intersect multiple arms get skipped instead of trying to drill through
the gap.
"""
from __future__ import annotations

import math
from typing import Optional

from build123d import RegularPolygon, Pos, Rot, extrude, Shape


def _safe_volume(shape) -> float:
    try:
        return float(shape.volume)
    except Exception:
        return 0.0


def _solids_of(shape):
    """Return list of Solid children, or [shape] if it has volume but no .solids()."""
    if shape is None:
        return []
    try:
        solids = list(shape.solids())
        if solids:
            return solids
    except Exception:
        pass
    if _safe_volume(shape) > 0:
        return [shape]
    return []


def _axis_rotations(cut_axis: str):
    """Return (rot_to_z, rot_back) for aligning cut_axis with +Z, then back.

    rot_to_z is applied to the part so its `cut_axis` direction lines up with
    +Z; rot_back undoes the rotation on the trussified result.
    """
    a = cut_axis.upper()
    if a == "Z":
        return None, None
    if a == "Y":
        # +Y -> +Z via Rot(X=+90). Inverse: Rot(X=-90).
        return Rot(X=90), Rot(X=-90)
    if a == "X":
        # +X -> +Z via Rot(Y=-90). Inverse: Rot(Y=+90).
        return Rot(Y=-90), Rot(Y=90)
    raise ValueError("cut_axis must be 'X', 'Y', or 'Z'")


def trussify(
    part: Shape,
    cell_size: float = 12.0,
    wall_thickness: float = 2.0,
    safety_margin: float = 2.0,
    cut_axis: str = "Z",
    region: Optional[tuple] = None,
    progress: Optional[callable] = None,
) -> Shape:
    """Cut a hex-honeycomb truss pattern through `part` along the given axis.

    Args:
        part: A build123d Shape (typically a Part/Solid).
        cell_size: Hex across-flats in mm.
        wall_thickness: Wall between adjacent hex cells, in mm.
        safety_margin: Minimum material to leave around existing edges/holes.
        cut_axis: "X", "Y", or "Z" — direction the hex tubes run through.
        region: Optional (x_min, y_min, z_min, x_max, y_max, z_max) bbox
            (in the part's coordinate system) that restricts which cells are
            considered. Useful for targeting one arm/zone of a complex part.
        progress: Optional callable(cells_tested, cells_total).

    Returns:
        A new Shape with the truss pattern removed.
    """
    if float(cell_size) <= 0 or float(wall_thickness) < 0 or float(safety_margin) < 0:
        raise ValueError("cell_size > 0, wall_thickness >= 0, safety_margin >= 0")

    rot_to_z, rot_back = _axis_rotations(cut_axis)
    work = (rot_to_z * part) if rot_to_z is not None else part

    # If a region is supplied (in the part's frame), rotate it the same way
    # so it expresses bounds in the working frame. For axis-aligned bboxes
    # under 90° rotations, transforming the 8 corners and taking min/max
    # gives a correct axis-aligned bbox in the rotated frame.
    work_region = None
    if region is not None:
        if len(region) != 6:
            raise ValueError("region must be (x_min, y_min, z_min, x_max, y_max, z_max)")
        work_region = _rotate_bbox(region, rot_to_z)

    cut = _trussify_along_z(
        work,
        cell_size=float(cell_size),
        wall_thickness=float(wall_thickness),
        safety_margin=float(safety_margin),
        region=work_region,
        progress=progress,
    )

    return (rot_back * cut) if rot_back is not None else cut


def _rotate_bbox(bbox6, rot):
    """Transform an axis-aligned bbox (x0,y0,z0,x1,y1,z1) under a 90° rotation."""
    x0, y0, z0, x1, y1, z1 = bbox6
    if rot is None:
        return (x0, y0, z0, x1, y1, z1)
    # Extract the underlying 90° rotation as a wrapped affine and apply to corners.
    from build123d import Vector
    loc = rot.to_location() if hasattr(rot, "to_location") else None
    if loc is None:
        loc = rot.location if hasattr(rot, "location") else None
    if loc is None:
        # Fall back: build a tiny box, rotate it, read back its bbox.
        from build123d import Box
        size = (x1 - x0, y1 - y0, z1 - z0)
        center = ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2)
        b = Box(*size).translate(center)
        b = rot * b
        bb = b.bounding_box()
        return (bb.min.X, bb.min.Y, bb.min.Z, bb.max.X, bb.max.Y, bb.max.Z)
    corners = [
        Vector(x, y, z)
        for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)
    ]
    rotated = [loc * c for c in corners]
    xs = [v.X for v in rotated]
    ys = [v.Y for v in rotated]
    zs = [v.Z for v in rotated]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))


def _trussify_along_z(
    part: Shape,
    cell_size: float,
    wall_thickness: float,
    safety_margin: float,
    region: Optional[tuple] = None,
    progress: Optional[callable] = None,
) -> Shape:
    """Z-axis trussify with local material analysis per cell."""
    W = cell_size
    t = wall_thickness
    m = safety_margin

    bbox = part.bounding_box()
    if region is not None:
        rx0, ry0, rz0, rx1, ry1, rz1 = region
        xmin = max(bbox.min.X, rx0)
        xmax = min(bbox.max.X, rx1)
        ymin = max(bbox.min.Y, ry0)
        ymax = min(bbox.max.Y, ry1)
        # Clamp Z too: tubes will only consider material inside the region's Z
        # band, so a region restricting one arm won't pick up tines from
        # other arms further along the cut axis.
        zmin = max(bbox.min.Z, rz0)
        zmax = min(bbox.max.Z, rz1)
    else:
        xmin, xmax = bbox.min.X, bbox.max.X
        ymin, ymax = bbox.min.Y, bbox.max.Y
        zmin, zmax = bbox.min.Z, bbox.max.Z

    # Hex geometry: flat-top, circumradius r, across-flats W.
    r_cut = W / math.sqrt(3)
    r_test = (W + 2 * m) / math.sqrt(3)

    # Tiling for flat-top hexes.
    pitch_x = (W + t) * math.sqrt(3) / 2
    pitch_y = W + t

    # Discovery prism: a small hex along the cut axis used to find the local
    # material segment at each cell. Smaller than the inflated test so it
    # doesn't get rejected at the boundary — we use it just to locate material.
    z_pad = 1.0
    discover_template = Pos(0, 0, zmin - z_pad) * extrude(
        RegularPolygon(r_cut, 6), (zmax - zmin) + 2 * z_pad
    )
    # Safety prism: inflated hex used as the actual containment test, restricted
    # to the segment height we discover.
    cut_template_2d = RegularPolygon(r_cut, 6)
    test_template_2d = RegularPolygon(r_test, 6)

    # Area of the cut hex (across-flats = W). Used to confirm the discovery
    # prism intersects a contiguous solid (no internal voids).
    hex_cut_area = math.sqrt(3) / 2.0 * W * W

    # Pre-count cells for progress.
    cols = max(1, int((xmax - xmin + W) / pitch_x) + 2)
    rows = max(1, int((ymax - ymin + W) / pitch_y) + 2)
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

            # Stage 1: discover the local material segment along the cut axis.
            discover = Pos(x, y, 0) * discover_template
            try:
                discovered = discover & part
            except Exception:
                y += pitch_y
                continue

            solids = _solids_of(discovered)
            if not solids:
                y += pitch_y
                continue
            # Prism crosses a gap between disconnected solids — refuse to cut.
            if len(solids) > 1:
                y += pitch_y
                continue

            seg = solids[0]
            seg_bbox = seg.bounding_box()
            mz_lo, mz_hi = seg_bbox.min.Z, seg_bbox.max.Z
            seg_height = mz_hi - mz_lo
            if seg_height < 1.0:
                y += pitch_y
                continue

            # Confirm the discovery prism is itself a clean column of material
            # (no internal voids cutting through the cut hex).
            discover_expected = hex_cut_area * seg_height
            if _safe_volume(seg) < 0.97 * discover_expected:
                y += pitch_y
                continue

            # Stage 2: safety test. The INFLATED hex, restricted to the local
            # segment height, must be fully inside the part. This guarantees
            # safety_margin of material on every side of the cut hex.
            inflated = Pos(x, y, mz_lo) * extrude(test_template_2d, seg_height)
            try:
                overshoot = _safe_volume(inflated - part)
            except Exception:
                y += pitch_y
                continue
            if overshoot > 0.05 * _safe_volume(inflated):
                # Inflated hex pokes out of the part somewhere — too close to a
                # boundary or hole. Skip.
                y += pitch_y
                continue

            # Safe. Cut hex through the actual material extent + small overshoot.
            cut_hex = Pos(x, y, mz_lo - 1) * extrude(cut_template_2d, seg_height + 2)
            cuts.append(cut_hex)
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
