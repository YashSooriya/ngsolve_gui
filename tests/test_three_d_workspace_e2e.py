from pathlib import Path
import json
import tempfile
import time
import os
import threading
from unittest.mock import patch

from ngapp.e2e import app_test
from playwright.sync_api import Page

from ngsolve_gui.three_d_project import (
    UNASSIGNED_MATERIAL,
)


@app_test("ngsolve_gui.appconfig")
def test_three_d_viewport_renders_and_selects_model_items(page: Page, app):
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    fixtures = Path(__file__).parent / "fixtures"
    step_path = fixtures / "magnet_shield_4k.step"
    trace_path = fixtures / "golay_x_trace.txt"
    assert app._start_3d_project_from_menu()
    workspace = app._three_d_workspace
    with patch.object(
        app,
        "_pick_artifact",
        side_effect=[
            (str(step_path), step_path.read_bytes()),
            (str(trace_path), trace_path.read_bytes()),
        ],
    ):
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
    page.locator(".mmfem-3d-model-tree").get_by_text(
        geometry["name"], exact=True
    ).click()
    mesh_size = page.get_by_label("Maximum element size (mm)")
    mesh_size.wait_for(timeout=10_000)
    # Keep this end-to-end UI check bounded on developer machines while still
    # generating a real volume mesh for CAD selection and viewport verification.
    mesh_size.fill("100")
    mesh_size.press("Tab")
    deadline = time.monotonic() + 5
    while geometry.get("mesh_maxh_mm") != 100.0 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert geometry["mesh_maxh_mm"] == 100.0
    assert geometry["material"] == UNASSIGNED_MATERIAL
    assert workspace._run_study_button.ui_disable is True
    assert page.get_by_role("button", name="Run study").is_disabled()
    page.get_by_text(
        "No physical material is assumed. Choose one from the library before running.",
        exact=True,
    ).wait_for(timeout=10_000)
    try:
        page.wait_for_function(
            """() => {
                const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
                return graph && graph._fullLayout
                    && Array.isArray(graph.data) && graph.data.length >= 2;
            }""",
            timeout=60_000,
        )
    except Exception:
        diagnostics = {
            "viewport": page.viewport_size,
            "page_errors": page_errors,
            "project_geometry_count": len(workspace.project.get("geometries", [])),
            "project_trace_count": len(workspace.project.get("wire_trace_sets", [])),
            "preview_generation": workspace._preview_generation,
            "preview_running": workspace._preview_running,
            "preview_pending": workspace._preview_pending is not None,
            "preview_figure_count": len(workspace._preview._figures),
            "preview_hidden": workspace._preview.ui_hidden,
            "empty_state_hidden": workspace._preview_empty_state.ui_hidden,
            "preview_traces": [
                {"type": trace.type, "meta": trace.meta}
                for figure in workspace._preview._figures
                for trace in figure.data
            ],
            "empty_title": workspace._preview_empty_title.ui_children,
            "empty_detail": workspace._preview_empty_detail.ui_children,
            "status": workspace._status.ui_children,
            "graph_count": page.locator(".mmfem-3d-viewport .js-plotly-plot").count(),
            "browser_graph": page.locator(
                ".mmfem-3d-viewport .js-plotly-plot"
            ).first.evaluate(
                """graph => ({
                    dataCount: graph.data && graph.data.length,
                    clickBound: graph._mmfem_click_bound,
                    fullLayout: Boolean(graph._fullLayout),
                    traceMeta: graph.data && graph.data.map(trace => trace.meta),
                    connected: graph.isConnected,
                })"""
            ) if page.locator(".mmfem-3d-viewport .js-plotly-plot").count() else None,
            "worker_threads": [
                thread.name for thread in threading.enumerate()
                if thread.name.startswith("MMFEM-")
            ],
            "body_text": page.locator("body").inner_text()[:1800],
        }
        try:
            diagnostic_dir = Path(
                os.environ.get("MMFEM_UI_SCREENSHOT_DIR", "tests/output")
            )
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(diagnostic_dir / "3d-preview-timeout.png"), full_page=True)
        except Exception as screenshot_error:
            diagnostics["screenshot_error"] = str(screenshot_error)
        print("THREED_PREVIEW_TIMEOUT " + json.dumps(diagnostics, default=str))
        raise
    properties = page.locator(".mmfem-property-editor-body")
    properties.evaluate("element => { element.scrollTop = 0; }")
    page.wait_for_timeout(500)
    screenshot_dir = os.environ.get("MMFEM_UI_SCREENSHOT_DIR")
    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "01-3d-step-awaiting-material.png"),
            full_page=True,
        )
    page.get_by_label("Default material").click()
    page.get_by_text("Stainless steel (4 K / OVC)", exact=True).last.click()
    live_geometry = workspace.project["geometries"][0]
    deadline = time.monotonic() + 10
    while (
        live_geometry["material"] != "Stainless steel (4 K / OVC)"
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert live_geometry["material"] == "Stainless steel (4 K / OVC)"
    deadline = time.monotonic() + 10
    while workspace._run_study_button.ui_disable and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert workspace._run_study_button.ui_disable is False
    page.get_by_text(
        "Material data is embedded in the project.", exact=True
    ).wait_for(timeout=10_000)
    tree = page.locator(".mmfem-3d-model-tree")
    tree.get_by_text("magnet_shield_4k.step", exact=True).wait_for(timeout=10_000)
    run_button = page.get_by_role("button", name="Run study")
    deadline = time.monotonic() + 10
    while run_button.is_disabled() and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert not run_button.is_disabled()
    tree.get_by_text(live_geometry["name"], exact=True).click()
    page.get_by_label("Maximum element size (mm)").wait_for(timeout=10_000)
    page.wait_for_timeout(250)
    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "02-3d-material-assigned.png"),
            full_page=True,
        )

    graph = page.locator(".mmfem-3d-viewport .js-plotly-plot").first
    page.wait_for_function(
        """() => {
            const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
            return graph && graph._fullLayout
                && Array.isArray(graph.data) && graph.data.length >= 2;
        }""",
        timeout=20_000,
    )
    assert graph.locator("canvas").count() > 0
    bounds = graph.bounding_box()
    assert bounds is not None and bounds["width"] > 400 and bounds["height"] > 300
    graph.evaluate("element => element.emit('plotly_click', {points: [{curveNumber: 0}]})")
    page.get_by_text("Geometry model", exact=True).wait_for(timeout=10_000)
    assert workspace._selection == ("geometry", geometry["id"])
    page.get_by_role("button", name="Generate mesh", exact=True).click()
    deadline = time.monotonic() + 70
    while workspace._mesh_worker_active and time.monotonic() < deadline:
        page.wait_for_timeout(250)
    page.wait_for_function(
        """() => {
            const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
            return graph && graph.data && graph.data.some(
                trace => trace.meta && trace.meta.mmfem_generated_mesh
            );
        }""",
        timeout=90_000,
    )
    browser_traces = graph.evaluate(
        "element => element.data.map(trace => ({type: trace.type, name: trace.name, meta: trace.meta, color: trace.color, lineColor: trace.line && trace.line.color}))"
    )
    assert workspace._mesh_results[geometry["id"]]["status"] == "ready"
    assert workspace._mesh_results[geometry["id"]]["volume_elements"] > 0
    assert workspace._preview._figures[0].data[0].color == "#f59e0b"
    assert browser_traces[0]["color"] == "#f59e0b"
    assert browser_traces[1]["lineColor"] == "#33475b"
    page.get_by_text("Ready ·", exact=False).wait_for(timeout=10_000)
    page.get_by_text("Tetrahedral mean ratio", exact=False).wait_for(timeout=10_000)
    if screenshot_dir:
        page.wait_for_timeout(1000)
        page.screenshot(
            path=str(Path(screenshot_dir) / "03-3d-generated-mesh.png"),
            full_page=True,
        )
    selected_boundary = int(workspace._mesh_results[geometry["id"]]["boundary_region_ids"][0])
    graph.evaluate(
        "element => element.emit('plotly_click', {points: [{curveNumber: 0, pointNumber: 0}]})"
    )
    deadline = time.monotonic() + 10
    while (
        selected_boundary not in workspace._selection_draft.get("region_ids", [])
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert selected_boundary in workspace._selection_draft["region_ids"]
    page.get_by_label("Selection name").fill("Selected STEP boundary")
    deadline = time.monotonic() + 10
    while (
        workspace._selection_draft.get("name") != "Selected STEP boundary"
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert workspace._selection_draft["name"] == "Selected STEP boundary"
    page.get_by_label("Boundary condition").click()
    page.locator(".q-menu:visible .q-item").filter(
        has_text="Vector potential A = 0"
    ).click()
    deadline = time.monotonic() + 10
    while (
        workspace._selection_draft.get("boundary_condition") != "magnetic_potential_zero"
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert workspace._selection_draft["boundary_condition"] == "magnetic_potential_zero"
    page.get_by_role("button", name="Save selection").click()
    deadline = time.monotonic() + 10
    while not live_geometry.get("boundary_selections") and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert live_geometry.get("boundary_selections")
    assert live_geometry["boundary_selections"][0]["boundary_ids"] == [selected_boundary]
    selection = live_geometry["boundary_selections"][0]
    assert selection["boundary_condition"] == "magnetic_potential_zero"
    assert len(selection["boundary_tags"]) == 1
    assert workspace._selection == ("named_selection", selection["id"])
    try:
        page.wait_for_function(
            """() => {
                const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
                return graph && graph.data && graph.data.some(
                    trace => trace.meta && trace.meta.mmfem_boundary_overlay
                        && Array.isArray(trace.x) && trace.x.length > 0
                );
            }""",
            timeout=20_000,
        )
    except Exception:
        current_mesh = workspace._mesh_results.get(geometry["id"], {})
        current_figure = (
            workspace._preview._figures[0]
            if workspace._preview._figures else None
        )
        diagnostics = {
            "page_errors": page_errors,
            "selection": workspace._selection,
            "active_boundary_ids": workspace._active_boundary_region_ids(geometry["id"]),
            "saved_boundary_selection": selection,
            "mesh_status": current_mesh.get("status"),
            "mesh_boundary_region_count": len(current_mesh.get("boundary_region_ids", [])),
            "mesh_regions": current_mesh.get("boundary_regions"),
            "preview_generation": workspace._preview_generation,
            "preview_running": workspace._preview_running,
            "preview_pending": workspace._preview_pending is not None,
            "preview_traces": [
                trace.meta for trace in current_figure.data
            ] if current_figure else None,
            "browser_traces": page.locator(
                ".mmfem-3d-viewport .js-plotly-plot"
            ).first.evaluate(
                "element => element.data.map(trace => trace.meta)"
            ) if page.locator(".mmfem-3d-viewport .js-plotly-plot").count() else None,
            "status": workspace._status.ui_children,
        }
        try:
            diagnostic_dir = Path(
                os.environ.get("MMFEM_UI_SCREENSHOT_DIR", "tests/output")
            )
            diagnostic_dir.mkdir(parents=True, exist_ok=True)
            page.screenshot(
                path=str(diagnostic_dir / "3d-boundary-overlay-timeout.png"),
                full_page=True,
            )
        except Exception as screenshot_error:
            diagnostics["screenshot_error"] = str(screenshot_error)
        print("THREED_BOUNDARY_OVERLAY_TIMEOUT " + json.dumps(diagnostics, default=str))
        raise
    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "04-3d-named-boundary-selection.png"),
            full_page=True,
        )
    page.get_by_role("button", name="Mesh edges").click()
    page.wait_for_function(
        """() => {
            const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
            return graph && graph.data && graph.data[1] && graph.data[1].visible === false;
        }""",
        timeout=10_000,
    )
    page.get_by_role("button", name="Surface mesh").click()
    page.wait_for_function(
        """() => {
            const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
            return graph && graph.data && graph.data[0] && graph.data[0].visible === false;
        }""",
        timeout=10_000,
    )
    page.get_by_role("button", name="Surface mesh").click()
    page.get_by_role("button", name="Mesh edges").click()
    page.wait_for_function(
        """() => {
            const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
            return graph && graph.data && graph.data[0] && graph.data[0].visible !== false
                && graph.data[1] && graph.data[1].visible !== false;
        }""",
        timeout=10_000,
    )
    tree.get_by_text("Time-harmonic EM", exact=True).click()
    page.get_by_label("Frequency (Hz)").wait_for(timeout=10_000)
    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "05-3d-study-configuration.png"),
            full_page=True,
        )
    page.wait_for_timeout(2500)
    properties = page.locator(".mmfem-property-editor-body")
    properties.evaluate("element => { element.scrollTop = 0; }")
    page.wait_for_timeout(500)
    page.screenshot(
        path=str(Path(tempfile.gettempdir()) / "mmfem_stage4_3d_mesh_inspection.png"),
        full_page=True,
    )
    properties.evaluate(
        """element => {
            const title = Array.from(element.children).find(
                child => child.textContent.trim() === 'Named selections'
            );
            if (!title) return;
            const offset = title.getBoundingClientRect().top
                - element.getBoundingClientRect().top + element.scrollTop;
            const maxScroll = Math.max(0, element.scrollHeight - element.clientHeight);
            element.scrollTop = Math.max(0, Math.min(offset - 8, maxScroll - 12));
        }"""
    )
    page.wait_for_timeout(500)
    page.screenshot(
        path=str(Path(tempfile.gettempdir()) / "mmfem_stage4_3d_named_selections.png"),
        full_page=True,
    )
    page.set_viewport_size({"width": 960, "height": 680})
    page.wait_for_function(
        """() => {
            const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
            const rect = graph && graph.getBoundingClientRect();
            return rect && rect.width > 300 && rect.height > 250;
        }""",
        timeout=10_000,
    )
    page.wait_for_timeout(1000)
    properties.evaluate("element => { element.scrollTop = 0; }")
    page.wait_for_timeout(500)
    page.screenshot(
        path=str(Path(tempfile.gettempdir()) / "mmfem_stage4_3d_mesh_inspection_compact.png"),
        full_page=True,
    )
    tree = page.locator(".mmfem-3d-model-tree")
    tree.get_by_text(live_geometry["name"], exact=True).click()
    page.get_by_text("Geometry model", exact=True).wait_for(timeout=10_000)
    tree.get_by_text("magnet_shield_4k.step", exact=True).wait_for(timeout=10_000)
    tree.get_by_text("Selected STEP boundary", exact=True).wait_for(timeout=10_000)
    tree.get_by_text("Selected STEP boundary", exact=True).click()
    page.get_by_text("Named boundary selection", exact=True).wait_for(timeout=10_000)
    page.screenshot(
        path=str(Path(tempfile.gettempdir()) / "mmfem_stage5_3d_boundary_properties.png"),
        full_page=True,
    )

    # Expose the actual per-domain control in a rendered property editor. The
    # selection is backed by the generated Netgen domain region; the size itself
    # is entered through the same input an engineer uses in the application.
    domain_region = workspace._mesh_results[geometry["id"]]["domain_regions"][0]
    workspace._selection_draft.update({
        "geometry_id": geometry["id"],
        "kind": "domain",
        "selection_id": None,
        "name": "Refined shield volume",
        "region_ids": [int(domain_region["id"])],
        "material": None,
    })
    assert workspace._save_named_selection(geometry)
    domain_selection = geometry["domain_selections"][-1]
    workspace.select_item("named_selection", domain_selection["id"])
    app._update()
    local_maxh = page.get_by_label("Local max element size (mm)")
    local_maxh.fill("25")
    local_maxh.press("Tab")
    deadline = time.monotonic() + 5
    while domain_selection.get("mesh_maxh_mm") != 25.0 and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert domain_selection.get("mesh_maxh_mm") == 25.0
    page.locator(".mmfem-property-editor-body").evaluate(
        "element => { element.scrollTop = 0; }"
    )
    page.wait_for_timeout(300)
    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "05-3d-local-domain-mesh-control.png"),
            full_page=True,
        )

    page.set_viewport_size({"width": 1440, "height": 900})
    workspace.select_item("study")
    app._update()
    page.get_by_text("Time-harmonic electromagnetic study", exact=True).wait_for(timeout=10_000)
    page.get_by_label("Frequency (Hz)").wait_for(timeout=10_000)
    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "06-3d-harmonic-study-settings.png"),
            full_page=True,
        )
