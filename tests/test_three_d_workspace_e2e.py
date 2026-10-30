from pathlib import Path
import tempfile
import time

from ngapp.e2e import app_test
from playwright.sync_api import Page

from ngsolve_gui.three_d_project import add_step_geometry, add_wire_trace, new_3d_project


@app_test("ngsolve_gui.appconfig")
def test_three_d_viewport_renders_and_selects_model_items(page: Page, app):
    fixtures = Path(__file__).parent / "fixtures"
    project = new_3d_project("Viewport interaction check")
    assets = {}
    geometry = add_step_geometry(
        project,
        assets,
        "magnet_shield_4k.step",
        (fixtures / "magnet_shield_4k.step").read_bytes(),
        units="mm",
        material="Shield",
    )
    geometry["mesh_maxh_mm"] = 30.0
    add_wire_trace(
        project,
        assets,
        "golay_x_trace.txt",
        (fixtures / "golay_x_trace.txt").read_bytes(),
    )
    assert app._start_3d_project_from_menu()
    workspace = app._three_d_workspace
    assert workspace.load_project_data(project, assets)
    app._update()

    graph = page.locator(".mmfem-3d-viewport .js-plotly-plot").first
    page.wait_for_function(
        """() => {
            const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
            return graph && graph._fullLayout && graph._mmfem_click_bound
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
    assert workspace.generate_mesh() is True
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
    page.wait_for_timeout(2500)
    page.screenshot(
        path=str(Path(tempfile.gettempdir()) / "mmfem_stage3_3d_volume_mesh.png"),
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
    page.screenshot(
        path=str(Path(tempfile.gettempdir()) / "mmfem_stage3_3d_volume_mesh_compact.png"),
        full_page=True,
    )
