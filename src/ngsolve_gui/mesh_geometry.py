"""Geometry-aware mesh edge polylines used by the Solve mesh preview."""

from __future__ import annotations

import math


def axisymmetric_mesh_edge_polylines(mesh):
    """Return unique 2D mesh edges, sampling edges from high-order geometry."""
    from ngsolve import ET, ElementId, VOL

    reference_vertices = {
        ET.TRIG: ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
        ET.QUAD: ((-1.0, -1.0), (1.0, -1.0), (1.0, 1.0), (-1.0, 1.0)),
    }
    try:
        curve_order = int(mesh.GetCurveOrder())
    except (AttributeError, TypeError, ValueError):
        curve_order = 1
    sample_count = max(8, curve_order * 4) if curve_order > 1 else 1
    edges = {}

    for element in mesh.Elements(VOL):
        vertices = tuple(element.vertices)
        if len(vertices) < 3:
            continue
        reference = reference_vertices.get(element.type)
        transformation = None
        if curve_order > 1 and reference and len(reference) == len(vertices):
            try:
                transformation = mesh.GetTrafo(ElementId(VOL, element.nr))
                if not transformation.curved:
                    transformation = None
            except (AttributeError, RuntimeError, TypeError, ValueError):
                transformation = None

        for index, start in enumerate(vertices):
            end_index = (index + 1) % len(vertices)
            end = vertices[end_index]
            key = tuple(sorted((start.nr, end.nr)))
            if key in edges:
                continue
            start_point = mesh[start].point
            end_point = mesh[end].point
            endpoints = (
                (float(start_point[0]), float(start_point[1])),
                (float(end_point[0]), float(end_point[1])),
            )
            points = endpoints
            if transformation is not None:
                try:
                    first = reference[index]
                    last = reference[end_index]
                    mapped = []
                    for sample in range(sample_count + 1):
                        fraction = sample / sample_count
                        ref_x = first[0] + fraction * (last[0] - first[0])
                        ref_y = first[1] + fraction * (last[1] - first[1])
                        point = transformation(ref_x, ref_y).point
                        mapped.append((float(point[0]), float(point[1])))
                    chord_deviation = max(
                        math.dist(
                            point,
                            (
                                endpoints[0][0] + fraction * (endpoints[1][0] - endpoints[0][0]),
                                endpoints[0][1] + fraction * (endpoints[1][1] - endpoints[0][1]),
                            ),
                        )
                        for sample, point in enumerate(mapped)
                        for fraction in (sample / sample_count,)
                    )
                    tolerance = max(1e-12, math.dist(*endpoints) * 1e-8)
                    if chord_deviation > tolerance:
                        points = tuple(mapped)
                except (AttributeError, RuntimeError, TypeError, ValueError, ZeroDivisionError):
                    points = endpoints
            edges[key] = points

    return list(edges.values())
