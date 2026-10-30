from pathlib import Path
import tempfile
import time
import json
from unittest.mock import patch

from ngapp.e2e import app_test
from netgen.occ import Box, Pnt
from playwright.sync_api import Page

from ngsolve_gui.axisymmetric_model import builtin_materials
from ngsolve_gui.sections.regions import RegionsSection
from ngsolve_gui.sections.study_info import StudyInfoSection
from ngsolve_gui.three_d_project import add_step_geometry, add_wire_trace, new_3d_project
import ngsolve_gui.solve_workspace as solve_workspace


@app_test("ngsolve_gui.appconfig")
def test_3d_run_button_completes_and_opens_post_process(page: Page, app):
    with tempfile.TemporaryDirectory(prefix="mmfem_3d_run_e2e_") as temporary:
        run_history = Path(temporary) / "runs"
        with patch.object(
            solve_workspace,
            "default_run_history_root",
            lambda: run_history,
        ):
            _exercise_3d_run_handoff(page, app, Path(temporary))


def _exercise_3d_run_handoff(page, app, tmp_path):
    cube_path = tmp_path / "em_cube.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(cube_path))

    project = new_3d_project("3D run handoff check")
    project["study"].update(frequency_hz=50.0, polynomial_order=1, air_padding=2.0)
    materials = {material["name"]: material for material in builtin_materials()}
    project["materials"] = [materials["Copper"], materials["Air"]]
    assets = {}
    geometry = add_step_geometry(
        project, assets, cube_path.name, cube_path.read_bytes(), units="mm", material="Copper"
    )
    geometry["mesh_maxh_mm"] = 10.0
    trace_set = add_wire_trace(
        project,
        assets,
        "coil_trace.txt",
        b"5 -20 5\n5 30 5\n",
        units="mm",
    )
    trace_set["current"] = 1.0
    trace_set["material"] = "Air"

    assert app._start_3d_project_from_menu() is True
    workspace = app._three_d_workspace
    assert workspace.load_project_data(project, assets) is True
    app._update()

    page.get_by_role("button", name="Run study").wait_for(timeout=10_000)
    page.get_by_role("button", name="Run study").click()
    context = app._solver_run_context
    deadline = time.monotonic() + 5
    while context is None and time.monotonic() < deadline:
        page.wait_for_timeout(100)
        context = app._solver_run_context
    assert context and context["project_type"] == "3d", (
        f"3D Run did not start: {workspace._status.ui_children!r}"
    )
    output_dir = Path(context["output_dir"])

    deadline = time.monotonic() + 90
    while app._solver_job_active and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert app._solver_job_active is False

    manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["frequency_hz"] == 50.0
    assert (output_dir / "ngsolve_gui" / "solution.pkl").is_file()
    assert list((output_dir / "ngsolve_gui" / "fields").glob("*.pkl"))

    deadline = time.monotonic() + 30
    while (
        (
            app._workspace_mode != "post_process"
            or not app.app_data.get_tabs()
            or app.tab_panel.comp is None
        )
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert app._workspace_mode == "post_process"
    assert app.app_data.get_tabs()
    assert workspace.ui_hidden is True
    assert app._post_process_workspace.ui_hidden is False
    component = app.tab_panel.comp
    assert component is not None
    assert component.mesh.ne > 0
    assert component.cf.dim == 3
    assert component.run_metadata["frequency_hz"] == 50.0
    assert component.run_metadata["status"] == "complete"
    assert component.field_metadata["field_key"] == "gfAAC"
    alternate_field = next(
        item for item in component.available_result_fields
        if item["field_key"] == "gfBAC"
    )
    assert alternate_field["label"].startswith("AC magnetic flux density [T]")
    assert callable(component._ngsolve_gui_open_result_field)
    assert StudyInfoSection in component.property_sections
    assert RegionsSection in component.property_sections
    assert StudyInfoSection not in component.hidden_sections
    assert RegionsSection not in component.hidden_sections
    assert component.property_xref()["label"] == "Open as mesh"
    assert {str(name) for name in component.mesh.GetMaterials()} >= {"Copper", "Air"}

    component._ngsolve_gui_open_result_field(alternate_field["path"])
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        loaded_component = app.tab_panel.comp
        if (
            loaded_component is not None
            and loaded_component.field_metadata
            and loaded_component.field_metadata.get("field_key") == "gfBAC"
        ):
            break
        page.wait_for_timeout(100)
    assert app.tab_panel.comp.field_metadata["field_key"] == "gfBAC"

    app._workspace_mode_toggle.select("solve")
    assert workspace.ui_hidden is False
    assert app._post_process_workspace.ui_hidden is True
