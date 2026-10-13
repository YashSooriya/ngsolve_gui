"""Create a 3D visualization mesh from an axisymmetric meridian field.

The solver stores its axisymmetric results on an ``(r, z)`` mesh. This module
revolves that mesh around the axial axis and rotates cylindrical vector
components into Cartesian coordinates for display. The source solution is
sampled on a meridian lattice matched to its finite-element order; the resulting
first-order field is for visualization, while the saved 2D solution remains untouched.
"""

from __future__ import annotations

import math

import netgen.meshing as ngm
import ngsolve as ngs


DEFAULT_SECTORS = 64
MAX_TETRAHEDRA = 300_000
MIN_SECTORS = 8
MAX_MERIDIAN_SUBDIVISIONS = 4


def revolve_gridfunction(
    grid_function,
    sectors: int = DEFAULT_SECTORS,
    *,
    _allow_coarsen: bool = True,
):
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
    try:
        sectors = int(sectors)
    except (TypeError, ValueError) as error:
        raise ValueError("Revolution resolution must be an integer") from error
    sectors = max(MIN_SECTORS, sectors)

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

    source_triangles = []
    for element in source_mesh.Elements(ngs.VOL):
        vertices = [vertex.nr for vertex in element.vertices]
        material = str(element.mat)
        if len(vertices) == 3:
            source_triangles.append((vertices, material))
        elif len(vertices) == 4:
            source_triangles.extend(
                (
                    ([vertices[0], vertices[1], vertices[2]], material),
                    ([vertices[0], vertices[2], vertices[3]], material),
                )
            )
        else:
            raise ValueError(
                "The meridian mesh must contain triangles or quadrilaterals"
            )
    if not source_triangles:
        raise ValueError("The meridian mesh contains no volume elements")

    # Sampling only the original mesh vertices loses higher-order interior
    # displacement modes. Subdivide each meridian triangle at the source
    # polynomial order, then sample those points before revolving the field.
    try:
        source_order = int(grid_function.space.globalorder)
    except (AttributeError, TypeError, ValueError):
        source_order = 1
    subdivisions = max(1, min(source_order, MAX_MERIDIAN_SUBDIVISIONS))
    while (
        len(source_triangles) * subdivisions**2 * sectors * 3
        > MAX_TETRAHEDRA
        and subdivisions > 1
    ):
        subdivisions -= 1
    if len(source_triangles) * subdivisions**2 * sectors * 3 > MAX_TETRAHEDRA:
        if not _allow_coarsen:
            raise ValueError(
                "The meridian mesh is still too large after coarsening for a "
                "responsive 3D revolution."
            )
        from .axisymmetric_preview import coarsen_meridian_field

        max_triangles = MAX_TETRAHEDRA // (sectors * 3)
        coarse_field = coarsen_meridian_field(
            grid_function, max_triangles=max_triangles
        )
        return revolve_gridfunction(
            coarse_field, sectors=sectors, _allow_coarsen=False
        )

    meridian_points = {}
    meridian_point_ids = {}

    def meridian_node(vertices, weights):
        key = tuple(
            sorted(
                (int(vertex), int(weight))
                for vertex, weight in zip(vertices, weights)
                if weight
            )
        )
        if key not in meridian_point_ids:
            point_id = len(meridian_points)
            denominator = float(subdivisions)
            meridian_points[point_id] = (
                sum(source_points[vertex][0] * weight for vertex, weight in key)
                / denominator,
                sum(source_points[vertex][1] * weight for vertex, weight in key)
                / denominator,
            )
            meridian_point_ids[key] = point_id
        return meridian_point_ids[key]

    triangles = []
    for vertices, material in source_triangles:
        lattice = {}
        for first_weight in range(subdivisions + 1):
            for second_weight in range(subdivisions - first_weight + 1):
                weights = (
                    subdivisions - first_weight - second_weight,
                    first_weight,
                    second_weight,
                )
                lattice[(first_weight, second_weight)] = meridian_node(
                    vertices, weights
                )
        for first_weight in range(subdivisions):
            for second_weight in range(subdivisions - first_weight):
                first = lattice[(first_weight, second_weight)]
                second = lattice[(first_weight + 1, second_weight)]
                third = lattice[(first_weight, second_weight + 1)]
                triangles.append(([first, second, third], material))
                if first_weight + second_weight + 2 <= subdivisions:
                    fourth = lattice[(first_weight + 1, second_weight + 1)]
                    triangles.append(([second, fourth, third], material))

    edge_materials = {}
    for vertices, material in source_triangles:
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
    default_material = materials[0]
    material_order = {str(name): index for index, name in enumerate(materials)}
    boundary_segments = []
    for vertices in boundary_edges:
        key = tuple(sorted(vertices))
        adjacent = edge_materials.get(key) or {default_material}
        if len(adjacent) > 2:
            raise ValueError(
                "A revolved meridian edge cannot border more than two regions"
            )
        adjacent_names = tuple(
            sorted(
                adjacent,
                key=lambda name: material_order.get(str(name), len(material_order)),
            )
        )
        for step in range(subdivisions):
            first_node = meridian_node(
                vertices, (subdivisions - step, step)
            )
            second_node = meridian_node(
                vertices, (subdivisions - step - 1, step + 1)
            )
            boundary_segments.append((first_node, second_node, adjacent_names))

    estimated_tetrahedra = len(triangles) * sectors * 3
    if estimated_tetrahedra > MAX_TETRAHEDRA:
        if not _allow_coarsen:
            raise ValueError(
                "The meridian mesh is still too large after coarsening for a "
                "responsive 3D revolution."
            )
        from .axisymmetric_preview import coarsen_meridian_field

        max_triangles = MAX_TETRAHEDRA // (sectors * 3)
        coarse_field = coarsen_meridian_field(
            grid_function, max_triangles=max_triangles
        )
        return revolve_gridfunction(
            coarse_field, sectors=sectors, _allow_coarsen=False
        )

    target_ngmesh = ngm.Mesh(dim=3)
    material_ids = {
        str(name): target_ngmesh.AddRegion(str(name), 3) for name in materials
    }
    boundary_descriptors = {}
    default_material_id = next(iter(material_ids.values()))
    for adjacent_names in dict.fromkeys(segment[2] for segment in boundary_segments):
        descriptor_number = len(boundary_descriptors) + 1
        dominant_name = adjacent_names[0]
        dominant_id = material_ids.get(dominant_name, default_material_id)
        exterior_id = (
            material_ids.get(adjacent_names[1], default_material_id)
            if len(adjacent_names) == 2
            else 0
        )
        boundary_descriptors[adjacent_names] = target_ngmesh.Add(
            ngm.FaceDescriptor(
                surfnr=descriptor_number,
                domin=dominant_id,
                domout=exterior_id,
                bc=descriptor_number,
            )
        )
        if exterior_id:
            boundary_name = (
                f"Revolved interface: {dominant_name} / {adjacent_names[1]}"
            )
        else:
            boundary_name = f"Revolved exterior: {dominant_name}"
        target_ngmesh.SetBCName(descriptor_number - 1, boundary_name)

    point_ids: dict[int, list] = {}
    point_coordinates: dict[int, tuple[float, float, float]] = {}
    point_payload: dict[int, tuple[int, int]] = {}
    for meridian_node_id, (radius, axial) in meridian_points.items():
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
            point_payload[point_id.nr] = (meridian_node_id, sector)
        point_ids[meridian_node_id] = ring

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
    for first_node, second_node, adjacent_names in boundary_segments:
        boundary_descriptor = boundary_descriptors[adjacent_names]
        for sector in range(sectors):
            next_sector = (sector + 1) % sectors
            first = point_ids[first_node][sector]
            second = point_ids[second_node][sector]
            second_next = point_ids[second_node][next_sector]
            first_next = point_ids[first_node][next_sector]
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
        meridian_points,
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
    meridian_points,
    axis_tolerance,
    sectors,
):
    """Sample meridian subdivision nodes into first-order 3D H1 fields."""
    import numpy as np

    dtype = complex if source.is_complex else float
    source_values = {}
    for meridian_node_id, (radius, axial) in meridian_points.items():
        value = np.asarray(
            source(source_mesh(float(radius), float(axial))), dtype=dtype
        )
        source_values[meridian_node_id] = value.reshape(-1)

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
            meridian_node_id, sector = point_payload[netgen_vertex_number]
            values = source_values[meridian_node_id]
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
        if abs(meridian_points[meridian_node_id][0]) <= axis_tolerance:
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
