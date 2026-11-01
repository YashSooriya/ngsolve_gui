from pathlib import Path
from types import SimpleNamespace

from ngapp.components import Component
import plotly.graph_objects as go
import numpy as np

from ngsolve_gui.three_d_workspace import ThreeDWorkspace
from ngsolve_gui.three_d_project import (
    UNASSIGNED_MATERIAL,
    add_step_geometry,
    add_wire_trace,
    new_3d_project,
    package_3d_project,
    unpack_3d_project,
)


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


def test_3d_study_waits_for_a_real_material_assignment(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    project = new_3d_project("Material assignment")
    project["materials"] = [{
        "id": "test-unassigned-material",
        "name": UNASSIGNED_MATERIAL,
        "properties": {"relative_permeability": 1.0, "electrical_conductivity": 0.0},
    }]
    assets = {}
    geometry = add_step_geometry(
        project,
        assets,
        "part.step",
        b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n",
        units="mm",
    )
    add_wire_trace(project, assets, "coil.txt", b"0 0 0\n0 0 1\n")
    workspace.project = project
    workspace.assets = assets
    workspace._rebuild_items()

    assert geometry["material"] == UNASSIGNED_MATERIAL
    assert workspace._run_study_button.ui_disable is True

    workspace._set_material_reference(geometry, SimpleNamespace(value="Copper"))

    assert geometry["material"] == "Copper"
    assert workspace._run_study_button.ui_disable is False

    geometry["material"] = UNASSIGNED_MATERIAL
    workspace._update_study_controls()
    assert workspace._has_resolved_study_materials() is False
    assert workspace._run_study_button.ui_disable is True


def test_3d_side_panels_collapse_and_reopen_without_losing_width(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())

    workspace.toggle_tree()
    assert workspace._tree_splitter.ui_model_value == 0
    assert workspace._tree_reopen_tab.ui_hidden is False
    workspace.toggle_tree()
    assert workspace._tree_splitter.ui_model_value == 250
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
    workspace._rebuild_preview = lambda: None
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
    labels = {getattr(child, "ui_label", None) for child in workspace._properties.ui_children}
    assert {"Default material", "Length units", "Maximum element size (mm)"} <= labels


def test_named_domain_mesh_size_invalidates_preview_mesh_and_persists(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    geometry = {
        "id": "geometry-1",
        "name": "shield.step",
        "material": "Copper",
        "units": "mm",
        "asset": "assets/geometry/shield.step",
        "domain_selections": [{
            "id": "selection-1",
            "name": "Inner shield",
            "domain_ids": [0],
            "domain_tags": ["MMFEM_SOLID_000000000000000000000001"],
        }],
    }
    workspace.project["geometries"] = [geometry]
    selection = geometry["domain_selections"][0]
    workspace._mesh_results[geometry["id"]] = {"status": "ready"}
    workspace._rebuild_preview = lambda: None

    workspace.select_item("named_selection", selection["id"])
    labels = {getattr(child, "ui_label", None) for child in workspace._properties.ui_children}
    assert "Local max element size (mm)" in labels

    assert workspace._set_named_selection_mesh_maxh(
        geometry, selection, SimpleNamespace(value="2.5")
    ) is True
    assert selection["mesh_maxh_mm"] == 2.5
    assert geometry["id"] not in workspace._mesh_results
    assert workspace.dirty is True


def test_named_domain_mesh_size_rejects_conflicting_overlapping_selections(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    workspace._rebuild_preview = lambda: None
    geometry = {
        "id": "geometry-1",
        "name": "shield.step",
        "domain_selections": [
            {"id": "selection-1", "name": "Inner shield", "domain_ids": [0], "domain_tags": ["MMFEM_SOLID_000000000000000000000001"], "mesh_maxh_mm": 2.0},
            {"id": "selection-2", "name": "Refined shield", "domain_ids": [0], "domain_tags": ["MMFEM_SOLID_000000000000000000000001"], "mesh_maxh_mm": 1.0},
        ],
    }

    assert workspace._set_named_selection_mesh_maxh(
        geometry, geometry["domain_selections"][1], SimpleNamespace(value="3")
    ) is False
    assert geometry["domain_selections"][1]["mesh_maxh_mm"] == 1.0


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
    workspace._rebuild_preview = lambda: None
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
    workspace._rebuild_preview = lambda: None
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
    assert "Material data is embedded in the project." in workspace._properties.ui_children[3].ui_children[0]


def test_preview_worker_dispatches_ui_updates_to_the_application_loop(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    scheduled = []

    class Loop:
        def is_running(self):
            return True

        def call_soon_threadsafe(self, callback, *args):
            scheduled.append((callback, args))

        def call_later(self, _delay, callback, *args):
            callback(*args)

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


def test_preview_selection_updates_are_coalesced_on_the_application_loop(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)

    class Handle:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    class Loop:
        def __init__(self):
            self.callbacks = []

        def is_running(self):
            return True

        def call_later(self, delay, callback):
            handle = Handle()
            self.callbacks.append((delay, callback, handle))
            return handle

    loop = Loop()
    workspace = ThreeDWorkspace(SimpleNamespace(loop=loop))
    calls = []
    workspace._update_preview_selection = lambda: calls.append("restyled")

    workspace._schedule_preview_selection_update()
    first_handle = workspace._preview_selection_update_handle
    workspace._schedule_preview_selection_update()

    assert first_handle.cancelled is True
    assert [entry[0] for entry in loop.callbacks] == [0.05, 0.05]
    assert calls == []
    loop.callbacks[-1][1]()
    assert calls == ["restyled"]
    assert workspace._preview_selection_update_handle is None


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


def test_mesh_size_changes_are_persisted_and_invalidate_the_display_mesh(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    item = {
        "id": "geometry-1",
        "name": "shield.step",
        "material": "Shield",
        "units": "mm",
        "asset": "assets/geometry/shield.step",
        "sha256": "shield-hash",
    }
    workspace.project["geometries"] = [item]
    workspace.assets[item["asset"]] = b"STEP"
    workspace._selection = ("geometry", item["id"])
    workspace._rebuild_items()
    workspace._rebuild_properties()
    workspace._mesh_results[item["id"]] = {"status": "ready"}
    previews = []
    workspace._rebuild_preview = lambda: previews.append(True)

    workspace._set_mesh_maxh(item, SimpleNamespace(value="12.5"))

    assert item["mesh_maxh_mm"] == 12.5
    assert workspace.dirty is True
    assert item["id"] not in workspace._mesh_results
    assert previews == [True]
    assert "Mesh not generated." in workspace._mesh_property_status.ui_children[0]
    assert workspace._generate_mesh_button.ui_disable is False


def test_mesh_result_updates_tree_inspector_and_viewport_state(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    item = {
        "id": "geometry-1",
        "name": "shield.step",
        "material": "Shield",
        "units": "mm",
        "asset": "assets/geometry/shield.step",
        "sha256": "shield-hash",
    }
    workspace.project["geometries"] = [item]
    workspace._selection = ("geometry", item["id"])
    workspace._mesh_generation = 3
    workspace._mesh_job = {
        "generation": 3,
        "geometry_id": item["id"],
        "sha256": item["sha256"],
        "maxh_mm": 30.0,
    }
    workspace._mesh_worker_active = True
    rebuilt = []
    workspace._rebuild_preview = lambda: rebuilt.append(True)

    workspace._publish_mesh_result(
        3,
        item["id"],
        item["sha256"],
        30.0,
        {
            "mesh": object(),
            "vertices_mm": [],
            "triangles": [],
            "edges": [],
            "volume_elements": 1200,
            "surface_faces": 480,
            "wireframe_available": True,
        },
        None,
    )

    assert workspace._mesh_results[item["id"]]["status"] == "ready"
    assert "1,200 volume elements" in workspace._mesh_property_status.ui_children[0]
    assert workspace._geometry_items.ui_children[0].ui_children[2].ui_name == "mdi-grid"
    assert workspace._generate_mesh_button.ui_disable is False
    assert rebuilt == [True]
    assert "Mesh ready" in workspace._status.ui_children[0]


def test_selected_mesh_figure_is_styled_before_asynchronous_plotly_render(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    workspace._selection = ("geometry", "geometry-1")
    figure = go.Figure([
        go.Mesh3d(
            x=[0, 1, 0], y=[0, 0, 1], z=[0, 0, 0], i=[0], j=[1], k=[2],
            meta={"mmfem_kind": "geometry", "mmfem_item_id": "geometry-1", "mmfem_base_color": "#6886ac"},
        ),
        go.Scatter3d(
            x=[0, 1], y=[0, 0], z=[0, 0], mode="lines",
            meta={"mmfem_kind": "geometry", "mmfem_item_id": "geometry-1", "mmfem_base_color": "#33475b", "mmfem_mesh_edges": True},
        ),
    ])

    updates = workspace._style_preview_figure(figure)

    assert figure.data[0].color == "#f59e0b"
    assert figure.data[1].line.color == "#33475b"
    assert figure.data[1].line.width == 2.5
    assert updates[0]["color"] == "#f59e0b"
    assert updates[1]["line.color"] == "#33475b"


def test_boundary_selection_updates_only_the_mesh_overlay_trace(monkeypatch):
    from ngsolve_gui.three_d_preview import preview_figure

    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    geometry = {
        "id": "geometry-1",
        "name": "shield.step",
        "material": "Copper",
        "units": "mm",
        "asset": "assets/geometry/shield.step",
    }
    workspace.project["geometries"] = [geometry]
    workspace.assets[geometry["asset"]] = b"STEP"
    workspace._selection = ("geometry", geometry["id"])
    workspace._selection_draft.update({
        "geometry_id": geometry["id"],
        "kind": "boundary",
        "region_ids": [],
    })
    workspace._mesh_results[geometry["id"]] = {
        "status": "ready",
        "vertices_mm": np.asarray([
            [0, 0, 0], [1000, 0, 0], [0, 1000, 0], [1000, 1000, 0],
        ], dtype=np.float32),
        "triangles": np.asarray([[0, 1, 2], [1, 3, 2]], dtype=np.int32),
        "boundary_region_ids": np.asarray([4, 4], dtype=np.int32),
    }
    figure = preview_figure({
        "geometry": [{
            "id": geometry["id"],
            "name": geometry["name"],
            "vertices": workspace._mesh_results[geometry["id"]]["vertices_mm"] * 0.001,
            "triangles": workspace._mesh_results[geometry["id"]]["triangles"],
            "edges": np.empty((0, 2), dtype=np.int32),
            "generated_mesh": True,
            "boundary_region_ids": [4, 4],
            "highlighted_boundary_ids": [],
        }],
        "traces": [],
    })
    calls = []

    class FakePlotly:
        def _call_method_ignore_return(self, method, args):
            calls.append((method, args))

    class FakePlot:
        _plotly = FakePlotly()
        _js_component = object()

    workspace._preview._figures = [figure]
    workspace._preview._plots = [FakePlot()]
    workspace._rebuild_preview = lambda: (_ for _ in ()).throw(
        AssertionError("Boundary selection rebuilt the complete 3D scene")
    )
    workspace._schedule_preview_selection_update = lambda: None

    workspace._set_selection_draft_regions(
        geometry, SimpleNamespace(value=[4])
    )

    overlay = figure.data[1]
    assert overlay.visible is True
    assert len(overlay.x) == 12
    assert len(calls) == 1
    assert calls[0][0] == "restyle"
    assert calls[0][1][2] == [1]
    assert calls[0][1][1]["x"][0] == list(overlay.x)


def _workspace_with_test_mesh(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    project = new_3d_project("Named selection test")
    assets = {}
    fixture = Path(__file__).parent / "fixtures" / "magnet_shield_4k.step"
    geometry = add_step_geometry(project, assets, fixture.name, fixture.read_bytes(), units="mm")
    workspace.project = project
    workspace.assets = assets
    workspace._selection = ("geometry", geometry["id"])
    workspace._selection_draft["geometry_id"] = geometry["id"]
    workspace._mesh_results[geometry["id"]] = {
        "status": "ready",
        "volume_elements": 4,
        "surface_faces": 3,
        "boundary_region_ids": np.asarray([0, 1, 2], dtype=np.int32),
        "boundary_regions": [
            {
                "id": 0,
                "name": "Boundary 1",
                "elements": 1,
                "boundary_tag": "MMFEM_FACE_000000000000000000000000",
            },
            {
                "id": 1,
                "name": "Boundary 2",
                "elements": 1,
                "boundary_tag": "MMFEM_FACE_000000000000000000000001",
            },
            {
                "id": 2,
                "name": "Boundary 3",
                "elements": 1,
                "boundary_tag": "MMFEM_FACE_000000000000000000000002",
            },
        ],
        "domain_regions": [{
            "id": 0,
            "name": "Shield",
            "elements": 4,
            "domain_tag": "MMFEM_SOLID_000000000000000000000001",
        }],
        "tetra_quality": {
            "minimum": 0.3,
            "p05": 0.4,
            "median": 0.7,
            "mean": 0.65,
            "below_0_1": 0,
            "below_0_1_percent": 0.0,
        },
    }
    workspace._rebuild_preview = lambda: None
    workspace._rebuild_items()
    workspace._rebuild_properties()
    return workspace, geometry


def test_mesh_quality_and_named_domain_boundary_selections_persist(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    workspace._set_selection_draft_regions(geometry, SimpleNamespace(value=[0, 2]))
    workspace._set_selection_draft_name(SimpleNamespace(value="Symmetry surfaces"))

    assert workspace._save_named_selection(geometry) is True

    selection = geometry["boundary_selections"][0]
    assert selection["name"] == "Symmetry surfaces"
    assert selection["boundary_ids"] == [0, 2]
    assert selection["boundary_tags"] == [
        "MMFEM_FACE_000000000000000000000000",
        "MMFEM_FACE_000000000000000000000002",
    ]
    assert "median 0.700" in workspace._mesh_quality_message(geometry["id"])
    assert workspace._selection == ("named_selection", selection["id"])
    assert workspace._active_boundary_region_ids(geometry["id"]) == [0, 2]

    restored, _assets = unpack_3d_project(package_3d_project(workspace.project, workspace.assets))
    assert restored["geometries"][0]["boundary_selections"] == [selection]

    workspace.select_item("named_selection", selection["id"])
    assert workspace._selection_draft["region_ids"] == [0, 2]
    labels = {getattr(child, "ui_label", None) for child in workspace._properties.ui_children}
    assert "Length units" not in labels
    assert "Maximum element size (mm)" not in labels
    assert "Fallback for unassigned domains" not in labels


def test_named_boundary_inspector_only_shows_boundary_properties(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    workspace._set_selection_draft_regions(geometry, SimpleNamespace(value=[1]))
    workspace._set_selection_draft_name(SimpleNamespace(value="Outer boundary"))

    assert workspace._save_named_selection(geometry) is True

    selection = geometry["boundary_selections"][0]
    workspace.select_item("named_selection", selection["id"])
    labels = {getattr(child, "ui_label", None) for child in workspace._properties.ui_children}
    assert "Selection name" in labels
    assert "Region material" not in labels
    assert "Default material" not in labels
    assert "Length units" not in labels
    assert "Maximum element size (mm)" not in labels
    assert "Boundary condition" in labels


def test_default_boundary_selection_can_include_regions_without_step_face_tags(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    workspace._mesh_results[geometry["id"]]["boundary_regions"][0]["boundary_tag"] = None
    workspace._set_selection_draft_regions(geometry, SimpleNamespace(value=[0]))
    workspace._set_selection_draft_name(SimpleNamespace(value="Outer air boundary"))

    assert workspace._save_named_selection(geometry) is True

    selection = geometry["boundary_selections"][0]
    assert selection["boundary_ids"] == [0]
    assert "boundary_tags" not in selection


def test_explicit_boundary_condition_rejects_regions_without_step_face_tags(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    workspace._mesh_results[geometry["id"]]["boundary_regions"][0]["boundary_tag"] = None
    workspace._set_selection_draft_regions(geometry, SimpleNamespace(value=[0]))
    workspace._set_selection_draft_name(SimpleNamespace(value="Unmapped face"))
    workspace._set_selection_draft_boundary_condition(
        SimpleNamespace(value="magnetic_potential_zero")
    )

    assert workspace._save_named_selection(geometry) is False
    assert geometry["boundary_selections"] == []


def test_legacy_boundary_selection_requires_recreation_before_condition(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    selection = {
        "id": "existing-face",
        "name": "Existing STEP boundary",
        "boundary_ids": [1],
    }
    geometry["boundary_selections"].append(selection)

    assert workspace._set_named_selection_boundary_condition(
        geometry,
        selection,
        SimpleNamespace(value="magnetic_potential_zero"),
    ) is False
    assert "boundary_condition" not in selection


def test_current_boundary_selection_can_change_supported_condition(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    selection = {
        "id": "current-face",
        "name": "Current STEP boundary",
        "boundary_ids": [1],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000001"],
    }
    geometry["boundary_selections"].append(selection)
    workspace.select_item("named_selection", selection["id"])

    assert workspace._set_named_selection_boundary_condition(
        geometry,
        selection,
        SimpleNamespace(value="magnetic_potential_zero"),
    ) is True
    assert selection["boundary_condition"] == "magnetic_potential_zero"


def test_named_boundary_selection_assigns_only_supported_time_harmonic_condition(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    workspace._set_selection_draft_regions(geometry, SimpleNamespace(value=[1]))
    workspace._set_selection_draft_name(SimpleNamespace(value="Fixed magnetic boundary"))
    workspace._set_selection_draft_boundary_condition(
        SimpleNamespace(value="magnetic_potential_zero")
    )

    assert workspace._save_named_selection(geometry) is True

    selection = geometry["boundary_selections"][0]
    assert selection["boundary_condition"] == "magnetic_potential_zero"
    assert selection["boundary_tags"] == ["MMFEM_FACE_000000000000000000000001"]
    workspace.select_item("named_selection", selection["id"])
    controls = {
        getattr(child, "ui_label", None): child
        for child in workspace._properties.ui_children
    }
    assert controls["Boundary condition"].ui_model_value == "magnetic_potential_zero"
    assert {option["value"] for option in controls["Boundary condition"].ui_options} == {
        "default",
        "magnetic_potential_zero",
        "mechanical_fixed",
    }

    assert workspace._set_named_selection_boundary_condition(
        geometry, selection, SimpleNamespace(value="default")
    ) is True
    assert "boundary_condition" not in selection
    assert selection["boundary_tags"] == ["MMFEM_FACE_000000000000000000000001"]


def test_mechanical_study_property_editor_exposes_only_relevant_configuration(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    workspace.select_item("study")
    geometry["material"] = "Stainless steel (4 K / OVC)"

    assert workspace._set_study_physics(SimpleNamespace(value="harmonic_mechanical")) is True
    assert workspace._study_node.ui_children[1].ui_children == ["Time-harmonic mechanics"]
    workspace._rebuild_properties()
    controls = {
        getattr(child, "ui_label", None): child
        for child in workspace._properties.ui_children
    }

    assert controls["Physics"].ui_model_value == "harmonic_mechanical"
    assert {option["value"] for option in controls["Physics"].ui_options} == {
        "harmonic_em",
        "harmonic_mechanical",
    }
    assert {"Body force X (N/m³)", "Body force Y (N/m³)", "Body force Z (N/m³)"} <= set(controls)
    assert "Air-region padding (model spans)" not in controls
    assert workspace._set_mechanical_body_force(0, SimpleNamespace(value="1000")) is True
    assert workspace.project["study"]["mechanical_body_force_n_m3"] == [1000.0, 0.0, 0.0]
    assert workspace._run_study_button.ui_disable is False

    geometry["boundary_selections"].append({
        "id": "fixed-face",
        "name": "Fixed support",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000000"],
        "boundary_condition": "mechanical_fixed",
    })
    workspace._update_study_controls()
    assert workspace._run_study_button.ui_disable is False

    geometry["boundary_selections"][0]["boundary_condition"] = "magnetic_potential_zero"
    workspace._update_study_controls()
    assert workspace._run_study_button.ui_disable is True


def test_invalidated_boundary_assignment_can_be_reassigned_from_current_mesh(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    selection = {
        "id": "old-support",
        "name": "Old support faces",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000099"],
        "boundary_condition": "mechanical_fixed",
    }
    geometry["boundary_selections"].append(selection)
    geometry["material"] = "Stainless steel (4 K / OVC)"
    workspace.project["study"].update(
        physics="harmonic_mechanical",
        mechanical_body_force_n_m3=[1.0, 0.0, 0.0],
    )
    workspace._rebuild_items()

    assert workspace._selection_recovery_message(geometry, "boundary", selection)
    assert workspace._run_study_button.ui_disable is True
    tree_row = workspace._named_selection_tree_item(geometry, "boundary", selection)
    assert any(
        getattr(node, "ui_children", None) == ["Needs review"]
        for node in tree_row.ui_children
    )

    workspace.select_item("named_selection", selection["id"])
    assert workspace._begin_named_selection_reassignment(
        geometry, "boundary", selection
    )
    assert workspace._selection_draft["selection_id"] == selection["id"]
    assert workspace._selection_draft["region_ids"] == []
    workspace._set_selection_draft_regions(geometry, SimpleNamespace(value=[1]))
    assert workspace._save_named_selection(geometry)

    assert selection["boundary_ids"] == [1]
    assert selection["boundary_tags"] == ["MMFEM_FACE_000000000000000000000001"]
    assert workspace._selection_recovery_message(geometry, "boundary", selection) is None
    assert workspace._selection == ("named_selection", selection["id"])
    assert workspace._run_study_button.ui_disable is False


def test_invalidated_domain_material_and_mesh_control_can_be_reassigned(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    selection = {
        "id": "old-domain",
        "name": "Old solid assignment",
        "domain_ids": [0],
        "domain_tags": ["MMFEM_SOLID_000000000000000000000099"],
        "material": "Copper",
        "mesh_maxh_mm": 2.0,
    }
    geometry["domain_selections"].append(selection)

    assert workspace._selection_recovery_message(geometry, "domain", selection)
    workspace.select_item("named_selection", selection["id"])
    assert workspace._begin_named_selection_reassignment(
        geometry, "domain", selection
    )
    workspace._set_selection_draft_regions(geometry, SimpleNamespace(value=[0]))
    assert workspace._save_named_selection(geometry)

    assert selection["domain_ids"] == [0]
    assert selection["domain_tags"] == ["MMFEM_SOLID_000000000000000000000001"]
    assert selection["material"] == "Copper"
    assert selection["mesh_maxh_mm"] == 2.0
    assert workspace._selection_recovery_message(geometry, "domain", selection) is None


def test_replace_step_keeps_saved_assignments_and_invalidates_old_mesh(monkeypatch):
    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    workspace = ThreeDWorkspace(SimpleNamespace())
    project = new_3d_project("Replace STEP test")
    assets = {}
    fixture = Path(__file__).parent / "fixtures" / "magnet_shield_4k.step"
    geometry = add_step_geometry(project, assets, fixture.name, fixture.read_bytes(), units="mm")
    geometry["material"] = "Copper"
    geometry["boundary_selections"] = [{
        "id": "keep-selection",
        "name": "Support faces",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000001"],
        "boundary_condition": "mechanical_fixed",
    }]
    workspace.project = project
    workspace.assets = assets
    workspace._mesh_results[geometry["id"]] = {"status": "ready"}
    workspace._selection = ("geometry", geometry["id"])
    workspace.app = SimpleNamespace(
        _pick_artifact=lambda **_kwargs: (str(fixture), fixture.read_bytes())
    )
    old_asset = geometry["asset"]
    old_hash = geometry["sha256"]

    assert workspace.replace_step(geometry)

    assert geometry["id"] == project["geometries"][0]["id"]
    assert geometry["asset"] != old_asset
    assert geometry["sha256"] == old_hash
    assert old_asset not in workspace.assets
    assert workspace.assets[geometry["asset"]] == fixture.read_bytes()
    assert geometry["material"] == "Copper"
    assert geometry["boundary_selections"][0]["id"] == "keep-selection"
    assert geometry["boundary_selections"][0]["boundary_tags"] == [
        "MMFEM_FACE_000000000000000000000001"
    ]
    assert geometry["id"] not in workspace._mesh_results


def test_failed_step_replacement_preserves_the_existing_asset(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    original_asset = geometry["asset"]
    original_payload = workspace.assets[original_asset]
    original_hash = geometry["sha256"]
    workspace.app = SimpleNamespace(
        _pick_artifact=lambda **_kwargs: ("broken.step", b"not a STEP file")
    )

    assert workspace.replace_step(geometry) is False

    assert geometry["asset"] == original_asset
    assert geometry["sha256"] == original_hash
    assert workspace.assets[original_asset] == original_payload


def test_named_volume_domain_selection_is_saved_with_region_indices(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    workspace._set_selection_draft_kind(geometry, SimpleNamespace(value="domain"))
    workspace._set_selection_draft_regions(geometry, SimpleNamespace(value=[0]))
    workspace._set_selection_draft_name(SimpleNamespace(value="Shield volume"))

    assert workspace._save_named_selection(geometry) is True
    assert geometry["domain_selections"] == [{
        "id": workspace._selection[1],
        "name": "Shield volume",
        "domain_ids": [0],
        "domain_tags": ["MMFEM_SOLID_000000000000000000000001"],
    }]


def test_named_volume_domain_selection_can_assign_a_project_material(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    workspace._set_selection_draft_kind(geometry, SimpleNamespace(value="domain"))
    workspace._set_selection_draft_regions(geometry, SimpleNamespace(value=[0]))
    workspace._set_selection_draft_name(SimpleNamespace(value="Copper volume"))
    workspace._set_selection_draft_material(SimpleNamespace(value="Copper"))

    assert workspace._save_named_selection(geometry) is True
    selection = geometry["domain_selections"][0]
    assert selection["material"] == "Copper"
    assert selection["domain_tags"] == ["MMFEM_SOLID_000000000000000000000001"]
    assert any(material["name"] == "Copper" for material in workspace.project["materials"])

    workspace.select_item("named_selection", selection["id"])
    controls = {
        getattr(child, "ui_label", None): child
        for child in workspace._properties.ui_children
    }
    assert "Region material" in controls
    assert "Fallback for unassigned domains" not in controls
    options = controls["Region material"].ui_options
    assert any(option["value"] == "__inherit__" for option in options)

    assert workspace._set_named_selection_material(
        selection, SimpleNamespace(value="__inherit__")
    ) is True
    assert "material" not in selection
    assert workspace._set_named_selection_material(
        selection, SimpleNamespace(value="Copper")
    ) is True
    assert selection["material"] == "Copper"


def test_plot_mesh_face_click_selects_boundary_region_and_visibility_controls(monkeypatch):
    workspace, geometry = _workspace_with_test_mesh(monkeypatch)
    figure = go.Figure([
        go.Mesh3d(
            x=[0, 1, 0], y=[0, 0, 1], z=[0, 0, 0], i=[0, 0, 0], j=[1, 1, 1], k=[2, 2, 2],
            meta={
                "mmfem_kind": "geometry",
                "mmfem_item_id": geometry["id"],
                "mmfem_base_color": "#6886ac",
                "mmfem_generated_mesh": True,
            },
        ),
        go.Scatter3d(
            x=[0, 1], y=[0, 0], z=[0, 0], mode="lines",
            meta={
                "mmfem_kind": "geometry",
                "mmfem_item_id": geometry["id"],
                "mmfem_base_color": "#33475b",
                "mmfem_mesh_edges": True,
            },
        ),
    ])
    workspace._preview._figures = [figure]

    workspace._on_preview_plotly_click(0, 0, 1)
    assert workspace._selection_draft["region_ids"] == [1]
    assert workspace._active_boundary_region_ids(geometry["id"]) == [1]
    updates = workspace._style_preview_figure(figure)
    assert figure.data[0].visible is True

    workspace.toggle_mesh_edges()
    workspace._style_preview_figure(figure)
    assert figure.data[1].visible is False
    assert updates[1]["line.color"] == "#33475b"

    workspace.toggle_mesh_surface()
    workspace._style_preview_figure(figure)
    assert figure.data[0].visible is False
