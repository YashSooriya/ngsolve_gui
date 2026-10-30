from types import SimpleNamespace

from ngapp.components import Div

from ngsolve_gui.app import NGSolveGui
from ngsolve_gui import cerbsim_style as cb
from ngsolve_gui.function import FunctionComponent
from ngsolve_gui.webgpu_tab import WebgpuTab


def test_solution_views_use_a_page_level_labeled_toolbar():
    component = FunctionComponent.__new__(FunctionComponent)
    mesh_or_geometry_view = WebgpuTab.__new__(WebgpuTab)

    assert component._uses_page_toolbar()
    assert mesh_or_geometry_view._uses_page_toolbar()


def test_page_toolbar_action_has_a_visible_word_label():
    component = SimpleNamespace(_page_toolbar_enabled=True)
    button = WebgpuTab._vtool(
        component,
        "mdi-overscan",
        "Fit view  ·  r",
        lambda: None,
        label="Fit view",
    )

    assert button.ui_label == "Fit view"
    assert button.ui_icon == "mdi-overscan"


def test_page_toolbar_and_clip_controls_are_outside_the_viewport_overlay():
    component = WebgpuTab.__new__(WebgpuTab)
    component._page_toolbar_enabled = True
    component._tool_dock = Div("Tools")
    component._clip_toolbar = Div("Clip controls")
    component._viewport = Div()
    component._viewport._update_frontend = lambda *args, **kwargs: None
    component.ui_slots = {}
    component._update_frontend = lambda *args, **kwargs: None
    canvas = Div("Canvas")
    canvas_overlay = Div("Legend")
    pick_overlay = Div("Pick")
    overlays = [
        canvas,
        component._tool_dock,
        pick_overlay,
        component._clip_toolbar,
        canvas_overlay,
    ]

    component._sync_viewport_layout(overlays)

    assert component._page_toolbar_children() == [
        component._tool_dock, component._clip_toolbar
    ]
    assert component.ui_children == [component._viewport]
    assert component._viewport.ui_children == [
        canvas,
        pick_overlay,
        canvas_overlay,
    ]


def test_active_postprocess_toolbar_is_hosted_above_side_panels():
    host = SimpleNamespace(ui_children=[], ui_hidden=True)
    tools = [Div("Camera controls"), Div("Clipping controls")]
    component = SimpleNamespace(_page_toolbar_children=lambda: tools)
    app = SimpleNamespace(
        _post_process_toolbar=host,
        _open_solutions_toolbar_button=Div("Open solutions"),
    )

    NGSolveGui._sync_post_process_toolbar(app, component)

    assert host.ui_children[0] is app._open_solutions_toolbar_button
    assert host.ui_children[1].ui_class == cb.vp_sep
    assert host.ui_children[2:] == tools
    assert host.ui_hidden is False


def test_view_without_page_toolbar_keeps_the_existing_overlay_layout():
    component = WebgpuTab.__new__(WebgpuTab)
    component._page_toolbar_enabled = False
    component._tool_dock = Div("Tools")
    component._clip_toolbar = None
    component.ui_slots = {}
    component._update_frontend = lambda *args, **kwargs: None
    overlays = [Div("Canvas"), component._tool_dock]

    component._sync_viewport_layout(overlays)

    assert component.ui_children == overlays


def test_3d_postprocess_camera_actions_target_the_active_result():
    result = SimpleNamespace(
        reset_camera=lambda: "result-fit",
        set_camera_view=lambda view: f"result-{view}",
    )
    model = SimpleNamespace(
        reset_camera=lambda: "model-fit",
        set_camera_view=lambda view: f"model-{view}",
    )
    app = SimpleNamespace(
        _active_project_type="3d",
        _workspace_mode="post_process",
        _three_d_workspace=model,
        tab_panel=SimpleNamespace(comp=result),
    )
    app._active_view_action = lambda method, *args: NGSolveGui._active_view_action(
        app, method, *args
    )

    assert NGSolveGui._menu_fit_view(app) == "result-fit"
    assert NGSolveGui._menu_camera_view(app, "isometric") == "result-isometric"


def test_3d_solve_camera_actions_still_target_the_model_workspace():
    model = SimpleNamespace(
        reset_camera=lambda: "model-fit",
        set_camera_view=lambda view: f"model-{view}",
    )
    app = SimpleNamespace(
        _active_project_type="3d",
        _workspace_mode="solve",
        _three_d_workspace=model,
    )
    app._active_view_action = lambda method, *args: NGSolveGui._active_view_action(
        app, method, *args
    )

    assert NGSolveGui._menu_fit_view(app) == "model-fit"
    assert NGSolveGui._menu_camera_view(app, "isometric") == "model-isometric"
