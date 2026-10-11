"""Visualization-only expansion of a 3D quarter-domain solution.

Quarter meshes produced by the coupled solver mark their coordinate symmetry
planes with boundary names such as ``SymmX`` and ``SymmY``. This module mirrors
the existing tetrahedral mesh across those planes and samples the saved field
onto a continuous first-order display field. The original solved grid function
is never changed.
"""

from __future__ import annotations

import math
import re

import netgen.meshing as ngm
import ngsolve as ngs


_SYMMETRY_BOUNDARY = re.compile(r"(?:symm|symmetry)[ _-]*([xyz])", re.IGNORECASE)


def find_quarter_symmetry_planes(mesh):
    """Return tagged coordinate planes for a quarter-domain 3D mesh.

    Each returned plane includes the coordinate, the coordinate axis index,
    and the side occupied by the original quarter mesh. A plane is accepted
    only when it is a planar boundary at one extreme of the source mesh.
    """
    if getattr(mesh, "dim", None) != 3:
        return ()

    boundaries = tuple(str(name) for name in mesh.GetBoundaries())
    vertices = list(mesh.vertices)
    if not vertices:
        return ()
    coordinates = [
        tuple(float(value) for value in mesh[vertex].point[:3])
        for vertex in vertices
    ]
    bounds = [
        (min(point[axis] for point in coordinates), max(point[axis] for point in coordinates))
        for axis in range(3)
    ]
    scale = max(
        *(high - low for low, high in bounds),
        *(abs(value) for low, high in bounds for value in (low, high)),
        1e-12,
    )
    tolerance = max(scale * 1e-9, 1e-12)
    boundary_coordinates = {index: [] for index in range(len(boundaries))}
    for element in mesh.Elements(ngs.BND):
        if 0 <= int(element.index) < len(boundaries):
            for vertex in element.vertices:
                boundary_coordinates[int(element.index)].append(
                    tuple(float(value) for value in mesh[vertex].point[:3])
                )

    planes = []
    seen_axes = set()
    for boundary_index, name in enumerate(boundaries):
        match = _SYMMETRY_BOUNDARY.fullmatch(name.strip())
        if match is None:
            continue
        axis_name = match.group(1).lower()
        axis = "xyz".index(axis_name)
        if axis in seen_axes:
            continue
        points = boundary_coordinates.get(boundary_index, ())
        if not points:
            continue
        plane_values = [point[axis] for point in points]
        plane = sum(plane_values) / len(plane_values)
        if max(plane_values) - min(plane_values) > tolerance:
            continue
        low, high = bounds[axis]
        if abs(low - plane) <= tolerance and high - plane > tolerance:
            source_side = 1
        elif abs(high - plane) <= tolerance and plane - low > tolerance:
            source_side = -1
        else:
            continue
        planes.append(
            {
                "name": axis_name,
                "axis": axis,
                "coordinate": plane,
                "source_side": source_side,
            }
        )
        seen_axes.add(axis)

    # This UI is specifically for a quarter (two-plane) domain. Octants and
    # arbitrary partial models need separate metadata to avoid guessing.
    return tuple(sorted(planes, key=lambda plane: plane["axis"])) if len(planes) == 2 else ()


def mirror_quarter_gridfunction(grid_function, symmetry_planes, *, vector_kind="polar"):
    """Return ``(full_mesh, display_field)`` reflected across two tagged planes.

    Scalar values repeat unchanged. Polar vectors (for example displacement
    or magnetic vector potential) transform as ordinary vectors. Axial vectors
    (for example magnetic flux density) include the determinant sign of the
    reflection. The output is an order-one display field on a mirrored mesh;
    the source solution remains unchanged.
    """
    if not isinstance(grid_function, ngs.GridFunction):
        raise TypeError("Symmetry expansion requires an NGSolve GridFunction")
    source_mesh = grid_function.space.mesh
    if source_mesh.dim != 3:
        raise ValueError("Quarter symmetry expansion is only available for 3D results")
    planes = tuple(symmetry_planes)
    if len(planes) != 2 or len({int(plane["axis"]) for plane in planes}) != 2:
        raise ValueError("A quarter-domain result must have two independent symmetry planes")
    if any(
        int(plane["axis"]) not in (0, 1, 2)
        or int(plane["source_side"]) not in (-1, 1)
        for plane in planes
    ):
        raise ValueError("Symmetry plane axes and source sides are invalid")
    if grid_function.dim not in (1, 3):
        raise ValueError("Symmetry expansion supports scalar or 3-component fields")
    if vector_kind not in {"polar", "axial"}:
        raise ValueError("Vector kind must be 'polar' or 'axial'")

    mirrored_ngmesh = source_mesh.ngmesh.Copy()
    for plane in planes:
        axis = int(plane["axis"])
        point = [0.0, 0.0, 0.0]
        normal = [0.0, 0.0, 0.0]
        point[axis] = float(plane["coordinate"])
        normal[axis] = 1.0
        mirrored_ngmesh = mirrored_ngmesh.Mirror(
            ngm.Point3d(*point), ngm.Vec3d(*normal)
        )

    target_mesh = ngs.Mesh(mirrored_ngmesh)
    curve_order = int(source_mesh.GetCurveOrder())
    if curve_order > 1:
        target_mesh.Curve(curve_order)

    scalar_space = ngs.H1(
        target_mesh,
        order=1,
        complex=bool(grid_function.is_complex),
    )
    component_fields = [
        ngs.GridFunction(scalar_space, name=f"{grid_function.name}_full_{index}")
        for index in range(grid_function.dim)
    ]
    tolerance = max(
        max(
            float(plane.get("tolerance", 0.0)),
            abs(float(plane["coordinate"])) * 1e-10,
        )
        for plane in planes
    )
    tolerance = max(tolerance, 1e-12)
    import numpy as np

    source_vertices = list(source_mesh.vertices)
    source_coordinates = [
        tuple(float(value) for value in source_mesh[vertex].point[:3])
        for vertex in source_vertices
    ]
    source_bounds = [
        (
            min(point[axis] for point in source_coordinates),
            max(point[axis] for point in source_coordinates),
        )
        for axis in range(3)
    ]
    coordinate_scale = max(
        *(high - low for low, high in source_bounds),
        1e-12,
    )
    coordinate_key_tolerance = max(coordinate_scale * 1e-12, 1e-15)

    def point_key(point):
        return tuple(round(float(value) / coordinate_key_tolerance) for value in point)

    # Mirror.Mesh retains every original topological vertex. Evaluate the
    # source field once per original vertex, then reuse those values for the
    # four copies instead of repeatedly searching the source mesh.
    source_values = {}
    for vertex, point in zip(source_vertices, source_coordinates):
        source_values[point_key(point)] = np.asarray(
            grid_function(source_mesh(*point))
        ).reshape(-1)

    for vertex in target_mesh.vertices:
        point = [float(value) for value in target_mesh[vertex].point[:3]]
        source_point = list(point)
        signs = [1, 1, 1]
        for plane in planes:
            axis = int(plane["axis"])
            coordinate = float(plane["coordinate"])
            distance = point[axis] - coordinate
            if abs(distance) <= tolerance:
                source_point[axis] = coordinate
            elif distance * int(plane["source_side"]) < 0:
                signs[axis] = -1
                source_point[axis] = 2.0 * coordinate - point[axis]

        values = source_values.get(point_key(source_point))
        if values is None:
            values = np.asarray(
                grid_function(source_mesh(*source_point))
            ).reshape(-1)
        if grid_function.dim == 1:
            factors = (1,)
        elif vector_kind == "axial":
            determinant = math.prod(signs)
            factors = tuple(determinant * sign for sign in signs)
        else:
            factors = tuple(signs)

        dofs = scalar_space.GetDofNrs(vertex)
        if len(dofs) != 1:
            raise RuntimeError("Expected one first-order H1 DoF per mesh vertex")
        for index, component in enumerate(component_fields):
            component.vec[dofs[0]] = values[index] * factors[index]

    if grid_function.dim == 1:
        display_field = component_fields[0]
    else:
        display_field = ngs.CF(tuple(component_fields))
    return target_mesh, display_field
