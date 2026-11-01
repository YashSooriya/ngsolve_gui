from __future__ import annotations

import json
import math
import os
import pickle
import shutil
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from ngapp.e2e import app_test
from playwright.sync_api import Page
import pytest

from ngsolve_gui.axisymmetric_model import (
    builtin_materials,
    new_model,
    new_studies,
    validate_model,
)
import ngsolve_gui.solve_workspace as solve_workspace
from .solver_test_support import SOLVER_UNAVAILABLE_REASON, solver_checkout_available


def _harmonic_coil_project():
    model = new_model("Stage 8A axisymmetric coil")
    model["materials"] = builtin_materials()
    outer_vertices = [[0, -0.01], [0.04, -0.01], [0.04, 0.01], [0, 0.01]]
    circle_vertices = [
        [0.02 + 0.004 * math.cos(2 * math.pi * index / 48),
         0.004 * math.sin(2 * math.pi * index / 48)]
        for index in range(48)
    ]
    model["geometry"]["regions"] = [
        {
            "id": "region-air",
            "name": "Air domain",
            "material_id": "material-air",
            "parent_id": None,
            "mechanical": False,
            "vertices": outer_vertices,
            "shape": {
                "type": "rectangle",
                "r_min": 0,
                "z_min": -0.01,
                "width": 0.04,
                "height": 0.02,
                "dimension_expressions": {
                    "r_min": "0", "z_min": "-0.01",
                    "width": "0.04", "height": "0.02",
                },
            },
            "sources": {},
            "edge_ids": [],
        },
        {
            "id": "region-coil",
            "name": "Copper coil",
            "material_id": "material-copper",
            "parent_id": "region-air",
            "mechanical": False,
            "vertices": circle_vertices,
            "shape": {
                "type": "circle",
                "r_center": 0.02,
                "z_center": 0,
                "radius": 0.004,
                "dimension_expressions": {
                    "r_center": "0.02", "z_center": "0", "radius": "0.004",
                },
            },
            "sources": {
                "dc_current_density": "0",
                "ac_current_density_real": "1e6",
                "ac_current_density_imaginary": "0",
                "mechanical_body_force": {"r": "0", "z": "0"},
            },
            "edge_ids": [],
        },
    ]
    conditions = [
        {"id": "bc-axis", "name": "Axis of symmetry", "type": "axis_of_symmetry"},
        {"id": "bc-reference", "name": "Magnetic reference", "type": "magnetic_potential_zero"},
    ]
    model["boundary_conditions"] = conditions
    edges = []
    for region_index, region in enumerate(model["geometry"]["regions"]):
        vertices = region["vertices"]
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            condition_id = None
            if region_index == 0:
                condition_id = (
                    "bc-axis"
                    if abs(start[0]) <= 1e-12 and abs(end[0]) <= 1e-12
                    else "bc-reference"
                )
            edge_id = f"edge-{region_index}-{index}"
            region["edge_ids"].append(edge_id)
            edges.append({
                "id": edge_id,
                "name": edge_id,
                "vertices": [start, end],
                "boundary_condition_ids": [condition_id] if condition_id else [],
            })
    model["geometry"].update({
        "regions": model["geometry"]["regions"],
        "edges": edges,
        "constraints": [],
    })
    model["physics"] = {
        "dc_magnetic": {"enabled": False},
        "harmonic_electromagnetic": {"enabled": True, "frequency_hz": "50"},
        "mechanics": {"enabled": False},
        "coupling": {"enabled": False},
    }
    model["mesh"].update({
        "element_size": "0.01",
        "polynomial_order": 2,
        "curve_order": 2,
    })
    studies = new_studies()
    studies["studies"][0]["physics"] = ["harmonic_electromagnetic"]
    studies["studies"][0]["frequency_hz"] = ["50"]
    errors = validate_model(model)
    assert errors == [], "Invalid verification project: " + " ".join(errors)
    return model, studies


@app_test("ngsolve_gui.appconfig")
@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_axisymmetric_run_button_executes_harmonic_project(page: Page, app):
    with tempfile.TemporaryDirectory(prefix="mmfem_axisym_run_e2e_") as temporary:
        root = Path(temporary)
        run_history = root / "runs"
        with patch.object(
            solve_workspace,
            "default_run_history_root",
            lambda: run_history,
        ):
            _exercise_axisymmetric_run(page, app, root)


def _exercise_axisymmetric_run(page, app, root):
    model, studies = _harmonic_coil_project()
    assert app._start_new_problem() is True
    workspace = app.solve_workspace
    workspace.set_model(
        model,
        studies,
        {"schema_version": 1, "active_section": "studies", "camera": "fit"},
    )
    workspace.select_section("studies")
    app._update()
    page.get_by_role("button", name="Run study").wait_for(timeout=10_000)
    frequency = page.get_by_label("Frequency points (comma separated)")
    frequency.wait_for(timeout=10_000)
    assert frequency.input_value() == "50"
    page.get_by_text("Air domain", exact=True).wait_for(timeout=10_000)
    page.wait_for_function(
        """() => {
            const canvas = document.querySelector('svg.solve-sketch-canvas');
            return canvas && canvas.querySelector('[data-sketch-region-id="region-air"]')
                && canvas.querySelector('[data-sketch-region-id="region-coil"]');
        }""",
        timeout=10_000,
    )

    screenshot_dir = os.environ.get("MMFEM_UI_SCREENSHOT_DIR")
    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "08-axisymmetric-study-setup.png"),
            full_page=True,
        )

    page.get_by_role("button", name="Run study").click()
    deadline = time.monotonic() + 5
    context = app._solver_run_context
    while context is None and time.monotonic() < deadline:
        page.wait_for_timeout(100)
        context = app._solver_run_context
    assert context is not None, f"Axisymmetric run did not start: {workspace.message!r}"
    output_dir = Path(context["output_dir"])

    deadline = time.monotonic() + 90
    while app._solver_job_active and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert app._solver_job_active is False
    manifest_path = output_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["frequency_hz"] == ["50"]
    assert manifest["mesh_settings"]["polynomial_order"] == 2
    diagnostics = manifest["diagnostics"]
    process_context = json.loads(
        (output_dir / diagnostics["process_context"]).read_text(encoding="utf-8")
    )
    assert process_context["run_kind"] == "study"
    assert process_context["native_modules"]["ngsolve"]["file"]
    assert (output_dir / diagnostics["native_crash_log"]).is_file()
    assert (output_dir / "mesh.vol").is_file()
    field_path = output_dir / "ngsolve_gui" / "fields" / "B_AC.pkl"
    assert field_path.is_file()

    import ngsolve

    mesh = ngsolve.Mesh(str(output_dir / "mesh.vol"))
    with field_path.open("rb") as stream:
        field = pickle.load(stream)
    field_value = field(mesh(0.02, 0.005))
    magnitude = math.sqrt(sum(abs(value) ** 2 for value in field_value))
    assert math.isfinite(magnitude) and magnitude > 1e-9

    assert app._workspace_mode == "solve"
    assert workspace.runs[-1]["status"] == "Complete"

    # Run the same saved axisymmetric setup again without restarting the app.
    # This catches stale solver-job state and verifies the second run receives
    # the same frequency and finite-element order as the first.
    app._run_axisymmetric_solver()
    second_context = app._solver_run_context
    deadline = time.monotonic() + 5
    while second_context is None and time.monotonic() < deadline:
        page.wait_for_timeout(100)
        second_context = app._solver_run_context
    assert second_context is not None
    second_output_dir = Path(second_context["output_dir"])
    deadline = time.monotonic() + 90
    while app._solver_job_active and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert app._solver_job_active is False
    second_manifest = json.loads(
        (second_output_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert second_output_dir != output_dir
    assert second_manifest["status"] == "complete"
    assert second_manifest["frequency_hz"] == manifest["frequency_hz"]
    assert second_manifest["mesh_settings"]["polynomial_order"] == 2
    assert (second_output_dir / "ngsolve_gui" / "fields" / "B_AC.pkl").is_file()

    workspace.select_section("runs")
    app._update()
    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "09-axisymmetric-completed-run-history.png"),
            full_page=True,
        )
    evidence_dir = os.environ.get("MMFEM_STAGE8A_EVIDENCE_DIR")
    if evidence_dir:
        shutil.copytree(output_dir, Path(evidence_dir) / "ui-axisymmetric-run", dirs_exist_ok=True)
        shutil.copytree(
            second_output_dir,
            Path(evidence_dir) / "ui-axisymmetric-run-2",
            dirs_exist_ok=True,
        )
        (Path(evidence_dir) / "ui-axisymmetric-field-check.json").write_text(
            json.dumps({
                "frequency_hz": 50,
                "polynomial_order": 2,
                "mesh_elements": mesh.ne,
                "mesh_vertices": mesh.nv,
                "sample_r_m": 0.02,
                "sample_z_m": 0.005,
                "B_magnitude_T": magnitude,
                "run_manifest": "ui-axisymmetric-run/run_manifest.json",
                "consecutive_run_statuses": [manifest["status"], second_manifest["status"]],
            }, indent=2),
            encoding="utf-8",
        )
