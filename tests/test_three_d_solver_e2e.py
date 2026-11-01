from pathlib import Path
import os
import math
import pickle
import tempfile
import time
import json
from unittest.mock import patch

from ngapp.e2e import app_test
from netgen.occ import Box, Pnt
from playwright.sync_api import Page
import pytest

from ngsolve_gui.axisymmetric_model import builtin_materials
from ngsolve_gui.sections.regions import RegionsSection
from ngsolve_gui.sections.study_info import StudyInfoSection
from ngsolve_gui.three_d_project import add_step_geometry, add_wire_trace, new_3d_project
import ngsolve_gui.solve_workspace as solve_workspace
from .solver_test_support import SOLVER_UNAVAILABLE_REASON, solver_checkout_available


def _wait_for_result_load(page: Page, app, generation_before: int) -> None:
    """Wait until a newly opened field has finished reading its source file."""
    footer = app.footer
    deadline = time.monotonic() + 30
    load_thread = None
    done_event = None
    while footer._generation <= generation_before and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert footer._generation > generation_before, "The run result did not start loading."
    while footer._idle.ui_hidden and time.monotonic() < deadline:
        load_thread = load_thread or footer._thread
        done_event = done_event or footer._done_event
        page.wait_for_timeout(50)
    assert footer._idle.ui_hidden is False, "The run result did not finish loading."
    if done_event is not None:
        while not done_event.is_set() and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert done_event.is_set(), "The result loader did not signal completion."
    if load_thread is not None:
        load_thread.join(timeout=2)
        assert not load_thread.is_alive(), "The result loader thread remained active."


@app_test("ngsolve_gui.appconfig")
@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_3d_run_button_completes_and_opens_post_process(page: Page, app):
    with tempfile.TemporaryDirectory(prefix="mmfem_3d_run_e2e_") as temporary:
        evidence_dir = os.environ.get("MMFEM_STAGE8A_EVIDENCE_DIR")
        run_history = (
            Path(evidence_dir) / "ui-3d-run"
            if evidence_dir
            else Path(temporary) / "runs"
        )
        with patch.object(
            solve_workspace,
            "default_run_history_root",
            lambda: run_history,
        ):
            _exercise_3d_run_handoff(page, app, Path(temporary))


def _exercise_3d_run_handoff(page, app, tmp_path):
    cube_path = tmp_path / "em_cube.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(cube_path))
    trace_path = tmp_path / "coil_trace.txt"
    trace_path.write_text(
        "0.005 -0.02 0.005\n0.005 0.03 0.005\n",
        encoding="utf-8",
    )

    assert app._start_3d_project_from_menu() is True
    workspace = app._three_d_workspace
    project_name = page.get_by_label("Project name")
    project_name.fill("Stage 8A 3D harmonic wire benchmark")
    project_name.press("Tab")
    deadline = time.monotonic() + 5
    while (
        workspace.project["name"] != "Stage 8A 3D harmonic wire benchmark"
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert workspace.project["name"] == "Stage 8A 3D harmonic wire benchmark"

    with patch.object(
        app,
        "_pick_artifact",
        side_effect=[
            (str(cube_path), cube_path.read_bytes()),
            (str(trace_path), trace_path.read_bytes()),
        ],
    ):
        page.get_by_label("STEP units").click()
        page.get_by_text("Millimetres", exact=True).last.click()
        page.get_by_role("button", name="Import STEP", exact=True).click()
        deadline = time.monotonic() + 10
        while not workspace.project["geometries"] and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert workspace.project["geometries"]
        page.get_by_role("button", name="Import wire traces", exact=True).click()
        deadline = time.monotonic() + 10
        while not workspace.project["wire_trace_sets"] and time.monotonic() < deadline:
            page.wait_for_timeout(100)
        assert workspace.project["wire_trace_sets"]
    app._update()

    geometry = workspace.project["geometries"][0]
    workspace.select_item("geometry", geometry["id"])
    app._update()
    assert geometry["units"] == "mm"
    assert geometry["material"] == "Unassigned"
    run_button = page.get_by_role("button", name="Run study")
    deadline = time.monotonic() + 10
    while not run_button.is_disabled() and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert run_button.is_disabled()

    page.get_by_label("Default material").click()
    page.get_by_text("Copper", exact=True).last.click()
    deadline = time.monotonic() + 5
    while geometry["material"] != "Copper" and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert geometry["material"] == "Copper"
    page.get_by_text(
        "Material data is embedded in the project.", exact=True
    ).wait_for(timeout=5_000)
    deadline = time.monotonic() + 10
    while run_button.is_disabled() and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert not run_button.is_disabled()
    workspace.select_item("geometry", geometry["id"])
    app._update()
    page.get_by_label("Maximum element size (mm)").wait_for(timeout=10_000)
    page.wait_for_timeout(250)

    mesh_size = page.get_by_label("Maximum element size (mm)")
    mesh_size.fill("5")
    mesh_size.press("Tab")
    deadline = time.monotonic() + 5
    while geometry.get("mesh_maxh_mm") != 5.0 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert geometry["mesh_maxh_mm"] == 5.0

    page.get_by_role("button", name="Generate mesh", exact=True).click()
    deadline = time.monotonic() + 60
    while workspace._mesh_worker_active and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    page.wait_for_function(
        """() => {
            const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
            return graph && graph.data && graph.data.some(
                trace => trace.meta && trace.meta.mmfem_generated_mesh
            );
        }""",
        timeout=60_000,
    )
    assert workspace._mesh_results[geometry["id"]]["status"] == "ready"
    assert workspace._mesh_results[geometry["id"]]["volume_elements"] > 0

    workspace.select_item("study")
    app._update()
    frequency = page.get_by_label("Frequency (Hz)")
    frequency.fill("63")
    frequency.press("Tab")
    deadline = time.monotonic() + 5
    while workspace.project["study"]["frequency_hz"] != 63.0 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert workspace.project["study"]["frequency_hz"] == 63.0

    page.get_by_label("Finite-element order").click()
    page.get_by_text("2 · quadratic", exact=True).last.click()
    deadline = time.monotonic() + 5
    while workspace.project["study"]["polynomial_order"] != 2 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert workspace.project["study"]["polynomial_order"] == 2

    air_padding = page.get_by_label("Air-region padding (model spans)")
    air_padding.fill("1.5")
    air_padding.press("Tab")
    deadline = time.monotonic() + 5
    while workspace.project["study"]["air_padding"] != 1.5 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert workspace.project["study"]["air_padding"] == 1.5
    page.keyboard.press("Escape")
    page.get_by_text("Time-harmonic electromagnetic study", exact=True).click()
    page.wait_for_timeout(500)
    screenshot_dir = os.environ.get("MMFEM_UI_SCREENSHOT_DIR")
    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "05-3d-study-settings-entered-in-ui.png"),
            full_page=True,
        )

    page.get_by_role("button", name="Run study").wait_for(timeout=10_000)
    first_load_generation = app.footer._generation
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
    _wait_for_result_load(page, app, first_load_generation)

    manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["frequency_hz"] == 63.0
    assert manifest["polynomial_order"] == 2
    assert manifest["air_padding"] == 1.5
    diagnostics = manifest["diagnostics"]
    process_context = json.loads(
        (output_dir / diagnostics["process_context"]).read_text(encoding="utf-8")
    )
    assert process_context["run_kind"] == "study"
    assert process_context["native_modules"]["ngsolve"]["file"]
    assert (output_dir / diagnostics["native_crash_log"]).is_file()
    assert (output_dir / "ngsolve_gui" / "solution.pkl").is_file()
    field_files = list((output_dir / "ngsolve_gui" / "fields").glob("*.pkl"))
    assert field_files
    import ngsolve

    solver_mesh = ngsolve.Mesh(str(output_dir / "mesh.vol"))
    flux_density_path = next(path for path in field_files if "gfBAC" in path.name)
    with flux_density_path.open("rb") as stream:
        flux_density = pickle.load(stream)
    sample_point_m = (0.02, 0.02, 0.02)
    flux_density_value = flux_density(solver_mesh(*sample_point_m))
    flux_density_magnitude = math.sqrt(sum(abs(value) ** 2 for value in flux_density_value))
    assert math.isfinite(flux_density_magnitude) and flux_density_magnitude > 1e-9
    evidence_dir = os.environ.get("MMFEM_STAGE8A_EVIDENCE_DIR")
    if evidence_dir:
        (Path(evidence_dir) / "ui-3d-field-check.json").write_text(
            json.dumps({
                "frequency_hz": 63.0,
                "polynomial_order": 2,
                "air_padding": 1.5,
                "mesh_elements": solver_mesh.ne,
                "sample_point_m": sample_point_m,
                "AC_flux_density_magnitude_T": flux_density_magnitude,
                "field_file": str(flux_density_path.relative_to(output_dir)).replace("\\", "/"),
            }, indent=2),
            encoding="utf-8",
        )

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
    assert component.run_metadata["frequency_hz"] == 63.0
    assert component.run_metadata["polynomial_order"] == 2
    assert component.run_metadata["air_padding"] == 1.5
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

    run_tree_entry = next(
        item for item in app.navigator._run_history
        if Path(item["output_path"]) == output_dir
    )
    assert {item["kind"] for item in run_tree_entry["results"]} == {"field", "mesh"}
    screenshot_dir = os.environ.get("MMFEM_UI_SCREENSHOT_DIR")

    alternate_load_generation = app.footer._generation
    page.locator(".mmfem-run-result-entry").filter(
        has_text=alternate_field["label"]
    ).click()
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
    _wait_for_result_load(page, app, alternate_load_generation)
    if screenshot_dir:
        if not app._prop_visible:
            app._toggle_property_panel()
        page.wait_for_function(
            """() => [...document.querySelectorAll('*')].some(element => {
                if (element.textContent.trim().toLowerCase() !== 'study information') {
                    return false;
                }
                const bounds = element.getBoundingClientRect();
                return bounds.width > 0 && bounds.height > 0;
            })""",
            timeout=5_000,
        )
        property_panel = app.property_host._panels.get(app.tab_panel.comp)
        assert property_panel is not None and property_panel.ui_hidden is False
        assert app._inner_splitter.ui_model_value > 0
        property_title_bounds = page.evaluate(
            """() => {
                const matches = [...document.querySelectorAll('*')]
                    .filter(element =>
                        element.textContent.trim().toLowerCase() === 'study information'
                    )
                    .map(element => element.getBoundingClientRect())
                    .filter(bounds => bounds.width > 0 && bounds.height > 0)
                    .sort((left, right) => left.x - right.x);
                const bounds = matches.at(-1);
                return bounds ? {x: bounds.x, y: bounds.y, width: bounds.width} : null;
            }"""
        )
        assert property_title_bounds and property_title_bounds["x"] > 700
        page.get_by_role("button", name="Fit view").click()
        page.wait_for_timeout(1500)
        page.screenshot(
            path=str(Path(screenshot_dir) / "06-3d-alternate-field.png"),
            full_page=True,
        )

    page.get_by_role("button", name="Generated mesh", exact=True).click()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        loaded_component = app.tab_panel.comp
        if loaded_component is not None and type(loaded_component).__name__ == "MeshComponent":
            break
        page.wait_for_timeout(100)
    assert type(app.tab_panel.comp).__name__ == "MeshComponent"
    assert app.tab_panel.comp.mesh.ne > 0
    page.wait_for_timeout(1_500)

    # Repeat the same UI-configured run in the same application session. This
    # verifies the completed job releases its controls/process state for the
    # next simulation and that each run keeps an independent terminal manifest.
    second_load_generation = app.footer._generation
    assert app.run_3d_study(workspace) is True
    second_context = app._solver_run_context
    deadline = time.monotonic() + 5
    while second_context is None and time.monotonic() < deadline:
        page.wait_for_timeout(100)
        second_context = app._solver_run_context
    assert second_context and second_context["project_type"] == "3d"
    second_output_dir = Path(second_context["output_dir"])
    deadline = time.monotonic() + 90
    while app._solver_job_active and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert app._solver_job_active is False
    _wait_for_result_load(page, app, second_load_generation)
    second_manifest = json.loads(
        (second_output_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert second_output_dir != output_dir
    assert second_manifest["status"] == "complete"
    assert second_manifest["frequency_hz"] == manifest["frequency_hz"]
    assert second_manifest["polynomial_order"] == manifest["polynomial_order"]
    assert second_manifest["air_padding"] == manifest["air_padding"]
    assert (second_output_dir / "ngsolve_gui" / "solution.pkl").is_file()
    if evidence_dir:
        second_context_data = json.loads(
            (second_output_dir / second_manifest["diagnostics"]["process_context"])
            .read_text(encoding="utf-8")
        )
        (Path(evidence_dir) / "ui-3d-consecutive-runs.json").write_text(
            json.dumps({
                "first_pid": process_context["pid"],
                "second_pid": second_context_data["pid"],
                "first_status": manifest["status"],
                "second_status": second_manifest["status"],
                "configuration_propagated": all(
                    second_manifest[key] == manifest[key]
                    for key in ("frequency_hz", "polynomial_order", "air_padding")
                ),
            }, indent=2),
            encoding="utf-8",
        )


@app_test("ngsolve_gui.appconfig")
def test_3d_failed_process_finishes_with_actionable_diagnostics(page: Page, app):
    with tempfile.TemporaryDirectory(prefix="mmfem_3d_failure_e2e_") as temporary:
        root = Path(temporary)
        fake_solver_root = root / "solver"
        runner = fake_solver_root / "scripts" / "run_gui_3d_model.py"
        runner.parent.mkdir(parents=True)
        started_marker = root / "failure-solver-started"
        release_marker = root / "release-failure-solver"
        runner.write_text(
            "from pathlib import Path\n"
            "import time\n"
            f"started = Path({str(started_marker)!r})\n"
            f"release = Path({str(release_marker)!r})\n"
            "started.write_text('started', encoding='utf-8')\n"
            "deadline = time.monotonic() + 15\n"
            "while not release.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.02)\n"
            "if not release.exists():\n"
            "    raise SystemExit(8)\n"
            "print('intentional solver failure', flush=True)\n"
            "raise SystemExit(7)\n",
            encoding="utf-8",
        )
        evidence_dir = os.environ.get("MMFEM_STAGE8A_EVIDENCE_DIR")
        run_history = (
            Path(evidence_dir) / "ui-3d-failure"
            if evidence_dir
            else root / "runs"
        )
        cube_path = root / "em_cube.step"
        Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(cube_path))
        project = new_3d_project("3D failure diagnostic check")
        materials = {material["name"]: material for material in builtin_materials()}
        project["materials"] = [materials["Copper"], materials["Air"]]
        assets = {}
        add_step_geometry(
            project,
            assets,
            cube_path.name,
            cube_path.read_bytes(),
            units="mm",
            material="Copper",
        )
        trace_set = add_wire_trace(
            project,
            assets,
            "coil_trace.txt",
            b"5 -20 5\n5 30 5\n",
            units="mm",
        )
        trace_set["material"] = "Air"

        with (
            patch.object(solve_workspace, "default_run_history_root", lambda: run_history),
            patch.object(app, "_find_solver_root", return_value=fake_solver_root),
        ):
            assert app._start_3d_project_from_menu() is True
            workspace = app._three_d_workspace
            assert workspace.load_project_data(project, assets) is True
            app._update()
            page.get_by_role("button", name="Run study").click()
            deadline = time.monotonic() + 5
            context = app._solver_run_context
            while (
                (context is None or not started_marker.exists())
                and time.monotonic() < deadline
            ):
                page.wait_for_timeout(50)
                context = app._solver_run_context
            try:
                assert context is not None and started_marker.exists(), (
                    "The failed-process study did not reach the fake child process: "
                    f"{workspace._status.ui_children!r}"
                )
            finally:
                release_marker.write_text("release", encoding="utf-8")
            output_dir = Path(context["output_dir"])

            deadline = time.monotonic() + 10
            while app._solver_job_active and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert app._solver_job_active is False
            page.locator("#q-app").get_by_text(
                "Study failed:", exact=False
            ).wait_for(timeout=5_000)
            manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
            assert manifest["status"] == "failed"
            assert "status 7" in manifest["error"]
            assert "0x00000007" in manifest["error"]
            assert "solver.log" in manifest["error"]
            assert "intentional solver failure" in (output_dir / "solver.log").read_text(
                encoding="utf-8"
            )
            deadline = time.monotonic() + 5
            while workspace._study_running and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert workspace._study_running is False
            page.wait_for_timeout(1_500)


@app_test("ngsolve_gui.appconfig")
def test_3d_cancelled_process_finishes_with_cancelled_status(page: Page, app):
    with tempfile.TemporaryDirectory(prefix="mmfem_3d_cancel_e2e_") as temporary:
        root = Path(temporary)
        fake_solver_root = root / "solver"
        runner = fake_solver_root / "scripts" / "run_gui_3d_model.py"
        runner.parent.mkdir(parents=True)
        runner.write_text(
            "import time\nprint('waiting for cancellation', flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        evidence_dir = os.environ.get("MMFEM_STAGE8A_EVIDENCE_DIR")
        run_history = (
            Path(evidence_dir) / "ui-3d-cancellation"
            if evidence_dir
            else root / "runs"
        )
        cube_path = root / "em_cube.step"
        Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(cube_path))
        project = new_3d_project("3D cancellation diagnostic check")
        materials = {material["name"]: material for material in builtin_materials()}
        project["materials"] = [materials["Copper"], materials["Air"]]
        assets = {}
        add_step_geometry(
            project,
            assets,
            cube_path.name,
            cube_path.read_bytes(),
            units="mm",
            material="Copper",
        )
        trace_set = add_wire_trace(
            project,
            assets,
            "coil_trace.txt",
            b"5 -20 5\n5 30 5\n",
            units="mm",
        )
        trace_set["material"] = "Air"

        with (
            patch.object(solve_workspace, "default_run_history_root", lambda: run_history),
            patch.object(app, "_find_solver_root", return_value=fake_solver_root),
        ):
            assert app._start_3d_project_from_menu() is True
            workspace = app._three_d_workspace
            assert workspace.load_project_data(project, assets) is True
            app._update()
            page.get_by_role("button", name="Run study").click()
            deadline = time.monotonic() + 5
            context = app._solver_run_context
            while context is None and time.monotonic() < deadline:
                page.wait_for_timeout(100)
                context = app._solver_run_context
            assert context is not None, (
                "The cancellation study did not start: "
                f"{workspace._status.ui_children!r}"
            )
            output_dir = Path(context["output_dir"])

            deadline = time.monotonic() + 5
            process = app._solver_process
            while (
                (process is None or process.poll() is not None)
                and time.monotonic() < deadline
            ):
                page.wait_for_timeout(100)
                process = app._solver_process
            assert process is not None and process.poll() is None
            page.get_by_role("button", name="Cancel", exact=True).click()

            deadline = time.monotonic() + 10
            while app._solver_job_active and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert app._solver_job_active is False
            manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
            assert manifest["status"] == "cancelled"
            assert "Study cancelled" in " ".join(workspace._status.ui_children)
            deadline = time.monotonic() + 5
            while workspace._study_running and time.monotonic() < deadline:
                page.wait_for_timeout(100)
            assert workspace._study_running is False

            screenshot_dir = os.environ.get("MMFEM_UI_SCREENSHOT_DIR")
            if screenshot_dir:
                page.screenshot(
                    path=str(Path(screenshot_dir) / "07-3d-cancelled-study.png"),
                    full_page=True,
                )
            page.wait_for_timeout(1_500)
