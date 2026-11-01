from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
from ngapp.e2e import app_test
from playwright.sync_api import Page

from ngsolve_gui.axisymmetric_model import (
    builtin_materials,
    new_model,
    new_studies,
    validate_model,
)


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


@app_test("ngsolve_gui.appconfig")
def test_axisymmetric_geometry_creation_renders_3d_preview(page: Page, app):
    page.locator(".q-layout").wait_for(state="visible", timeout=15_000)
    assert app._start_new_problem() is True
    workspace = app.solve_workspace
    workspace.set_model(
        _axisymmetric_preview_model(),
        new_studies(),
        {"schema_version": 1, "active_section": "geometry", "camera": "fit"},
    )
    app._update()

    page.wait_for_function(
        """() => {
            const canvas = document.querySelector('svg.solve-sketch-canvas');
            return canvas && canvas.querySelector(
                '[data-sketch-region-id="preview-shield"]'
            );
        }""",
        timeout=10_000,
    )

    screenshot_dir = os.environ.get("MMFEM_UI_SCREENSHOT_DIR")
    if screenshot_dir:
        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        page.screenshot(
            path=str(Path(screenshot_dir) / "00-axisymmetric-geometry-creation.png"),
            full_page=True,
        )
    page.get_by_role("button", name="Preview in 3D", exact=True).click()
    deadline = time.monotonic() + 15
    while not workspace._preview_3d_active and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert workspace._preview_3d_active, workspace.message
    preview = workspace._preview_3d_component
    assert preview is not None, workspace.message

    page.get_by_role("dialog").wait_for(state="visible", timeout=5_000)

    from ngapp.e2e_webgpu import _ensure_scene_ready, capture_canvas

    _ensure_scene_ready(page, preview)
    page.wait_for_function(
        """() => {
            const canvas = document.querySelector(
                'canvas.mmfem-axisymmetric-3d-preview-canvas'
            );
            if (!canvas) return false;
            const rect = canvas.getBoundingClientRect();
            return canvas.isConnected && canvas.width > 0 && canvas.height > 0
                && rect.width > 400 && rect.height > 300;
        }""",
        timeout=15_000,
    )
    assert workspace._preview_3d_active is True
    assert len(workspace._preview_3d_renderers) == 1
    assert workspace._preview_3d_scene is not None
    assert all(
        float(value) == float(value)
        for point in workspace._preview_3d_scene.bounding_box
        for value in point
    )
    rendered = capture_canvas(page, preview)
    border = np.concatenate(
        (
            rendered[:16].reshape(-1, rendered.shape[2]),
            rendered[-16:].reshape(-1, rendered.shape[2]),
            rendered[:, :16].reshape(-1, rendered.shape[2]),
            rendered[:, -16:].reshape(-1, rendered.shape[2]),
        )
    )
    background = np.median(border[..., :3], axis=0)
    foreground_fraction = float(
        np.mean(np.linalg.norm(rendered[..., :3] - background, axis=2) > 18)
    )
    assert foreground_fraction > 0.05, (
        "WebGPU readback contains no visible revolved geometry; "
        f"foreground fraction={foreground_fraction:.4f}"
    )

    if screenshot_dir:
        page.screenshot(
            path=str(Path(screenshot_dir) / "07-axisymmetric-geometry-3d-preview.png"),
            full_page=True,
        )
        from PIL import Image

        Image.fromarray(rendered, mode="RGBA").save(
            Path(screenshot_dir) / "07-axisymmetric-geometry-3d-preview-webgpu-readback.png"
        )

    page.get_by_role("dialog").get_by_role(
        "button", name="Exit 3D Preview", exact=True
    ).click()
    deadline = time.monotonic() + 5
    while workspace._preview_3d_active and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert workspace._preview_3d_active is False
    assert workspace._preview_3d_component is None
    page.locator("svg.solve-sketch-canvas").wait_for(state="visible", timeout=5_000)
