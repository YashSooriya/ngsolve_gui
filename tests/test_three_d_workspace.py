from types import SimpleNamespace

from ngapp.components import Component
import plotly.graph_objects as go

from ngsolve_gui.three_d_workspace import ThreeDWorkspace


def test_three_d_workspace_is_persistent_and_keeps_unsaved_imports(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    assert "mmfem-3d-project-preview" in workspace._preview.ui_class
    assert workspace.ui_hidden is True
    assert workspace._tree_panel is not None
    assert workspace._properties_panel is not None
    assert len(workspace.ui_children) == 3
    assert "height:100%" in workspace.ui_style
    assert "flex:1 1 auto" in workspace._body.ui_style
    assert "; height:0;" not in workspace._tree_splitter.ui_style
    assert "; height:0;" not in workspace._inner_splitter.ui_style
    assert workspace._preview.ui_hidden is True
    assert workspace._preview_empty_state.ui_hidden is False
    assert "mmfem-workspace-panel-header" in workspace._tree_panel.ui_children[0].ui_class
    assert "mmfem-workspace-panel-header" in workspace._properties_panel.ui_children[0].ui_class

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


def test_3d_side_panels_collapse_and_reopen_without_losing_width(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())

    workspace.toggle_tree()
    assert workspace._tree_splitter.ui_model_value == 0
    assert workspace._tree_reopen_tab.ui_hidden is False
    workspace.toggle_tree()
    assert workspace._tree_splitter.ui_model_value == 230
    assert workspace._tree_reopen_tab.ui_hidden is True

    workspace.toggle_properties()
    assert workspace._inner_splitter.ui_model_value == 0
    assert workspace._properties_reopen_tab.ui_hidden is False
    workspace.toggle_properties()
    assert workspace._inner_splitter.ui_model_value == 300
    assert workspace._properties_reopen_tab.ui_hidden is True


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


def test_viewport_selection_syncs_to_tree_properties_and_preview(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    workspace.project["geometries"] = [{
        "id": "geometry-1",
        "name": "shield.step",
        "material": "Shield",
        "units": "mm",
        "asset": "assets/geometry/shield.step",
    }]
    workspace.project["wire_trace_sets"] = [{
        "id": "trace-set-1",
        "name": "Golay X",
        "material": "Air",
        "traces": [{"name": "golay_x_trace.txt", "filename": "assets/traces/golay.txt"}],
    }]
    figure = go.Figure([
        go.Mesh3d(
            x=[0, 1, 0], y=[0, 0, 1], z=[0, 0, 0], i=[0], j=[1], k=[2],
            meta={"mmfem_kind": "geometry", "mmfem_item_id": "geometry-1", "mmfem_base_color": "#6886ac"},
        ),
        go.Scatter3d(
            x=[0, 1], y=[0, 1], z=[0, 0], mode="lines",
            meta={"mmfem_kind": "trace_set", "mmfem_item_id": "trace-set-1", "mmfem_base_color": "#69a77b"},
        ),
    ])
    calls = []

    class FakePlotly:
        def _call_method_ignore_return(self, method, args):
            calls.append((method, args))

    class FakePlot:
        _plotly = FakePlotly()
        _js_component = object()

    workspace._preview._figures = [figure]
    workspace._preview._plots = [FakePlot()]
    workspace._rebuild_items()

    workspace._on_preview_plotly_click(0, 1)

    assert workspace._selection == ("trace_set", "trace-set-1")
    assert workspace._properties.ui_children[0].ui_children == ["Wire trace set"]
    assert workspace._trace_items.ui_children[0].ui_children[1].ui_children == ["Golay X"]
    assert figure.data[1].line.color == "#f59e0b"
    assert figure.data[1].line.width == 8
    assert any(
        method == "restyle" and args[1].get("line.color") == "#f59e0b"
        for method, args in calls
    )

    workspace._on_preview_plotly_click(0, 99)
    assert workspace._selection == ("trace_set", "trace-set-1")


def test_material_reference_options_include_existing_libraries_and_preserve_unknowns(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)

    class Library:
        def load(self):
            return [
                {"id": "custom-copper", "name": "Copper", "properties": {}},
                {"id": "custom-aluminium", "name": "Custom aluminium", "properties": {}},
            ]

    workspace = ThreeDWorkspace(SimpleNamespace(), material_library=Library())
    options = workspace._material_options("Legacy shield")
    names = [option["value"] for option in options]

    assert names.count("Copper") == 1
    assert "Custom aluminium" in names
    assert names[-1] == "Legacy shield"
    assert options[-1]["label"] == "Legacy shield (current project value)"


def test_material_reference_selection_updates_project_and_tree_badge(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    item = {
        "id": "geometry-1",
        "name": "shield.step",
        "material": "Legacy shield",
        "units": "mm",
        "asset": "assets/geometry/shield.step",
    }
    workspace.project["geometries"] = [item]
    workspace.select_item("geometry", "geometry-1")

    workspace._set_material_reference(item, SimpleNamespace(value="Copper"))

    assert item["material"] == "Copper"
    assert workspace.dirty is True
    assert workspace._geometry_items.ui_children[0].ui_children[2].ui_children == ["Copper"]
    assert "3D solver material use is not available yet" in workspace._properties.ui_children[3].ui_children[0]


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
