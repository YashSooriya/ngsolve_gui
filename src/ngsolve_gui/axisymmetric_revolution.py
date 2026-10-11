"""Create a 3D visualization mesh from an axisymmetric meridian field.

The solver stores its axisymmetric results on an ``(r, z)`` mesh. This module
revolves that mesh around the axial axis and rotates cylindrical vector
components into Cartesian coordinates for display. The source solution is
sampled only at its existing mesh vertices; the resulting first-order field is
for visualization, while the saved 2D solution remains untouched.
"""

from __future__ import annotations

import math

import netgen.meshing as ngm
import ngsolve as ngs


DEFAULT_SECTORS = 32
MAX_TETRAHEDRA = 900_000
MIN_SECTORS = 8


def revolve_gridfunction(grid_function, sectors: int = DEFAULT_SECTORS):
    """Return ``(mesh3d, field3d)`` for a scalar or vector axisymmetric field.

    Scalars repeat around the revolution. Two-component vectors are interpreted
    as ``(radial, axial)`` and three-component vectors as
    ``(radial, circumferential, axial)``. Axis points are shared between angular
    sectors and their radial/circumferential vector values are set to zero.
    """
    if not isinstance(grid_function, ngs.GridFunction):
        raise TypeError("3D revolution requires an NGSolve GridFunction")
    source_mesh = grid_function.space.mesh
    if source_mesh.dim != 2:
        raise ValueError("3D revolution is only available for 2D meridian solutions")
    if grid_function.dim not in (1, 2, 3):
        raise ValueError(
            "Only scalar, radial-axial, or cylindrical 3D fields can be revolved"
        )

    source_vertices = list(source_mesh.vertices)
    source_points = {}
    radius_max = 0.0
    z_min = math.inf
    z_max = -math.inf
    for vertex in source_vertices:
        point = source_mesh[vertex].point
        radius, axial = float(point[0]), float(point[1])
        source_points[vertex.nr] = (radius, axial)
        radius_max = max(radius_max, radius)
        z_min, z_max = min(z_min, axial), max(z_max, axial)

    scale = max(radius_max, z_max - z_min, 1e-15)
    axis_tolerance = scale * 1e-12
    if min(radius for radius, _ in source_points.values()) < -axis_tolerance:
        raise ValueError("Axisymmetric meshes must use non-negative radius coordinates")
    materials = list(source_mesh.GetMaterials()) or ["default"]

    triangles = []
    for element in source_mesh.Elements(ngs.VOL):
        vertices = [vertex.nr for vertex in element.vertices]
        material = str(element.mat)
        if len(vertices) == 3:
            triangles.append((vertices, material))
        elif len(vertices) == 4:
            triangles.extend(
                (
                    ([vertices[0], vertices[1], vertices[2]], material),
                    ([vertices[0], vertices[2], vertices[3]], material),
                )
            )
        else:
            raise ValueError(
                "The meridian mesh must contain triangles or quadrilaterals"
            )
    if not triangles:
        raise ValueError("The meridian mesh contains no volume elements")
    edge_materials = {}
    for vertices, material in triangles:
        for index, first in enumerate(vertices):
            second = vertices[(index + 1) % len(vertices)]
            edge_materials.setdefault(tuple(sorted((first, second))), set()).add(
                material
            )

    boundary_edges = [
        [vertex.nr for vertex in element.vertices]
        for element in source_mesh.Elements(ngs.BND)
        if len(element.vertices) == 2
    ]
    boundary_materials = {}
    default_material = materials[0]
    for vertices in boundary_edges:
        key = tuple(sorted(vertices))
        adjacent = edge_materials.get(key, {default_material})
        boundary_materials[key] = sorted(adjacent)[0]

    try:
        sectors = int(sectors)
    except (TypeError, ValueError) as error:
        raise ValueError("Revolution resolution must be an integer") from error
    sectors = max(MIN_SECTORS, sectors)
    estimated_tetrahedra = len(triangles) * sectors * 3
    if estimated_tetrahedra > MAX_TETRAHEDRA:
        sectors = MAX_TETRAHEDRA // (len(triangles) * 3)
        if sectors < MIN_SECTORS:
            raise ValueError(
                "The meridian mesh is too large for a responsive 3D revolution "
                f"({len(triangles):,} triangles). Refine or reduce the 2D mesh first."
            )

    target_ngmesh = ngm.Mesh(dim=3)
    material_ids = {
        str(name): target_ngmesh.AddRegion(str(name), 3) for name in materials
    }
    boundary_descriptors = {}
    for material_name in dict.fromkeys(boundary_materials.values()):
        descriptor_number = len(boundary_descriptors) + 1
        domain_number = material_ids.get(
            material_name, next(iter(material_ids.values()))
        )
        boundary_descriptors[material_name] = target_ngmesh.Add(
            ngm.FaceDescriptor(
                surfnr=descriptor_number,
                domin=domain_number,
                domout=0,
                bc=descriptor_number,
            )
        )
        target_ngmesh.SetBCName(
            descriptor_number - 1, f"Revolved exterior: {material_name}"
        )

    point_ids: dict[int, list] = {}
    point_coordinates: dict[int, tuple[float, float, float]] = {}
    point_payload: dict[int, tuple[int, int]] = {}
    for vertex in source_vertices:
        radius, axial = source_points[vertex.nr]
        on_axis = abs(radius) <= axis_tolerance
        ring = []
        for sector in range(sectors):
            if on_axis and ring:
                ring.append(ring[0])
                continue
            angle = 2.0 * math.pi * sector / sectors
            x_coord = radius * math.cos(angle)
            y_coord = radius * math.sin(angle)
            point_id = target_ngmesh.Add(
                ngm.MeshPoint(ngm.Point3d(x_coord, y_coord, axial))
            )
            ring.append(point_id)
            point_coordinates[point_id.nr] = (x_coord, y_coord, axial)
            point_payload[point_id.nr] = (vertex.nr, sector)
        point_ids[vertex.nr] = ring

    determinant_tolerance = max(scale**3 * 1e-15, 1e-300)
    recipes = (
        ((0, 0), (1, 0), (2, 0), (2, 1)),
        ((0, 0), (1, 0), (1, 1), (2, 1)),
        ((0, 0), (0, 1), (1, 1), (2, 1)),
    )

    default_material_id = next(iter(material_ids.values()))
    for vertices, material in triangles:
        ordered = sorted(vertices)
        rings = [point_ids[index] for index in ordered]
        region_id = material_ids.get(material, default_material_id)
        for recipe in recipes:
            first_sector_ids = [rings[vertex][layer] for vertex, layer in recipe]
            xyz = [point_coordinates[point_id.nr] for point_id in first_sector_ids]
            determinant = _tetrahedron_determinant(xyz)
            if abs(determinant) <= determinant_tolerance:
                continue
            oriented_recipe = recipe
            if determinant < 0:
                oriented_recipe = (recipe[0], recipe[2], recipe[1], recipe[3])
            for sector in range(sectors):
                next_sector = (sector + 1) % sectors
                ids = [
                    rings[vertex][sector if layer == 0 else next_sector]
                    for vertex, layer in oriented_recipe
                ]
                target_ngmesh.Add(ngm.Element3D(index=region_id, vertices=ids))

    # Revolve only the source mesh's exterior edges into surface triangles.
    # This avoids retaining a large tetra-face incidence table just to recover
    # the outer skin, while the volume tetrahedra remain available for clipping.
    for edge_vertices in boundary_edges:
        first_vertex, second_vertex = edge_vertices
        material_name = boundary_materials[tuple(sorted(edge_vertices))]
        boundary_descriptor = boundary_descriptors[material_name]
        for sector in range(sectors):
            next_sector = (sector + 1) % sectors
            first = point_ids[first_vertex][sector]
            second = point_ids[second_vertex][sector]
            second_next = point_ids[second_vertex][next_sector]
            first_next = point_ids[first_vertex][next_sector]
            for triangle in (
                (first, second, second_next),
                (first, second_next, first_next),
            ):
                if len({point.nr for point in triangle}) < 3:
                    continue
                target_ngmesh.Add(
                    ngm.Element2D(index=boundary_descriptor, vertices=triangle)
                )

    target_mesh = ngs.Mesh(target_ngmesh)
    target_field = _interpolate_vertex_field(
        grid_function,
        source_mesh,
        target_mesh,
        point_payload,
        source_points,
        axis_tolerance,
        sectors,
    )
    target_mesh.Curve(1)
    return target_mesh, target_field


def _tetrahedron_determinant(points):
    a, b, c, d = points
    u = tuple(b[index] - a[index] for index in range(3))
    v = tuple(c[index] - a[index] for index in range(3))
    w = tuple(d[index] - a[index] for index in range(3))
    return (
        u[0] * (v[1] * w[2] - v[2] * w[1])
        - u[1] * (v[0] * w[2] - v[2] * w[0])
        + u[2] * (v[0] * w[1] - v[1] * w[0])
    )


def _interpolate_vertex_field(
    source,
    source_mesh,
    target_mesh,
    point_payload,
    source_points,
    axis_tolerance,
    sectors,
):
    """Sample the source at meridian vertices into first-order 3D H1 fields."""
    import numpy as np

    dtype = complex if source.is_complex else float
    source_values = {}
    for vertex in source_mesh.vertices:
        radius, axial = source_mesh[vertex].point
        value = np.asarray(
            source(source_mesh(float(radius), float(axial))), dtype=dtype
        )
        source_values[vertex.nr] = value.reshape(-1)

    scalar_space = ngs.H1(target_mesh, order=1, complex=source.is_complex)
    if source.dim == 1:
        component_fields = [
            ngs.GridFunction(scalar_space, name=f"{source.name}_revolved")
        ]
    else:
        component_fields = [
            ngs.GridFunction(scalar_space, name=f"{source.name}_revolved_{axis}")
            for axis in ("x", "y", "z")
        ]

    for vertex in target_mesh.vertices:
        dofs = scalar_space.GetDofNrs(vertex)
        if len(dofs) != 1:
            raise RuntimeError("Expected one first-order H1 DoF at each 3D mesh vertex")
        dof = dofs[0]
        netgen_vertex_number = vertex.nr + 1
        try:
            source_vertex, sector = point_payload[netgen_vertex_number]
            values = source_values[source_vertex]
        except KeyError as error:
            raise RuntimeError(
                "Could not map a revolved mesh vertex to the meridian field"
            ) from error
        if source.dim == 1:
            component_fields[0].vec[dof] = values[0]
            continue

        angle = 2.0 * math.pi * sector / sectors
        if source.dim == 2:
            radial, axial = values[0], values[1]
            circumferential = 0
        else:
            radial, circumferential, axial = values[:3]
        if abs(source_points[source_vertex][0]) <= axis_tolerance:
            x_value = y_value = 0
        else:
            cosine, sine = math.cos(angle), math.sin(angle)
            x_value = radial * cosine - circumferential * sine
            y_value = radial * sine + circumferential * cosine
        for component, value in zip(component_fields, (x_value, y_value, axial)):
            component.vec[dof] = value
    if source.dim == 1:
        return component_fields[0]
    return ngs.CF(tuple(component_fields))
