import copy
import json
import math
import sys
from types import ModuleType, SimpleNamespace

import pytest

from ngapp import utils
from ngapp.components import Div

import ngsolve_gui.app as app_module
from ngsolve_gui.app import NGSolveGui
from ngsolve_gui.axisymmetric_model import builtin_materials
from ngsolve_gui.solve_workspace import SolveWorkspace, _load_saved_run_history


@pytest.fixture
def standalone_components(monkeypatch):
    environment = utils.Environment(utils.EnvironmentType.STANDALONE, have_backend=False)
    environment.frontend.update_component = lambda *args, **kwargs: None
    monkeypatch.setattr(utils, "_environment", environment)


def test_workspace_builds_nested_regions_and_recomputes_dimension_parameters(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    outer = workspace.model["geometry"]["regions"][0]
    workspace._set_region_value(outer["id"], "material_id", "material-air")

    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.003,
        ("rectangle", "z_min"): 0.003,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.005,
    })
    workspace._add_primitive("rectangle")
    coil = workspace.model["geometry"]["regions"][1]
    workspace._set_region_value(coil["id"], "material_id", "material-copper")
    assert coil["parent_id"] == outer["id"]

    parameter = {"id": "outer-width", "name": "outer_width", "expression": "0.02", "unit": "m"}
    workspace.model["parameters"].append(parameter)
    workspace._set_region_dimension(outer["id"], "width", "outer_width")
    workspace.model["boundary_conditions"].append(
        {"id": "boundary-outer", "name": "Outer boundary", "type": "magnetic_potential_zero"}
    )
    exterior_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if not all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"])
    )
    workspace._set_edge_condition(exterior_edge["id"], "electromagnetic", "boundary-outer")

    assert outer["shape"]["width"] == 0.02
    assert workspace.validation_errors() == []


def test_geometry_and_physics_value_inputs_use_si_units(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._primitive_values[("rectangle", "width")] == pytest.approx(0.02)
    assert workspace._primitive_values[("circle", "radius")] == pytest.approx(0.005)
    assert all(
        field._props["suffix"] == "m"
        for shape in ("rectangle", "circle")
        for key, field in workspace._region_dimension_groups[shape].items()
        if key != "group"
    )

    def components(items):
        for item in items:
            yield item
            yield from components(getattr(item, "ui_children", []))

    workspace._add_primitive("rectangle")
    sources = workspace._sources_properties()
    source_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in components(sources)
        if getattr(item, "ui_label", None)
    }
    assert source_units["DC current density"] == "A/m²"
    assert source_units["Radial body force"] == "N/m³"

    workspace.model["materials"] = builtin_materials()
    workspace.selected_material_id = "material-copper"
    material_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in components(workspace._material_properties())
        if getattr(item, "ui_label", None)
    }
    assert material_units["Electrical conductivity"] == "S/m"
    assert material_units["Young's modulus"] == "Pa"
    assert material_units["Density"] == "kg/m³"

    workspace.model["boundary_conditions"] = [
        {"id": "traction", "name": "Traction", "type": "mechanical_traction"}
    ]
    workspace.selected_boundary_id = "traction"
    boundary_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in components(workspace._boundary_properties())
        if getattr(item, "ui_label", None)
    }
    assert boundary_units["Radial traction"] == "N/m²"
    assert workspace._mesh_properties()[1]._props["suffix"] == "m"
    study_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in components(workspace._study_properties())
        if getattr(item, "ui_label", None)
    }
    assert study_units["Frequency points (comma separated)"] == "Hz"


def test_generated_mesh_preview_is_visible_and_invalidated_by_edits(standalone_components):
    workspace = SolveWorkspace()
    workspace.set_mesh_preview([
        ((0.0, 0.0), (1.0, 0.0)),
        ((1.0, 0.0), (0.0, 1.0)),
    ])

    assert workspace.mesh_preview_visible
    assert not workspace._mesh_preview_button.ui_disable
    assert workspace._mesh_preview_button.ui_label == "Hide mesh"
    assert workspace._canvas_mesh_path in workspace._canvas_scene.ui_children

    workspace.add_parameter()

    assert workspace.mesh_preview_edges == []
    assert not workspace.mesh_preview_visible
    assert workspace._mesh_preview_button.ui_disable


def test_ngsolve_mesh_edges_are_deduplicated_for_preview():
    from netgen.geom2d import unit_square
    from ngsolve import Mesh, VOL
    from ngsolve_gui.app import _axisymmetric_mesh_edges

    mesh = Mesh(unit_square.GenerateMesh(maxh=0.5))
    preview_edges = _axisymmetric_mesh_edges(mesh)
    element_edge_count = sum(len(element.vertices) for element in mesh.Elements(VOL))

    assert preview_edges
    assert len(preview_edges) < element_edge_count
    assert all(len(start) == len(end) == 2 for start, end in preview_edges)


def test_model_edits_support_undo_redo_and_clear_redo_after_new_edit(standalone_components):
    history_notifications = []
    workspace = SolveWorkspace(on_history_change=lambda: history_notifications.append(True))

    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    region_id = region["id"]
    assert workspace.can_undo
    assert not workspace.can_redo

    assert workspace.undo()
    assert workspace.model["geometry"]["regions"] == []
    assert not workspace.can_undo
    assert workspace.can_redo

    assert workspace.redo()
    assert workspace.model["geometry"]["regions"][0]["id"] == region_id
    assert workspace.can_undo
    assert not workspace.can_redo

    workspace.delete_region(region_id)
    assert workspace.model["geometry"]["regions"] == []
    assert workspace.undo()
    assert workspace.model["geometry"]["regions"][0]["id"] == region_id
    assert workspace.selected_region_id == region_id

    workspace._set_region_dimension(region_id, "width", "0.03")
    region = workspace.model["geometry"]["regions"][0]
    assert region["shape"]["width"] == 0.03
    assert workspace.undo()
    assert workspace.model["geometry"]["regions"][0]["shape"]["width"] == 0.02
    assert workspace.redo()
    assert workspace.model["geometry"]["regions"][0]["shape"]["width"] == 0.03

    workspace.delete_region(region_id)
    assert workspace.model["geometry"]["regions"] == []
    assert workspace.undo()
    assert workspace.can_redo
    workspace._set_region_value(region_id, "name", "Outer shell")
    assert workspace.model["geometry"]["regions"][0]["name"] == "Outer shell"
    assert not workspace.can_redo
    assert len(history_notifications) >= 10


def test_delete_undo_and_redo_shortcut_actions_target_solve_workspace(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    app = SimpleNamespace(
        _workspace_mode="solve",
        solve_workspace=workspace,
        _solve_shortcut_context_active=lambda: True,
    )

    assert NGSolveGui._delete_selected_region_shortcut(app)
    assert workspace.model["geometry"]["regions"] == []
    assert NGSolveGui._undo_solve_action(app)
    assert len(workspace.model["geometry"]["regions"]) == 1
    assert NGSolveGui._redo_solve_action(app)
    assert workspace.model["geometry"]["regions"] == []


def test_model_title_supports_inline_rename_commit_and_cancel(standalone_components):
    workspace = SolveWorkspace()
    title = workspace._model_title
    assert title._props["title"] == "Double-click to rename this model"

    title._handle("focus")
    assert not workspace._model_title_editing
    assert title.ui_readonly

    title._handle("dblclick")
    editor = workspace._model_title_input
    assert editor is not None
    assert workspace._model_title_editing
    assert not title.ui_readonly
    editor._handle("update:model-value", "Quarter magnet")
    editor._handle("keydown", {"key": "Enter"})

    assert workspace.model["name"] == "Quarter magnet"
    assert title.ui_model_value == "Quarter magnet"
    assert title.ui_readonly
    assert not workspace._model_title_editing

    title._handle("focus")
    assert not workspace._model_title_editing
    title._handle("dblclick")
    editor = workspace._model_title_input
    editor._handle("update:model-value", "Discard this name")
    editor._handle("keydown", {"key": "Escape"})

    assert workspace.model["name"] == "Quarter magnet"
    assert title.ui_model_value == "Quarter magnet"
    assert title.ui_readonly
    assert not workspace._model_title_editing


def test_setup_check_flashes_bottom_status_for_pass_and_failure(standalone_components):
    workspace = SolveWorkspace()
    workspace.validation_errors = lambda: []

    workspace.validate_action()

    assert "Setup checks passed" in workspace.message
    assert "mmfem-solve-status-flash-a" in workspace._bottom_bar.ui_class

    workspace.validation_errors = lambda: ["Create a geometry region."]
    workspace.validate_action()

    assert "found 1 issue" in workspace.message
    assert "mmfem-solve-status-flash-b" in workspace._bottom_bar.ui_class


def test_study_opens_solver_log_and_close_button_minimises_it(standalone_components):
    workspace = None
    run_observations = []

    def on_run():
        run_observations.append(workspace._log_visible)

    workspace = SolveWorkspace(on_run=on_run)
    workspace.validation_errors = lambda: []
    workspace.run_study_action()

    assert run_observations == [True]
    assert workspace._log_visible
    assert not workspace._log_panel.ui_hidden

    workspace._log_close_button._callbacks["click.stop"][0](None)
    assert not workspace._log_visible
    assert workspace._log_panel.ui_hidden

    workspace.validation_errors = lambda: ["Create a geometry region."]
    workspace.run_study_action()
    assert run_observations == [True]
    assert not workspace._log_visible


def test_solver_job_can_be_cancelled_and_is_recorded(standalone_components):
    cancelled = []
    workspace = SolveWorkspace(on_run=lambda: None, on_cancel=lambda: cancelled.append(True))
    workspace.validation_errors = lambda: []
    workspace.run_study_action()

    assert workspace._job_active
    assert not workspace._cancel_button.ui_hidden
    workspace._cancel_solver_job()
    assert cancelled == [True]
    assert workspace._cancel_button.ui_label == "Stopping…"

    workspace.finish_solver_job(
        "Study cancelled", cancelled=True, run_kind="Study", run_name="Study: test"
    )
    assert not workspace._job_active
    assert workspace._cancel_button.ui_hidden
    assert workspace.runs[-1]["status"] == "Cancelled"


def test_solver_timestamp_import_is_not_shadowed_by_component_star_import():
    assert callable(app_module._datetime.now)
    assert app_module._timezone.utc is not None


def test_saved_run_history_recovers_manifest_and_marks_running_as_interrupted(tmp_path):
    run_dir = tmp_path / "nested_model"
    run_dir.mkdir()
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "schema": "mm-fem.run",
        "kind": "study",
        "status": "running",
        "model_name": "Nested model",
        "created_utc": "2026-10-10T12:00:00+00:00",
    }), encoding="utf-8")

    runs = _load_saved_run_history(tmp_path)

    assert runs == [{
        "name": "Study: Nested model",
        "kind": "Study",
        "status": "Interrupted",
        "finished": "2026-10-10T12:00:00+00:00",
        "output_path": str(run_dir),
    }]


def test_run_history_exposes_a_frequency_selector_for_sweep_fields(standalone_components, tmp_path):
    fields_dir = tmp_path / "run" / "ngsolve_gui" / "fields"
    fields_dir.mkdir(parents=True)
    for name in ("B_DC.pkl", "B_AC_001_250Hz.pkl", "B_AC_002_500Hz.pkl"):
        (fields_dir / name).touch()
    run = {
        "name": "Study: frequency sweep",
        "kind": "Study",
        "status": "Complete",
        "finished": "now",
        "output_path": str(fields_dir.parents[1]),
    }
    workspace = SolveWorkspace(on_open_file=lambda path: None)
    workspace.runs = [run]

    properties = workspace._run_properties()
    frequency_select = next(
        item for item in properties
        if getattr(item, "ui_label", None) == "Frequency"
    )
    assert [option["value"] for option in frequency_select.ui_options] == ["250", "500"]
    assert run["selected_frequency_hz"] == "250"

    workspace._set_run_frequency(run, "500")
    assert run["selected_frequency_hz"] == "500"


def test_3d_preview_replaces_viewport_and_restores_editable_sketch(standalone_components):
    workspace = SolveWorkspace()
    preview = Div()
    workspace._build_3d_preview_component = lambda: preview
    original_canvas = workspace._canvas

    workspace.toggle_3d_preview()

    assert workspace._preview_3d_active
    assert workspace._preview_3d_component is preview
    assert workspace._canvas_host.ui_children == [preview]
    assert workspace._preview_3d_button.ui_label == "Preview in 3D"
    assert all(control.ui_hidden for control in workspace._sketch_view_controls)

    workspace.toggle_3d_preview()

    assert not workspace._preview_3d_active
    assert workspace._preview_3d_component is None
    assert workspace._canvas_host.ui_children == [original_canvas]
    assert workspace._preview_3d_button.ui_label == "Preview in 3D"
    assert all(not control.ui_hidden for control in workspace._sketch_view_controls)


def test_3d_preview_shows_shaded_solid_and_axis_without_edges_or_navigation_cube(
    standalone_components, monkeypatch
):
    import ngapp.components
    import netgen

    axis_indicator = object()
    geometry_renderer = None

    class Solid:
        faces = []

        def mat(self, _name):
            return self

    class Renderer:
        def __init__(self, _geometry):
            self.faces = SimpleNamespace(active=None, set_colors=lambda _colors: None)
            self.edges = SimpleNamespace(active=None)

    def make_geometry_renderer(geometry):
        nonlocal geometry_renderer
        geometry_renderer = Renderer(geometry)
        return geometry_renderer

    class Axes:
        def __new__(cls):
            return axis_indicator

    class Preview:
        def __init__(self, **kwargs):
            self.ui_class = None
            self.renderers = None

        def on_mounted(self, callback):
            pass

        def draw(self, renderers):
            self.renderers = renderers
            return SimpleNamespace(
                options=SimpleNamespace(camera=SimpleNamespace(reset=lambda *_args: None)),
                bounding_box=(),
                render=lambda: None,
            )

    occ = ModuleType("netgen.occ")
    occ.Axis = lambda *_args: object()
    occ.Compound = lambda _solids: object()
    occ.Face = lambda _polygon: object()
    occ.MakePolygon = lambda _vertices: object()
    occ.OCCGeometry = lambda _compound: SimpleNamespace(faces=[])
    occ.Pnt = lambda *_args: object()
    occ.Revolve = lambda *_args: Solid()
    occ.Vec = lambda *_args: object()
    occ.Vertex = lambda _point: object()
    ngsolve_webgpu = ModuleType("ngsolve_webgpu")
    ngsolve_webgpu.GeometryRenderer = make_geometry_renderer
    webgpu = ModuleType("webgpu")
    webgpu.CoordinateAxes = Axes
    monkeypatch.setattr(netgen, "occ", occ, raising=False)
    monkeypatch.setitem(sys.modules, "netgen.occ", occ)
    monkeypatch.setitem(sys.modules, "ngsolve_webgpu", ngsolve_webgpu)
    monkeypatch.setitem(sys.modules, "webgpu", webgpu)
    monkeypatch.setattr(ngapp.components, "WebgpuComponent", Preview)

    workspace = SolveWorkspace()
    workspace.model["geometry"]["regions"] = [{
        "id": "circle-1",
        "name": "circle 1",
        "vertices": [[0.02, 0.0], [0.023, 0.0], [0.023, 0.003], [0.02, 0.003]],
    }]
    preview = workspace._build_3d_preview_component()

    assert geometry_renderer.faces.active is True
    assert geometry_renderer.edges.active is False
    assert preview.renderers == [geometry_renderer, axis_indicator]


def test_3d_preview_failure_keeps_editable_sketch_active(standalone_components):
    workspace = SolveWorkspace()
    workspace._build_3d_preview_component = lambda: (_ for _ in ()).throw(ValueError("invalid profile"))

    workspace.toggle_3d_preview()

    assert not workspace._preview_3d_active
    assert workspace._canvas_host.ui_children == [workspace._canvas]
    assert workspace._preview_3d_button.ui_label == "Preview in 3D"
    assert "invalid profile" in workspace.message


def test_same_edge_can_have_independent_em_and_mechanical_conditions(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.003,
        ("rectangle", "z_min"): 0.003,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.005,
    })
    outer = workspace.model["geometry"]["regions"][0]
    workspace._set_region_value(outer["id"], "material_id", "material-air")
    workspace._add_primitive("rectangle")
    coil = workspace.model["geometry"]["regions"][1]
    workspace._set_region_value(coil["id"], "material_id", "material-copper")
    workspace.model["boundary_conditions"].extend([
        {"id": "boundary-outer", "name": "Outer boundary", "type": "magnetic_potential_zero"},
        {"id": "boundary-fixed", "name": "Fixed support", "type": "mechanical_fixed"},
    ])
    coil["mechanical"] = True
    workspace.model["physics"]["mechanics"]["enabled"] = True

    outer_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if not all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"])
    )
    workspace._set_edge_condition(outer_edge["id"], "electromagnetic", "boundary-outer")
    workspace._set_edge_condition(outer_edge["id"], "mechanical", "boundary-fixed")

    updated_edge = next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == outer_edge["id"])
    assert set(updated_edge["boundary_condition_ids"]) == {"boundary-outer", "boundary-fixed"}
    assert workspace.validation_errors() == []


def test_electromagnetic_run_requires_an_exterior_reference_boundary(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")

    assert any("anchor the electromagnetic solution" in error for error in workspace.validation_errors())


def test_drag_sketches_dimensioned_rectangle_and_circle_regions(standalone_components):
    workspace = SolveWorkspace()
    project, _ = workspace._canvas_projection()
    workspace._create_region_from_canvas_drag(
        "rectangle", project((0.0, 0.0)), project((0.020, 0.020))
    )
    rectangle = workspace.model["geometry"]["regions"][0]
    assert rectangle["name"] == "square 1"
    assert rectangle["shape"]["width"] == pytest.approx(0.020)
    assert rectangle["shape"]["height"] == pytest.approx(0.020)
    assert {item["type"] for item in rectangle["constraints"]} >= {
        "horizontal", "vertical", "radial_position", "axial_position", "width", "height"
    }

    project, _ = workspace._canvas_projection()
    workspace._create_region_from_canvas_drag(
        "circle", project((0.010, 0.010)), project((0.012, 0.010))
    )
    circle = workspace.model["geometry"]["regions"][1]
    assert circle["name"] == "circle 1"
    assert circle["parent_id"] == rectangle["id"]
    assert circle["shape"]["radius"] == pytest.approx(0.002)
    assert {item["type"] for item in circle["constraints"]} >= {
        "radial_position", "axial_position", "radius"
    }

    project, _ = workspace._canvas_projection()
    workspace._create_region_from_canvas_drag(
        "rectangle", project((0.030, 0.030)), project((0.040, 0.040))
    )
    workspace._create_region_from_canvas_drag(
        "circle", project((0.055, 0.010)), project((0.057, 0.010))
    )
    assert [region["name"] for region in workspace.model["geometry"]["regions"]] == [
        "square 1", "circle 1", "square 2", "circle 2"
    ]


def test_mouse_drawn_geometry_uses_si_values_with_0_01mm_precision(standalone_components):
    workspace = SolveWorkspace()
    project, _ = workspace._canvas_projection()
    workspace._create_region_from_canvas_drag(
        "rectangle",
        project((0.0034567, -0.0023467)),
        project((0.0101234, 0.0078912)),
    )
    rectangle = workspace.model["geometry"]["regions"][0]["shape"]
    assert rectangle["r_min"] == pytest.approx(0.00346)
    assert rectangle["z_min"] == pytest.approx(-0.00235)
    assert rectangle["width"] == pytest.approx(0.00666)
    assert rectangle["height"] == pytest.approx(0.01024)
    assert rectangle["dimension_expressions"] == {
        "r_min": "0.00346",
        "z_min": "-0.00235",
        "width": "0.00666",
        "height": "0.01024",
    }

    circle_workspace = SolveWorkspace()
    project, _ = circle_workspace._canvas_projection()
    circle_workspace._create_region_from_canvas_drag(
        "circle",
        project((0.0200034567, -0.0023467)),
        project((0.0251234, 0.0019879)),
    )
    circle = circle_workspace.model["geometry"]["regions"][0]["shape"]
    assert circle["r_center"] == pytest.approx(0.02)
    assert circle["z_center"] == pytest.approx(-0.00235)
    assert circle["radius"] == pytest.approx(0.00671)
    assert circle["dimension_expressions"] == {
        "r_center": "0.02",
        "z_center": "-0.00235",
        "radius": "0.00671",
    }


def test_manual_region_dimensions_keep_precision_beyond_hundredth_mm(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]

    workspace._set_region_dimension(region["id"], "width", "0.0201234")

    assert region["shape"]["width"] == pytest.approx(0.0201234)
    assert region["shape"]["dimension_expressions"]["width"] == "0.0201234"


def test_dragging_region_moves_shape_by_hundredth_mm_and_preserves_edge_assignments(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    edge = workspace.model["geometry"]["edges"][0]
    edge["name"] = "Fixed edge"
    edge["boundary_condition_ids"] = ["support"]
    edge_ids = list(region["edge_ids"])
    before = copy.deepcopy(region["shape"])
    project, _ = workspace._canvas_projection()

    workspace._move_region_from_canvas_drag(
        region["id"], project((0.004, 0.005)), project((0.0040146, 0.0050246))
    )

    assert region["shape"]["r_min"] - before["r_min"] == pytest.approx(0.00001)
    assert region["shape"]["z_min"] - before["z_min"] == pytest.approx(0.00002)
    assert region["shape"]["width"] == before["width"]
    assert region["shape"]["height"] == before["height"]
    assert region["shape"]["dimension_expressions"]["r_min"] == "1e-05"
    assert region["shape"]["dimension_expressions"]["z_min"] == "2e-05"
    assert region["edge_ids"] == edge_ids
    moved_edge = next(item for item in workspace.model["geometry"]["edges"] if item["id"] == edge["id"])
    assert moved_edge["name"] == "Fixed edge"
    assert moved_edge["boundary_condition_ids"] == ["support"]


def test_browser_region_move_keeps_canvas_mounted(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    before = region["shape"]["r_min"]
    project, _ = workspace._canvas_projection()
    rendered = []
    monkeypatch.setattr(workspace, "render_canvas", lambda: rendered.append(True))
    monkeypatch.setattr(workspace, "_refresh_model_tree", lambda: None)
    monkeypatch.setattr(workspace, "_render_inspector", lambda: None)

    moved = workspace._move_region_from_canvas_drag(
        region["id"], project((0.004, 0.005)), project((0.0040146, 0.0050246)),
        preserve_canvas=True,
    )

    assert moved is True
    assert region["shape"]["r_min"] - before == pytest.approx(0.00001)
    assert rendered == []


def test_dragging_region_rejects_axis_crossing_and_restores_geometry(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    before_shape = copy.deepcopy(region["shape"])
    before_vertices = copy.deepcopy(region["vertices"])
    before_edges = copy.deepcopy(workspace.model["geometry"]["edges"])
    project, _ = workspace._canvas_projection()

    workspace._move_region_from_canvas_drag(
        region["id"], project((0.010, 0.0)), project((-0.010, 0.0))
    )

    assert region["shape"] == before_shape
    assert region["vertices"] == before_vertices
    assert workspace.model["geometry"]["edges"] == before_edges
    assert "r = 0" in workspace.message


def test_dragging_circle_moves_its_centre_without_changing_its_radius(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("circle")
    region = workspace.model["geometry"]["regions"][0]
    before = copy.deepcopy(region["shape"])
    project, _ = workspace._canvas_projection()

    workspace._move_region_from_canvas_drag(
        region["id"], project((0.010, 0.005)), project((0.01002, 0.00497))
    )

    assert region["shape"]["r_center"] - before["r_center"] == pytest.approx(0.00002)
    assert region["shape"]["z_center"] - before["z_center"] == pytest.approx(-0.00003)
    assert region["shape"]["radius"] == before["radius"]


def test_dragging_region_rejects_crossing_another_region(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.005,
        ("rectangle", "z_min"): 0.005,
        ("rectangle", "width"): 0.005,
        ("rectangle", "height"): 0.005,
    })
    workspace._add_primitive("rectangle")
    child = workspace.model["geometry"]["regions"][1]
    before_shape = copy.deepcopy(child["shape"])
    before_vertices = copy.deepcopy(child["vertices"])
    project, _ = workspace._canvas_projection()

    workspace._move_region_from_canvas_drag(
        child["id"], project((0.0, 0.0)), project((0.012, 0.0))
    )

    assert child["shape"] == before_shape
    assert child["vertices"] == before_vertices
    assert "crossing or touching" in workspace.message


def test_canvas_drag_on_region_body_moves_region_instead_of_box_selecting_edges(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace.zoom_canvas(0.5)
    region = workspace.model["geometry"]["regions"][0]
    before = region["shape"]["r_min"]
    project, _ = workspace._canvas_projection()
    start, end = project((0.010, 0.010)), project((0.0102, 0.010))
    points = iter((start, end, end))
    workspace._canvas_event_point = lambda event, refresh_transform=False: next(points)
    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0, "region_id": region["id"]}))
    workspace._on_canvas_mouse_move(SimpleNamespace(value={}))
    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert region["shape"]["r_min"] - before == pytest.approx(0.0002)
    assert workspace.selected_region_id == region["id"]
    assert workspace.selected_edge_ids == []


def test_drag_box_selects_enclosed_or_crossed_edges_and_shift_adds(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    project, _ = workspace._canvas_projection()
    projected = [project(point) for point in region["vertices"]]

    # A left-to-right window fully enclosing the sketch selects its outline.
    left = min(point[0] for point in projected) - 2
    right = max(point[0] for point in projected) + 2
    top = min(point[1] for point in projected) - 2
    bottom = max(point[1] for point in projected) + 2
    workspace._select_edges_in_canvas_box((left, top), (right, bottom))
    assert set(workspace.selected_edge_ids) == {
        edge["id"] for edge in workspace.model["geometry"]["edges"]
    }

    # A left-to-right box selects only edges fully inside; a right-to-left
    # crossing box catches a segment even when both endpoints are outside.
    edge = workspace.model["geometry"]["edges"][0]
    first, second = [project(point) for point in edge["vertices"]]
    mid_x = (first[0] + second[0]) / 2
    narrow = (mid_x - 4, mid_x + 4, first[1] - 1, first[1] + 1)
    workspace._select_edges_in_canvas_box(
        (narrow[0], narrow[2]), (narrow[1], narrow[3])
    )
    assert workspace.selected_edge_ids == []
    workspace._select_edges_in_canvas_box(
        (narrow[1], narrow[2]), (narrow[0], narrow[3])
    )
    assert workspace.selected_edge_ids == [edge["id"]]
    workspace._select_edges_in_canvas_box(
        (left, top), (right, bottom), additive=True
    )
    assert set(workspace.selected_edge_ids) == {
        item["id"] for item in workspace.model["geometry"]["edges"]
    }


def test_canvas_mouse_gesture_creates_rectangle_and_maps_screen_coordinates(standalone_components):
    workspace = SolveWorkspace()
    workspace._screen_to_svg = (1.25, 0.0, 0.0, 1.25, 100.0, 200.0)
    point = workspace._canvas_event_point(
        SimpleNamespace(value={"x": 125.0, "y": 300.0})
    )
    assert point == pytest.approx((20.0, 80.0))

    project, _ = workspace._canvas_projection()
    start, end = project((0.003, 0.004)), project((0.008, 0.010))
    points = iter((start, (start[0] + 10, start[1] + 10), end))
    workspace._canvas_event_point = lambda event, refresh_transform=False: next(points)
    workspace.set_sketch_tool("rectangle", announce=False)
    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))
    workspace._on_canvas_mouse_move(SimpleNamespace(value={}))
    assert workspace._canvas_drag["moved"]
    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))
    assert len(workspace.model["geometry"]["regions"]) == 1
    assert workspace.model["geometry"]["regions"][0]["shape"]["width"] == pytest.approx(0.005)
    assert workspace.sketch_tool == "select"


def test_canvas_mouse_gesture_creates_circle_and_restores_select_tool(standalone_components):
    workspace = SolveWorkspace()
    project, _ = workspace._canvas_projection()
    start, end = project((0.010, 0.004)), project((0.013, 0.008))
    points = iter((start, (start[0] + 9, start[1] + 8), end))
    workspace._canvas_event_point = lambda event, refresh_transform=False: next(points)
    workspace.set_sketch_tool("circle", announce=False)

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))
    workspace._on_canvas_mouse_move(SimpleNamespace(value={}))
    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    circle = workspace.model["geometry"]["regions"][0]
    assert circle["shape"]["type"] == "circle"
    assert circle["shape"]["radius"] == pytest.approx(math.hypot(0.003, 0.004))
    assert workspace.sketch_tool == "select"


def test_canvas_region_target_is_read_before_pointer_coordinates_are_consumed(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    pointer_events = [{"type": "mousedown", "timeStamp": 42.0, "regionId": "region-square"}]
    fake_js = SimpleNamespace(
        eval=lambda script: (
            pointer_events[0]["regionId"]
            if pointer_events and "events.find" in script
            else None
        )
    )
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: fake_js), raising=False)

    def consume_pointer(event, refresh_transform=False):
        pointer_events.clear()
        return (120.0, 180.0)

    workspace._canvas_event_point = consume_pointer
    workspace._on_canvas_mouse_down(SimpleNamespace(value={
        "button": 0, "type": "mousedown", "timeStamp": 42.0,
    }))

    assert workspace._canvas_drag["region_id"] == "region-square"


def test_canvas_region_target_falls_back_to_latest_native_mousedown(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    pointer_events = [{"type": "mousedown", "timeStamp": 42.0, "regionId": "region-square"}]
    fake_js = SimpleNamespace(
        eval=lambda script: pointer_events[-1]["regionId"] if pointer_events else None
    )
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: fake_js), raising=False)
    workspace._canvas_event_point = lambda event, refresh_transform=False: (120.0, 180.0)

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))

    assert workspace._canvas_drag["region_id"] == "region-square"


def test_canvas_region_hit_test_finds_region_when_browser_target_is_missing(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    monkeypatch.setattr(workspace, "_read_canvas_region_id", lambda value: None)
    project, _ = workspace._canvas_projection()
    workspace._canvas_event_point = lambda event, refresh_transform=False: project((0.010, 0.010))

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))

    assert workspace._canvas_drag["region_id"] == region["id"]


def test_canvas_blank_hit_from_browser_does_not_move_a_nearby_region(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    project, _ = workspace._canvas_projection()
    center = tuple(sum(point[index] for point in region["vertices"]) / len(region["vertices"]) for index in (0, 1))
    monkeypatch.setattr(workspace, "_read_canvas_region_id", lambda value: "")
    monkeypatch.setattr(workspace, "_region_at_canvas_point", lambda point: region["id"])
    workspace._canvas_event_point = lambda event, refresh_transform=False: project(center)

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))

    assert workspace._canvas_drag["operation"] == "pan"
    assert workspace._canvas_drag["region_id"] is None


def test_canvas_drag_motion_and_release_do_not_rebuild_the_canvas(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    project, _ = workspace._canvas_projection()
    points = [point for edge in workspace.model["geometry"]["edges"] for point in edge["vertices"]]
    projected = [project(point) for point in points]
    start = (min(point[0] for point in projected) - 2, min(point[1] for point in projected) - 2)
    end = (max(point[0] for point in projected) + 2, max(point[1] for point in projected) + 2)
    points = iter((start, end, end))
    workspace._canvas_event_point = lambda event, refresh_transform=False: next(points)
    workspace.set_sketch_tool("select", announce=False)
    renders = []
    workspace.render_canvas = lambda: renders.append("render")

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0, "shiftKey": True}))
    assert workspace._canvas_drag["operation"] == "marquee"
    workspace._on_canvas_mouse_move(SimpleNamespace(value={}))
    assert renders == []

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))
    assert renders == []
    assert len(workspace.selected_edge_ids) == 4


def test_empty_canvas_drag_pans_and_shift_drag_retains_marquee_selection(standalone_components):
    workspace = SolveWorkspace()
    center_before, width_before, _ = workspace._current_canvas_world_view()
    start, end = (100.0, 120.0), (138.0, 146.0)
    points = iter((start, end, end))
    workspace._canvas_event_point = lambda event, refresh_transform=False: next(points)

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))
    assert workspace._canvas_drag["operation"] == "pan"
    workspace._on_canvas_mouse_move(SimpleNamespace(value={}))
    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    center_after, width_after, _ = workspace._current_canvas_world_view()
    pixels_per_world = (workspace._canvas_plot_bounds()[2] - workspace._canvas_plot_bounds()[0]) / width_before
    assert center_after[0] == pytest.approx(center_before[0] - 38.0 / pixels_per_world)
    assert center_after[1] == pytest.approx(center_before[1] + 26.0 / pixels_per_world)
    assert width_after == pytest.approx(width_before)

    points = iter(((160.0, 180.0), (190.0, 210.0)))
    workspace._canvas_event_point = lambda event, refresh_transform=False: next(points)
    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0, "shiftKey": True}))
    assert workspace._canvas_drag["operation"] == "marquee"
    workspace._on_canvas_pointer_cancel()
    assert workspace._canvas_drag is None


def test_canvas_pointer_cancel_clears_an_incomplete_sketch(standalone_components):
    workspace = SolveWorkspace()
    workspace._canvas_drag = {"tool": "rectangle", "start": (1, 2), "current": (4, 5)}
    workspace._screen_to_svg = (1, 0, 0, 1, 0, 0)

    workspace._on_canvas_pointer_cancel()

    assert workspace._canvas_drag is None
    assert workspace._screen_to_svg is None


def test_region_creation_updates_only_changed_canvas_and_tree_subsections(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    updates = []
    frontend = utils._environment.frontend
    capture = lambda component, data, method, **kwargs: updates.append((component, data, method))
    monkeypatch.setattr(
        frontend,
        "update_component",
        capture,
    )

    workspace._add_primitive("rectangle")

    updated_components = [update[0] for update in updates]
    assert frontend.update_component is capture
    assert len(updated_components) == len(set(map(id, updated_components)))
    assert workspace._left_items not in updated_components
    assert all(component in updated_components for component in (
        workspace._tree_entry_lists["geometry"],
        workspace._tree_entry_lists["sources"],
        workspace._canvas_scene,
        workspace._canvas_dimensions,
        workspace._region_panel,
        workspace._region_name_input,
    ))
    assert workspace._canvas_grid not in updated_components
    assert len(workspace.model["geometry"]["regions"]) == 1


def test_region_add_and_remove_keep_tree_grid_and_existing_scene_nodes_mounted(standalone_components):
    workspace = SolveWorkspace()
    center, width, _ = workspace._current_canvas_world_view()
    tree_groups = tuple(workspace._left_items.ui_children)
    grid_nodes = tuple(workspace._canvas_grid.ui_children)
    grid_signature = workspace._canvas_grid_signature
    geometry_entries = workspace._tree_entry_lists["geometry"]
    sources_entries = workspace._tree_entry_lists["sources"]

    workspace._add_primitive("rectangle")
    assert workspace._view_center == center
    assert workspace._view_world_width == width
    assert workspace._canvas_grid_signature == grid_signature
    rectangle = workspace.model["geometry"]["regions"][0]
    rectangle_node = workspace._canvas_region_nodes[rectangle["id"]]
    rectangle_edges = {
        edge_id: workspace._canvas_edge_nodes[edge_id]
        for edge_id in rectangle["edge_ids"]
    }

    workspace._primitive_values.update({
        ("circle", "r_center"): 0.03,
        ("circle", "z_center"): 0.0,
        ("circle", "radius"): 0.002,
    })
    workspace._add_primitive("circle")
    circle = next(
        region for region in workspace.model["geometry"]["regions"]
        if region["shape"]["type"] == "circle"
    )

    assert tuple(workspace._left_items.ui_children) == tree_groups
    assert workspace._tree_entry_lists["geometry"] is geometry_entries
    assert workspace._tree_entry_lists["sources"] is sources_entries
    assert tuple(workspace._canvas_grid.ui_children) == grid_nodes
    assert workspace._canvas_grid_signature == grid_signature
    assert workspace._canvas_region_nodes[rectangle["id"]] is rectangle_node
    assert all(workspace._canvas_edge_nodes[edge_id] is nodes for edge_id, nodes in rectangle_edges.items())

    workspace.delete_region(circle["id"])

    assert tuple(workspace._left_items.ui_children) == tree_groups
    assert tuple(workspace._canvas_grid.ui_children) == grid_nodes
    assert workspace._canvas_grid_signature == grid_signature
    assert workspace._canvas_region_nodes[rectangle["id"]] is rectangle_node
    assert all(workspace._canvas_edge_nodes[edge_id] is nodes for edge_id, nodes in rectangle_edges.items())


def test_region_click_updates_selection_without_rebuilding_sketch_scene(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region_id = workspace.model["geometry"]["regions"][0]["id"]
    scene = workspace._canvas_scene
    scene_children = tuple(scene.ui_children)
    tree_children = tuple(workspace._left_items.ui_children)
    inspector_children = tuple(workspace._inspector.ui_children)
    tree_button = workspace._tree_entry_buttons[f"geometry:{region_id}"]
    renders = []
    workspace.render_canvas = lambda: renders.append("render")
    workspace._refresh_model_tree = lambda: (_ for _ in ()).throw(AssertionError("tree rebuilt on selection"))
    workspace._render_inspector = lambda: (_ for _ in ()).throw(AssertionError("inspector rebuilt on selection"))

    workspace.select_region(region_id)

    assert renders == []
    assert tuple(scene.ui_children) == scene_children
    assert tuple(workspace._left_items.ui_children) == tree_children
    assert tuple(workspace._inspector.ui_children) == inspector_children
    assert tree_button.ui_color == "primary"
    assert not workspace._region_panel.ui_hidden
    assert workspace._region_name_input.ui_model_value == "square 1"
    dimension_labels = [
        child._props["textContent"]
        for child in workspace._canvas_dimensions.ui_children
        if child._props.get("textContent")
    ]
    assert dimension_labels == ["W 0.02 m", "H 0.02 m"]

    edge_id = workspace.model["geometry"]["edges"][0]["id"]
    workspace.select_edge(edge_id)
    assert tuple(workspace._left_items.ui_children) == tree_children
    assert tuple(workspace._inspector.ui_children) == inspector_children
    assert workspace._region_panel.ui_hidden
    assert not workspace._edge_panel.ui_hidden


def test_selecting_geometry_restores_its_inspector_after_another_section(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region_id = workspace.model["geometry"]["regions"][0]["id"]
    edge_id = workspace.model["geometry"]["edges"][0]["id"]

    workspace.select_section("materials")
    assert workspace._inspector_view == "materials"

    workspace.select_region(region_id)

    assert workspace._inspector_view == "geometry"
    assert tuple(workspace._inspector.ui_children) == tuple(workspace._geometry_inspector_children)
    assert not workspace._region_panel.ui_hidden
    assert {option["label"] for option in workspace._region_material_select.ui_options} >= {"Air", "Copper"}

    workspace.select_section("materials")
    workspace.select_edge(edge_id)

    assert workspace._inspector_view == "geometry"
    assert workspace._region_panel.ui_hidden
    assert not workspace._edge_panel.ui_hidden


def test_region_properties_explain_the_current_sketch_constraint_scope(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace.selected_region_id = workspace.model["geometry"]["regions"][0]["id"]

    note = (
        "These are built-in relationships for the rectangle and circle tools. "
        "This version does not solve general user-defined sketch constraints."
    )
    properties = workspace._geometry_properties()

    assert any(getattr(component, "ui_children", None) == [note] for component in properties)


def test_canvas_toolbar_wraps_when_the_viewport_is_narrow(standalone_components):
    workspace = SolveWorkspace()
    toolbar = workspace._canvas_panel.ui_children[0]

    assert "flex-wrap:wrap" in toolbar._props["style"]
    assert "min-width:0" in toolbar._props["style"]


def test_canvas_drag_suppresses_the_click_that_follows_mouseup(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    scripts = []
    fake_js = SimpleNamespace(eval=scripts.append)
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: fake_js), raising=False)

    workspace._install_canvas_pointer_capture()

    assert "suppressCanvasClickUntil" in scripts[0]
    assert "event.stopImmediatePropagation()" in scripts[0]
    assert "document.addEventListener('click', capture, true)" in scripts[0]
    assert "setPointerCapture" in scripts[0]
    assert "pointercancel" in scripts[0]
    assert "window.addEventListener('blur'" in scripts[0]
    assert "regionKnown" in scripts[0]
    assert "edgeId" in scripts[0]
    assert "data-sketch-edge-id" in scripts[0]


def test_stationary_region_pointer_gesture_selects_region_after_pointer_capture(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region_id = workspace.model["geometry"]["regions"][0]["id"]
    workspace.selected_region_id = None
    completed = ["region", "select", 120, 180, 120, 180, region_id, False, False, 1, 0, 0, 1, ""]
    fake_js = SimpleNamespace(eval=lambda _script: completed)
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: fake_js), raising=False)

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert workspace.selected_region_id == region_id
    assert workspace.selected_edge_ids == []


def test_stationary_edge_pointer_gesture_selects_edge_after_pointer_capture(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    edge_id = workspace.model["geometry"]["edges"][0]["id"]
    workspace.selected_region_id = None
    completed = ["edge", "select", 120, 180, 120, 180, "", False, False, 1, 0, 0, 1, edge_id]
    fake_js = SimpleNamespace(eval=lambda _script: completed)
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: fake_js), raising=False)

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert workspace.selected_edge_ids == [edge_id]
    assert workspace.selected_region_id is None


def test_modified_stationary_edge_pointer_gesture_adds_edge_selection(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    first, second = workspace.model["geometry"]["edges"][:2]
    workspace.selected_edge_ids = [first["id"]]
    workspace.selected_edge_id = first["id"]
    completed = ["marquee", "select", 120, 180, 120, 180, "", True, False, 1, 0, 0, 1, second["id"]]
    fake_js = SimpleNamespace(eval=lambda _script: completed)
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: fake_js), raising=False)

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert workspace.selected_edge_ids == [first["id"], second["id"]]


def test_canvas_render_keeps_background_and_layer_components_mounted(standalone_components):
    workspace = SolveWorkspace()
    root_layers = tuple(workspace._canvas.ui_children)

    workspace._add_primitive("rectangle")

    assert tuple(workspace._canvas.ui_children) == root_layers
    assert root_layers == (
        workspace._canvas_background,
        workspace._canvas_grid,
        workspace._canvas_scene,
        workspace._canvas_dimensions,
        workspace._canvas_preview,
    )


def test_model_tree_groups_sections_and_places_children_under_their_parent(standalone_components):
    workspace = SolveWorkspace()
    assert workspace.model["materials"] == []
    assert workspace.model["boundary_conditions"] == []

    assert [button.ui_label for button in workspace._section_buttons.values()] == [
        "Geometry",
        "Parameters",
        "Materials",
        "Physics & coupling",
        "Boundary conditions",
        "Sources & loads",
        "Mesh",
        "Solver settings",
        "Study",
        "Run history",
    ]
    assert all(button.ui_align == "left" for button in workspace._section_buttons.values())
    assert len(workspace._left_items.ui_children) == 3
    model_group, analysis_group, results_group = workspace._left_items.ui_children
    assert model_group.ui_children[1].ui_children[0] is workspace._section_buttons["geometry"]
    geometry_children = model_group.ui_children[1].ui_children[1]
    assert geometry_children.ui_children[0].ui_children[0] == "REGIONS · 0"
    assert analysis_group.ui_children[1].ui_children[0] is workspace._section_buttons["mesh"]
    assert results_group.ui_children[1].ui_children[0] is workspace._section_buttons["runs"]
    assert workspace._tree_subsection("materials") == ("MATERIALS · 0", [])
    assert workspace._tree_subsection("boundaries") == ("CONDITIONS · 0", [])
    assert [option["label"] for option in workspace._material_options()] == [
        "Unassigned", "Air", "Copper"
    ]
    assert workspace._tree_splitter.ui_slot_before == [workspace._tree]
    assert workspace._tree_splitter.ui_slot_after == [workspace._properties_splitter]
    assert workspace._properties_splitter.ui_slot_before == [workspace._canvas_panel]
    assert workspace._properties_splitter.ui_slot_after == [workspace._right]
    assert "flex:1 1 auto" in workspace._tree_splitter._props["style"]
    assert "flex:1 1 auto" in workspace._properties_splitter._props["style"]


def test_blank_axisymmetric_view_starts_at_r_zero_with_world_space_ticks(standalone_components):
    workspace = SolveWorkspace()
    project, unproject = workspace._canvas_projection()
    plot = workspace._canvas_plot_bounds()
    center = ((plot[0] + plot[2]) / 2, (plot[1] + plot[3]) / 2)

    assert project((0.0, 0.0))[0] == pytest.approx(72.0)
    assert unproject((72.0, 320.0))[0] == pytest.approx(0.0)
    assert unproject(center)[1] == pytest.approx(0.0)
    assert unproject((center[0], plot[1]))[1] == pytest.approx(-unproject((center[0], plot[3]))[1])
    ticks = workspace._coordinate_ticks(0.0, 0.022)
    assert ticks[0] == pytest.approx(0.0)
    steps = {round(b - a, 10) for a, b in zip(ticks, ticks[1:])}
    assert len(steps) == 1
    svg_text = [item for item in workspace._canvas_grid.ui_children if item._component_name == "text"]
    assert any(item._props.get("textContent") == "Axis of rotation  ·  r = 0" for item in svg_text)
    assert all(not item.ui_children for item in svg_text)


def test_default_axisymmetric_fit_centres_z_zero_for_positive_z_geometry(standalone_components):
    workspace = SolveWorkspace()
    workspace.model["geometry"]["regions"] = [{
        "vertices": [[0.0, 0.0], [0.02, 0.0], [0.02, 0.02], [0.0, 0.02]],
    }]

    project, unproject = workspace._canvas_projection()
    plot = workspace._canvas_plot_bounds()
    viewport_midpoint = (plot[1] + plot[3]) / 2

    assert project((0.0, 0.0))[1] == pytest.approx(viewport_midpoint)
    assert unproject((plot[0], plot[1]))[1] == pytest.approx(-unproject((plot[0], plot[3]))[1])
    assert project((0.0, 0.02))[1] < viewport_midpoint


def test_saved_axisymmetric_view_keeps_its_user_selected_z_centre(standalone_components):
    workspace = SolveWorkspace()
    workspace.set_model(
        workspace.model,
        layout={
            "schema_version": 1,
            "active_section": "geometry",
            "view": {"center_r": 0.01, "center_z": 0.005, "width": 0.04},
        },
    )

    assert workspace._current_canvas_world_view()[0][1] == pytest.approx(0.005)


def test_browser_view_transform_commits_once_at_its_final_zoom_and_pan(standalone_components):
    workspace = SolveWorkspace()
    plot = workspace._canvas_plot_bounds()
    center_before, width_before, _ = workspace._current_canvas_world_view()
    pixel_center = ((plot[0] + plot[2]) / 2, (plot[1] + plot[3]) / 2)

    assert workspace._commit_browser_canvas_view(
        (2.0, pixel_center[0] * -1.0, pixel_center[1] * -1.0, 1),
        render=False,
    )
    center_after_zoom, width_after_zoom, _ = workspace._current_canvas_world_view()
    assert center_after_zoom == pytest.approx(center_before)
    assert width_after_zoom == pytest.approx(width_before / 2)
    assert not workspace._commit_browser_canvas_view(
        (2.0, pixel_center[0] * -1.0, pixel_center[1] * -1.0, 1),
        render=False,
    )

    assert workspace._commit_browser_canvas_view((1.0, 40.0, -24.0, 2), render=False)
    center_after_pan, width_after_pan, _ = workspace._current_canvas_world_view()
    assert width_after_pan == pytest.approx(width_after_zoom)
    assert center_after_pan[0] < center_after_zoom[0]
    assert center_after_pan[1] < center_after_zoom[1]


def test_sketch_uses_browser_local_motion_and_only_sends_completed_events(standalone_components):
    workspace = SolveWorkspace()
    callbacks = workspace._canvas._callbacks

    assert "mouseup" in callbacks
    assert "wheel" in callbacks
    assert "mousemove" not in callbacks
    assert "mousedown" not in callbacks


def test_region_names_are_not_drawn_inside_sketch_regions(standalone_components):
    workspace = SolveWorkspace()
    workspace.model["geometry"]["regions"] = [
        {
            "id": "outer",
            "name": "Outer domain",
            "parent_id": None,
            "material_id": None,
            "shape": {"type": "rectangle", "r_min": 0.0, "z_min": -0.01, "width": 0.02, "height": 0.02},
            "vertices": [[0.0, -0.01], [0.02, -0.01], [0.02, 0.01], [0.0, 0.01]],
        },
        {
            "id": "inner",
            "name": "Coil",
            "parent_id": "outer",
            "material_id": None,
            "shape": {"type": "rectangle", "r_min": 0.008, "z_min": -0.003, "width": 0.004, "height": 0.006},
            "vertices": [[0.008, -0.003], [0.012, -0.003], [0.012, 0.003], [0.008, 0.003]],
        },
    ]

    workspace.render_canvas()
    region_text = [
        item for item in workspace._canvas_scene.ui_children
        if item._component_name == "text" and "data-sketch-region-id" in item._props
    ]

    assert region_text == []
    assert not any(
        item._component_name == "text" and item._props.get("textContent") in {"Outer domain", "Coil"}
        for item in workspace._canvas_scene.ui_children
    )


def test_empty_sketch_has_no_centered_viewport_instruction(standalone_components):
    workspace = SolveWorkspace()
    text_nodes = [
        item for item in workspace._canvas_scene.ui_children
        if item._component_name == "text"
    ]

    assert text_nodes == []


def test_sketch_grid_labels_do_not_intercept_or_select_canvas_drags(standalone_components):
    workspace = SolveWorkspace()
    svg_text = [item for item in workspace._canvas_grid.ui_children if item._component_name == "text"]

    assert svg_text
    assert "user-select:none" in workspace._canvas._props["style"]
    assert "-webkit-user-select:none" in workspace._canvas._props["style"]
    assert all("pointer-events:none" in item._props["style"] for item in svg_text)
    assert all("user-select:none" in item._props["style"] for item in svg_text)


def test_axisymmetric_view_keeps_axis_visible_for_regions_away_from_axis(standalone_components):
    workspace = SolveWorkspace()
    workspace.model["geometry"]["regions"] = [{"vertices": [[0.01, 0.0], [0.02, 0.0], [0.02, 0.01], [0.01, 0.01]]}]

    project, unproject = workspace._canvas_projection()

    assert project((0.0, 0.0))[0] == pytest.approx(72.0)
    assert project((0.01, 0.0))[0] > 72.0
    assert unproject((72.0, 320.0))[0] == pytest.approx(0.0)


def test_grid_zoom_keeps_world_geometry_and_anchor_aligned(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    project_before, unproject_before = workspace._canvas_projection()
    anchor = (700.0, 210.0)
    anchor_world = unproject_before(anchor)
    original_vertices = [
        tuple(point)
        for point in workspace.model["geometry"]["regions"][0]["vertices"]
    ]
    span_before = project_before((0.020, 0.0))[0] - project_before((0.0, 0.0))[0]

    workspace.zoom_canvas(0.5, anchor)

    project_after, unproject_after = workspace._canvas_projection()
    span_after = project_after((0.020, 0.0))[0] - project_after((0.0, 0.0))[0]
    assert unproject_after(anchor) == pytest.approx(anchor_world)
    assert span_after == pytest.approx(2 * span_before)
    assert [tuple(point) for point in workspace.model["geometry"]["regions"][0]["vertices"]] == original_vertices


def test_canvas_wheel_zooms_at_the_current_pointer(standalone_components):
    workspace = SolveWorkspace()
    calls = []
    anchor = (740.0, 200.0)
    workspace._canvas_event_point = lambda event, refresh_transform=False: anchor
    workspace.zoom_canvas = lambda factor, point=None, render=True: calls.append((factor, point, render))
    workspace._schedule_canvas_zoom_render = lambda: calls.append("schedule")

    workspace._on_canvas_wheel(SimpleNamespace(value={"deltaY": 120.0}))

    assert calls == [(pytest.approx(math.exp(0.18)), anchor, False), "schedule"]


def test_wheel_zoom_coalesces_canvas_renders_until_input_settles(standalone_components, monkeypatch):
    import ngsolve_gui.solve_workspace as solve_workspace_module

    timers = []

    class FakeTimer:
        def __init__(self, interval, callback, args=()):
            self.interval = interval
            self.callback = callback
            self.args = args
            self.cancelled = False
            timers.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

        def fire(self):
            self.callback(*self.args)

    monkeypatch.setattr(solve_workspace_module, "Timer", FakeTimer)
    workspace = SolveWorkspace()
    anchor = (740.0, 200.0)
    workspace._canvas_event_point = lambda event, refresh_transform=False: anchor
    renders = []
    workspace.render_canvas = lambda: renders.append("render")
    initial_width = workspace._current_canvas_world_view()[1]

    workspace._on_canvas_wheel(SimpleNamespace(value={"deltaY": 120.0}))
    first_timer = timers[-1]
    workspace._on_canvas_wheel(SimpleNamespace(value={"deltaY": -60.0}))
    second_timer = timers[-1]

    assert first_timer.cancelled
    assert renders == []
    assert workspace._current_canvas_world_view()[1] == pytest.approx(
        initial_width * math.exp(0.09)
    )
    second_timer.fire()
    assert renders == ["render"]


def test_grid_zoom_out_expands_without_a_fixed_view_boundary(standalone_components):
    workspace = SolveWorkspace()
    _, initial_width, _ = workspace._current_canvas_world_view()
    workspace.render_canvas = lambda: None

    for _ in range(500):
        workspace.zoom_canvas(1.2)

    _, zoomed_width, _ = workspace._current_canvas_world_view()
    assert zoomed_width > initial_width * 1e30
    assert math.isfinite(zoomed_width)
    step = workspace._canvas_grid_step()
    ticks = workspace._ticks_for_step(-zoomed_width / 2, zoomed_width / 2, step)
    assert 0 < len(ticks) <= 200
    assert all(math.isfinite(tick) for tick in ticks)


def test_grid_snap_toggle_snaps_sketch_points_to_grid_intersections(standalone_components):
    workspace = SolveWorkspace()
    workspace.toggle_snap_to_grid()
    assert workspace.snap_to_grid
    project, unproject = workspace._canvas_projection()
    grid_step = workspace._canvas_grid_step()
    unsnapped = project((grid_step * 1.37, grid_step * 2.62))
    snapped = workspace._snap_canvas_point(unsnapped)
    radial, axial = unproject(snapped)
    assert radial == pytest.approx(round(radial / grid_step) * grid_step)
    assert axial == pytest.approx(round(axial / grid_step) * grid_step)

    workspace.toggle_snap_to_grid()
    assert not workspace.snap_to_grid
    assert workspace._snap_canvas_point(unsnapped) == unsnapped


def test_grid_snap_button_is_present_with_the_sketch_tools(standalone_components):
    workspace = SolveWorkspace()
    toolbar = workspace._canvas_panel.ui_children[0]
    slots = toolbar.ui_slots["default"]
    snap_index = next(index for index, item in enumerate(slots) if getattr(item, "ui_icon", None) == "mdi-magnet")
    fit_view_index = next(index for index, item in enumerate(slots) if getattr(item, "ui_label", None) == "Fit view")
    snap_button = slots[snap_index]

    assert workspace._snap_button is snap_button
    assert snap_button.ui_icon == "mdi-magnet"
    assert snap_button.ui_label == "Snap"
    assert "flex:0 0 auto" in snap_button.ui_style
    assert snap_index < fit_view_index


def test_added_model_parameter_is_listed_in_tree(standalone_components):
    workspace = SolveWorkspace()
    workspace.select_section("parameters")

    workspace.add_parameter()

    model_group = workspace._left_items.ui_children[0]
    parameter_row = model_group.ui_children[2]
    parameter_entries = workspace._tree_entry_lists["parameters"]
    assert parameter_row.ui_children[1] is workspace._tree_entry_containers["parameters"]
    assert any(child.ui_label == "length_1" for child in parameter_entries.ui_children)
    assert workspace.selected_parameter_id == workspace.model["parameters"][0]["id"]


def test_parameter_dimension_is_selected_from_physical_units(standalone_components):
    workspace = SolveWorkspace()
    workspace.add_parameter()
    parameter = workspace.model["parameters"][0]
    entries = workspace._parameter_properties()
    widgets = []

    def collect(items):
        for item in items:
            widgets.append(item)
            collect(getattr(item, "ui_children", []))

    collect(entries)
    dimension = next(item for item in widgets if getattr(item, "ui_label", None) == "Dimension")

    assert dimension.ui_model_value == "m"
    assert {option["value"] for option in dimension.ui_options} >= {"m", "A/m^2", "Pa", "Hz"}
    assert parameter["unit"] == "m"


def test_model_tree_counts_only_project_materials_and_assigned_boundary_conditions(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    edges = workspace.model["geometry"]["edges"]
    assert all(edge["boundary_condition_ids"] == [] for edge in edges)

    workspace._set_region_value(region["id"], "material_id", "material-air")
    assert workspace._tree_subsection("materials")[1][0][1] == "Air · 1 region"
    workspace._set_region_value(region["id"], "material_id", "material-copper")
    materials = {entry[1] for entry in workspace._tree_subsection("materials")[1]}
    assert materials == {"Air · 0 regions", "Copper · 1 region"}

    condition = {"id": "boundary-fixed", "name": "Fixed support", "type": "mechanical_fixed"}
    workspace.model["boundary_conditions"].append(condition)
    workspace._refresh_model_tree()
    assert workspace._tree_subsection("boundaries")[1][0][1] == "Fixed support · 0 edges"
    workspace._set_edge_condition(edges[1]["id"], "mechanical", condition["id"])
    assert workspace._tree_subsection("boundaries")[1][0][1] == "Fixed support · 1 edge"


def test_unused_builtin_material_can_be_removed_from_the_model(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._set_region_value(region["id"], "material_id", "material-air")
    workspace._set_region_value(region["id"], "material_id", None)

    workspace.remove_material("material-air")

    assert workspace.model["materials"] == []
    assert workspace._tree_subsection("materials") == ("MATERIALS · 0", [])


def test_parameter_removal_is_available_and_blocked_while_referenced(standalone_components):
    workspace = SolveWorkspace()
    workspace.add_parameter()
    parameter = workspace.model["parameters"][0]
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._set_region_dimension(region["id"], "width", "length_1*2")

    workspace.remove_parameter(parameter["id"])

    assert workspace.model["parameters"] == [parameter]
    assert "Cannot remove 'length_1'" in workspace.message
    assert "region 'square 1' width" in workspace.message

    workspace._set_region_dimension(region["id"], "width", "0.02")
    workspace.remove_parameter(parameter["id"])
    assert workspace.model["parameters"] == []
    assert "Removed parameter 'length_1'" in workspace.message


def test_physics_panel_does_not_duplicate_frequency_or_regional_load_fields(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    physics = workspace._physics_properties()
    sources = workspace._sources_properties()

    def component_labels(nodes):
        labels = []
        for node in nodes:
            label = getattr(node, "ui_label", None)
            if label:
                labels.append(label)
            labels.extend(component_labels(getattr(node, "ui_children", [])))
        return labels

    physics_labels = component_labels(physics)
    source_labels = component_labels(sources)
    assert "Configure study frequencies" in physics_labels
    assert "Study frequencies" not in physics_labels
    assert "DC current density" not in physics_labels
    assert "DC current density" in source_labels
    assert "Radial body force" in source_labels
    assert "Region sources apply to" in source_labels


def test_model_and_properties_panels_can_be_collapsed_and_restored(standalone_components):
    workspace = SolveWorkspace()

    workspace._toggle_tree_panel()
    workspace._toggle_properties_panel()
    assert workspace._tree_splitter.ui_model_value == 0
    assert workspace._properties_splitter.ui_model_value == 0
    assert workspace._tree_splitter.ui_limits == [0, 420]
    assert workspace._properties_splitter.ui_limits == [0, 500]

    workspace._toggle_tree_panel()
    workspace._toggle_properties_panel()
    assert workspace._tree_splitter.ui_model_value == 240
    assert workspace._properties_splitter.ui_model_value == 340
    assert workspace._tree_splitter.ui_limits == [180, 420]
    assert workspace._properties_splitter.ui_limits == [240, 500]


def test_setup_validation_checks_actual_study_frequency_list(standalone_components):
    workspace = SolveWorkspace()
    workspace.studies["studies"][0]["frequency_hz"] = ["0"]

    assert any("Study frequency" in error for error in workspace.validation_errors())
    assert workspace._validation_issue_section("Study frequency must be positive") == "studies"


def test_canvas_event_reads_pointer_cache_when_ngapp_omits_coordinates(standalone_components):
    workspace = SolveWorkspace()
    workspace._screen_to_svg = (1.25, 0.0, 0.0, 1.25, 100.0, 200.0)
    workspace._read_canvas_pointer = lambda value: (125.0, 300.0)

    point = workspace._canvas_event_point(
        SimpleNamespace(value={"type": "mousedown", "button": 0, "timeStamp": 12.5})
    )

    assert point == pytest.approx((20.0, 80.0))
