from types import SimpleNamespace

from ngapp.components import Div

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

    assert component.ui_children == [
        component._tool_dock,
        component._clip_toolbar,
        component._viewport,
    ]
    assert component._viewport.ui_children == [
        canvas,
        pick_overlay,
        canvas_overlay,
    ]


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
