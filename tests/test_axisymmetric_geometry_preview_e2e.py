from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

import numpy as np
import pytest
from ngapp.e2e import app_test
from playwright.sync_api import Page

from ngsolve_gui.axisymmetric_model import (
    builtin_materials,
    new_model,
    new_studies,
    validate_model,
)

_REVIEW_SCREEN_SIZE = {"width": 1920, "height": 1080}


def _capture_review_screenshot(page, path):
    previous_size = page.viewport_size
    try:
        if previous_size != _REVIEW_SCREEN_SIZE:
            page.set_viewport_size(_REVIEW_SCREEN_SIZE)
            page.wait_for_timeout(350)
        page.screenshot(path=str(path), full_page=False)
    finally:
        if previous_size and previous_size != _REVIEW_SCREEN_SIZE:
            page.set_viewport_size(previous_size)
            page.wait_for_timeout(150)


def _axisymmetric_preview_model():
    model = new_model("Axisymmetric 3D preview")
    model["materials"] = builtin_materials()
    region = {
        "id": "preview-shield",
        "name": "Steel shield",
        "material_id": "material-air",
        "parent_id": None,
        "mechanical": False,
        "vertices": [[0.02, -0.1], [0.05, -0.1], [0.05, 0.1], [0.02, 0.1]],
        "shape": {
            "type": "rectangle",
            "r_min": 0.02,
            "z_min": -0.1,
            "width": 0.03,
            "height": 0.2,
            "dimension_expressions": {
                "r_min": "0.02",
                "z_min": "-0.1",
                "width": "0.03",
                "height": "0.2",
            },
        },
        "sources": {},
        "edge_ids": [],
    }
    model["geometry"]["regions"] = [region]
    for index, start in enumerate(region["vertices"]):
        end = region["vertices"][(index + 1) % len(region["vertices"])]
        edge_id = f"preview-edge-{index}"
        region["edge_ids"].append(edge_id)
        model["geometry"]["edges"].append({
            "id": edge_id,
            "name": edge_id,
            "vertices": [start, end],
            "boundary_condition_ids": [],
        })
    errors = validate_model(model, scope="draft")
    assert errors == [], "Invalid preview model: " + " ".join(errors)
    return model


def _failed_request_diagnostic(request):
    parsed = urlsplit(request.url)
    listening = None
    if parsed.hostname and parsed.port:
        try:
            with socket.create_connection(
                (parsed.hostname, parsed.port), timeout=0.1
            ):
                listening = True
        except OSError:
            listening = False
    return (
        f"request failed: {request.method} {request.url.split('?')[0]} "
        f"({request.failure}); localhost port listening at failure={listening}"
    )


@app_test("ngsolve_gui.appconfig")
def test_axisymmetric_geometry_creation_renders_3d_preview(page: Page, app):
    browser_errors = []
    page.on("pageerror", lambda error: browser_errors.append(str(error)))
    page.on(
        "console",
        lambda message: browser_errors.append(f"console.{message.type}: {message.text}")
        if message.type == "error"
        else None,
    )
    page.on(
        "requestfailed",
        lambda request: browser_errors.append(_failed_request_diagnostic(request)),
    )
    try:
        page.locator(".q-layout").wait_for(state="visible", timeout=15_000)
    except Exception as error:
        try:
            page_text = page.locator("body").inner_text(timeout=1_000)
        except Exception:
            page_text = "<body text unavailable>"
        raise AssertionError(
            "The app shell did not mount. Browser diagnostics: "
            f"{browser_errors!r}; body text: {page_text[:1000]!r}"
        ) from error
    assert app._start_new_problem() is True
    workspace = app.solve_workspace
    model = _axisymmetric_preview_model()
    original_vertices = [list(point) for point in model["geometry"]["regions"][0]["vertices"]]
    workspace.set_model(
        model,
        new_studies(),
        {"schema_version": 1, "active_section": "geometry", "camera": "fit"},
    )
    app._update()

    page.wait_for_function(
        """() => {
            const svg = document.querySelector('svg.solve-sketch-canvas');
            return svg && svg.querySelector(
                '[data-sketch-region-id="preview-shield"]'
            );
        }""",
        timeout=10_000,
    )

    def grid_state():
        return page.locator("svg.solve-sketch-canvas").evaluate(
            """svg => {
                const grid = svg.querySelector('#solve-sketch-grid');
                const liveGrid = svg.querySelector('#solve-sketch-live-grid');
                const staticStyle = grid && getComputedStyle(grid);
                const liveStyle = liveGrid && getComputedStyle(liveGrid);
                const staticVisible = Boolean(staticStyle)
                    && staticStyle.display !== 'none'
                    && staticStyle.visibility !== 'hidden';
                const liveVisible = Boolean(liveStyle)
                    && liveStyle.display !== 'none'
                    && liveStyle.visibility !== 'hidden';
                return {
                    visible: staticVisible || liveVisible,
                    staticDisplay: staticStyle ? staticStyle.display : null,
                    staticVisibility: staticStyle ? staticStyle.visibility : null,
                    staticVisibilityAttribute: grid ? grid.getAttribute('visibility') : null,
                    childCount: grid ? grid.childElementCount : 0,
                    liveChildCount: liveGrid ? liveGrid.childElementCount : 0,
                    revision: svg.getAttribute('data-sketch-grid-revision'),
                    regionCount: svg.querySelectorAll('[data-sketch-region-id]').length,
                    samples: grid ? [...grid.children].slice(0, 4).map(element => ({
                        tag: element.tagName,
                        stroke: getComputedStyle(element).stroke,
                        opacity: getComputedStyle(element).opacity,
                        display: getComputedStyle(element).display,
                        visibility: getComputedStyle(element).visibility,
                    })) : [],
                    liveDisplay: liveStyle ? liveStyle.display : null,
                    liveVisibility: liveStyle ? liveStyle.visibility : null,
                    liveDisplayAttribute: liveGrid ? liveGrid.getAttribute('display') : null,
                    commitSnapshot: Boolean(svg.querySelector(
                        '[data-sketch-view-commit-snapshot="true"]'
                    )),
                    liveSamples: liveGrid ? [...liveGrid.children].slice(0, 4).map(element => ({
                        tag: element.tagName,
                        stroke: getComputedStyle(element).stroke,
                        opacity: getComputedStyle(element).opacity,
                        visibility: getComputedStyle(element).visibility,
                        dLength: element.getAttribute('d')?.length ?? null,
                    })) : [],
                };
            }"""
        )

    grid_before = grid_state()
    assert grid_before["visible"] and grid_before["childCount"] > 0
    initial_viewport = page.viewport_size
    if initial_viewport != _REVIEW_SCREEN_SIZE:
        page.set_viewport_size(_REVIEW_SCREEN_SIZE)
        page.wait_for_timeout(300)
    screenshot_dir = os.environ.get("MMFEM_UI_SCREENSHOT_DIR")
    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "00-axisymmetric-geometry-creation.png"
        )
    sketch_bounds = page.locator("svg.solve-sketch-canvas").bounding_box()
    assert sketch_bounds is not None
    page.mouse.move(
        sketch_bounds["x"] + sketch_bounds["width"] / 2,
        sketch_bounds["y"] + sketch_bounds["height"] / 2,
    )
    page.mouse.wheel(0, 120)
    page.wait_for_function(
        """() => {
            const svg = document.querySelector('svg.solve-sketch-canvas');
            if (!svg || svg.querySelector(
                '[data-sketch-view-commit-snapshot="true"]'
            )) return false;
            const grid = svg.querySelector('#solve-sketch-grid');
            const liveGrid = svg.querySelector('#solve-sketch-live-grid');
            const visible = (element) => element
                && getComputedStyle(element).display !== 'none'
                && getComputedStyle(element).visibility !== 'hidden';
            return visible(grid) || visible(liveGrid);
        }""",
        timeout=10_000,
    )
    grid_after_wheel = grid_state()
    assert grid_after_wheel["visible"] and grid_after_wheel["childCount"] > 0
    assert grid_after_wheel["commitSnapshot"] is False
    assert workspace.model["geometry"]["regions"][0]["vertices"] == original_vertices
    assert page.locator(
        'svg.solve-sketch-canvas [data-sketch-region-id="preview-shield"]'
    ).count() > 0
    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "00b-axisymmetric-geometry-after-wheel.png"
        )

    # The production VM has no WebGPU. Keep the test on that path even when the
    # CI/browser host happens to expose the optional API.
    page.evaluate(
        """() => Object.defineProperty(navigator, 'gpu', {
            configurable: true, value: undefined
        })"""
    )
    assert page.evaluate("() => navigator.gpu === undefined")

    page.get_by_role("button", name="Preview in 3D", exact=True).click()
    deadline = time.monotonic() + 15
    while not workspace._preview_3d_active and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert workspace._preview_3d_active, workspace.message
    assert workspace._preview_3d_component is not None, workspace.message
    plot = workspace._preview_3d_plot
    assert plot is not None
    page.get_by_role("dialog").wait_for(state="visible", timeout=5_000)

    graph = page.locator(
        ".mmfem-axisymmetric-3d-preview-plot .js-plotly-plot"
    ).first
    page.wait_for_function(
        """() => {
            const graph = document.querySelector(
                '.mmfem-axisymmetric-3d-preview-plot .js-plotly-plot'
            );
            return graph && graph._fullLayout && graph.data
                && graph.data.some(trace => trace.type === 'mesh3d');
        }""",
        timeout=20_000,
    )
    assert graph.locator("canvas").count() > 0
    webgl_available = page.evaluate(
        """() => {
            const canvas = document.createElement('canvas');
            const context = canvas.getContext('webgl2') || canvas.getContext('webgl');
            if (context) context.getExtension('WEBGL_lose_context')?.loseContext();
            return Boolean(context);
        }"""
    )
    assert webgl_available, "The browser cannot create WebGL for the preview fallback."
    webgl_warning = page.locator(
        ".mmfem-axisymmetric-3d-preview-webgl-warning"
    )
    page.wait_for_function(
        """() => document.querySelector(
            '.mmfem-axisymmetric-3d-preview-webgl-warning'
        )?.getAttribute('data-webgl-check') === 'available'""",
        timeout=10_000,
    )
    assert not webgl_warning.is_visible()
    traces = graph.evaluate(
        "graph => graph.data.map(trace => ({type: trace.type, meta: trace.meta}))"
    )
    assert any(
        trace["type"] == "mesh3d"
        and trace["meta"]["mmfem_region_id"] == "preview-shield"
        for trace in traces
    )
    region_trace_index = workspace._preview_3d_region_trace_indices["preview-shield"]
    region_trace = workspace._preview_3d_figure.data[region_trace_index]
    z_span = float(np.ptp(region_trace.z))
    radial_span = max(float(np.ptp(region_trace.x)), float(np.ptp(region_trace.y)))
    assert z_span == pytest.approx(0.2, rel=0.03)
    assert z_span / radial_span == pytest.approx(2.0, rel=0.15)
    assert workspace._preview_3d_figure.layout.scene.aspectmode == "data"
    assert workspace._preview_3d_figure.layout.scene.zaxis.title.text == "Z (m)"

    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "07-axisymmetric-geometry-3d-preview.png"
        )

    # The visibility table updates only the relevant display traces.
    toggle = page.locator('[aria-label="Visibility for Steel shield"]')
    toggle.wait_for(state="visible", timeout=5_000)
    toggle.locator('[title="Translucent"]').click()
    page.wait_for_function(
        """regionId => {
            const graph = document.querySelector(
                '.mmfem-axisymmetric-3d-preview-plot .js-plotly-plot'
            );
            const trace = graph?.data?.find(item =>
                item.meta?.mmfem_region_id === regionId
                    && item.meta?.mmfem_kind === 'axisymmetric_region'
            );
            return trace && trace.opacity < 0.4 && trace.opacity > 0.35;
        }""",
        arg="preview-shield",
        timeout=10_000,
    )
    toggle.locator('[title="Hidden"]').click()
    assert workspace._preview_3d_figure.data[region_trace_index].visible is False
    toggle.locator('[title="Opaque"]').click()
    assert workspace._preview_3d_figure.data[region_trace_index].visible is True
    assert workspace._preview_3d_figure.data[region_trace_index].opacity == 1.0

    # Resize, zoom and rotate the actual browser viewport, then return to editing.
    preview_bounds = graph.bounding_box()
    assert preview_bounds is not None and preview_bounds["width"] > 400
    page.mouse.move(
        preview_bounds["x"] + preview_bounds["width"] / 2,
        preview_bounds["y"] + preview_bounds["height"] / 2,
    )
    page.mouse.wheel(0, 120)
    page.wait_for_timeout(250)
    page.mouse.down()
    page.mouse.move(
        preview_bounds["x"] + preview_bounds["width"] / 2 + 90,
        preview_bounds["y"] + preview_bounds["height"] / 2 + 45,
        steps=5,
    )
    page.mouse.up()
    if initial_viewport:
        page.set_viewport_size(initial_viewport)
    page.wait_for_timeout(350)
    resized_bounds = graph.bounding_box()
    assert resized_bounds is not None
    assert resized_bounds["width"] > 400 and resized_bounds["height"] > 300

    page.get_by_role("dialog").get_by_role(
        "button", name="Exit 3D Preview", exact=True
    ).click()
    deadline = time.monotonic() + 5
    while (
        workspace._preview_3d_active or workspace._preview_3d_component is not None
    ) and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert workspace._preview_3d_active is False
    assert workspace._preview_3d_component is None
    assert workspace.model["geometry"]["regions"][0]["vertices"] == original_vertices
    grid_after = grid_state()
    assert grid_after["visible"] and grid_after["childCount"] > 0
    assert grid_after["regionCount"] == grid_before["regionCount"]
    assert page.locator("svg.solve-sketch-canvas").is_visible()
    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "08-axisymmetric-geometry-after-preview.png"
        )

    # Simulate a machine with neither renderer. The UI must replace the plot
    # with explicit recovery guidance instead of leaving a blank dialog.
    page.evaluate(
        """() => {
            const original = HTMLCanvasElement.prototype.getContext;
            HTMLCanvasElement.prototype.getContext = function(type, ...args) {
                if (['webgl2', 'webgl', 'experimental-webgl'].includes(type)) return null;
                return original.call(this, type, ...args);
            };
        }"""
    )
    page.get_by_role("button", name="Preview in 3D", exact=True).click()
    page.wait_for_function(
        """() => {
            const warning = document.querySelector(
                '.mmfem-axisymmetric-3d-preview-webgl-warning'
            );
            const plot = document.querySelector(
                '.mmfem-axisymmetric-3d-preview-plot'
            );
            return warning && warning.getAttribute('data-webgl-check') === 'unavailable'
                && warning.getClientRects().length > 0
                && getComputedStyle(warning).display !== 'none'
                && plot && getComputedStyle(plot).display === 'none';
        }""",
        timeout=10_000,
    )
    warning = page.locator(".mmfem-axisymmetric-3d-preview-webgl-warning")
    assert "Enable browser hardware acceleration" in warning.inner_text()
    assert warning.is_visible()
    assert page.locator(
        ".mmfem-axisymmetric-3d-preview-plot"
    ).evaluate("element => getComputedStyle(element).display === 'none'")
    if screenshot_dir:
        _capture_review_screenshot(
            page, Path(screenshot_dir) / "09-axisymmetric-preview-no-webgl-guidance.png"
        )
    page.get_by_role("dialog").get_by_role(
        "button", name="Exit 3D Preview", exact=True
    ).click()
    deadline = time.monotonic() + 5
    while (
        workspace._preview_3d_active or workspace._preview_3d_component is not None
    ) and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert workspace._preview_3d_active is False
    assert workspace._preview_3d_component is None
