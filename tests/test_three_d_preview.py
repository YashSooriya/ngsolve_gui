from pathlib import Path

import pytest

from ngsolve_gui.three_d_preview import preview_figure, project_preview_payload
from ngsolve_gui.three_d_project import (
    add_step_geometry,
    add_wire_trace,
    detect_step_unit,
    new_3d_project,
    package_3d_project,
    unpack_3d_project,
)


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
