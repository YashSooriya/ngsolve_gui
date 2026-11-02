from pathlib import Path
import json
import math
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


_REVIEW_SCREEN_SIZE = {"width": 1920, "height": 1080}


def _capture_review_screenshot(page, path):
    """Capture a screen-sized workspace view, then restore the test viewport."""
    previous_size = page.viewport_size
    try:
        if previous_size != _REVIEW_SCREEN_SIZE:
            page.set_viewport_size(_REVIEW_SCREEN_SIZE)
            page.wait_for_timeout(250)
        page.screenshot(path=str(path), full_page=False)
    finally:
        if previous_size and previous_size != _REVIEW_SCREEN_SIZE:
            page.set_viewport_size(previous_size)
            page.wait_for_timeout(150)


@app_test("ngsolve_gui.appconfig")
def test_three_d_viewport_renders_and_selects_model_items(page: Page, app):
    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    initial_viewport = page.viewport_size
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
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "01-3d-step-awaiting-material.png"
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
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "02-3d-material-assigned.png"
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
    geometry_selection_started = time.perf_counter()
    graph.evaluate("element => element.emit('plotly_click', {points: [{curveNumber: 0}]})")
    page.get_by_text("Geometry model", exact=True).wait_for(timeout=10_000)
    geometry_selection_ms = (
        time.perf_counter() - geometry_selection_started
    ) * 1000
    assert workspace._selection == ("geometry", geometry["id"])
    mesh_generation_started = time.perf_counter()
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
    mesh_generation_seconds = time.perf_counter() - mesh_generation_started
    browser_traces = graph.evaluate(
        "element => element.data.map(trace => ({type: trace.type, name: trace.name, meta: trace.meta, color: trace.color, lineColor: trace.line && trace.line.color}))"
    )
    assert workspace._mesh_results[geometry["id"]]["status"] == "ready"
    assert workspace._mesh_results[geometry["id"]]["volume_elements"] > 0
    mesh_volume_elements = workspace._mesh_results[geometry["id"]][
        "volume_elements"
    ]
    assert workspace._preview._figures[0].data[0].color == "#f59e0b"
    assert browser_traces[0]["color"] == "#f59e0b"
    assert browser_traces[1]["lineColor"] == "#33475b"
    page.get_by_text("Ready ·", exact=False).wait_for(timeout=10_000)
    page.get_by_text("Tetrahedral mean ratio", exact=False).wait_for(timeout=10_000)
    if screenshot_dir:
        page.wait_for_timeout(1000)
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "03-3d-generated-mesh.png"
        )
    selected_boundary = int(workspace._mesh_results[geometry["id"]]["boundary_region_ids"][0])
    boundary_profile = {
        "click_to_editor_ms": [],
        "page_evaluate_ms": [],
        "click_to_handler_ms": [],
        "handler_ms": [],
        "handler_to_editor_ms": [],
        "rebuild_items_ms": [],
        "rebuild_properties_ms": [],
        "update_boundary_overlay_ms": [],
        "boundary_overlay_trace_ms": [],
        "preview_selection_update_ms": [],
    }

    click_contexts = []
    click_context_lock = threading.Lock()
    plot = workspace._preview._plots[0]
    original_click_handler = plot._click_handler

    def timed_click_handler(curve_number, point_number=None):
        with click_context_lock:
            context = next(
                (entry for entry in click_contexts if not entry["claimed"]), None
            )
            if context is not None:
                context["claimed"] = True
                context["handler_started"] = time.perf_counter()
        try:
            return original_click_handler(curve_number, point_number)
        finally:
            if context is not None:
                context["handler_finished"] = time.perf_counter()
                context["handler_done"].set()

    plot._click_handler = timed_click_handler

    def begin_profiled_click():
        context = {
            "started": time.perf_counter(),
            "claimed": False,
            "handler_done": threading.Event(),
        }
        with click_context_lock:
            click_contexts.append(context)
        return context

    def record_profiled_click(context, editor_visible_at):
        assert context["handler_done"].wait(10), "Plotly click handler did not finish"
        handler_started = context["handler_started"]
        handler_finished = context["handler_finished"]
        boundary_profile["click_to_editor_ms"].append(
            (editor_visible_at - context["started"]) * 1000
        )
        boundary_profile["page_evaluate_ms"].append(context["page_evaluate_ms"])
        boundary_profile["click_to_handler_ms"].append(
            (handler_started - context["started"]) * 1000
        )
        boundary_profile["handler_ms"].append(
            (handler_finished - handler_started) * 1000
        )
        boundary_profile["handler_to_editor_ms"].append(
            max(0.0, (editor_visible_at - handler_finished) * 1000)
        )
        return {
            "click_to_editor_ms": round(
                (editor_visible_at - context["started"]) * 1000, 2
            ),
            "page_evaluate_ms": round(context["page_evaluate_ms"], 2),
            "click_to_handler_start_ms": round(
                (handler_started - context["started"]) * 1000, 2
            ),
            "handler_ms": round((handler_finished - handler_started) * 1000, 2),
            "handler_to_editor_ms": round(
                max(0.0, (editor_visible_at - handler_finished) * 1000), 2
            ),
        }

    def timed_workspace_method(name, original):
        def timed(*args, **kwargs):
            started = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                boundary_profile[name].append((time.perf_counter() - started) * 1000)
        return timed

    for method_name, result_key in (
        ("_rebuild_items", "rebuild_items_ms"),
        ("_rebuild_properties", "rebuild_properties_ms"),
        ("_update_boundary_overlay", "update_boundary_overlay_ms"),
        ("_update_preview_selection", "preview_selection_update_ms"),
    ):
        setattr(
            workspace,
            method_name,
            timed_workspace_method(result_key, getattr(workspace, method_name)),
        )
    from ngsolve_gui import three_d_preview
    three_d_preview.boundary_overlay_trace = timed_workspace_method(
        "boundary_overlay_trace_ms", three_d_preview.boundary_overlay_trace
    )
    boundary_click_context = begin_profiled_click()
    evaluate_started = time.perf_counter()
    graph.evaluate(
        "element => element.emit('plotly_click', {points: [{curveNumber: 0, pointNumber: 0}]})"
    )
    boundary_click_context["page_evaluate_ms"] = (
        time.perf_counter() - evaluate_started
    ) * 1000
    page.get_by_label("Selection name").wait_for(timeout=10_000)
    deadline = time.monotonic() + 10
    while (
        selected_boundary not in workspace._selection_draft.get("region_ids", [])
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert selected_boundary in workspace._selection_draft["region_ids"]
    def wait_for_boundary_chip(region_id):
        region = next(
            item
            for item in workspace._mesh_results[geometry["id"]]["boundary_regions"]
            if int(item["id"]) == int(region_id)
        )
        label = f"{region['name']} · {region['elements']:,} surface triangles"
        page.wait_for_function(
            """label => {
                return [...document.querySelectorAll(
                    '.mmfem-property-editor-body input[aria-label="Boundary regions"]'
                )].some(input => input.value.includes(label));
            }""",
            arg=label,
            timeout=10_000,
        )

    wait_for_boundary_chip(selected_boundary)
    first_selection_sample = record_profiled_click(
        boundary_click_context, time.perf_counter()
    )

    # Repeated face picks measure the same path without repeating STEP import
    # or meshing. Use one point from each distinct current-mesh boundary region.
    mesh_result = workspace._mesh_results[geometry["id"]]
    points_by_region = {}
    for point_index, region_id in enumerate(mesh_result["boundary_region_ids"]):
        points_by_region.setdefault(int(region_id), point_index)
    repeated_regions = [
        (region_id, point_index)
        for region_id, point_index in points_by_region.items()
        if region_id != selected_boundary
    ][:5]
    repeated_selection_samples = []
    for region_id, point_index in repeated_regions:
        context = begin_profiled_click()
        evaluate_started = time.perf_counter()
        graph.evaluate(
            "(element, point) => element.emit('plotly_click', "
            "{points: [{curveNumber: 0, pointNumber: point}]})",
            point_index,
        )
        context["page_evaluate_ms"] = (time.perf_counter() - evaluate_started) * 1000
        deadline = time.monotonic() + 5
        while (
            region_id not in workspace._selection_draft.get("region_ids", [])
            and time.monotonic() < deadline
        ):
            page.wait_for_timeout(10)
        assert region_id in workspace._selection_draft["region_ids"]
        wait_for_boundary_chip(region_id)
        sample = record_profiled_click(context, time.perf_counter())
        repeated_selection_samples.append({"region_id": region_id, **sample})

    def nearest_rank_percentile(values, fraction):
        ordered = sorted(values)
        if not ordered:
            return None
        index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
        return round(ordered[index], 2)
    boundary_performance = {
        "environment": {
            "storage_change": "VM moved to SSD after the earlier Stage 8B baseline; comparisons with the older timings are confounded by the storage change.",
            "viewport_px": initial_viewport,
            "mesh_volume_elements": mesh_result["volume_elements"],
            "mesh_boundary_triangles": int(len(mesh_result["boundary_region_ids"])),
            "measurement": "Local Playwright development environment; methods timed on the Python side; end-to-end samples include browser event dispatch.",
        },
        "first_selection_ms": {
            key: round(values[0], 2) if values else None
            for key, values in boundary_profile.items()
        },
        "first_selection_sample": first_selection_sample,
        "repeated_selection_samples": repeated_selection_samples,
        "selection_latency_ms": {
            "sample_count": len(boundary_profile["click_to_editor_ms"]),
            "p50": nearest_rank_percentile(boundary_profile["click_to_editor_ms"], 0.50),
            "p95_nearest_rank": nearest_rank_percentile(boundary_profile["click_to_editor_ms"], 0.95),
            "handler_start_p50": nearest_rank_percentile(boundary_profile["click_to_handler_ms"], 0.50),
            "handler_p50": nearest_rank_percentile(boundary_profile["handler_ms"], 0.50),
            "after_handler_p50": nearest_rank_percentile(boundary_profile["handler_to_editor_ms"], 0.50),
        },
        "repeated_method_medians_ms": {
            key: round(sorted(values[1:])[len(values[1:]) // 2], 2)
            if len(values) > 1 else None
            for key, values in boundary_profile.items()
        },
    }
    for region_id, point_index in repeated_regions:
        graph.evaluate(
            "(element, point) => element.emit('plotly_click', "
            "{points: [{curveNumber: 0, pointNumber: point}]})",
            point_index,
        )
        deadline = time.monotonic() + 5
        while (
            region_id in workspace._selection_draft.get("region_ids", [])
            and time.monotonic() < deadline
        ):
            page.wait_for_timeout(10)
        assert region_id not in workspace._selection_draft["region_ids"]
    workspace._update_boundary_overlay()
    # Let the coalesced Plotly restyle run before writing method timings.
    page.wait_for_timeout(100)
    evidence_dir = os.environ.get("MMFEM_STAGE8A_EVIDENCE_DIR")
    if evidence_dir:
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        (Path(evidence_dir) / "ui-3d-boundary-selection-profile.json").write_text(
            json.dumps(boundary_performance, indent=2), encoding="utf-8"
        )
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
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "04-3d-named-boundary-selection.png"
        )
    # Simulate CAD edits invalidating a previously saved boundary assignment.
    # The property editor must identify it, block the affected study, and let
    # the user pick current faces before updating the saved condition.
    selection["boundary_tags"] = ["MMFEM_FACE_000000000000000000000099"]
    workspace._rebuild_items()
    workspace.select_item("named_selection", selection["id"])
    app._update()
    assert workspace._run_study_button.ui_disable is True
    page.get_by_text("Needs review", exact=True).wait_for(timeout=10_000)
    page.get_by_role("button", name="Reassign entities").wait_for(timeout=10_000)
    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "04a-3d-invalid-boundary-recovery.png"
        )
    page.get_by_role("button", name="Reassign entities").click()
    page.get_by_text("Select current boundary regions", exact=False).wait_for(
        timeout=10_000
    )
    assert workspace._selection_draft["region_ids"] == []
    graph.evaluate(
        "element => element.emit('plotly_click', "
        "{points: [{curveNumber: 0, pointNumber: 0}]})"
    )
    deadline = time.monotonic() + 5
    while (
        selected_boundary not in workspace._selection_draft.get("region_ids", [])
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(10)
    assert selected_boundary in workspace._selection_draft["region_ids"]
    page.get_by_role("button", name="Update selection").click()
    expected_boundary_tag = next(
        region["boundary_tag"]
        for region in workspace._mesh_results[geometry["id"]]["boundary_regions"]
        if int(region["id"]) == selected_boundary
    )
    deadline = time.monotonic() + 5
    while (
        selection.get("boundary_tags") != [expected_boundary_tag]
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(50)
    assert selection["boundary_ids"] == [selected_boundary]
    assert selection["boundary_tags"] == [expected_boundary_tag]
    deadline = time.monotonic() + 5
    while workspace._run_study_button.ui_disable and time.monotonic() < deadline:
        page.wait_for_timeout(10)
    assert workspace._run_study_button.ui_disable is False, json.dumps({
        "physics": workspace.project["study"].get("physics"),
        "has_resolved_materials": workspace._has_resolved_study_materials(),
        "boundary_condition": selection.get("boundary_condition"),
        "boundary_tags": selection.get("boundary_tags"),
        "current_boundary_tags": [
            region.get("boundary_tag")
            for region in workspace._mesh_results[geometry["id"]]["boundary_regions"]
        ],
        "recovery_message": workspace._selection_recovery_message(
            geometry, "boundary", selection
        ),
        "mesh_status": workspace._mesh_results[geometry["id"]].get("status"),
        "geometry_material": geometry.get("material"),
        "wire_trace_material": workspace.project.get("wire_trace_sets", [{}])[0].get("material"),
    }, default=str)
    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "04b-3d-reassigned-boundary.png"
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
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "05-3d-study-configuration.png"
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

    # Configure the mechanics-only study while the freshly generated mesh is
    # still available for mapping a selected face to its stable STEP tag.
    tree.get_by_text("Selected STEP boundary", exact=True).click()
    page.get_by_label("Boundary condition").click()
    page.locator(".q-menu:visible .q-item").filter(
        has_text="Default solver behavior"
    ).click()
    deadline = time.monotonic() + 5
    while "boundary_condition" in selection and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert "boundary_condition" not in selection
    workspace.select_item("study")
    app._update()
    page.get_by_label("Physics").click()
    page.locator(".q-menu:visible .q-item").filter(
        has_text="Time-harmonic mechanics"
    ).click()
    deadline = time.monotonic() + 5
    while (
        workspace.project["study"].get("physics") != "harmonic_mechanical"
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert workspace.project["study"]["physics"] == "harmonic_mechanical"
    tree.get_by_text("Time-harmonic mechanics", exact=True).wait_for(timeout=10_000)
    force_x = page.get_by_label("Body force X (N/m³)")
    force_x.fill("1000")
    force_x.press("Tab")
    deadline = time.monotonic() + 5
    while (
        workspace.project["study"]["mechanical_body_force_n_m3"][0] != 1000.0
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert workspace.project["study"]["mechanical_body_force_n_m3"] == [
        1000.0, 0.0, 0.0
    ]
    page.get_by_text(
        "Solves linear time-harmonic elasticity on all solid domains.", exact=False
    ).wait_for(timeout=10_000)
    page.wait_for_function(
        """() => {
            const button = Array.from(document.querySelectorAll('button')).find(
                item => item.textContent.includes('Run study')
            );
            return button && !button.disabled;
        }""",
        timeout=10_000,
    )
    assert not page.get_by_role("button", name="Run study").is_disabled()
    page.evaluate("document.activeElement && document.activeElement.blur()")
    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "07-3d-harmonic-mechanics-settings.png"
        )
    tree.get_by_text("Selected STEP boundary", exact=True).click()
    page.get_by_label("Boundary condition").click()
    page.locator(".q-menu:visible .q-item").filter(
        has_text="Fixed support (u = 0)"
    ).click()
    deadline = time.monotonic() + 5
    while (
        selection.get("boundary_condition") != "mechanical_fixed"
        and time.monotonic() < deadline
    ):
        page.wait_for_timeout(100)
    assert selection["boundary_condition"] == "mechanical_fixed"
    page.wait_for_function(
        """() => {
            const button = Array.from(document.querySelectorAll('button')).find(
                item => item.textContent.includes('Run study')
            );
            return button && !button.disabled;
        }""",
        timeout=10_000,
    )
    assert not page.get_by_role("button", name="Run study").is_disabled()
    page.evaluate("document.activeElement && document.activeElement.blur()")
    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "08-3d-mechanical-fixed-support.png"
        )
    # Return to EM with the saved condition cleared; the same named faces and
    # domain selection continue to persist in the project.
    tree.get_by_text("Selected STEP boundary", exact=True).click()
    page.get_by_label("Boundary condition").click()
    page.locator(".q-menu:visible .q-item").filter(
        has_text="Default solver behavior"
    ).click()
    deadline = time.monotonic() + 5
    while "boundary_condition" in selection and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    workspace.select_item("study")
    app._update()
    page.get_by_label("Physics").click()
    page.locator(".q-menu:visible .q-item").filter(
        has_text="Time-harmonic electromagnetics"
    ).click()
    deadline = time.monotonic() + 5
    while workspace.project["study"].get("physics") != "harmonic_em" and time.monotonic() < deadline:
        page.wait_for_timeout(100)
    assert workspace.project["study"]["physics"] == "harmonic_em"

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
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "05-3d-local-domain-mesh-control.png"
        )

    graph_width_before_resize = graph.bounding_box()["width"]
    viewport_resize_started = time.perf_counter()
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_function(
        """previousWidth => {
            const graph = document.querySelector(
                '.mmfem-3d-viewport .js-plotly-plot'
            );
            return graph
                && graph.getBoundingClientRect().width > previousWidth + 50;
        }""",
        arg=graph_width_before_resize,
        timeout=10_000,
    )
    viewport_resize_ms = (time.perf_counter() - viewport_resize_started) * 1000
    workspace.select_item("study")
    app._update()
    page.get_by_text("Harmonic study", exact=True).wait_for(timeout=10_000)
    page.get_by_label("Frequency (Hz)").wait_for(timeout=10_000)
    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "06-3d-harmonic-study-settings.png"
        )
    evidence_dir = os.environ.get("MMFEM_STAGE8A_EVIDENCE_DIR")
    if evidence_dir:
        Path(evidence_dir).mkdir(parents=True, exist_ok=True)
        (Path(evidence_dir) / "ui-3d-interaction-performance.json").write_text(
            json.dumps({
                "representative_step": step_path.name,
                    "mesh_volume_elements": mesh_volume_elements,
                "viewport_initial_px": initial_viewport,
                "viewport_after_resize_px": {"width": 1440, "height": 900},
                "review_screenshot_px": _REVIEW_SCREEN_SIZE,
                "storage_note": (
                    "The VM moved to an SSD after the earlier Stage 8B timings. "
                    "Any timing comparison with that baseline includes a hardware/storage change."
                ),
                "boundary_selection_profile": boundary_performance,
                "measurements_ms": {
                    "geometry_selection_to_property_editor": round(
                        geometry_selection_ms, 2
                    ),
                    "face_selection_to_boundary_property_editor": round(
                        boundary_performance["selection_latency_ms"]["p50"], 2
                    ),
                    "viewport_resize_to_plot_width_update": round(
                        viewport_resize_ms, 2
                    ),
                    "mesh_generation_to_visible_mesh": round(
                        mesh_generation_seconds * 1000, 2
                    ),
                },
                "notes": "Local Playwright development environment; interaction timings include browser event dispatch and are diagnostic, not release benchmarks. Review screenshots use a 1920x1080 viewport and capture the full application window.",
            }, indent=2),
            encoding="utf-8",
        )
