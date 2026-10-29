from types import SimpleNamespace

from ngapp.components import Component

from ngsolve_gui.three_d_workspace import ThreeDWorkspace


def test_three_d_workspace_is_persistent_and_keeps_unsaved_imports(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    assert "mmfem-3d-project-preview" in workspace._preview.ui_class
    assert workspace.ui_hidden is True
    assert workspace._tree_panel is not None
    assert workspace._properties_panel is not None
    assert len(workspace.ui_children) == 3

    assert workspace.project["geometries"] == []
    assert workspace.project["wire_trace_sets"] == []
    assert workspace._read_units(workspace._geometry_units) == "auto"
    workspace._geometry_unit = "mm"
    assert workspace._read_units(workspace._geometry_units) == "mm"
    assert workspace.show_new() is True
    assert workspace.ui_hidden is False
    workspace.project["name"] = "Unsaved shield"
    workspace._mark_changed()

    assert workspace.show_new() is False
    assert workspace.ui_hidden is False
    assert workspace.project["name"] == "Unsaved shield"
    assert workspace.dirty is True


def test_three_d_tree_selection_updates_the_properties_panel(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    workspace.project["geometries"] = [{
        "id": "geometry-1",
        "name": "shield.step",
        "material": "Shield",
        "units": "mm",
        "asset": "assets/geometry/shield.step",
    }]
    workspace._rebuild_items()

    workspace.select_item("geometry", "geometry-1")

    assert workspace._selection == ("geometry", "geometry-1")
    assert workspace._properties.ui_children[0].ui_children == ["Geometry model"]
    assert workspace._geometry_items.ui_children[0].ui_children[1].ui_children == ["shield.step"]


def test_preview_worker_dispatches_ui_updates_to_the_application_loop(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    scheduled = []

    class Loop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, callback, *args):
            scheduled.append((callback, args))

    workspace = ThreeDWorkspace(SimpleNamespace(loop=Loop()))
    workspace._preview_generation = 4
    drawn = []
    statuses = []
    def update(figure):
        workspace._preview.data = {"obj": figure}
        drawn.append(True)
    workspace._preview = SimpleNamespace(data=None, update_figure=update)
    workspace._set_status = lambda message, **kwargs: statuses.append((message, kwargs))
    import plotly.graph_objects as go
    figure = go.Figure()

    workspace._dispatch_preview_result(4, figure=figure)

    assert drawn == []
    assert len(scheduled) == 1
    callback, args = scheduled.pop()
    callback(*args)
    assert workspace._preview.data == {"obj": figure}
    assert drawn == [True]
    assert statuses[0][0].startswith("Preview ready")

    workspace._dispatch_preview_result(3, figure=object())
    callback, args = scheduled.pop()
    callback(*args)
    assert len(drawn) == 1


def test_view_controls_keep_geometry_and_plot_canvas(monkeypatch):
    import plotly.graph_objects as go

    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    assert "mmfem-3d-project-preview" in workspace._preview.ui_class
    figure = go.Figure(go.Mesh3d(x=[0, 1, 0], y=[0, 0, 1], z=[0, 0, 0], i=[0], j=[1], k=[2]))
    workspace._preview._figures = [figure]
    calls = []

    class FakePlotly:
        def _call_method_ignore_return(self, method, args):
            calls.append((method, args))

    class FakePlot:
        _plotly = FakePlotly()
        _js_component = object()

    workspace._preview._plots = [FakePlot()]
    workspace._preview.redraw = lambda: (_ for _ in ()).throw(AssertionError("Full plot redraw used"))
    workspace._rebuild_preview = lambda: (_ for _ in ()).throw(AssertionError("View changes rebuilt geometry"))
    workspace.set_camera_view("top")
    assert figure.layout.scene.camera.eye.z == 1.8
    workspace.toggle_axes()
    assert figure.layout.scene.xaxis.visible is False
    workspace.toggle_transparency()
    assert figure.data[0].opacity == 0.4
    assert [method for method, _args in calls] == ["relayout", "relayout", "restyle"]
    assert calls[0][1][1]["scene.camera"]["eye"]["z"] == 1.8
    assert calls[2][1][2] == [0]
