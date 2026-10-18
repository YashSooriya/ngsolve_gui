"""Coarsen large meridian meshes for responsive axisymmetric 3D previews."""

from __future__ import annotations

from collections import defaultdict
import math

import numpy as np
import ngsolve as ngs
from netgen.geom2d import SplineGeometry


def coarsen_meridian_field(source, max_triangles: int):
    """Return a P1 field on a coarse mesh with the source interfaces preserved.

    Large 2D solver meshes can contain hundreds of thousands of triangles. A
    full 3D revolution of that mesh is too large to build interactively. This
    routine rebuilds the meridian regions from their material/interface edges,
    simplifies only those boundary polylines, and samples the original solution
    at the new mesh vertices. The solver mesh and saved solution are untouched.
    """
    source_mesh = source.space.mesh
    materials = [str(name) for name in source_mesh.GetMaterials()]
    if not materials:
        materials = ["default"]
    material_ids = {name: index + 1 for index, name in enumerate(materials)}
    coordinates = {
        vertex.nr: (
            float(source_mesh[vertex].point[0]),
            float(source_mesh[vertex].point[1]),
        )
        for vertex in source_mesh.vertices
    }
    if not coordinates:
        raise ValueError("The axisymmetric mesh has no vertices")

    edge_adjacency = defaultdict(list)
    for element in source_mesh.Elements(ngs.VOL):
        vertices = [vertex.nr for vertex in element.vertices]
        if len(vertices) not in (3, 4):
            continue
        material = str(element.mat)
        center = (
            sum(coordinates[index][0] for index in vertices) / len(vertices),
            sum(coordinates[index][1] for index in vertices) / len(vertices),
        )
        for index, first in enumerate(vertices):
            second = vertices[(index + 1) % len(vertices)]
            edge_adjacency[tuple(sorted((first, second)))].append(
                (material, first, second, center)
            )

    boundary_names = {}
    for element in source_mesh.Elements(ngs.BND):
        vertices = [vertex.nr for vertex in element.vertices]
        if len(vertices) == 2:
            boundary_names.setdefault(
                tuple(sorted(vertices)), str(element.mat)
            )

    edge_keys = set(boundary_names)
    for edge, adjacent in edge_adjacency.items():
        adjacent_materials = {item[0] for item in adjacent}
        if len(adjacent) == 1 or len(adjacent_materials) > 1:
            edge_keys.add(edge)
    if not edge_keys:
        raise ValueError("The axisymmetric mesh has no recoverable region boundaries")

    boundary_groups = defaultdict(list)
    for edge in sorted(edge_keys):
        adjacent = edge_adjacency.get(edge)
        if not adjacent:
            raise ValueError("A meridian boundary edge has no adjacent elements")
        adjacent_materials = sorted(
            {item[0] for item in adjacent},
            key=lambda name: material_ids.get(name, len(material_ids)),
        )
        if len(adjacent_materials) > 2:
            raise ValueError("A meridian boundary edge borders more than two regions")

        left_name = adjacent_materials[0]
        left_element = next(item for item in adjacent if item[0] == left_name)
        first, second, center = left_element[1], left_element[2], left_element[3]
        ax, ay = coordinates[first]
        bx, by = coordinates[second]
        cx, cy = center
        cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if cross < 0:
            first, second = second, first

        if len(adjacent_materials) > 1:
            right_name = adjacent_materials[1]
        elif len(adjacent) > 1:
            # A named internal boundary may separate two elements with the
            # same material. Retain it without splitting the material region.
            right_name = left_name
        else:
            right_name = None

        boundary_name = boundary_names.get(edge)
        if not boundary_name:
            if right_name and right_name != left_name:
                boundary_name = f"Revolved interface: {left_name} / {right_name}"
            else:
                boundary_name = f"Revolved exterior: {left_name}"
        boundary_groups[(left_name, right_name, boundary_name)].append(
            (first, second)
        )

    radii = [point[0] for point in coordinates.values()]
    axial = [point[1] for point in coordinates.values()]
    scale = max(max(radii) - min(radii), max(axial) - min(axial), 1e-15)
    tolerance = scale * 5e-4
    geometry = SplineGeometry()
    geometry_points = {}

    def add_point(vertex_id):
        if vertex_id not in geometry_points:
            geometry_points[vertex_id] = geometry.AddPoint(*coordinates[vertex_id])
        return geometry_points[vertex_id]

    for (left_name, right_name, boundary_name), edges in boundary_groups.items():
        orientation = {
            tuple(sorted((first, second))): (first, second)
            for first, second in edges
        }
        for chain in _trace_chains(edges):
            simplified = _simplify_chain(chain, coordinates, tolerance)
            first_edge = tuple(sorted((chain[0], chain[1])))
            follows_material_orientation = (
                orientation[first_edge] == (chain[0], chain[1])
            )
            left_id = material_ids[left_name]
            right_id = (
                material_ids[right_name] if right_name is not None else 0
            )
            for first, second in zip(simplified, simplified[1:]):
                start, end = (
                    (first, second)
                    if follows_material_orientation
                    else (second, first)
                )
                geometry.Append(
                    [add_point(start), add_point(end)],
                    leftdomain=left_id,
                    rightdomain=right_id,
                    bc=boundary_name,
                )

    for name, index in material_ids.items():
        geometry.SetMaterial(index, name)

    target_triangles = max(64, int(max_triangles))
    maxh = scale / max(1.0, math.sqrt(target_triangles / 2.0))
    coarse_mesh = None
    for _attempt in range(8):
        generated = geometry.GenerateMesh(
            maxh=maxh,
            grading=0.3,
            segmentsperedge=1,
            optimize2d="smcm",
            optsteps2d=1,
        )
        coarse_mesh = ngs.Mesh(generated)
        triangle_count = sum(
            1 if len(element.vertices) == 3 else 2
            for element in coarse_mesh.Elements(ngs.VOL)
            if len(element.vertices) in (3, 4)
        )
        if 0 < triangle_count <= target_triangles:
            break
        maxh *= 1.2
    else:
        raise ValueError(
            "The region boundaries need too many elements for a responsive "
            "3D revolution preview."
        )

    if source.dim == 1:
        space = ngs.H1(coarse_mesh, order=1, complex=bool(source.is_complex))
    else:
        space = ngs.VectorH1(
            coarse_mesh,
            order=1,
            dim=source.dim,
            complex=bool(source.is_complex),
        )
    coarse_field = ngs.GridFunction(
        space, name=f"{getattr(source, 'name', 'field')}_preview"
    )
    dtype = complex if source.is_complex else float
    for vertex in coarse_mesh.vertices:
        point = coarse_mesh[vertex].point
        value = np.asarray(
            source(source_mesh(float(point[0]), float(point[1]))),
            dtype=dtype,
        ).reshape(-1)
        dofs = space.GetDofNrs(vertex)
        if len(dofs) != len(value):
            raise RuntimeError(
                "Could not transfer a meridian field to the preview mesh"
            )
        for dof, component in zip(dofs, value):
            coarse_field.vec[dof] = component
    return coarse_field


def _trace_chains(edges):
    """Trace connected boundary polylines from unordered mesh edges."""
    neighbors = defaultdict(set)
    unused = set()
    for first, second in edges:
        if first == second:
            continue
        edge = tuple(sorted((first, second)))
        if edge in unused:
            continue
        unused.add(edge)
        neighbors[first].add(second)
        neighbors[second].add(first)

    def walk(start, following):
        chain = [start, following]
        unused.remove(tuple(sorted((start, following))))
        previous, current = start, following
        while len(neighbors[current]) == 2:
            choices = [
                vertex
                for vertex in neighbors[current]
                if vertex != previous
                and tuple(sorted((current, vertex))) in unused
            ]
            if not choices:
                break
            next_vertex = min(choices)
            unused.remove(tuple(sorted((current, next_vertex))))
            chain.append(next_vertex)
            previous, current = current, next_vertex
            if current == start:
                break
        return chain

    chains = []
    for vertex in sorted(neighbors):
        if len(neighbors[vertex]) == 2:
            continue
        for following in sorted(neighbors[vertex]):
            if tuple(sorted((vertex, following))) in unused:
                chains.append(walk(vertex, following))
    while unused:
        first, second = min(unused)
        chains.append(walk(first, second))
    return chains


def _simplify_chain(chain, coordinates, tolerance):
    closed = len(chain) > 2 and chain[0] == chain[-1]
    if not closed:
        return _rdp(chain, coordinates, tolerance)

    points = chain[:-1]
    if len(points) <= 3:
        return list(points) + [points[0]]
    start_index = min(
        range(len(points)),
        key=lambda index: (
            coordinates[points[index]][0],
            coordinates[points[index]][1],
            points[index],
        ),
    )
    points = points[start_index:] + points[:start_index]
    farthest = max(
        range(1, len(points)),
        key=lambda index: math.dist(
            coordinates[points[0]], coordinates[points[index]]
        ),
    )
    first_arc = _rdp(points[: farthest + 1], coordinates, tolerance)
    second_arc = _rdp(points[farthest:] + points[:1], coordinates, tolerance)
    simplified = first_arc[:-1] + second_arc[:-1]
    if len(set(simplified)) < 3:
        return list(chain)
    return simplified + [simplified[0]]


def _rdp(points, coordinates, tolerance):
    """Iterative Ramer-Douglas-Peucker simplification of a boundary chain."""
    if len(points) <= 2:
        return list(points)
    keep = {0, len(points) - 1}
    pending = [(0, len(points) - 1)]
    while pending:
        first_index, last_index = pending.pop()
        first = coordinates[points[first_index]]
        last = coordinates[points[last_index]]
        greatest_distance = tolerance
        greatest_index = None
        for index in range(first_index + 1, last_index):
            distance = _point_segment_distance(
                coordinates[points[index]], first, last
            )
            if distance > greatest_distance:
                greatest_distance = distance
                greatest_index = index
        if greatest_index is not None:
            keep.add(greatest_index)
            pending.append((first_index, greatest_index))
            pending.append((greatest_index, last_index))
    return [points[index] for index in sorted(keep)]


def _point_segment_distance(point, first, second):
    dx = second[0] - first[0]
    dy = second[1] - first[1]
    denominator = dx * dx + dy * dy
    if denominator == 0:
        return math.dist(point, first)
    position = max(
        0.0,
        min(
            1.0,
            ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy)
            / denominator,
        ),
    )
    projection = (
        first[0] + position * dx,
        first[1] + position * dy,
    )
    return math.dist(point, projection)

