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
        figure.add_trace(go.Mesh3d(
            # Send ordinary lists through ngapp's websocket bridge. NumPy arrays
            # are accepted by Plotly in Python but are not reliably marshalled
            # to the browser component by the app's cross-process serializer.
            x=vertices[:, 0].tolist(),
            y=vertices[:, 1].tolist(),
            z=vertices[:, 2].tolist(),
            i=triangles[:, 0].tolist(),
            j=triangles[:, 1].tolist(),
            k=triangles[:, 2].tolist(),
            name=item.get("name", "Geometry"),
            color=_MATERIAL_COLORS[index % len(_MATERIAL_COLORS)],
            opacity=1.0,
            flatshading=True,
            hoverinfo="name",
            showscale=False,
            meta={
                "mmfem_kind": "geometry",
                "mmfem_item_id": item.get("id"),
                "mmfem_base_color": _MATERIAL_COLORS[index % len(_MATERIAL_COLORS)],
            },
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
