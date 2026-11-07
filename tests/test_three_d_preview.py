from pathlib import Path

import pytest

from ngsolve_gui.three_d_preview import (
    axisymmetric_preview_figure,
    boundary_overlay_trace,
    preview_figure,
    project_preview_payload,
    tessellate_occ_feature_edges,
    tessellate_occ_shape,
)
from ngsolve_gui.three_d_project import (
    add_step_geometry,
    add_wire_trace,
    detect_step_unit,
    new_3d_project,
    package_3d_project,
    unpack_3d_project,
)


def test_axisymmetric_occ_preview_tessellation_preserves_dimensions_and_edges():
    import numpy as np
    from netgen.occ import Axis, Circle, Compound, Dir, Face, Pnt, Revolve, Segment, Vec, Wire

    profile = Face(Wire(Circle(Pnt(0.035, 0.0, 0.0), Dir(0.0, 1.0, 0.0), 0.015)))
    solid = Revolve(profile, Axis(Pnt(0.0, 0.0, 0.0), Vec(0.0, 0.0, 1.0)), 360.0)
    mesh = tessellate_occ_shape(solid)
    edge = Segment(Pnt(0.02, 0.0, -0.1), Pnt(0.05, 0.0, -0.1))
    feature_edges = tessellate_occ_feature_edges(Compound([edge]))

    assert mesh["vertices"].ndim == 2 and mesh["vertices"].shape[1] == 3
    assert mesh["triangles"].ndim == 2 and mesh["triangles"].shape[1] == 3
    assert np.isfinite(mesh["vertices"]).all()
    assert float(mesh["vertices"][:, 2].max() - mesh["vertices"][:, 2].min()) == pytest.approx(
        0.03, rel=0.03
    )
    assert feature_edges.ndim == 3 and feature_edges.shape[1:] == (2, 3)
    assert len(feature_edges) > 0

    figure = axisymmetric_preview_figure([{
        "id": "test-region",
        "name": "Torus",
        "color": "#6886ac",
        **mesh,
        "feature_edges": feature_edges,
    }])
    assert [trace.type for trace in figure.data] == ["mesh3d", "scatter3d"]
    assert figure.layout.scene.aspectmode == "data"
    assert figure.layout.scene.dragmode == "orbit"


def test_realistic_magnet_shield_step_imports_and_combines_with_golay_trace():
    fixtures = Path(__file__).parent / "fixtures"
    step = fixtures / "magnet_shield_4k.step"
    trace = fixtures / "golay_x_trace.txt"

    step_data = step.read_bytes()
    assert detect_step_unit(step_data) == "mm"
    project = new_3d_project("Shield test")
    assets = {}
    add_step_geometry(project, assets, step.name, step_data, units="mm", material="4K_Shield")
    add_wire_trace(project, assets, trace.name, trace.read_bytes())
    restored, restored_assets = unpack_3d_project(package_3d_project(project, assets))
    payload = project_preview_payload(restored, restored_assets)
    surface = payload["geometry"][0]
    figure = preview_figure(payload)

    assert len(surface["vertices"]) > 0
    assert len(surface["triangles"]) > 0
    assert surface["vertices"][:, 0].max() == pytest.approx(0.88204, abs=1e-3)
    assert surface["vertices"][:, 2].max() == pytest.approx(0.6579, abs=1e-3)
    assert len(surface["vertices"]) < len(surface["triangles"]) * 3
    assert surface["id"] == restored["geometries"][0]["id"]
    assert payload["traces"][0]["trace_set_id"] == restored["wire_trace_sets"][0]["id"]
    assert len(figure.data) == 2
    assert figure.data[0].type == "mesh3d"
    assert figure.data[0].meta["mmfem_kind"] == "geometry"
    assert figure.data[0].meta["mmfem_item_id"] == surface["id"]
    assert figure.data[1].type == "scatter3d"
    assert figure.data[1].meta["mmfem_kind"] == "trace_set"
    assert figure.data[1].meta["mmfem_item_id"] == payload["traces"][0]["trace_set_id"]


def test_golay_conductors_fit_inside_the_realistic_shield_bore():
    import math
    import runpy
    from netgen.occ import Cylinder, Z
    from ngsolve_gui.three_d_project import parse_wire_trace

    fixtures = Path(__file__).parent / "fixtures"
    sample = runpy.run_path(str(fixtures / "realistic_sample.py"))
    shield = sample["realistic_shield"]()
    assert len(shield.solids) == 1
    assert shield.mass > 0
    for axis in ("x", "y"):
        points = [point for point in parse_wire_trace((fixtures / f"golay_{axis}_trace.txt").read_bytes(), "m") if point is not None]
        radius = max(math.hypot(point[0], point[1]) for point in points) + sample["WIRE_RADIUS_M"]
        half_length = max(abs(point[2]) for point in points) + sample["WIRE_RADIUS_M"]
        assert radius <= sample["BORE_RADIUS_M"] - sample["CLEARANCE_M"] + 1e-12
        assert half_length <= sample["BORE_HALF_LENGTH_M"] - sample["CLEARANCE_M"] + 1e-12
        envelope = Cylinder((0, 0, -half_length), Z, r=radius, h=2 * half_length)
        assert (shield * envelope).mass < 1e-12


def test_generated_volume_mesh_uses_boundary_facets_and_edge_trace():
    import numpy as np

    figure = preview_figure({
        "geometry": [{
            "id": "geometry-1",
            "name": "box.step",
            "vertices": np.asarray([
                [0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1],
            ]),
            "triangles": np.asarray([[0, 1, 2], [0, 1, 3]]),
            "edges": np.asarray([[0, 1], [0, 2], [0, 3]]),
            "generated_mesh": True,
        }],
        "traces": [],
    })

    assert len(figure.data) == 2
    assert figure.data[0].type == "mesh3d"
    assert figure.data[0].name == "box.step volume mesh"
    assert figure.data[0].meta["mmfem_generated_mesh"] is True
    assert figure.data[1].type == "scatter3d"
    assert figure.data[1].meta["mmfem_item_id"] == "geometry-1"
    assert figure.data[1].meta["mmfem_mesh_edges"] is True
    assert list(figure.data[1].x) == [
        0.0, 1.0, None, 0.0, 0.0, None, 0.0, 0.0, None
    ]


def test_selected_mesh_boundary_draws_an_outline_overlay_without_changing_the_surface():
    figure = preview_figure({
        "geometry": [{
            "id": "geometry-1",
            "name": "box.step",
            "vertices": [
                [0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0],
            ],
            "triangles": [[0, 1, 2], [1, 3, 2]],
            "edges": [],
            "generated_mesh": True,
            "boundary_region_ids": [4, 4],
            "highlighted_boundary_ids": [4],
        }],
        "traces": [],
    })

    assert len(figure.data) == 2
    assert figure.data[0].meta["mmfem_generated_mesh"] is True
    assert figure.data[0].color == "#6886ac"
    assert figure.data[1].meta["mmfem_boundary_overlay"] is True
    assert figure.data[1].line.color == "#f59e0b"
    assert len(figure.data[1].x) == 12
    assert figure.data[1].visible is True


def test_closed_solid_selection_keeps_each_cad_face_outline_visible():
    import numpy as np

    vertices = np.asarray([
        [0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1],
    ], dtype=np.float32)
    triangles = np.asarray([
        [0, 2, 1], [0, 1, 3], [1, 2, 3], [2, 0, 3],
    ], dtype=np.int32)
    boundary_region_ids = np.asarray([0, 1, 2, 3], dtype=np.int32)

    outline = boundary_overlay_trace(
        vertices,
        triangles,
        boundary_region_ids,
        [0, 1, 2, 3],
        geometry_id="geometry-1",
        name="tetrahedron",
        outline_each_region=True,
    )

    assert outline.visible is True
    assert len(outline.x) > 0
    assert outline.line.color == "#f59e0b"


def test_generated_mesh_keeps_an_empty_boundary_overlay_trace():
    import numpy as np

    figure = preview_figure({
        "geometry": [{
            "id": "geometry-1",
            "name": "box.step",
            "vertices": np.asarray([
                [0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0],
            ]),
            "triangles": np.asarray([[0, 1, 2], [1, 3, 2]]),
            "edges": np.empty((0, 2), dtype=np.int32),
            "generated_mesh": True,
            "boundary_region_ids": [4, 4],
            "highlighted_boundary_ids": [],
        }],
        "traces": [],
    })

    overlay = figure.data[1]
    assert overlay.meta["mmfem_boundary_overlay"] is True
    assert list(overlay.x) == []
    assert overlay.visible is False


def test_solid_visibility_filters_display_facets_and_pick_regions():
    import numpy as np

    figure = preview_figure({
        "geometry": [{
            "id": "geometry-1",
            "name": "two_body.step",
            "vertices": np.asarray([
                [0, 0, 0], [1, 0, 0], [0, 1, 0],
                [2, 0, 0], [3, 0, 0], [2, 1, 0],
            ]),
            "triangles": np.asarray([[0, 1, 2], [3, 4, 5]]),
            "edges": np.asarray([
                [0, 1], [0, 2], [1, 2], [3, 4], [3, 5], [4, 5],
            ]),
            "generated_mesh": True,
            "boundary_region_ids": [10, 20],
            "boundary_domain_tags": ["solid-a", "solid-b"],
            "hidden_solid_tags": ["solid-b"],
            "highlighted_boundary_ids": [10],
        }],
        "traces": [],
    })

    surface = figure.data[0]
    edges = figure.data[1]
    assert list(surface.i) == [0]
    assert list(surface.j) == [1]
    assert list(surface.k) == [2]
    assert surface.meta["mmfem_visible_boundary_region_ids"] == [10]
    assert set(value for value in edges.x if value is not None) <= {0.0, 1.0}


def test_boundary_overlay_uses_cached_edges_without_rebuilding_region_index(monkeypatch):
    import numpy as np
    import ngsolve_gui.three_d_preview as preview

    vertices = np.asarray([
        [0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0],
    ], dtype=np.float32)
    triangles = np.asarray([[0, 1, 2], [1, 3, 2]], dtype=np.int32)
    region_ids = np.asarray([4, 4], dtype=np.int32)
    edge_cache = preview.boundary_overlay_edge_cache(triangles, region_ids)
    expected = preview.boundary_overlay_trace(
        vertices, triangles, region_ids, [4], geometry_id="g", name="box"
    )

    def unexpected_cache_rebuild(*_args, **_kwargs):
        raise AssertionError("Cached boundary edges should be reused")

    monkeypatch.setattr(preview, "boundary_overlay_edge_cache", unexpected_cache_rebuild)
    actual = preview.boundary_overlay_trace(
        vertices, triangles, region_ids, [4], geometry_id="g", name="box",
        edge_cache=edge_cache,
    )

    assert list(actual.x) == list(expected.x)
    assert list(actual.y) == list(expected.y)
    assert list(actual.z) == list(expected.z)
