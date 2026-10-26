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
    assert max(point[0] for point in surface["vertices"]) == pytest.approx(0.243, abs=1e-3)
    assert max(point[2] for point in surface["vertices"]) == pytest.approx(0.25, abs=1e-3)
    assert len(figure.data) == 2
    assert figure.data[0].type == "mesh3d"
    assert figure.data[1].type == "scatter3d"
