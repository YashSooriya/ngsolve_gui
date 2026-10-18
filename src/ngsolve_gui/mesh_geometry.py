"""Geometry-aware mesh edge polylines used by the Solve mesh preview."""

from __future__ import annotations

from itertools import permutations
import math


def _coordinate_tolerance(points, scale):
    """Use a scale-relative tolerance bounded by floating-point resolution."""
    coordinate_resolution = max(
        (
            math.ulp(abs(float(value)))
            for point in points
            for value in point
        ),
        default=math.ulp(0.0),
    )
    return max(scale * 1e-8, coordinate_resolution * 16)


def _reference_vertices_by_mesh_vertex(transformation, reference_vertices, vertices, mesh):
    """Match reference corners to mesh vertices using the element transform.

    NGSolve may order an element's global vertices differently from the fixed
    reference-cell corner order. Match by transformed coordinates instead of
    assuming that both orders are identical; a wrong pairing can render a
    different edge in place of a curved boundary edge.
    """
    mapped_reference = []
    for reference in reference_vertices:
        point = transformation(*reference).point
        mapped_reference.append((float(point[0]), float(point[1])))

    mesh_points = []
    for vertex in vertices:
        point = mesh[vertex].point
        mesh_points.append((float(point[0]), float(point[1])))

    scale = max(
        (math.dist(first, second)
         for index, first in enumerate(mesh_points)
         for second in mesh_points[index + 1:]),
        default=0.0,
    )
    tolerance = _coordinate_tolerance(mapped_reference + mesh_points, scale)
    best_assignment = None
    best_error = math.inf
    for assignment in permutations(range(len(reference_vertices))):
        error = max(
            math.dist(mapped_reference[reference_index], mesh_points[vertex_index])
            for vertex_index, reference_index in enumerate(assignment)
        )
        if error < best_error:
            best_error = error
            best_assignment = assignment

    if best_assignment is None or best_error > tolerance:
        return None
    return {
        vertex.nr: reference_vertices[reference_index]
        for vertex, reference_index in zip(vertices, best_assignment)
    }


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
        reference_by_vertex = None
        if curve_order > 1 and reference and len(reference) == len(vertices):
            try:
                transformation = mesh.GetTrafo(ElementId(VOL, element.nr))
                if not transformation.curved:
                    transformation = None
                else:
                    reference_by_vertex = _reference_vertices_by_mesh_vertex(
                        transformation, reference, vertices, mesh
                    )
                    if reference_by_vertex is None:
                        transformation = None
            except (AttributeError, RuntimeError, TypeError, ValueError):
                transformation = None
                reference_by_vertex = None

        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            key = tuple(sorted((start.nr, end.nr)))
            start_point = mesh[start].point
            end_point = mesh[end].point
            endpoints = (
                (float(start_point[0]), float(start_point[1])),
                (float(end_point[0]), float(end_point[1])),
            )
            points = endpoints
            if transformation is not None:
                try:
                    first = reference_by_vertex[start.nr]
                    last = reference_by_vertex[end.nr]
                    mapped = []
                    for sample in range(sample_count + 1):
                        fraction = sample / sample_count
                        ref_x = first[0] + fraction * (last[0] - first[0])
                        ref_y = first[1] + fraction * (last[1] - first[1])
                        point = transformation(ref_x, ref_y).point
                        mapped.append((float(point[0]), float(point[1])))

                    mapping_tolerance = _coordinate_tolerance(
                        mapped + list(endpoints), math.dist(*endpoints)
                    )
                    endpoints_match = (
                        math.dist(mapped[0], endpoints[0]) <= mapping_tolerance
                        and math.dist(mapped[-1], endpoints[1]) <= mapping_tolerance
                    )
                    if endpoints_match:
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
                        if chord_deviation > mapping_tolerance:
                            points = tuple(mapped)
                except (AttributeError, KeyError, RuntimeError, TypeError, ValueError, ZeroDivisionError):
                    points = endpoints

            previous = edges.get(key)
            # A shared edge can be visited from a straight-sided element before
            # its curved neighbor. Keep the richer representation in that case.
            if previous is None or len(points) > len(previous):
                edges[key] = points

    return list(edges.values())
