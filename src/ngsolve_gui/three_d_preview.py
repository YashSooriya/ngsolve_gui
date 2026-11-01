"""Coarse, display-only tessellation for combined 3D project previews."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from .three_d_project import LENGTH_UNITS, parse_wire_trace, step_unit_scale


_MATERIAL_COLORS = ("#6886ac", "#d58651", "#69a77b", "#ae8bb7", "#d1b44f", "#4cabb0")
_MAX_SURFACE_TRIANGLES = 80_000
_MAX_TRACE_POINTS = 20_000


def tessellate_step(data, *, units="m", max_triangles=_MAX_SURFACE_TRIANGLES):
    """Return OCC's display tessellation, never a solver mesh."""
    if units not in LENGTH_UNITS:
        raise ValueError(f"Unsupported STEP length unit: {units!r}.")
    try:
        import numpy as np
        from netgen.occ import OCCGeometry
    except ImportError as error:
        raise RuntimeError("STEP preview requires the Netgen/NGSolve runtime.") from error

    scale = step_unit_scale(data, units=units)
    with tempfile.TemporaryDirectory(prefix="mmfem_step_preview_") as directory:
        source = Path(directory) / "geometry.step"
        source.write_bytes(bytes(data))
        imported = OCCGeometry(str(source))
        # Use the same OCC triangulation that ngsolve_webgpu uses for its
        # GeometryRenderer. It handles thin shells without creating a volume mesh.
        visual = imported._visualizationData()
        raw_vertices = visual.get("vertices")
        if raw_vertices is None or not len(raw_vertices) or len(raw_vertices) % 9:
            raise ValueError("The STEP file did not produce a valid surface for preview.")
        vertices = np.asarray(raw_vertices, dtype=np.float64).reshape((-1, 3))
        triangle_count = len(vertices) // 3
        if triangle_count > max_triangles:
            raise ValueError(
                f"The STEP preview is too detailed ({triangle_count:,} surface triangles). "
                "Simplify the CAD geometry before importing it."
            )
        vertices *= scale
        # OCC repeats every triangle's three corners. Share identical corners
        # before transmission; this changes no surface positions or triangles.
        vertices, inverse = np.unique(vertices, axis=0, return_inverse=True)
        triangles = inverse.astype(np.int32).reshape((-1, 3))
        vertices = vertices.astype(np.float32)
    return {"vertices": vertices, "triangles": triangles}


def project_preview_payload(project, assets):
    geometry = []
    for item in project.get("geometries", []):
        geometry.append({
            "id": item["id"],
            "name": item["name"],
            "material": item.get("material", "Shield"),
            **tessellate_step(
                assets[item["asset"]], units=item.get("units", "m")
            ),
        })
    traces = []
    for trace_set in project.get("wire_trace_sets", []):
        for trace in trace_set.get("traces", []):
            points = parse_wire_trace(assets[trace["filename"]], "m")
            points = _sample_trace_points(points, _MAX_TRACE_POINTS)
            traces.append({
                "trace_set_id": trace_set["id"],
                "name": trace.get("name", trace_set.get("name", "Wire trace")),
                "material": trace.get("material", trace_set.get("material", "Air")),
                "points": points,
            })
    return {"geometry": geometry, "traces": traces}


def _sample_trace_points(points, limit):
    if sum(point is not None for point in points) <= limit:
        return points
    segments = []
    current = []
    for point in points:
        if point is None:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(point)
    if current:
        segments.append(current)
    stride = max(1, math.ceil(sum(map(len, segments)) / max(1, limit - len(segments))))
    sampled = []
    for segment in segments:
        reduced = segment[::stride]
        if reduced[-1] != segment[-1]:
            reduced.append(segment[-1])
        if sampled:
            sampled.append(None)
        sampled.extend(reduced)
    return sampled


def boundary_overlay_edge_cache(triangles, boundary_region_ids):
    """Index unique surface edges by CAD region for quick repeated selection."""
    import numpy as np

    triangles = np.asarray(triangles, dtype=np.int32)
    region_ids = np.asarray(boundary_region_ids, dtype=np.int32)
    if triangles.ndim != 2 or triangles.shape[1] != 3 or len(region_ids) != len(triangles):
        return {}
    order = np.argsort(region_ids, kind="stable")
    sorted_region_ids = region_ids[order]
    if not len(sorted_region_ids):
        return {}
    split_points = np.flatnonzero(np.diff(sorted_region_ids)) + 1
    boundaries = np.concatenate(([0], split_points, [len(sorted_region_ids)]))
    cache = {}
    for start, end in zip(boundaries[:-1], boundaries[1:]):
        region_id = int(sorted_region_ids[start])
        selected_triangles = triangles[order[start:end]]
        edges = np.concatenate((
            selected_triangles[:, [0, 1]],
            selected_triangles[:, [1, 2]],
            selected_triangles[:, [2, 0]],
        ), axis=0)
        edges.sort(axis=1)
        unique_edges, edge_counts = np.unique(edges, axis=0, return_counts=True)
        cache[region_id] = (unique_edges, edge_counts.astype(np.int32, copy=False))
    return cache


def boundary_overlay_trace(
    vertices, triangles, boundary_region_ids, selected_region_ids, *, geometry_id, name,
    edge_cache=None,
):
    """Return a lightweight line trace outlining the selected mesh boundaries."""
    import numpy as np
    import plotly.graph_objects as go

    vertices = np.asarray(vertices, dtype=np.float64)
    triangles = np.asarray(triangles, dtype=np.int32)
    region_ids = np.asarray(boundary_region_ids, dtype=np.int32)
    selected_ids = {int(region_id) for region_id in selected_region_ids}
    edge_x, edge_y, edge_z = [], [], []
    if selected_ids and len(region_ids) == len(triangles):
        edge_cache = edge_cache or boundary_overlay_edge_cache(triangles, region_ids)
        selected_edges = [edge_cache[region_id] for region_id in selected_ids if region_id in edge_cache]
        if selected_edges:
            joined_edges = np.concatenate([edges for edges, _counts in selected_edges], axis=0)
            joined_counts = np.concatenate([counts for _edges, counts in selected_edges])
            unique_edges, inverse = np.unique(joined_edges, axis=0, return_inverse=True)
            edge_counts = np.bincount(
                inverse.reshape(-1), weights=joined_counts, minlength=len(unique_edges)
            )
            outline = unique_edges[edge_counts == 1]
            if len(outline):
                edge_coordinates = vertices[outline]
                coordinates = np.empty((len(outline), 3, 3), dtype=object)
                coordinates[:, 0, :] = edge_coordinates[:, 0, :]
                coordinates[:, 1, :] = edge_coordinates[:, 1, :]
                coordinates[:, 2, :] = None
                flattened = coordinates.reshape((-1, 3))
                edge_x, edge_y, edge_z = flattened[:, 0].tolist(), flattened[:, 1].tolist(), flattened[:, 2].tolist()

    return go.Scatter3d(
        x=edge_x,
        y=edge_y,
        z=edge_z,
        mode="lines",
        name=f"{name} selected boundary",
        line={"color": "#f59e0b", "width": 4},
        hoverinfo="skip",
        showlegend=False,
        visible=bool(edge_x),
        meta={
            "mmfem_kind": "boundary_overlay",
            "mmfem_item_id": geometry_id,
            "mmfem_base_color": "#f59e0b",
            "mmfem_boundary_overlay": True,
        },
    )


def preview_figure(payload):
    """Create a combined Plotly scene from display mesh and sampled wire paths."""
    try:
        import numpy as np
        import plotly.graph_objects as go
    except ImportError as error:
        raise RuntimeError("The 3D project preview requires Plotly.") from error

    figure = go.Figure()
    for index, item in enumerate(payload.get("geometry", [])):
        vertices = np.asarray(item["vertices"])
        triangles = np.asarray(item["triangles"])
        if vertices.size == 0 or triangles.size == 0:
            continue
        base_color = _MATERIAL_COLORS[index % len(_MATERIAL_COLORS)]
        mesh_trace = go.Mesh3d(
            # Send ordinary lists through ngapp's websocket bridge. NumPy arrays
            # are accepted by Plotly in Python but are not reliably marshalled
            # to the browser component by the app's cross-process serializer.
            x=vertices[:, 0].tolist(),
            y=vertices[:, 1].tolist(),
            z=vertices[:, 2].tolist(),
            i=triangles[:, 0].tolist(),
            j=triangles[:, 1].tolist(),
            k=triangles[:, 2].tolist(),
            name=(
                f"{item.get('name', 'Geometry')} volume mesh"
                if item.get("generated_mesh") else item.get("name", "Geometry")
            ),
            color=base_color,
            opacity=1.0,
            flatshading=True,
            hoverinfo="name",
            showscale=False,
            meta={
                "mmfem_kind": "geometry",
                "mmfem_item_id": item.get("id"),
                "mmfem_base_color": base_color,
                "mmfem_generated_mesh": bool(item.get("generated_mesh")),
            },
        )
        figure.add_trace(mesh_trace)
        edges = item.get("edges")
        if item.get("generated_mesh") and edges is not None and len(edges):
            edge_coordinates = vertices[np.asarray(edges, dtype=np.int32)]
            edge_x = []
            edge_y = []
            edge_z = []
            for start, end in edge_coordinates:
                edge_x.extend((float(start[0]), float(end[0]), None))
                edge_y.extend((float(start[1]), float(end[1]), None))
                edge_z.extend((float(start[2]), float(end[2]), None))
            figure.add_trace(go.Scatter3d(
                x=edge_x,
                y=edge_y,
                z=edge_z,
                mode="lines",
                name=f"{item.get('name', 'Geometry')} mesh edges",
                line={"color": "#33475b", "width": 1},
                hoverinfo="skip",
                showlegend=False,
                meta={
                    "mmfem_kind": "geometry",
                    "mmfem_item_id": item.get("id"),
                    "mmfem_base_color": "#33475b",
                    "mmfem_mesh_edges": True,
                },
            ))
        boundary_region_ids = item.get("boundary_region_ids")
        if item.get("generated_mesh") and boundary_region_ids is not None:
            figure.add_trace(boundary_overlay_trace(
                vertices,
                triangles,
                boundary_region_ids,
                item.get("highlighted_boundary_ids", []),
                geometry_id=item.get("id"),
                name=item.get("name", "Geometry"),
            ))
    for index, item in enumerate(payload.get("traces", [])):
        points = item["points"]
        figure.add_trace(go.Scatter3d(
            x=[None if point is None else point[0] for point in points],
            y=[None if point is None else point[1] for point in points],
            z=[None if point is None else point[2] for point in points],
            mode="lines",
            name=item.get("name", "Wire trace"),
            line={"color": _MATERIAL_COLORS[(index + 2) % len(_MATERIAL_COLORS)], "width": 5},
            hovertemplate="x=%{x:.5g} m<br>y=%{y:.5g} m<br>z=%{z:.5g} m<extra>%{fullData.name}</extra>",
            meta={
                "mmfem_kind": "trace_set",
                "mmfem_item_id": item.get("trace_set_id"),
                "mmfem_base_color": _MATERIAL_COLORS[(index + 2) % len(_MATERIAL_COLORS)],
            },
        ))
    figure.update_layout(
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        showlegend=True,
        uirevision="mmfem-3d-camera",
        legend={"orientation": "h", "y": 1.02, "x": 0.02},
        scene={
            "aspectmode": "data",
            "dragmode": "orbit",
            "xaxis_title": "X (m)",
            "yaxis_title": "Y (m)",
            "zaxis_title": "Z (m)",
            "bgcolor": "rgba(0,0,0,0)",
        },
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return figure
