"""Realistic magnet 4 K shield and matching Golay trace generation."""
from __future__ import annotations

import math
from pathlib import Path
from netgen.occ import Axis, Cylinder, Face, Pnt, Segment, SplineApproximation, Wire, Z

BORE_RADIUS_M = 0.447
BORE_HALF_LENGTH_M = 0.65390
WIRE_RADIUS_M = 0.001
CLEARANCE_M = 0.010


def realistic_shield():
    # Profile dimensions and support recess from the realistic magnet problem.
    bore_r, bore_l, thickness, outer_r = BORE_RADIUS_M, BORE_HALF_LENGTH_M, 0.004, 0.87804
    rad1, rad2, zd, flap = 0.950, 0.055, 0.1, 0.001
    xc1, yc1, xc2 = bore_r + thickness, bore_l - rad1, outer_r - rad2
    angle = math.asin((outer_r - xc1 - rad2) / (rad1 - rad2))
    yc2 = yc1 + (rad1 - rad2) * math.cos(angle)

    def profile(offset):
        r1, r2 = rad1 + offset, rad2 + offset
        return [
            (xc1, bore_l + offset),
            (xc1 + r1 * math.sin(angle / 2), yc1 + r1 * math.cos(angle / 2)),
            (xc1 + r1 * math.sin(angle), yc1 + r1 * math.cos(angle)),
            (xc2 + r2 * math.sin((angle + math.pi / 2) / 2),
             yc2 + r2 * math.cos((angle + math.pi / 2) / 2)),
            (outer_r + offset, yc2),
        ]

    inner, outer = profile(0), profile(thickness)
    outer[0] = (bore_r, bore_l + thickness)

    def solid(points, recessed=False):
        top = [Pnt(0, r, z) for r, z in points]
        bottom = [Pnt(0, r, -z) for r, z in points]
        edges = [SplineApproximation(top[:3]), SplineApproximation(top[2:])]
        if recessed:
            middle = [Pnt(0, outer_r, zd), Pnt(0, outer_r - flap, zd),
                      Pnt(0, outer_r - flap, -zd), Pnt(0, outer_r, -zd)]
            chain = [top[-1], *middle, bottom[-1]]
            edges.extend(Segment(a, b) for a, b in zip(chain, chain[1:]))
        else:
            edges.append(Segment(top[-1], bottom[-1]))
        edges.extend([SplineApproximation(list(reversed(bottom[2:]))),
                      SplineApproximation(list(reversed(bottom[:3]))), Segment(bottom[0], top[0])])
        return Face(Wire(edges)).Revolve(Axis((0, 0, 0), Z), 360)

    shield = solid(outer) - solid(inner, recessed=True)
    cutter = (Cylinder((0, 0, -zd), Z, r=outer_r, h=2 * zd)
              - Cylinder((0, 0, -zd), Z, r=outer_r - 10 * flap, h=2 * zd))
    shield = (shield - cutter).mat("4K_Shield")
    if len(shield.solids) != 1 or shield.mass <= 0:
        raise ValueError("The shield must be one nonempty solid.")
    bore = Cylinder((0, 0, -bore_l - thickness), Z, r=bore_r - 1e-6, h=2 * (bore_l + thickness))
    if (shield * bore).mass > 1e-12:
        raise ValueError("The shield obstructs its bore.")
    return shield


def make_golay_traces(source_directory, destination):
    from ngsolve_gui.three_d_project import parse_wire_trace
    traces = {axis: parse_wire_trace((Path(source_directory) / f"{axis}_gradient_grouped.txt").read_bytes(), "m")
              for axis in ("x", "y")}
    points = [point for trace in traces.values() for point in trace if point is not None]
    radial = max(math.hypot(point[0], point[1]) for point in points)
    axial = max(abs(point[2]) for point in points)
    scale = min(1.0, (BORE_RADIUS_M - WIRE_RADIUS_M - CLEARANCE_M) / radial,
                (BORE_HALF_LENGTH_M - WIRE_RADIUS_M - CLEARANCE_M) / axial)
    if not 0 < scale <= 1:
        raise ValueError("Invalid coil scale.")
    for axis, trace in traces.items():
        lines = ["nan nan nan" if point is None else " ".join(f"{coordinate * scale:.14g}" for coordinate in point)
                 for point in trace]
        (Path(destination) / f"golay_{axis}_trace.txt").write_text("\n".join(lines) + "\n", encoding="ascii")
    return scale
