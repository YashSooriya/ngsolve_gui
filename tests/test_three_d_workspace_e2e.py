from pathlib import Path
import tempfile

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
    page.screenshot(
        path=str(Path(tempfile.gettempdir()) / "mmfem_stage2_3d_viewport_selected.png"),
        full_page=True,
    )
