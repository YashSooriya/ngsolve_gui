import copy
import os
import json
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from ngapp import utils
from ngapp.components import Div

import ngsolve_gui.app as app_module
from ngsolve_gui.app import NGSolveGui
from ngsolve_gui.axisymmetric_model import builtin_materials, validate_model
from ngsolve_gui.solve_workspace import (
    SolveWorkspace,
    _load_saved_run_history,
    default_run_history_root,
)
from ngsolve_gui.webgpu_tab import WebgpuTab


@pytest.fixture
def standalone_components(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ngapp-config"))
    monkeypatch.setattr(
        utils,
        "user_config_dir",
        lambda app_name: str(tmp_path / "user-config" / app_name),
    )
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


def test_problem_dirty_state_tracks_edits_and_saved_snapshots(standalone_components):
    workspace = SolveWorkspace()
    assert workspace.dirty is False

    workspace.model["name"] = "Two solenoids"
    assert workspace.dirty is True

    workspace.mark_saved()
    assert workspace.dirty is False

    workspace.studies["studies"][0]["name"] = "Fully coupled study"
    assert workspace.dirty is True
    workspace.studies["studies"][0]["name"] = "DC + harmonic"
    assert workspace.dirty is False


def test_loading_a_problem_sets_a_clean_saved_snapshot(standalone_components):
    workspace = SolveWorkspace()
    model = copy.deepcopy(workspace.model)
    model["name"] = "Loaded problem"
    workspace._editor_drawer = {"kind": "source"}
    workspace.hp_target_selection_active = True

    workspace.set_model(model)

    assert workspace.model["name"] == "Loaded problem"
    assert workspace.dirty is False
    assert workspace._editor_drawer is None
    assert workspace.hp_target_selection_active is False


def test_problem_cannot_be_replaced_while_a_job_is_active(standalone_components):
    workspace = SolveWorkspace()
    model = copy.deepcopy(workspace.model)
    workspace._job_active = True

    with pytest.raises(RuntimeError, match="while a mesh or solver job is running"):
        workspace.set_model(model)


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
    region = workspace.model["geometry"]["regions"][0]
    workspace._open_sources_editor(region["id"])
    source_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in components(workspace._editor_drawer_children())
        if getattr(item, "ui_label", None)
    }
    assert source_units["DC current density"] == "A/m²"
    assert source_units["Radial body force"] == "N/m³"

    workspace.model["materials"] = builtin_materials()
    workspace.selected_material_id = "material-copper"
    workspace._open_material_editor("material-copper")
    material_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in components(workspace._editor_drawer_children())
        if getattr(item, "ui_label", None)
    }
    assert material_units["Electrical conductivity"] == "S/m"
    assert material_units["Young's modulus"] == "Pa"
    assert material_units["Density"] == "kg/m³"

    workspace.model["boundary_conditions"] = [
        {"id": "traction", "name": "Traction", "type": "mechanical_traction"}
    ]
    workspace.selected_boundary_id = "traction"
    workspace._open_boundary_editor("traction")
    boundary_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in components(workspace._editor_drawer_children())
        if getattr(item, "ui_label", None)
    }
    assert boundary_units["Radial traction"] == "N/m²"
    workspace._cancel_editor()
    assert workspace._mesh_properties()[1]._props["suffix"] == "m"
    study_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in components(workspace._study_properties())
        if getattr(item, "ui_label", None)
    }
    assert study_units["Frequency points (comma separated)"] == "Hz"

def test_mesh_panel_exposes_regional_sizes_and_boundary_or_region_hp_targets(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]

    def components(items):
        for item in items:
            yield item
            yield from components(getattr(item, "ui_children", []))

    controls = list(components(workspace._mesh_properties()))
    labels = [getattr(item, "ui_label", None) for item in controls]
    assert "Add region-wise element sizing" in labels
    assert "square 1 mesh size" not in labels
    assert "Number of hp layers" in labels
    assert "Layer grading factor" in labels
    assert "Select regions and edges" in labels

    workspace._open_region_mesh_size_editor()
    assert workspace._editor_drawer["kind"] == "region_mesh_size"
    editor_controls = list(components(workspace._editor_drawer_children()))
    editor_labels = [getattr(item, "ui_label", None) for item in editor_controls]
    assert "Region" in editor_labels
    assert "Target element size" in editor_labels
    size_input = next(item for item in editor_controls if getattr(item, "ui_label", None) == "Target element size")
    assert size_input.ui_suffix == "m"
    workspace._editor_drawer["draft"].update({"region_id": region["id"], "value": "0.002"})
    workspace._commit_editor()
    assert workspace.model["mesh"]["region_element_sizes"][region["id"]] == "0.002"

    workspace._set_mesh("hp_layers", 2)
    workspace._toggle_hp_layer_selection()
    workspace._toggle_hp_region_target(region["id"])
    edge_id = workspace.model["geometry"]["edges"][0]["id"]
    workspace._toggle_hp_edge_target(edge_id)
    assert workspace.model["mesh"]["hp_layers"] == 2
    assert workspace.model["mesh"]["hp_region_ids"] == [region["id"]]
    assert workspace.model["mesh"]["hp_edge_ids"] == [edge_id]
    workspace._toggle_hp_region_target(region["id"])
    assert workspace.model["mesh"]["hp_region_ids"] == []
    workspace._toggle_hp_layer_selection()
    workspace._remove_region_mesh_size(region["id"])
    assert region["id"] not in workspace.model["mesh"]["region_element_sizes"]

def test_regional_and_hp_settings_invalidate_mesh_preview(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace.set_mesh_preview([((0, 0), (1, 0))])
    workspace._set_region_mesh_size(region["id"], "0.005")
    assert workspace.mesh_preview_edges == []

    workspace.set_mesh_preview([((0, 0), (1, 0))])
    workspace._set_hp_layer_target("hp_edge_ids", workspace.model["geometry"]["edges"][0]["id"], True)
    assert workspace.mesh_preview_edges == []


def test_canvas_visibility_toggles_preserve_independent_mesh_preview(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace.set_mesh_preview([((0.0, 0.0), (1.0, 0.0))])
    region_node = workspace._canvas_region_nodes[region["id"]]
    edge_nodes = workspace._canvas_edge_nodes[region["edge_ids"][0]]

    assert workspace._canvas_background_button.ui_label == "Hide background"
    assert workspace._canvas_geometry_button.ui_label == "Hide geometry"
    assert workspace._canvas_grid_layer._props["display"] == "inline"
    assert region_node._props["display"] == "inline"

    workspace.toggle_canvas_background()
    assert not workspace._canvas_background_visible
    assert workspace._canvas_background_button.ui_label == "Show background"
    assert workspace._canvas_grid_layer._props["display"] == "none"
    assert region_node._props["display"] == "inline"
    assert workspace._canvas_mesh_path._props["display"] == "inline"

    workspace.toggle_canvas_geometry()
    assert not workspace._canvas_geometry_visible
    assert workspace._canvas_geometry_button.ui_label == "Show geometry"
    assert region_node._props["display"] == "none"
    assert edge_nodes[0]._props["display"] == "none"
    assert edge_nodes[1]._props["display"] == "none"
    assert workspace._canvas_dimensions._props["display"] == "none"
    assert workspace._canvas_mesh_path._props["display"] == "inline"

    workspace.toggle_canvas_background()
    workspace.toggle_canvas_geometry()
    assert workspace._canvas_grid_layer._props["display"] == "inline"
    assert region_node._props["display"] == "inline"
    assert edge_nodes[0]._props["display"] == "inline"
    assert edge_nodes[1]._props["display"] == "inline"


def test_generated_mesh_preview_is_invalidated_only_by_mesh_relevant_edits(standalone_components):
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
    workspace._set_solver("relative_tolerance", "1e-5")
    workspace._set_study_frequencies("study-static", "250, 500")

    assert workspace.mesh_preview_edges
    assert workspace.mesh_preview_visible

    workspace._set_mesh("element_size", "0.005")

    assert workspace.mesh_preview_edges == []
    assert not workspace.mesh_preview_visible
    assert workspace._mesh_preview_button.ui_disable

    workspace.set_mesh_preview([((0.0, 0.0), (1.0, 0.0))])
    workspace._add_primitive("rectangle")
    assert workspace.mesh_preview_edges == []


def test_mesh_preview_invalidates_when_a_mesh_parameter_changes(standalone_components):
    workspace = SolveWorkspace()
    workspace.add_parameter()
    parameter = workspace.model["parameters"][0]
    workspace._set_mesh("element_size", parameter["name"])
    workspace.set_mesh_preview([((0.0, 0.0), (1.0, 0.0))])

    workspace._set_parameter(parameter["id"], "expression", "0.005")

    assert workspace.mesh_preview_edges == []
    assert workspace._mesh_preview_button.ui_disable


def test_ngsolve_mesh_edges_are_complete_unique_and_curved_for_preview():
    from netgen.geom2d import MakeCircle, SplineGeometry, unit_square
    from ngsolve import Mesh, VOL
    from ngsolve_gui.app import _axisymmetric_mesh_edges

    def assert_preview_matches_mesh_topology(mesh, preview_edges):
        def point_key(point):
            return tuple(round(float(value), 12) for value in point[:2])

        expected = set()
        for element in mesh.Elements(VOL):
            vertices = tuple(element.vertices)
            for index, start in enumerate(vertices):
                end = vertices[(index + 1) % len(vertices)]
                expected.add(tuple(sorted((
                    point_key(mesh[start].point),
                    point_key(mesh[end].point),
                ))))

        actual = [tuple(sorted((point_key(edge[0]), point_key(edge[-1])))) for edge in preview_edges]
        assert len(actual) == len(expected)
        assert len(set(actual)) == len(actual)
        assert set(actual) == expected

    mesh = Mesh(unit_square.GenerateMesh(maxh=0.5))
    preview_edges = _axisymmetric_mesh_edges(mesh)
    element_edge_count = sum(len(element.vertices) for element in mesh.Elements(VOL))

    assert preview_edges
    assert len(preview_edges) < element_edge_count
    assert all(len(point) == 2 for edge in preview_edges for point in edge)
    assert_preview_matches_mesh_topology(mesh, preview_edges)

    circle = SplineGeometry()
    MakeCircle(circle, (0.0, 0.0), 1.0)
    curved_mesh = Mesh(circle.GenerateMesh(maxh=0.8))
    curved_mesh.Curve(4)
    curved_edges = _axisymmetric_mesh_edges(curved_mesh)
    assert any(len(edge) > 2 for edge in curved_edges)
    assert_preview_matches_mesh_topology(curved_mesh, curved_edges)

    # Small, arc-bounded region: mesh vertex ordering must not cause a curved
    # boundary edge to disappear from the SVG mesh preview.
    sketch = SplineGeometry()
    left = sketch.AppendPoint(0.007, 0.0)
    right = sketch.AppendPoint(0.012, 0.0)
    top = sketch.AppendPoint(0.0095, 0.0012)
    sketch.Append(["line", left, right], leftdomain=1, rightdomain=0, bc="base")
    sketch.Append(["spline3", right, top, left], leftdomain=1, rightdomain=0, bc="arc")
    sketch.SetMaterial(1, "Region_1")
    small_curved_mesh = Mesh(sketch.GenerateMesh(maxh=0.01))
    small_curved_mesh.Curve(3)
    small_preview_edges = _axisymmetric_mesh_edges(small_curved_mesh)
    assert_preview_matches_mesh_topology(small_curved_mesh, small_preview_edges)
    assert any(len(edge) > 2 for edge in small_preview_edges)

    # A 0.1 mm feature must retain the same complete edge topology.
    tiny = SplineGeometry()
    left = tiny.AppendPoint(0.00014, 0.0)
    right = tiny.AppendPoint(0.00024, 0.0)
    top = tiny.AppendPoint(0.00019, 0.000024)
    tiny.Append(["line", left, right], leftdomain=1, rightdomain=0, bc="base")
    tiny.Append(["spline3", right, top, left], leftdomain=1, rightdomain=0, bc="arc")
    tiny.SetMaterial(1, "Tiny_Region")
    tiny_mesh = Mesh(tiny.GenerateMesh(maxh=0.0001))
    tiny_mesh.Curve(3)
    tiny_preview_edges = _axisymmetric_mesh_edges(tiny_mesh)
    assert_preview_matches_mesh_topology(tiny_mesh, tiny_preview_edges)
    assert any(len(edge) > 2 for edge in tiny_preview_edges)


def test_mesh_preview_keeps_curved_edge_polyline_points(standalone_components):
    workspace = SolveWorkspace()
    workspace.set_mesh_preview([[
        [0.0, 0.0], [0.5, 0.25], [1.0, 0.0],
    ]])
    assert workspace.mesh_preview_edges == [((0.0, 0.0), (0.5, 0.25), (1.0, 0.0))]


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
    rename_button = workspace._model_title_rename_button
    assert title._props["title"] == "Model name. Double-click to edit or use Rename."
    assert rename_button.ui_label == "Rename"
    assert rename_button.ui_icon == "mdi-pencil-outline"
    assert rename_button.ui_outline
    assert "border:1px solid var(--border)" in workspace._model_title_control.ui_style

    title._handle("focus")
    assert not workspace._model_title_editing
    assert title.ui_readonly

    rename_button._handle("click.stop")
    editor = workspace._model_title_input
    assert editor is not None
    assert workspace._model_title_editing
    assert not title.ui_readonly
    assert rename_button.ui_hidden
    assert "border:1px solid var(--primary)" in workspace._model_title_control.ui_style
    editor._handle("update:model-value", "Quarter magnet")
    editor._handle("keydown", {"key": "Enter"})

    assert workspace.model["name"] == "Quarter magnet"
    assert title.ui_model_value == "Quarter magnet"
    assert title.ui_readonly
    assert not workspace._model_title_editing
    assert not rename_button.ui_hidden
    assert "border:1px solid var(--border)" in workspace._model_title_control.ui_style

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

    assert "Setup inputs passed" in workspace.message
    assert callable(workspace.validation_warnings)
    assert "mmfem-solve-status-flash-a" in workspace._bottom_bar.ui_class
    assert len(workspace.setup_check_results) == 13
    assert not workspace._messages_panel.ui_hidden

    workspace.validation_errors = lambda: ["Create a geometry region."]
    workspace.validate_action()

    assert "found 1 issue" in workspace.message
    assert "mmfem-solve-status-flash-b" in workspace._bottom_bar.ui_class


def test_setup_check_results_show_group_status_and_applicability(standalone_components):
    workspace = SolveWorkspace()
    workspace.model["physics"]["dc_magnetic"]["enabled"] = True
    workspace.model["physics"]["mechanics"]["enabled"] = True
    results = workspace._setup_check_results([
        "Mesh settings: target element size must be positive.",
        "Regional load 'coil current' dc_current_density: invalid expression.",
    ], warnings=[
        "No magnetic-potential boundary is explicitly assigned; natural boundaries are allowed."
    ])
    by_id = {item["id"]: item for item in results}

    assert len(results) == 13
    assert by_id["em_reference"]["status"] == "Warning"
    assert by_id["mechanics"]["status"] == "Passed"
    assert by_id["mesh"]["status"] == "Needs attention"
    assert by_id["loads"]["status"] == "Needs attention"
    assert by_id["geometry"]["status"] == "Passed"
    assert by_id["materials"]["status"] == "Passed"
    assert by_id["numerical_operator"]["status"] == "Pending"

    workspace.model["physics"]["dc_magnetic"]["enabled"] = False
    workspace.model["physics"]["harmonic_electromagnetic"]["enabled"] = False
    workspace.model["physics"]["mechanics"]["enabled"] = False
    results = workspace._setup_check_results([])
    by_id = {item["id"]: item for item in results}
    assert by_id["em_reference"]["status"] == "Not applicable"
    assert by_id["mechanics"]["status"] == "Not applicable"
    assert by_id["harmonic_frequencies"]["status"] == "Not applicable"


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


def test_messages_panel_has_close_button_to_minimise(standalone_components):
    workspace = SolveWorkspace()
    workspace.toggle_messages()

    assert workspace._messages_visible
    assert not workspace._messages_panel.ui_hidden
    assert workspace._messages_close_button.ui_icon == "mdi-close"
    assert workspace._messages_close_button in workspace._messages_header.ui_children

    workspace._messages_close_button._callbacks["click.stop"][0](None)
    assert not workspace._messages_visible
    assert workspace._messages_panel.ui_hidden

    workspace.toggle_messages()
    assert workspace._messages_visible
    assert not workspace._messages_panel.ui_hidden


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
        "id": os.path.normcase(str(run_dir.resolve())),
        "name": "Study: Nested model",
        "kind": "Study",
        "status": "Interrupted",
        "finished": "2026-10-10T12:00:00+00:00",
        "output_path": str(run_dir),
    }]


def test_run_history_context_menu_hides_entry_and_preserves_output(standalone_components, tmp_path):
    run_dir = tmp_path / "study_result"
    run_dir.mkdir()
    output_file = run_dir / "mesh.vol"
    output_file.write_text("stored mesh", encoding="utf-8")
    (run_dir / "run_manifest.json").write_text(json.dumps({
        "schema": "mm-fem.run",
        "kind": "study",
        "status": "complete",
        "model_name": "Nested model",
        "created_utc": "2026-10-10T12:00:00+00:00",
    }), encoding="utf-8")

    workspace = SolveWorkspace()
    workspace._run_history_root = tmp_path
    workspace.runs = _load_saved_run_history(tmp_path)
    workspace._refresh_model_tree()
    run_id = workspace.runs[0]["id"]
    tree_entry = workspace._tree_entry_buttons[f"runs:{run_id}"]
    context_menu = next(child for child in tree_entry.ui_children if child.__class__.__name__ == "QMenu")
    menu_list = context_menu.ui_children[0]
    delete_item = menu_list.ui_children[0]
    assert any(
        child == "Delete"
        for section in delete_item.ui_children
        for child in getattr(section, "ui_children", [])
    )

    delete_item._handle("click.stop")

    assert workspace.runs == []
    assert workspace._tree_subsection("runs") == ("RUNS · 0", [])
    assert output_file.read_text(encoding="utf-8") == "stored mesh"
    assert _load_saved_run_history(tmp_path) == []


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


def test_3d_preview_opens_large_modal_and_exit_restores_editable_sketch(standalone_components):
    workspace = SolveWorkspace()
    preview = Div()
    workspace._build_3d_preview_component = lambda: preview
    original_canvas = workspace._canvas

    workspace.toggle_3d_preview()

    assert workspace._preview_3d_active
    assert workspace._preview_3d_component is preview
    assert workspace._preview_3d_dialog.ui_model_value is True
    assert workspace._preview_3d_viewport.ui_children == [preview]
    assert workspace._canvas_host.ui_children == [original_canvas, workspace._canvas_resize_observer, workspace._canvas_context_menu]
    assert workspace._preview_3d_dialog._props["maximized"] is True
    assert workspace._preview_3d_dialog._props["persistent"] is True
    assert workspace._preview_3d_exit_button.ui_label == "Exit 3D Preview"
    assert workspace._preview_3d_button.ui_label == "Exit 3D Preview"
    assert all(not control.ui_hidden for control in workspace._sketch_view_controls)

    workspace.toggle_3d_preview()

    assert not workspace._preview_3d_active
    assert workspace._preview_3d_dialog.ui_model_value is False
    assert workspace._preview_3d_component is None
    assert workspace._preview_3d_viewport.ui_children == []
    assert workspace._canvas_host.ui_children == [original_canvas, workspace._canvas_resize_observer, workspace._canvas_context_menu]
    assert workspace._preview_3d_button.ui_label == "Preview in 3D"
    assert all(not control.ui_hidden for control in workspace._sketch_view_controls)


def test_axisymmetric_revolution_toolbar_controls_follow_mesh_dimension(standalone_components):
    tab = object.__new__(WebgpuTab)
    tab._page_toolbar_enabled = True
    tab.mesh = SimpleNamespace(dim=2)
    tab.axisymmetric_revolution_available = True

    assert tab._supports_camera_view_buttons()
    assert tab._supports_clipping()

    tab._camera_view_group = SimpleNamespace(ui_hidden=True)
    tab._clip_tool_group = SimpleNamespace(ui_hidden=True)
    tab._clip_tool = SimpleNamespace(ui_class="")
    tab._clip_toolbar = SimpleNamespace(ui_hidden=False)
    tab.clipping_enabled = SimpleNamespace(value=True)

    tab._sync_dynamic_3d_toolbar_state()
    assert tab._camera_view_group.ui_hidden is False
    assert tab._clip_tool_group.ui_hidden is False
    assert tab._clip_toolbar.ui_hidden is False

    tab.mesh.dim = 3
    tab._sync_dynamic_3d_toolbar_state()
    assert tab._camera_view_group.ui_hidden is False
    assert tab._clip_tool_group.ui_hidden is False
    assert tab._clip_toolbar.ui_hidden is False


def test_3d_preview_region_table_lists_each_region_with_three_visibility_states(standalone_components):
    workspace = SolveWorkspace()
    workspace.model["geometry"]["regions"] = [
        {"id": "outer", "name": "Outer air"},
        {"id": "coil", "name": "Coil"},
    ]
    workspace._preview_region_visibility = {"outer": "opaque", "coil": "translucent"}

    workspace._render_3d_preview_region_table()

    assert set(workspace._preview_visibility_controls) == {"outer", "coil"}
    assert len(workspace._preview_3d_region_rows.ui_children) == 2
    assert workspace._preview_visibility_controls["outer"].ui_model_value == "opaque"
    assert workspace._preview_visibility_controls["coil"].ui_model_value == "translucent"
    toggle_props = workspace._preview_visibility_controls["outer"]._props
    assert toggle_props["outline"] is False
    assert toggle_props["toggle-color"] == "blue-9"
    assert toggle_props["toggle-text-color"] == "white"
    assert [
        option["value"]
        for option in workspace._preview_visibility_controls["outer"]._props["options"]
    ] == ["opaque", "translucent", "hidden"]

    workspace._set_3d_region_visibility("outer", "hidden")

    assert workspace._preview_region_visibility["outer"] == "hidden"
    assert workspace._preview_visibility_controls["outer"].ui_model_value == "hidden"


def test_3d_preview_supports_opaque_translucent_and_hidden_regions_with_axis(
    standalone_components, monkeypatch
):
    import ngapp.components
    import netgen

    axis_indicator = object()
    geometry_renderers = []
    circle_profiles = []

    class Solid:
        def __init__(self, profile):
            self.faces = [profile]
            self.edges = [object()]

        def __sub__(self, _child):
            return self

        def mat(self, _name):
            return self

    class Renderer:
        def __init__(self, geometry):
            self.geo = geometry
            self.faces = SimpleNamespace(active=None, transparent=False, colors=None)
            self.faces.set_colors = lambda colors: setattr(
                self.faces, "colors", colors.copy()
            )
            self.edges = SimpleNamespace(active=None, colors=None)
            self.edges.set_colors = lambda colors: setattr(
                self.edges, "colors", colors.copy()
            )
            self.update_requests = 0

        def set_needs_update(self):
            self.update_requests += 1

    def make_geometry_renderer(geometry):
        renderer = Renderer(geometry)
        geometry_renderers.append(renderer)
        return renderer

    def make_circle(center, normal, radius):
        profile = object()
        circle_profiles.append((center, normal, radius, profile))
        return profile

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
    occ.Circle = make_circle
    occ.Dir = lambda *args: args
    occ.Face = lambda profile: profile
    occ.MakePolygon = lambda _vertices: object()
    occ.OCCGeometry = lambda solid: SimpleNamespace(
        faces=list(solid.faces), edges=list(solid.edges), shape=solid
    )
    occ.Pnt = lambda *args: args
    occ.Revolve = lambda profile, *_args: Solid(profile)
    occ.Vec = lambda *_args: object()
    occ.Vertex = lambda _point: object()
    occ.Wire = lambda profile: profile
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
    workspace.model["geometry"]["regions"] = [
        {
            "id": "circle-outer",
            "name": "circle 1",
            "shape": {
                "type": "circle", "r_center": 0.02, "z_center": 0.0, "radius": 0.01,
            },
            "vertices": [[0.01, 0.0], [0.03, 0.0], [0.02, 0.01]],
        },
        {
            "id": "circle-inner",
            "name": "circle 2",
            "shape": {
                "type": "circle", "r_center": 0.02, "z_center": 0.0, "radius": 0.002,
            },
            "vertices": [[0.018, 0.0], [0.022, 0.0], [0.02, 0.002]],
            "parent_id": "circle-outer",
        },
    ]
    preview = workspace._build_3d_preview_component()

    assert len(geometry_renderers) == 2
    outer = workspace._preview_3d_renderers["circle-outer"]
    inner = workspace._preview_3d_renderers["circle-inner"]
    assert outer.faces.active is True
    assert inner.faces.active is True
    assert outer.edges.active is True
    assert inner.edges.active is True
    assert [center for center, _normal, _radius, _profile in circle_profiles] == [
        (0.02, 0.0, 0.0), (0.02, 0.0, 0.0),
    ]
    assert [normal for _center, normal, _radius, _profile in circle_profiles] == [
        (0.0, 1.0, 0.0), (0.0, 1.0, 0.0),
    ]
    assert [radius for _center, _normal, radius, _profile in circle_profiles] == pytest.approx([0.01, 0.002])
    assert outer.faces.colors[3::4].tolist() == [1.0]
    assert inner.faces.colors[3::4].tolist() == [1.0]
    assert outer.faces.transparent is False
    assert inner.faces.transparent is False
    assert preview.renderers == [outer, inner, axis_indicator]

    workspace._set_3d_region_visibility("circle-inner", "translucent")
    assert outer.faces.colors[3::4].tolist() == [1.0]
    assert inner.faces.colors[3::4].tolist() == pytest.approx([0.38])
    assert outer.faces.transparent is False
    assert inner.faces.transparent is True

    # The exterior and each child retain independent alpha in their own passes.
    workspace._set_3d_region_visibility("circle-inner", "opaque")
    workspace._set_3d_region_visibility("circle-outer", "translucent")
    assert outer.faces.colors[3::4].tolist() == pytest.approx([0.38])
    assert inner.faces.colors[3::4].tolist() == [1.0]
    assert outer.faces.transparent is True
    assert inner.faces.transparent is False

    workspace._set_3d_region_visibility("circle-inner", "translucent")
    assert outer.faces.colors[3::4].tolist() == pytest.approx([0.38])
    assert inner.faces.colors[3::4].tolist() == pytest.approx([0.38])
    assert outer.faces.transparent is True
    assert inner.faces.transparent is True

    workspace._set_3d_region_visibility("circle-inner", "hidden")
    assert outer.faces.colors[3::4].tolist() == pytest.approx([0.38])
    assert inner.faces.colors[3::4].tolist() == [0.0]
    assert outer.faces.active is True
    assert inner.faces.active is False
    assert outer.edges.active is True
    assert inner.edges.active is False


def test_3d_preview_failure_keeps_editable_sketch_active(standalone_components):
    workspace = SolveWorkspace()
    workspace._build_3d_preview_component = lambda: (_ for _ in ()).throw(ValueError("invalid profile"))

    workspace.toggle_3d_preview()

    assert not workspace._preview_3d_active
    assert workspace._preview_3d_dialog.ui_model_value is False
    assert workspace._canvas_host.ui_children == [workspace._canvas, workspace._canvas_resize_observer, workspace._canvas_context_menu]
    assert workspace._preview_3d_button.ui_label == "Preview in 3D"
    assert "invalid profile" in workspace.message


def test_one_em_and_mechanical_condition_coexist_and_support_replaces_transmission(standalone_components):
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
    coil_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge["id"] in coil["edge_ids"]
        and not all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"])
    )
    workspace._set_edge_condition(outer_edge["id"], "electromagnetic", "boundary-outer")
    workspace._set_edge_condition(coil_edge["id"], "electromagnetic", "boundary-outer")
    after_em_assignment = next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == coil_edge["id"])
    auto_before_support = next(
        item for item in workspace.model["boundary_conditions"]
        if item.get("type") == "transmission_interface" and item.get("automatic_for_mechanics")
    )
    assert set(after_em_assignment["boundary_condition_ids"]) == {"boundary-outer", auto_before_support["id"]}
    workspace._set_edge_condition(coil_edge["id"], "mechanical", "boundary-fixed")

    updated_edge = next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == coil_edge["id"])
    automatic_interface = next(
        condition for condition in workspace.model["boundary_conditions"]
        if condition.get("type") == "transmission_interface"
        and condition.get("automatic_for_mechanics")
    )
    assert set(updated_edge["boundary_condition_ids"]) == {"boundary-outer", "boundary-fixed"}
    assert automatic_interface["id"] not in updated_edge["boundary_condition_ids"]
    assert updated_edge["mechanical_boundary_override"] is True
    assert workspace.validation_errors() == []


def test_electromagnetic_run_does_not_require_an_explicit_far_field_boundary(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace._set_region_value(workspace.model["geometry"]["regions"][0]["id"], "material_id", "material-air")

    assert not any(
        "anchor the electromagnetic solution" in error or "exterior edge" in error
        for error in workspace.validation_errors()
    )


def test_internal_magnetic_reference_does_not_create_an_exterior_requirement(standalone_components):
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
    inner = workspace.model["geometry"]["regions"][1]
    workspace._set_region_value(inner["id"], "material_id", "material-copper")
    workspace.model["boundary_conditions"].append(
        {"id": "boundary-inner", "name": "Inner reference", "type": "magnetic_potential_zero"}
    )
    inner_edge = next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] in inner["edge_ids"])
    workspace._set_edge_condition(inner_edge["id"], "electromagnetic", "boundary-inner")

    assert not any("exterior edge" in error for error in workspace.validation_errors())


def test_disconnected_em_regions_do_not_each_require_a_far_field_reference(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    first = workspace.model["geometry"]["regions"][0]
    workspace._set_region_value(first["id"], "material_id", "material-air")
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.05,
        ("rectangle", "z_min"): 0.03,
        ("rectangle", "width"): 0.01,
        ("rectangle", "height"): 0.01,
    })
    workspace._add_primitive("rectangle")
    second = workspace.model["geometry"]["regions"][1]
    workspace._set_region_value(second["id"], "material_id", "material-copper")

    assert not any("exterior edge" in error for error in workspace.validation_errors())


def test_positive_mass_harmonic_mechanics_does_not_require_em_or_a_fixed_edge(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._set_region_value(region["id"], "material_id", "material-copper")
    region["mechanical"] = True
    workspace.model["physics"]["dc_magnetic"]["enabled"] = False
    workspace.model["physics"]["harmonic_electromagnetic"]["enabled"] = False
    workspace.model["physics"]["mechanics"]["enabled"] = True
    workspace.studies["studies"][0]["frequency_hz"] = ["500"]

    assert workspace.validation_errors() == []
    assert any(
        "has no fixed or spring support" in warning
        for warning in workspace.validation_warnings()
    )

    workspace.studies["studies"][0]["frequency_hz"] = []
    assert any("positive frequency" in error for error in workspace.validation_errors())


def _mechanical_test_workspace(region_material="material-copper"):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._set_region_value(region["id"], "material_id", region_material)
    region["mechanical"] = True
    workspace.model["physics"]["mechanics"]["enabled"] = True
    workspace.model["boundary_conditions"].extend([
        {"id": "boundary-outer", "name": "Outer magnetic reference", "type": "magnetic_potential_zero"},
        {"id": "boundary-support", "name": "Mechanical support", "type": "mechanical_fixed"},
    ])
    outer_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if not all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"])
    )
    workspace._set_edge_condition(outer_edge["id"], "electromagnetic", "boundary-outer")
    return workspace, region, outer_edge


def test_massless_mechanics_requires_support_on_its_body_boundary(standalone_components):
    workspace, region, outer_edge = _mechanical_test_workspace()
    next(
        material for material in workspace.model["materials"]
        if material.get("id") == "material-copper"
    )["properties"]["density"] = "0"
    workspace.model["boundary_conditions"].append(
        {"id": "boundary-unrelated", "name": "Unrelated support", "type": "mechanical_fixed"}
    )
    workspace._set_edge_condition(outer_edge["id"], "mechanical", "boundary-unrelated")
    # The selected mechanical body is nested inside the non-mechanical parent;
    # assigning its support to the parent's unrelated outline must not pass.
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.003,
        ("rectangle", "z_min"): 0.003,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.005,
    })
    workspace._add_primitive("rectangle")
    inner = workspace.model["geometry"]["regions"][1]
    workspace._set_region_value(inner["id"], "material_id", "material-copper")
    region["mechanical"] = False
    inner["mechanical"] = True

    errors = workspace.validation_errors()
    assert any(
        "Mechanical boundary 'Unrelated support' is assigned to an edge that does not touch a mechanical region."
        in error for error in errors
    )
    assert any(
        "Massless harmonic mechanical component 'square 2'" in error
        for error in errors
    )


def test_positive_robin_spring_can_anchor_massless_axisymmetric_mechanics(standalone_components):
    workspace, _region, _outer_edge = _mechanical_test_workspace()
    next(
        material for material in workspace.model["materials"]
        if material.get("id") == "material-copper"
    )["properties"]["density"] = "0"
    workspace.model["boundary_conditions"].append({
        "id": "boundary-robin",
        "name": "Axial spring",
        "type": "mechanical_robin",
        "stiffness_normal": "1e6",
        "stiffness_tangential": "0",
    })
    horizontal_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if abs(float(edge["vertices"][1][1]) - float(edge["vertices"][0][1])) <= 1e-12
    )
    workspace._set_edge_condition(horizontal_edge["id"], "mechanical", "boundary-robin")

    assert not any("positive Robin support" in error for error in workspace.validation_errors())


def test_robin_spring_must_act_in_the_axial_direction_for_massless_mechanics(standalone_components):
    workspace, _region, _outer_edge = _mechanical_test_workspace()
    next(
        material for material in workspace.model["materials"]
        if material.get("id") == "material-copper"
    )["properties"]["density"] = "0"
    workspace.model["boundary_conditions"].append({
        "id": "boundary-robin",
        "name": "Radial-only spring",
        "type": "mechanical_robin",
        "stiffness_normal": "1e6",
        "stiffness_tangential": "0",
    })
    vertical_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if abs(float(edge["vertices"][1][0]) - float(edge["vertices"][0][0])) <= 1e-12
        and not all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"])
    )
    workspace._set_edge_condition(vertical_edge["id"], "mechanical", "boundary-robin")

    assert any("positive Robin support" in error for error in workspace.validation_errors())


def test_negative_robin_stiffness_is_reported_during_setup_check(standalone_components):
    workspace, _region, _outer_edge = _mechanical_test_workspace()
    workspace.model["boundary_conditions"].append({
        "id": "boundary-robin",
        "name": "Negative spring",
        "type": "mechanical_robin",
        "stiffness_normal": "-1e6",
        "stiffness_tangential": "0",
    })
    horizontal_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if abs(float(edge["vertices"][1][1]) - float(edge["vertices"][0][1])) <= 1e-12
    )
    workspace._set_edge_condition(horizontal_edge["id"], "mechanical", "boundary-robin")

    assert any("stiffnesses must be non-negative" in error for error in workspace.validation_errors())


def test_each_disconnected_massless_mechanical_body_needs_its_own_support(standalone_components):
    workspace, first_region, first_edge = _mechanical_test_workspace()
    next(
        material for material in workspace.model["materials"]
        if material.get("id") == "material-copper"
    )["properties"]["density"] = "0"
    workspace._set_edge_condition(first_edge["id"], "mechanical", "boundary-support")
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.05,
        ("rectangle", "z_min"): 0.03,
        ("rectangle", "width"): 0.01,
        ("rectangle", "height"): 0.01,
    })
    workspace._add_primitive("rectangle")
    second_region = workspace.model["geometry"]["regions"][1]
    workspace._set_region_value(second_region["id"], "material_id", "material-copper")
    second_region["mechanical"] = True

    support_issues = [
        issue for issue in workspace.validation_errors()
        if "positive Robin support" in issue
    ]
    assert first_region["id"] != second_region["id"]
    assert len(support_issues) == 1
    assert "square 2" in support_issues[0]


def test_mechanical_region_requires_positive_youngs_modulus(standalone_components):
    workspace, region, outer_edge = _mechanical_test_workspace("material-air")
    workspace._set_edge_condition(outer_edge["id"], "mechanical", "boundary-support")

    assert any("Material 'Air' needs positive Young's modulus" in error for error in workspace.validation_errors())


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


def test_circle_is_one_closed_boundary_and_legacy_circle_segments_collapse_on_load(standalone_components):
    from ngsolve_gui.axisymmetric_model import validate_model

    workspace = SolveWorkspace()
    workspace._add_primitive("circle")
    circle = workspace.model["geometry"]["regions"][0]
    geometry = workspace.model["geometry"]
    assert len(circle["edge_ids"]) == 1
    assert len(geometry["edges"]) == 1
    edge = geometry["edges"][0]
    assert edge["curve"]["type"] == "circle"
    assert edge["vertices"][0] == edge["vertices"][1]
    assert edge["curve"]["sampled_points"][0] == edge["curve"]["sampled_points"][-1]
    assert edge["point_ids"][0] == edge["point_ids"][1]
    assert validate_model(workspace.model, scope="draft") == []

    legacy = copy.deepcopy(workspace.model)
    circle = legacy["geometry"]["regions"][0]
    vertices = circle["vertices"]
    old_edges = []
    old_edge_ids = []
    condition = {"id": "bc-circle", "name": "Circle boundary", "type": "natural"}
    legacy["boundary_conditions"].append(condition)
    for index, start in enumerate(vertices):
        edge_id = f"old-circle-edge-{index}"
        old_edge_ids.append(edge_id)
        old_edges.append({
            "id": edge_id,
            "name": f"Old circle segment {index + 1}",
            "vertices": [start, vertices[(index + 1) % len(vertices)]],
            "curve": {
                "type": "arc",
                "center": [circle["shape"]["r_center"], circle["shape"]["z_center"]],
                "radius": circle["shape"]["radius"],
                "clockwise": False,
            },
            "boundary_condition_ids": [condition["id"]],
        })
    circle["edge_ids"] = old_edge_ids
    legacy["geometry"]["edges"] = old_edges
    legacy["mesh"]["hp_edge_ids"] = old_edge_ids

    loaded = SolveWorkspace()
    loaded.set_model(legacy)
    loaded_circle = loaded.model["geometry"]["regions"][0]
    loaded_edges = loaded.model["geometry"]["edges"]
    assert len(loaded_circle["edge_ids"]) == 1
    assert len(loaded_edges) == 1
    assert loaded_edges[0]["curve"]["type"] == "circle"
    assert loaded_edges[0]["boundary_condition_ids"] == [condition["id"]]
    assert loaded.model["mesh"]["hp_edge_ids"] == loaded_circle["edge_ids"]
    assert validate_model(loaded.model, scope="draft") == []


def test_sketch_line_network_splits_faces_and_reuses_a_shared_boundary(standalone_components):
    workspace = SolveWorkspace()
    for first, second in (
        ((0, 0), (4, 0)),
        ((4, 0), (2, 3)),
        ((2, 3), (0, 0)),
    ):
        assert workspace._add_sketch_entity("line", [first, second])
    original = workspace.model["geometry"]["regions"][0]
    original_id = original["id"]
    original["material_id"] = "material-copper"

    assert workspace._add_sketch_entity("line", [(4, 0), (5, 1.5)])
    assert workspace._add_sketch_entity("line", [(5, 1.5), (3, 1.5)])
    # Redrawing the shared boundary must not create a third face or duplicate the topological edge.
    assert workspace._add_sketch_entity("line", [(4, 0), (3, 1.5)])

    regions = [item for item in workspace.model["geometry"]["regions"] if item.get("shape", {}).get("type") == "sketch"]
    assert len(regions) == 2
    assert any(item["id"] == original_id and item["material_id"] == "material-copper" for item in regions)
    shared_edges = set(regions[0]["edge_ids"]).intersection(regions[1]["edge_ids"])
    assert len(shared_edges) == 1, {
        "regions": [(item["vertices"], item["edge_ids"], item["shape"]["boundary"]) for item in regions],
        "edges": [(item["id"], item["vertices"], item.get("sketch_entity_id")) for item in workspace.model["geometry"]["edges"]],
    }
    shared_edge = next(item for item in workspace.model["geometry"]["edges"] if item["id"] in shared_edges)
    assert {tuple(round(value, 12) for value in point) for point in shared_edge["vertices"]} == {(4.0, 0.0), (3.0, 1.5)}


def test_intersecting_sketch_lines_keep_open_segments_after_triangle_closes(standalone_components):
    workspace = SolveWorkspace()
    for first, second in (
        ((1, 0), (3, 0)),       # west to east
        ((2, -1), (2, 1)),      # south to north
        ((2, 1), (3, 0)),       # close the north-east triangle
    ):
        assert workspace._add_sketch_entity("line", [first, second])

    regions = [
        item for item in workspace.model["geometry"]["regions"]
        if item.get("shape", {}).get("type") == "sketch"
    ]
    assert len(regions) == 1

    def segment_key(edge):
        return frozenset(tuple(round(float(value), 12) for value in point) for point in edge["vertices"])

    segments = {segment_key(edge) for edge in workspace.model["geometry"]["edges"]}
    expected = {
        frozenset({(1.0, 0.0), (2.0, 0.0)}),    # west branch
        frozenset({(2.0, 0.0), (3.0, 0.0)}),    # east side of triangle
        frozenset({(2.0, -1.0), (2.0, 0.0)}),   # south branch
        frozenset({(2.0, 0.0), (2.0, 1.0)}),    # north side of triangle
        frozenset({(2.0, 1.0), (3.0, 0.0)}),    # closing diagonal
    }
    assert segments == expected
    assert len(workspace.model["geometry"]["sketch_entities"]) == 3

    west_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if segment_key(edge) == frozenset({(1.0, 0.0), (2.0, 0.0)})
    )
    project, _ = workspace._canvas_projection()
    assert workspace._trim_sketch_edge_at_canvas_point(west_edge["id"], project((1.5, 0.0)))
    trimmed_segments = {
        segment_key(edge) for edge in workspace.model["geometry"]["edges"]
    }
    assert frozenset({(1.0, 0.0), (2.0, 0.0)}) not in trimmed_segments
    assert frozenset({(2.0, -1.0), (2.0, 0.0)}) in trimmed_segments
    assert len([
        item for item in workspace.model["geometry"]["regions"]
        if item.get("shape", {}).get("type") == "sketch"
    ]) == 1


def test_sketch_endpoints_snap_to_nearby_edge_interiors(standalone_components):
    workspace = SolveWorkspace()
    triangle = [
        ((0.004, 0.0), (0.016, 0.0)),
        ((0.016, 0.0), (0.010, 0.010)),
        ((0.010, 0.010), (0.004, 0.0)),
    ]
    for first, second in triangle:
        assert workspace._add_sketch_entity("line", [first, second])
    assert len([item for item in workspace.model["geometry"]["regions"] if item.get("shape", {}).get("type") == "sketch"]) == 1

    project, unproject = workspace._canvas_projection()
    target = project((0.010, 0.0))
    near_edge = unproject((target[0], target[1] + 4.0))
    assert workspace._add_sketch_entity("line", [(0.010, 0.010), near_edge])
    regions = [item for item in workspace.model["geometry"]["regions"] if item.get("shape", {}).get("type") == "sketch"]
    assert len(regions) == 2


def test_nearby_arc_endpoint_snaps_exactly_and_closes_sketch_region(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    scale = 100_000.0
    project = lambda point: (point[0] * scale, point[1] * scale)
    unproject = lambda point: (point[0] / scale, point[1] / scale)
    monkeypatch.setattr(workspace, "_canvas_projection", lambda: (project, unproject))

    start, through, end = (0.006, 0.0), (0.010, 0.004), (0.014, 0.0)
    assert workspace._add_sketch_entity("arc", [start, through, end])
    assert workspace._add_sketch_entity("line", [start, (0.01399, 0.00001)])

    line = next(
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity["type"] == "line"
    )
    assert line["sampled_points"][-1] == pytest.approx(end, abs=1e-12)
    regions = [
        item for item in workspace.model["geometry"]["regions"]
        if item.get("shape", {}).get("type") == "sketch"
    ]
    assert len(regions) == 1


def test_sketch_inference_snaps_to_line_midpoint_and_arc_center(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    scale = 100_000.0
    project = lambda point: (point[0] * scale, point[1] * scale)
    unproject = lambda point: (point[0] / scale, point[1] / scale)
    monkeypatch.setattr(workspace, "_canvas_projection", lambda: (project, unproject))

    assert workspace._add_sketch_entity("line", [(0.005, 0.0), (0.015, 0.0)])
    line_midpoint = (0.010, 0.0)
    snapped, matched = workspace._snap_sketch_endpoint((0.01002, 0.00003), return_match=True)
    assert matched
    assert snapped == pytest.approx(line_midpoint, abs=1e-12)

    assert workspace._add_sketch_entity("arc", [(0.020, 0.0), (0.025, 0.006), (0.030, 0.0)])
    arc = next(item for item in workspace.model["geometry"]["sketch_entities"] if item["type"] == "arc")
    center = tuple(arc["center"])
    snapped, matched = workspace._snap_sketch_endpoint(
        (center[0] + 0.00002, center[1] - 0.00003),
        return_match=True,
    )
    assert matched
    assert snapped == pytest.approx(center, abs=1e-12)


def test_spline_has_no_center_or_midpoint_inference_target(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    scale = 100_000.0
    project = lambda point: (point[0] * scale, point[1] * scale)
    unproject = lambda point: (point[0] / scale, point[1] / scale)
    monkeypatch.setattr(workspace, "_canvas_projection", lambda: (project, unproject))
    spline = {
        "id": "spline-test",
        "type": "spline3",
        "points": [[0.005, 0.0], [0.010, 0.010], [0.015, 0.0]],
        "control_point": [0.010, 0.010],
        "sampled_points": [[0.005, 0.0], [0.0075, 0.003], [0.010, 0.004], [0.0125, 0.003], [0.015, 0.0]],
    }
    workspace.model["geometry"]["sketch_entities"] = [spline]
    workspace.model["geometry"]["edges"] = [{
        "id": "spline-edge",
        "vertices": [spline["sampled_points"][0], spline["sampled_points"][-1]],
        "curve": {"type": "spline3", "sampled_points": spline["sampled_points"]},
        "sketch_entity_id": "spline-test",
    }]

    target = (0.010, 0.0)
    snapped, matched = workspace._snap_sketch_endpoint(target, return_match=True)
    assert not matched
    assert snapped == pytest.approx(target, abs=1e-12)


def test_line_and_trim_preview_paths_are_separate_from_marquee(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    scripts = []
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: SimpleNamespace(eval=scripts.append)), raising=False)
    workspace._install_canvas_pointer_capture()
    script = scripts[0]
    preview = script.split("const updatePreview = (svg, point, screenPoint = null) => {", 1)[1].split("const beginGesture", 1)[0]
    assert "solve-sketch-drag-line" in script
    assert "tool === 'line'" in preview
    assert "tool === 'arc'" in preview
    assert "hide(rect); hide(circle); hide(spline); hide(line); hide(trim);" in preview
    assert "tool === 'trim'" in preview
    assert "gestureScreenPoints.map(toScenePoint)" in preview
    assert "line.setAttribute('d'," in preview
    assert "dragStart[0]" in preview and "point[0]" in preview
    assert "updatePreview(pending.svg, toScenePoint(screenPoint), screenPoint);" in script
    assert "document.addEventListener('pointermove'" in script
    assert "solve-sketch-drag-trim" in script
    assert "corner rectangle" not in preview



def test_click_line_chain_waits_until_finish_and_then_commits_all_segments(standalone_components):
    workspace = SolveWorkspace()
    workspace.set_sketch_tool("line", announce=False)

    workspace._accept_line_sketch_point((0.002, 0.001))
    workspace._accept_line_sketch_point((0.006, 0.001))
    workspace._accept_line_sketch_point((0.006, 0.004))
    assert workspace.model["geometry"]["sketch_entities"] == []

    assert workspace._finish_line_sketch_chain()
    entities = workspace.model["geometry"]["sketch_entities"]
    assert [item["points"] for item in entities] == [
        [[0.002, 0.001], [0.006, 0.001]],
        [[0.006, 0.001], [0.006, 0.004]],
    ]
    assert len(workspace.model["geometry"]["edges"]) == 2


@pytest.mark.parametrize("tool", ["rectangle", "circle", "line", "arc", "spline", "trim"])
def test_escape_from_any_sketch_tool_returns_to_select(standalone_components, tool):
    from types import SimpleNamespace

    workspace = SolveWorkspace()
    workspace.set_sketch_tool(tool, announce=False)
    workspace._canvas_drag = {"operation": "draw"}
    workspace._pending_circle_points = [(0.002, 0.001)]
    if tool == "arc":
        workspace._pending_arc_points = [(0.002, 0.001), (0.004, 0.001)]
    workspace._on_canvas_keydown(SimpleNamespace(value={"key": "Escape"}))

    assert workspace.sketch_tool == "select"
    assert workspace._canvas_drag is None
    assert workspace._pending_circle_points == []
    assert workspace._pending_arc_points == []


def test_global_escape_returns_to_select_in_solve_workspace(standalone_components):
    from types import SimpleNamespace

    app = NGSolveGui.__new__(NGSolveGui)
    workspace = SolveWorkspace()
    workspace.set_sketch_tool("rectangle", announce=False)
    app.kb = SimpleNamespace(_mode=None, _help_visible=False, on_escape=lambda: None)
    app._workspace_mode = "solve"
    app.solve_workspace = workspace

    app._handle_escape_shortcut()

    assert workspace.sketch_tool == "select"


def test_global_escape_closes_keyboard_overlay_before_changing_tool(standalone_components):
    from types import SimpleNamespace

    app = NGSolveGui.__new__(NGSolveGui)
    workspace = SolveWorkspace()
    workspace.set_sketch_tool("rectangle", announce=False)
    app.kb = SimpleNamespace(_mode="help", _help_visible=False, on_escape=lambda: None)
    app._workspace_mode = "solve"
    app.solve_workspace = workspace

    app._handle_escape_shortcut()

    assert workspace.sketch_tool == "rectangle"


def test_enter_finishes_line_chain_and_escape_returns_to_select(standalone_components):
    from types import SimpleNamespace

    workspace = SolveWorkspace()
    workspace.set_sketch_tool("line", announce=False)
    workspace._accept_line_sketch_point((0.002, 0.001))
    workspace._accept_line_sketch_point((0.006, 0.001))
    workspace._on_canvas_keydown(SimpleNamespace(value={"key": "Enter"}))
    assert len(workspace.model["geometry"]["edges"]) == 1
    assert workspace.sketch_tool == "line"

    workspace._accept_line_sketch_point((0.006, 0.001))
    workspace._accept_line_sketch_point((0.006, 0.004))
    workspace._on_canvas_keydown(SimpleNamespace(value=None, key="Escape"))
    assert len(workspace.model["geometry"]["edges"]) == 2
    assert workspace.sketch_tool == "select"


def test_arc_three_point_click_order_is_start_end_then_through(standalone_components):
    workspace = SolveWorkspace()
    workspace.set_sketch_tool("arc", announce=False, variant="three-point")
    start, end, through = (0.004, 0.002), (0.010, 0.002), (0.007, 0.005)

    assert workspace._accept_arc_sketch_point(start, variant="three-point")
    assert workspace._accept_arc_sketch_point(end, variant="three-point")
    assert workspace._pending_arc_points == [start, end]
    assert workspace._accept_arc_sketch_point(through, variant="three-point")

    entity = workspace.model["geometry"]["sketch_entities"][0]
    assert entity["points"][0] == list(start)
    assert entity["points"][1] == list(through)
    assert entity["points"][2] == list(end)
    assert math.dist(entity["center"], start) == pytest.approx(entity["radius"])
    assert math.dist(entity["center"], through) == pytest.approx(entity["radius"])
    assert math.dist(entity["center"], end) == pytest.approx(entity["radius"])


def test_spline3_arc_uses_start_end_then_control_and_preserves_exact_curve(standalone_components):
    workspace = SolveWorkspace()
    start, end, control = (0.004, 0.001), (0.010, 0.001), (0.007, 0.005)
    workspace.set_sketch_tool("arc", announce=False, variant="spline3")

    assert workspace._accept_arc_sketch_point(start, variant="spline3")
    assert workspace._accept_arc_sketch_point(end, variant="spline3")
    assert workspace._pending_arc_points == [start, end]
    assert workspace._accept_arc_sketch_point(control, variant="spline3")

    entity = workspace.model["geometry"]["sketch_entities"][-1]
    edge = next(item for item in workspace.model["geometry"]["edges"] if item.get("sketch_entity_id") == entity["id"])
    assert entity["type"] == "spline3"
    assert entity["points"] == [list(start), list(control), list(end)]
    assert entity["control_point"] == list(control)
    assert edge["curve"]["type"] == "spline3"
    assert edge["curve"]["control_point"] == list(control)
    assert edge["vertices"] == [list(start), list(end)]
    expected = SolveWorkspace._sample_spline3(start, control, end)
    assert entity["sampled_points"] == expected
    assert len(expected) == 65
    assert expected[0] == list(start)
    assert expected[-1] == list(end)


def test_escape_cancels_arc_without_committing_geometry(standalone_components):
    from types import SimpleNamespace

    workspace = SolveWorkspace()
    workspace.set_sketch_tool("arc", announce=False, variant="spline3")
    workspace._accept_arc_sketch_point((0.004, 0.001), variant="spline3")
    workspace._accept_arc_sketch_point((0.010, 0.001), variant="spline3")

    workspace._on_canvas_keydown(SimpleNamespace(value={"key": "Escape"}))

    assert workspace.sketch_tool == "select"
    assert workspace._pending_arc_points == []
    assert workspace.model["geometry"]["sketch_entities"] == []
    assert workspace.model["geometry"]["edges"] == []


@pytest.mark.parametrize(("clockwise", "expected_midpoint_sign"), [(False, 1), (True, -1)])
def test_centerpoint_arc_projects_end_to_start_radius_and_follows_direction(
    standalone_components, clockwise, expected_midpoint_sign
):
    workspace = SolveWorkspace()
    center, start, cursor = (0.006, 0.001), (0.010, 0.001), (0.006, 0.005)
    workspace._accept_arc_sketch_point(center, variant="centerpoint")
    workspace._accept_arc_sketch_point(start, variant="centerpoint")
    assert workspace._accept_arc_sketch_point(
        cursor, variant="centerpoint", centerpoint_clockwise=clockwise
    )

    entity = workspace.model["geometry"]["sketch_entities"][0]
    assert math.dist(center, start) == pytest.approx(math.dist(center, entity["points"][-1]))
    assert math.dist(center, start) == pytest.approx(entity["radius"])
    assert math.dist(start, entity["points"][-1]) > 0
    assert entity["clockwise"] is clockwise
    assert math.copysign(1, entity["points"][1][1] - center[1]) == expected_midpoint_sign


def test_tangent_arc_starts_at_endpoint_and_has_common_tangent(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", [(0.002, 0.002), (0.006, 0.002)])
    endpoint = (0.006, 0.002)
    workspace._accept_arc_sketch_point(endpoint, variant="tangent")
    assert workspace._pending_arc_points[0] == endpoint
    assert workspace._accept_arc_sketch_point((0.010, 0.006), variant="tangent")

    arc = next(item for item in workspace.model["geometry"]["sketch_entities"] if item["type"] == "arc")
    edge = next(item for item in workspace.model["geometry"]["edges"] if item.get("sketch_entity_id") == arc["id"])
    tangent = (1.0, 0.0)
    radius_vector = (arc["center"][0] - endpoint[0], arc["center"][1] - endpoint[1])
    assert radius_vector[0] * tangent[0] + radius_vector[1] * tangent[1] == pytest.approx(0.0, abs=1e-10)
    assert math.dist(arc["center"], endpoint) == pytest.approx(arc["radius"])
    assert edge["vertices"][0] == list(endpoint)


@pytest.mark.parametrize(
    ("line_points", "endpoint", "expected_tangent"),
    [
        ([(0.004, 0.002), (0.008, 0.002)], (0.004, 0.002), (-1.0, 0.0)),
        ([(0.004, 0.002), (0.008, 0.002)], (0.008, 0.002), (1.0, 0.0)),
        ([(0.006, 0.004), (0.006, 0.008)], (0.006, 0.004), (0.0, -1.0)),
        ([(0.006, 0.004), (0.006, 0.008)], (0.006, 0.008), (0.0, 1.0)),
    ],
)
def test_tangent_arc_uses_outward_direction_at_both_curve_endpoints(
    standalone_components, line_points, endpoint, expected_tangent
):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", line_points)

    tangent_start = workspace._tangent_at_sketch_endpoint(endpoint)

    assert tangent_start is not None
    assert tangent_start[0] == pytest.approx(endpoint)
    assert tangent_start[1] == pytest.approx(expected_tangent)





def test_tangent_arc_prefers_clicked_edge_and_uses_exact_outward_tangent(standalone_components):
    workspace = SolveWorkspace()
    shared = (0.004, 0.002)
    assert workspace._add_sketch_entity("line", [shared, (0.009, 0.002)])
    horizontal_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge.get("sketch_entity_id") == workspace.model["geometry"]["sketch_entities"][0]["id"]
    )
    assert workspace._add_sketch_entity("line", [shared, (0.004, 0.009)])
    vertical_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge.get("sketch_entity_id") == workspace.model["geometry"]["sketch_entities"][1]["id"]
    )

    horizontal = workspace._tangent_at_sketch_endpoint(
        shared, preferred_edge_id=horizontal_edge["id"]
    )
    vertical = workspace._tangent_at_sketch_endpoint(
        shared, preferred_edge_id=vertical_edge["id"]
    )

    assert horizontal[0] == pytest.approx(shared)
    assert horizontal[1] == pytest.approx((-1.0, 0.0))
    assert vertical[0] == pytest.approx(shared)
    assert vertical[1] == pytest.approx((0.0, -1.0))


def test_tangent_arc_from_first_endpoint_uses_short_tangent_sweep(standalone_components):
    workspace = SolveWorkspace()
    start = (0.008, 0.002)
    assert workspace._add_sketch_entity("line", [(0.008, 0.002), (0.014, 0.002)])
    assert workspace._accept_arc_sketch_point(start, variant="tangent")
    assert workspace._accept_arc_sketch_point((0.006, 0.0005), variant="tangent")

    arc = next(item for item in workspace.model["geometry"]["sketch_entities"] if item["type"] == "arc")
    samples = arc["sampled_points"]
    derivative = (samples[1][0] - samples[0][0], samples[1][1] - samples[0][1])
    tangent_dot = derivative[0] * -1.0 + derivative[1] * 0.0
    start_angle = math.atan2(start[1] - arc["center"][1], start[0] - arc["center"][0])
    end = arc["points"][-1]
    end_angle = math.atan2(end[1] - arc["center"][1], end[0] - arc["center"][0])
    positive_sweep = (end_angle - start_angle) % (2 * math.pi)
    sweep = positive_sweep - 2 * math.pi if arc["clockwise"] else positive_sweep

    assert tangent_dot > 0
    assert abs(sweep) < math.pi


def test_selected_sketch_region_does_not_show_a_false_bounding_box(standalone_components):
    workspace = SolveWorkspace()
    start, through, end = (0.006, 0.003), (0.009, 0.006), (0.012, 0.003)
    assert workspace._add_sketch_entity("line", [start, end])
    assert workspace._add_sketch_entity("arc", [start, through, end])
    region = next(
        item for item in workspace.model["geometry"]["regions"]
        if item.get("shape", {}).get("type") == "sketch"
    )

    # The curved edge and its filled region contour must be present on the
    # first render after the arc closes the sketch, without requiring a click.
    arc_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge.get("curve", {}).get("type") == "arc"
    )
    visible_edge, _hit_edge = workspace._canvas_edge_nodes[arc_edge["id"]]
    assert visible_edge in workspace._canvas_scene.ui_children
    assert visible_edge._props["d"].count("L") >= 20
    contour = workspace._canvas_region_path_data(
        region, project=lambda point: (point[0] * 1e6, point[1] * 1e6)
    )
    assert contour.count("L") >= 20

    workspace.render_canvas()

    props = workspace._canvas_region_nodes[region["id"]]._props
    assert "outline:none" in props["style"]
    assert "tabindex" not in props


@pytest.mark.parametrize(
    ("curve_type", "curve_points"),
    [
        ("arc", [(0.006, 0.003), (0.009, 0.006), (0.012, 0.003)]),
        ("spline3", [(0.006, 0.003), (0.009, 0.006), (0.012, 0.003)]),
        ("spline", [(0.006, 0.003), (0.008, 0.006), (0.010, 0.006), (0.012, 0.003)]),
    ],
)
def test_shared_endpoints_do_not_merge_a_chord_with_a_curve(
    standalone_components, curve_type, curve_points
):
    workspace = SolveWorkspace()
    start, end = curve_points[0], curve_points[-1]

    assert workspace._add_sketch_entity("line", [start, end])
    assert workspace._add_sketch_entity(curve_type, curve_points)

    edges = workspace.model["geometry"]["edges"]
    line_edge = next(edge for edge in edges if edge.get("curve", {}).get("type") == "line")
    curve_edge = next(edge for edge in edges if edge.get("curve", {}).get("type") == curve_type)

    assert line_edge["id"] != curve_edge["id"]
    assert len(curve_edge["curve"]["sampled_points"]) >= 3

    errors = validate_model(workspace.model, scope="mesh")
    assert not any("duplicates another edge" in error for error in errors), errors


def test_centerpoint_arc_and_chord_are_distinct_mesh_edges(standalone_components):
    workspace = SolveWorkspace()
    start, end = (0.006, 0.003), (0.012, 0.003)
    center = (0.009, 0.003)
    assert workspace._add_sketch_entity("line", [start, end])
    assert workspace._accept_arc_sketch_point(center, variant="centerpoint")
    assert workspace._accept_arc_sketch_point(start, variant="centerpoint")
    assert workspace._accept_arc_sketch_point(end, variant="centerpoint")

    errors = validate_model(workspace.model, scope="mesh")
    assert not any("duplicates another edge" in error for error in errors), errors
    assert any(
        edge.get("curve", {}).get("type") == "arc"
        for edge in workspace.model["geometry"]["edges"]
    )


def test_validator_still_detects_identical_arc_edges(standalone_components):
    workspace = SolveWorkspace()
    start, through, end = (0.006, 0.003), (0.009, 0.006), (0.012, 0.003)
    assert workspace._add_sketch_entity("line", [start, end])
    assert workspace._add_sketch_entity("arc", [start, through, end])
    arc_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge.get("curve", {}).get("type") == "arc"
    )
    duplicate = copy.deepcopy(arc_edge)
    duplicate["id"] = "duplicate-arc-edge"
    duplicate["name"] = "Duplicate arc edge"
    workspace.model["geometry"]["edges"].append(duplicate)

    errors = validate_model(workspace.model, scope="mesh")
    assert any("Duplicate arc edge" in error and "duplicates another edge" in error for error in errors), errors


def test_selected_arc_radius_is_editable_and_preserves_endpoints(standalone_components):
    workspace = SolveWorkspace()
    for point in ((0.004, 0.002), (0.010, 0.002), (0.007, 0.005)):
        workspace._accept_arc_sketch_point(point, variant="three-point")
    edge = next(item for item in workspace.model["geometry"]["edges"] if item.get("curve", {}).get("type") == "arc")
    edge_id = edge["id"]
    endpoints = copy.deepcopy(edge["vertices"])
    workspace.selected_edge_id = edge_id
    workspace.selected_edge_ids = [edge_id]

    assert workspace._set_selected_arc_radius(0.004)
    updated = next(item for item in workspace.model["geometry"]["edges"] if item.get("curve", {}).get("type") == "arc")
    assert updated["vertices"] == endpoints
    assert updated["curve"]["radius"] == pytest.approx(0.004)
    assert workspace._set_selected_arc_radius(0.001) is False


def test_live_preview_supports_arc_construction_and_point_markers(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    scripts = []
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: SimpleNamespace(eval=scripts.append)), raising=False)
    workspace._install_canvas_pointer_capture()
    script = scripts[0]
    assert "arcPathThroughThreePoints" in script
    assert "arcPathFromCenter" in script
    assert "centerArcTrace" in script
    assert "centerArcTrace.travel" in script
    assert "__ngsolveSketchCenterArcClockwise" in script
    assert "Boolean(window.__ngsolveSketchCenterArcClockwise)" in script
    assert "spline3Path" in script
    assert "gestureSessionId + \":\" + String(finishedGesture)" in script
    assert "arcPathTangent" in script
    assert "const cancelArc" in script
    assert "solve-sketch-construction-points" in script
    assert "window.__ngsolveRefreshConstructionPreview" in script
    assert "hasPending" in script and "updateConstructionPreview" in script

def test_power_trim_leaves_curves_without_intersections_unchanged(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", [(0.002, 0.0), (0.008, 0.0)])
    assert workspace._add_sketch_entity("line", [(0.020, 0.004), (0.026, 0.004)])

    assert not workspace._trim_sketch_entities_along_stroke([(0.005, -0.001), (0.005, 0.001)])
    remaining = workspace.model["geometry"]["sketch_entities"]
    assert len(remaining) == 2
    assert remaining[0]["points"] == [[0.002, 0.0], [0.008, 0.0]]
    assert remaining[1]["points"] == [[0.02, 0.004], [0.026, 0.004]]


def test_power_trim_click_supports_arc_segments(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("arc", [(0.004, 0.0), (0.008, 0.004), (0.012, 0.0)])
    arc = workspace.model["geometry"]["sketch_entities"][0]
    assert workspace._add_sketch_entity("line", [(0.003, 0.002), (0.013, 0.002)])

    edge = next(edge for edge in workspace.model["geometry"]["edges"]
                if edge.get("sketch_entity_id") == arc["id"])
    project, _ = workspace._canvas_projection()
    assert workspace._trim_sketch_edge_at_canvas_point(edge["id"], project((0.008, 0.004)))

    arc_fragments = [
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == "arc"
    ]
    assert len(arc_fragments) == 2
    assert all(len(entity["sampled_points"]) >= 2 for entity in arc_fragments)
    assert any(entity.get("type") == "line" for entity in workspace.model["geometry"]["sketch_entities"])


def test_power_trim_can_cut_a_primitive_rectangle_edge(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.003,
        ("rectangle", "z_min"): 0.003,
        ("rectangle", "width"): 0.010,
        ("rectangle", "height"): 0.010,
    })
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    edge = next(edge for edge in workspace.model["geometry"]["edges"]
                if edge["id"] == region["edge_ids"][0])
    first, second = edge["vertices"]
    project, _ = workspace._canvas_projection()
    midpoint = project(((first[0] + second[0]) / 2, (first[1] + second[1]) / 2))

    assert workspace._trim_sketch_edge_at_canvas_point(edge["id"], midpoint)
    assert workspace.model["geometry"]["regions"] == []
    assert len(workspace.model["geometry"]["sketch_entities"]) == 3


def test_trimming_circle_outside_rectangle_does_not_restore_duplicate_edges(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.00432,
        ("rectangle", "z_min"): -0.01092,
        ("rectangle", "width"): 0.02195,
        ("rectangle", "height"): 0.02175,
    })
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("circle", "r_center"): 0.02537,
        ("circle", "z_center"): 0.0,
        ("circle", "radius"): 0.00836,
    })
    workspace._add_primitive("circle")
    geometry = workspace.model["geometry"]
    assert len(geometry["regions"]) == 3

    # Power-trim the outer circular cap beyond the rectangle's right edge.
    assert workspace._trim_sketch_entities_along_stroke(
        [(0.033, -0.005), (0.033, 0.005)]
    )

    geometry = workspace.model["geometry"]
    assert len(geometry["regions"]) == 2
    assert all(region.get("parent_id") is None for region in geometry["regions"])
    assert all(
        loop.get("role") == "outer"
        for region in geometry["regions"]
        for loop in region.get("boundary_loops", [])
    )
    edge_ids = {edge["id"] for edge in geometry["edges"]}
    referenced_edge_ids = {
        edge_id
        for region in geometry["regions"]
        for edge_id in region.get("edge_ids", [])
    }
    errors = validate_model(workspace.model, scope="mesh")
    assert not any("Unattached edge" in error for error in errors)
    assert edge_ids == referenced_edge_ids

    intervals_by_entity = {}
    for edge in geometry["edges"]:
        entity_id = edge.get("sketch_entity_id")
        if not entity_id:
            continue
        start = min(float(edge.get("sketch_t0", 0.0)), float(edge.get("sketch_t1", 1.0)))
        end = max(float(edge.get("sketch_t0", 0.0)), float(edge.get("sketch_t1", 1.0)))
        intervals_by_entity.setdefault(entity_id, []).append((start, end))
    for intervals in intervals_by_entity.values():
        intervals.sort()
        for previous, current in zip(intervals, intervals[1:]):
            assert previous[1] <= current[0] + 1e-10

    circle_entities = [
        entity for entity in geometry["sketch_entities"]
        if str(entity.get("curve_group_id", "")).startswith("circle:")
    ]
    rectangle_right = 0.00432 + 0.02195
    outside_circle_points = [
        point
        for entity in circle_entities
        for point in entity.get("sampled_points", [])
        if point[0] > rectangle_right + 1e-8
    ]
    assert circle_entities
    assert not outside_circle_points, (
        "Power Trim should remove the whole circle arc outside the rectangle; "
        f"remaining outside points: {outside_circle_points[:5]}"
    )



def test_trimming_large_circle_exterior_removes_the_full_arc_between_rectangle_intersections(standalone_components):
    workspace = SolveWorkspace()
    rect = {"r_min": 0.00351, "z_min": -0.00092, "width": 0.00395, "height": 0.00723}
    workspace._primitive_values.update({("rectangle", key): value for key, value in rect.items()})
    workspace._add_primitive("rectangle")
    circle = {"r_center": 0.0111, "z_center": 0.0036, "radius": 0.0065}
    workspace._primitive_values.update({("circle", key): value for key, value in circle.items()})
    workspace._add_primitive("circle")

    geometry = workspace.model["geometry"]
    circle_group = [
        entity for entity in geometry["sketch_entities"]
        if str(entity.get("curve_group_id", "")).startswith("circle:")
    ]
    assert len(circle_group) == 4
    rectangle_right = rect["r_min"] + rect["width"]
    rectangle_top = rect["z_min"] + rect["height"]
    assert any(
        point[0] > rectangle_right + 1e-8 or point[1] < rect["z_min"] - 1e-8
        or point[1] > rectangle_top + 1e-8
        for entity in circle_group for point in entity.get("sampled_points", [])
    )

    # Start inside the large circle and drag out through its far-side arc,
    # matching the viewport gesture without an edge preselection.
    assert workspace._trim_sketch_entities_along_stroke([
        (circle["r_center"], circle["z_center"]),
        (circle["r_center"] + circle["radius"] + 0.0005, circle["z_center"]),
    ])

    geometry = workspace.model["geometry"]
    remaining_circle = [
        entity for entity in geometry["sketch_entities"]
        if str(entity.get("curve_group_id", "")).startswith("circle:")
    ]
    assert remaining_circle
    assert len(geometry["regions"]) == 2
    assert all(
        rect["r_min"] - 1e-8 <= point[0] <= rectangle_right + 1e-8
        and rect["z_min"] - 1e-8 <= point[1] <= rectangle_top + 1e-8
        for entity in remaining_circle for point in entity.get("sampled_points", [])
    ), "The trimmed circle must retain only the arc inside the rectangle."

    edge_ids = [edge.get("id") for edge in geometry["edges"]]
    assert len(edge_ids) == len(set(edge_ids))
    assert set(edge_ids) == {
        edge_id for region in geometry["regions"] for edge_id in region.get("edge_ids", [])
    }


def test_trimming_circle_cap_when_drag_starts_on_edge_removes_whole_cap(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.00432,
        ("rectangle", "z_min"): -0.01092,
        ("rectangle", "width"): 0.02195,
        ("rectangle", "height"): 0.02175,
    })
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("circle", "r_center"): 0.02537,
        ("circle", "z_center"): 0.0,
        ("circle", "radius"): 0.00836,
    })
    workspace._add_primitive("circle")
    geometry = workspace.model["geometry"]
    circle_right = 0.02537 + 0.00836
    rightmost = (circle_right, 0.0)
    edge_distance = lambda edge: min(
        math.dist(tuple(point[:2]), rightmost)
        for point in (
            (edge.get("curve") or {}).get("sampled_points")
            or edge.get("vertices", [])
        )
    )
    target_edge = min(geometry["edges"], key=edge_distance)
    assert edge_distance(target_edge) < 1e-4

    rectangle_right = 0.00432 + 0.02195
    assert workspace._trim_sketch_entities_along_stroke(
        [(0.033, -0.005), (0.033, 0.005)],
        target_edge_id=target_edge["id"],
    )

    remaining_circle = [
        entity for entity in geometry["sketch_entities"]
        if str(entity.get("curve_group_id", "")).startswith("circle:")
    ]
    assert remaining_circle
    assert all(
        point[0] <= rectangle_right + 1e-8
        for entity in remaining_circle
        for point in entity.get("sampled_points", [])
    )


@pytest.mark.parametrize("use_click", [False, True])
@pytest.mark.parametrize("entity_type", ["arc", "spline3"])
def test_trimming_curve_cap_outside_rectangle_removes_full_arc_interval(
    standalone_components, entity_type, use_click
):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.005,
        ("rectangle", "z_min"): -0.015,
        ("rectangle", "width"): 0.021,
        ("rectangle", "height"): 0.030,
    })
    workspace._add_primitive("rectangle")
    rectangle_region = workspace.model["geometry"]["regions"][0]
    workspace._materialize_primitive_as_sketch(rectangle_region)
    workspace._refresh_sketch_generated_regions()
    start = (0.018, -0.010)
    end = (0.018, 0.010)
    if entity_type == "arc":
        workspace.set_sketch_tool("arc", announce=False, variant="three-point")
        assert workspace._accept_arc_sketch_point(start, variant="three-point")
        assert workspace._accept_arc_sketch_point(end, variant="three-point")
        assert workspace._accept_arc_sketch_point((0.028, 0.0), variant="three-point")
    else:
        assert workspace._add_sketch_entity("spline3", [start, (0.25, 0.0), end])
    rectangle_right = 0.026
    assert max(
        point[0]
        for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == entity_type
        for point in entity.get("sampled_points", [])
    ) > rectangle_right

    if use_click:
        curve_entity = next(
            entity for entity in workspace.model["geometry"]["sketch_entities"]
            if entity.get("type") == entity_type
        )
        candidate_edges = [
            edge for edge in workspace.model["geometry"]["edges"]
            if edge.get("sketch_entity_id") == curve_entity["id"]
        ]
        target_edge = max(
            candidate_edges,
            key=lambda edge: max(
                point[0]
                for point in (
                    (edge.get("curve") or {}).get("sampled_points")
                    or edge.get("vertices", [])
                )
            ),
        )
        samples = (
            (target_edge.get("curve") or {}).get("sampled_points")
            or target_edge.get("vertices", [])
        )
        trim_point = max(samples, key=lambda point: point[0])
        assert trim_point[0] > rectangle_right
        project, _ = workspace._canvas_projection()
        assert workspace._trim_sketch_edge_at_canvas_point(
            target_edge["id"], project(trim_point)
        )
    else:
        assert workspace._trim_sketch_entities_along_stroke(
            [(0.027, -0.009), (0.027, 0.009)]
        )
    remaining = [
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == entity_type
    ]
    assert remaining
    assert all(
        point[0] <= rectangle_right + 1e-8
        for entity in remaining
        for point in entity.get("sampled_points", [])
    )


def test_click_trim_circle_cap_removes_whole_outside_arc(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.00432,
        ("rectangle", "z_min"): -0.01092,
        ("rectangle", "width"): 0.02195,
        ("rectangle", "height"): 0.02175,
    })
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("circle", "r_center"): 0.02537,
        ("circle", "z_center"): 0.0,
        ("circle", "radius"): 0.00836,
    })
    workspace._add_primitive("circle")
    geometry = workspace.model["geometry"]
    circle_right = 0.02537 + 0.00836
    rightmost = (circle_right, 0.0)
    edge_distance = lambda edge: min(
        math.dist(tuple(point[:2]), rightmost)
        for point in (
            (edge.get("curve") or {}).get("sampled_points")
            or edge.get("vertices", [])
        )
    )
    target_edge = min(geometry["edges"], key=edge_distance)
    assert edge_distance(target_edge) < 1e-4

    project, _ = workspace._canvas_projection()
    assert workspace._trim_sketch_edge_at_canvas_point(
        target_edge["id"], project(rightmost)
    )
    rectangle_right = 0.00432 + 0.02195
    remaining_circle = [
        entity for entity in geometry["sketch_entities"]
        if str(entity.get("curve_group_id", "")).startswith("circle:")
    ]
    assert remaining_circle
    assert all(
        point[0] <= rectangle_right + 1e-8
        for entity in remaining_circle
        for point in entity.get("sampled_points", [])
    )


def test_trimming_a_short_closing_edge_does_not_refill_an_open_spline_contour(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", [(0.003, 0.0), (0.010, 0.0)])
    assert workspace._add_sketch_entity("line", [(0.006, 0.0), (0.006, 0.002)])
    assert workspace._add_sketch_entity("spline3", [(0.006, 0.002), (0.00826, 0.00152), (0.010, 0.0)])
    assert len(workspace.model["geometry"]["regions"]) == 1

    vertical = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if abs(edge["vertices"][0][0] - 0.006) < 1e-10
        and abs(edge["vertices"][1][0] - 0.006) < 1e-10
        and abs(edge["vertices"][0][1] - edge["vertices"][1][1]) > 1e-10
    )
    project, _ = workspace._canvas_projection()
    assert workspace._trim_sketch_edge_at_canvas_point(vertical["id"], project((0.006, 0.001)))

    assert workspace.model["geometry"]["regions"] == []
    assert {entity["type"] for entity in workspace.model["geometry"]["sketch_entities"]} == {"line", "spline3"}


def test_trimming_a_spline_overhang_preserves_its_attached_region(standalone_components):
    workspace = SolveWorkspace()
    base = [(0.01838, -0.00175), (0.02052, -0.00175)]
    spline = [(0.01625, -0.00175), (0.01773, -0.00047), (0.02052, -0.00175)]
    vertical = [(0.01838, -0.0011792779896355491), (0.01838, -0.00175)]
    geometry = workspace.model["geometry"]
    geometry["sketch_entities"] = [
        {"id": "base", "type": "line", "points": base, "sampled_points": base},
        {
            "id": "spline", "type": "spline3", "points": spline,
            "control_point": spline[1], "sampled_points": workspace._sample_spline3(*spline),
        },
        {"id": "vertical", "type": "line", "points": vertical, "sampled_points": vertical},
    ]
    workspace._refresh_sketch_generated_regions()
    assert len(geometry["regions"]) == 1

    assert workspace._trim_sketch_entities_along_stroke(
        [(0.017, -0.0005), (0.017, -0.0025)]
    )
    assert len(geometry["regions"]) == 1
    remaining_spline = next(entity for entity in geometry["sketch_entities"] if entity["type"] in {"spline", "spline3"})
    assert min(point[0] for point in remaining_spline["sampled_points"]) >= 0.01838 - 1e-8


def test_trimming_a_spline_region_boundary_removes_its_curve_edge(standalone_components):
    workspace = SolveWorkspace()
    base = [(0.01838, -0.00175), (0.02052, -0.00175)]
    spline = [(0.01625, -0.00175), (0.01773, -0.00047), (0.02052, -0.00175)]
    vertical = [(0.01838, -0.0011792779896355491), (0.01838, -0.00175)]
    geometry = workspace.model["geometry"]
    geometry["sketch_entities"] = [
        {"id": "base", "type": "line", "points": base, "sampled_points": base},
        {
            "id": "spline", "type": "spline3", "points": spline,
            "control_point": spline[1], "sampled_points": workspace._sample_spline3(*spline),
        },
        {"id": "vertical", "type": "line", "points": vertical, "sampled_points": vertical},
    ]
    workspace._refresh_sketch_generated_regions()
    assert len(geometry["regions"]) == 1

    assert workspace._trim_sketch_entities_along_stroke(
        [(0.0193, -0.0005), (0.0193, -0.0015)]
    )
    assert geometry["regions"] == []
    remaining_spline = next(
        entity for entity in geometry["sketch_entities"]
        if entity["type"] in {"spline", "spline3"}
    )
    assert max(point[0] for point in remaining_spline["sampled_points"]) <= 0.01838 + 1e-8
    assert not any(
        edge.get("sketch_entity_id") == remaining_spline["id"]
        and max(point[0] for point in edge.get("vertices", [])) > 0.01838 + 1e-8
        for edge in geometry["edges"]
    )


def test_trimming_the_overhang_preserves_the_curved_region(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", [(0.003, 0.0), (0.010, 0.0)])
    assert workspace._add_sketch_entity("line", [(0.006, 0.0), (0.006, 0.002)])
    assert workspace._add_sketch_entity("arc", [(0.006, 0.002), (0.00826, 0.00152), (0.010, 0.0)])
    assert len(workspace.model["geometry"]["regions"]) == 1

    overhang = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if all(abs(point[1]) < 1e-10 for point in edge["vertices"])
        and max(point[0] for point in edge["vertices"]) <= 0.006 + 1e-10
        and min(point[0] for point in edge["vertices"]) < 0.006 - 1e-10
    )
    project, _ = workspace._canvas_projection()
    assert workspace._trim_sketch_edge_at_canvas_point(overhang["id"], project((0.004, 0.0)))

    assert len(workspace.model["geometry"]["regions"]) == 1
    assert any(entity["type"] == "arc" for entity in workspace.model["geometry"]["sketch_entities"])
    assert any(
        entity["type"] == "line"
        and all(abs(point[1]) < 1e-10 for point in entity["points"])
        and min(point[0] for point in entity["points"]) >= 0.006 - 1e-10
        for entity in workspace.model["geometry"]["sketch_entities"]
    )


def test_trimming_a_short_closing_edge_does_not_refill_an_open_arc_contour(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", [(0.003, 0.0), (0.010, 0.0)])
    assert workspace._add_sketch_entity("line", [(0.006, 0.0), (0.006, 0.002)])
    assert workspace._add_sketch_entity("arc", [(0.006, 0.002), (0.00826, 0.00152), (0.010, 0.0)])
    assert len(workspace.model["geometry"]["regions"]) == 1

    vertical = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if abs(edge["vertices"][0][0] - 0.006) < 1e-10
        and abs(edge["vertices"][1][0] - 0.006) < 1e-10
        and abs(edge["vertices"][0][1] - edge["vertices"][1][1]) > 1e-10
    )
    project, _ = workspace._canvas_projection()
    assert workspace._trim_sketch_edge_at_canvas_point(vertical["id"], project((0.006, 0.001)))

    assert workspace.model["geometry"]["regions"] == []
    assert {entity["type"] for entity in workspace.model["geometry"]["sketch_entities"]} == {"line", "arc"}


def test_trimming_a_curved_boundary_removes_its_edge_with_the_open_face(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", [(0.003, 0.0), (0.010, 0.0)])
    assert workspace._add_sketch_entity("line", [(0.006, 0.0), (0.006, 0.002)])
    assert workspace._add_sketch_entity("arc", [(0.006, 0.002), (0.00826, 0.00152), (0.010, 0.0)])
    arc = next(entity for entity in workspace.model["geometry"]["sketch_entities"] if entity["type"] == "arc")
    arc_edge = next(edge for edge in workspace.model["geometry"]["edges"] if edge.get("sketch_entity_id") == arc["id"])
    project, _ = workspace._canvas_projection()

    assert workspace._trim_sketch_edge_at_canvas_point(arc_edge["id"], project((0.00826, 0.00152)))

    assert workspace.model["geometry"]["regions"] == []
    assert not any(entity["type"] == "arc" for entity in workspace.model["geometry"]["sketch_entities"])
    assert not any(edge.get("curve", {}).get("type") == "arc" for edge in workspace.model["geometry"]["edges"])


def test_trimming_the_region_base_keeps_the_curved_boundary_and_open_edges(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", [(0.003, 0.0), (0.010, 0.0)])
    assert workspace._add_sketch_entity("line", [(0.006, 0.0), (0.006, 0.002)])
    assert workspace._add_sketch_entity("arc", [(0.006, 0.002), (0.00826, 0.00152), (0.010, 0.0)])
    base = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if all(abs(point[1]) < 1e-10 for point in edge["vertices"])
        and {round(point[0], 6) for point in edge["vertices"]} == {0.006, 0.01}
    )
    project, _ = workspace._canvas_projection()

    assert workspace._trim_sketch_edge_at_canvas_point(base["id"], project((0.008, 0.0)))

    assert workspace.model["geometry"]["regions"] == []
    assert any(entity["type"] == "arc" for entity in workspace.model["geometry"]["sketch_entities"])
    assert any(
        entity["type"] == "line"
        and all(abs(point[0] - 0.006) < 1e-10 for point in entity["points"])
        for entity in workspace.model["geometry"]["sketch_entities"]
    )


def test_trim_stroke_does_not_remove_a_nearby_base_while_trimming_spline(standalone_components):
    workspace = SolveWorkspace()
    base = [(0.01838, -0.00175), (0.02052, -0.00175)]
    spline = [(0.01625, -0.00175), (0.01773, -0.00047), (0.02052, -0.00175)]
    vertical = [(0.01838, -0.0011792779896355491), (0.01838, -0.00175)]
    geometry = workspace.model["geometry"]
    geometry["sketch_entities"] = [
        {"id": "base", "type": "line", "points": base, "sampled_points": base},
        {
            "id": "spline", "type": "spline3", "points": spline,
            "control_point": spline[1], "sampled_points": workspace._sample_spline3(*spline),
        },
        {"id": "vertical", "type": "line", "points": vertical, "sampled_points": vertical},
    ]
    workspace._refresh_sketch_generated_regions()
    assert len(geometry["regions"]) == 1

    curve_samples = workspace._sample_spline3(*spline)
    curve_point = curve_samples[round(0.8 * (len(curve_samples) - 1))]
    project, unproject = workspace._canvas_projection()
    curve_canvas = project(curve_point)
    base_canvas = project((curve_point[0], base[0][1]))
    assert base_canvas[1] - curve_canvas[1] > 6.0

    # Cross the curved boundary but end only three screen pixels from the
    # neighboring base. The base is close, but the trim stroke does not touch it.
    stroke_canvas = [
        (curve_canvas[0], curve_canvas[1] - 3.0),
        (curve_canvas[0], base_canvas[1] - 3.0),
    ]
    assert workspace._trim_sketch_entities_along_stroke(
        [unproject(point) for point in stroke_canvas]
    )

    retained_base = next(
        entity for entity in geometry["sketch_entities"] if entity.get("id") == "base"
    )
    assert retained_base["points"] == base
    assert geometry["regions"] == []


def test_trim_stroke_removes_only_the_intersection_bounded_section(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", [(0.001, 0.0), (0.009, 0.0)])
    assert workspace._add_sketch_entity("line", [(0.003, -0.002), (0.003, 0.002)])
    assert workspace._add_sketch_entity("line", [(0.007, -0.002), (0.007, 0.002)])

    assert workspace._trim_sketch_entities_along_stroke([(0.005, -0.001), (0.005, 0.001)])
    horizontal = [
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == "line"
        and abs(entity["points"][0][1]) < 1e-10
        and abs(entity["points"][-1][1]) < 1e-10
    ]
    assert len(horizontal) == 2
    spans = sorted((entity["points"][0][0], entity["points"][-1][0]) for entity in horizontal)
    assert spans[0] == pytest.approx((0.001, 0.003))
    assert spans[1] == pytest.approx((0.007, 0.009))




def _make_trim_test_spline_divider(workspace):
    left = (0.004, 0.0018)
    divider_x = 0.0125
    right = (0.021, 0.0018)
    control = (divider_x, 0.007)
    curve_points = [left, control, right]
    curve_samples = workspace._sample_spline3(*curve_points)
    top = min(curve_samples, key=lambda point: abs(point[0] - divider_x))
    base = {"id": "trim-base", "type": "line", "points": [left, (divider_x, 0.0018)],
            "sampled_points": [left, (divider_x, 0.0018)]}
    curve = {"id": "trim-curve", "type": "spline3", "points": curve_points,
             "control_point": control, "sampled_points": curve_samples}
    divider = {"id": "trim-divider", "type": "line", "points": [top, (divider_x, 0.0018)],
               "sampled_points": [top, (divider_x, 0.0018)]}
    workspace.model["geometry"]["sketch_entities"] = [base, curve, divider]
    workspace._refresh_sketch_generated_regions()
    assert len(workspace.model["geometry"]["regions"]) == 1
    return base, curve, divider, top


def test_drag_trim_of_curved_region_edge_removes_to_the_divider(standalone_components):
    workspace = SolveWorkspace()
    base, curve, divider, _ = _make_trim_test_spline_divider(workspace)
    edge = next(
        item for item in workspace.model["geometry"]["edges"]
        if item.get("sketch_entity_id") == curve["id"]
        and min(item.get("sketch_t0", 0.0), item.get("sketch_t1", 1.0)) <= 0.25
        <= max(item.get("sketch_t0", 0.0), item.get("sketch_t1", 1.0))
    )
    point = curve["sampled_points"][len(curve["sampled_points"]) // 4]
    stroke = [(point[0], point[1] + 0.0002), (point[0], point[1] - 0.0002)]

    assert workspace._trim_sketch_entities_along_stroke(stroke, target_edge_id=edge["id"])

    remaining_curve = next(
        item for item in workspace.model["geometry"]["sketch_entities"]
        if item.get("type") in {"spline", "spline3"}
    )
    assert min(point[0] for point in remaining_curve["sampled_points"]) >= divider["points"][0][0] - 1e-8
    assert next(item for item in workspace.model["geometry"]["sketch_entities"]
                if item["id"] == base["id"])["points"] == base["points"]
    assert next(item for item in workspace.model["geometry"]["sketch_entities"]
                if item["id"] == divider["id"])["points"] == divider["points"]


def test_drag_trim_started_on_divider_does_not_trim_other_edges(standalone_components):
    workspace = SolveWorkspace()
    base, curve, divider, top = _make_trim_test_spline_divider(workspace)
    edge = next(
        item for item in workspace.model["geometry"]["edges"]
        if item.get("sketch_entity_id") == divider["id"]
    )
    before_curve = copy.deepcopy(curve)
    before_base = copy.deepcopy(base)
    y = (top[1] + base["points"][1][1]) / 2
    stroke = [(0.003, y), (0.022, y)]

    assert workspace._trim_sketch_entities_along_stroke(stroke, target_edge_id=edge["id"])

    remaining = {item["id"]: item for item in workspace.model["geometry"]["sketch_entities"]}
    assert remaining[base["id"]]["points"] == before_base["points"]
    remaining_curve = next(item for item in remaining.values() if item["type"] == "spline3")
    assert remaining_curve["points"] == before_curve["points"]
    assert divider["id"] not in remaining



def test_drag_trim_forwards_the_edge_under_pointer_as_its_target(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._canvas_drag = {"operation": "trim"}
    workspace._commit_browser_canvas_view = lambda *args, **kwargs: False
    workspace._sketch_screen_points_to_world = lambda points, snap=False: [(0.0, 0.0), (1.0, 1.0)]
    captured = {}
    workspace._trim_sketch_entities_along_stroke = lambda stroke, **kwargs: captured.update(
        stroke=stroke, **kwargs
    ) or True
    completed = [
        "trim", "trim", 10, 10, 30, 10, "", False, True, 1, 0, 0, 1,
        "edge-under-pointer", [[10, 10], [30, 10]],
    ]
    fake_js = SimpleNamespace(eval=lambda _script: completed)
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: fake_js), raising=False)

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert captured["target_edge_id"] == "edge-under-pointer"
    assert captured["stroke"] == [(0.0, 0.0), (1.0, 1.0)]


def test_trim_uses_region_boundary_parameters_for_approximate_junctions(standalone_components):
    workspace = SolveWorkspace()
    base, curve, divider, top = _make_trim_test_spline_divider(workspace)
    # Keep the generated face's topology but perturb the divider endpoint by
    # less than a screen pixel. The region boundary parameter remains the
    # authoritative endpoint for the curved edge.
    divider["points"][0] = (top[0], top[1] - 1e-6)
    divider["sampled_points"] = list(divider["points"])
    point = curve["sampled_points"][len(curve["sampled_points"]) // 4]
    stroke = [(point[0], point[1] + 0.0002), (point[0], point[1] - 0.0002)]

    assert workspace._trim_sketch_entities_along_stroke(stroke)

    remaining_curve = next(
        item for item in workspace.model["geometry"]["sketch_entities"]
        if item.get("type") in {"spline", "spline3"}
    )
    assert min(point[0] for point in remaining_curve["sampled_points"]) >= top[0] - 1e-8


def test_trim_preserves_visible_line_fragment_with_stale_entity_reference(standalone_components):
    workspace = SolveWorkspace()
    geometry = workspace.model["geometry"]
    left, middle, right = (0.003, -0.00175), (0.005, -0.00175), (0.007, -0.00175)
    control, top = (0.005, -0.00047), (0.005, -0.00111)
    geometry["sketch_entities"] = [
        {"id": "bottom-right", "type": "line", "points": [middle, right], "sampled_points": [middle, right]},
        {
            "id": "outer-curve", "type": "spline3", "points": [left, control, right],
            "control_point": control, "sampled_points": workspace._sample_spline3(left, control, right),
        },
        {"id": "divider", "type": "line", "points": [top, middle], "sampled_points": [top, middle]},
    ]
    workspace._refresh_sketch_generated_regions()
    assert len(geometry["regions"]) == 1
    geometry["edges"].append({
        "id": "orphan-left-bottom", "name": "Bottom line fragment",
        "vertices": [left, middle], "curve": {"type": "line", "sampled_points": [left, middle]},
        "sketch_entity_id": "bottom-right", "sketch_t0": 0.0, "sketch_t1": 0.5,
        "point_ids": [], "boundary_condition_ids": [],
    })
    project, unproject = workspace._canvas_projection()
    hit = project((0.006, -0.00175))
    stroke = [unproject((hit[0], hit[1] - 8.0)), unproject((hit[0], hit[1] + 8.0))]

    assert workspace._trim_sketch_entities_along_stroke(stroke)

    horizontal = [
        entity for entity in geometry["sketch_entities"]
        if entity.get("type") == "line"
        and abs(entity["points"][0][1] - left[1]) <= 1e-10
        and abs(entity["points"][-1][1] - left[1]) <= 1e-10
    ]
    assert len(horizontal) == 1
    assert sorted(tuple(point) for point in horizontal[0]["points"]) == [left, middle]
    assert not any(item.get("id") == "bottom-right" for item in geometry["sketch_entities"])
    assert any(item.get("id") == "divider" for item in geometry["sketch_entities"])
    assert any(item.get("id") == "outer-curve" for item in geometry["sketch_entities"])
    assert geometry["regions"]
    assert any(
        edge.get("sketch_entity_id") == horizontal[0]["id"]
        and sorted(tuple(point) for point in edge["vertices"]) == [left, middle]
        for edge in geometry["edges"]
    )


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


def test_axis_of_symmetry_boundary_tracks_region_addition_and_movement(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values[("rectangle", "r_min")] = 0.0
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]

    axis_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"])
    )
    axis_condition = next(
        condition for condition in workspace.model["boundary_conditions"]
        if condition["type"] == "axis_of_symmetry"
    )
    assert axis_condition["automatic_for_axis"] is True
    assert axis_edge["boundary_condition_ids"] == [axis_condition["id"]]
    assert [entry[1] for entry in workspace._tree_subsection("boundaries")[1]] == [
        "Axis of symmetry · 1 edge"
    ]

    project, _ = workspace._canvas_projection()
    workspace._move_region_from_canvas_drag(
        region["id"], project((0.0, 0.0)), project((0.004, 0.0))
    )

    assert all(
        condition["type"] != "axis_of_symmetry"
        for condition in workspace.model["boundary_conditions"]
    )
    assert all(
        not any(
            condition.get("type") == "axis_of_symmetry"
            for condition in workspace.model["boundary_conditions"]
            if condition["id"] in edge["boundary_condition_ids"]
        )
        for edge in workspace.model["geometry"]["edges"]
    )

    project, _ = workspace._canvas_projection()
    workspace._move_region_from_canvas_drag(
        region["id"], project((0.004, 0.0)), project((0.0, 0.0))
    )
    assert any(
        condition["type"] == "axis_of_symmetry"
        for condition in workspace.model["boundary_conditions"]
    )
    assert any(
        all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"])
        and any(
            condition.get("type") == "axis_of_symmetry"
            for condition in workspace.model["boundary_conditions"]
            if condition["id"] in edge["boundary_condition_ids"]
        )
        for edge in workspace.model["geometry"]["edges"]
    )


def test_mechanical_region_gets_automatic_transmission_boundary(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._set_region_value(region["id"], "material_id", "material-air")

    workspace._set_region_value(region["id"], "mechanical", True)

    condition = next(
        item for item in workspace.model["boundary_conditions"]
        if item.get("type") == "transmission_interface"
    )
    assert condition["automatic_for_mechanics"] is True
    for edge_id in region["edge_ids"]:
        edge = next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == edge_id)
        assigned_types = [
            item["type"] for item in workspace.model["boundary_conditions"]
            if item["id"] in edge["boundary_condition_ids"]
        ]
        if all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"]):
            assert assigned_types == ["axis_of_symmetry"]
        else:
            assert assigned_types == ["transmission_interface"]

    saved_model = copy.deepcopy(workspace.model)
    saved_model["boundary_conditions"] = [
        item for item in saved_model["boundary_conditions"]
        if not item.get("automatic_for_mechanics")
    ]
    for edge in saved_model["geometry"]["edges"]:
        edge["boundary_condition_ids"] = [
            item for item in edge["boundary_condition_ids"]
            if item != condition["id"]
        ]
    loaded = SolveWorkspace()
    loaded.set_model(saved_model)
    loaded_region = loaded.model["geometry"]["regions"][0]
    loaded_interface = next(
        item for item in loaded.model["boundary_conditions"]
        if item.get("type") == "transmission_interface"
    )
    for edge_id in loaded_region["edge_ids"]:
        edge = next(edge for edge in loaded.model["geometry"]["edges"] if edge["id"] == edge_id)
        assigned_types = [
            item["type"] for item in loaded.model["boundary_conditions"]
            if item["id"] in edge["boundary_condition_ids"]
        ]
        if all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"]):
            assert assigned_types == ["axis_of_symmetry"]
        else:
            assert assigned_types == ["transmission_interface"]

    loaded._set_region_value(loaded_region["id"], "mechanical", False)

    assert not any(
        item.get("type") == "transmission_interface"
        for item in loaded.model["boundary_conditions"]
    )


def test_nested_mechanical_regions_use_default_shared_contour_and_update_after_move(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.0,
        ("rectangle", "z_min"): -0.01,
        ("rectangle", "width"): 0.02,
        ("rectangle", "height"): 0.02,
    })
    workspace._add_primitive("rectangle")
    parent = workspace.model["geometry"]["regions"][0]
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.004,
        ("rectangle", "z_min"): -0.002,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.004,
    })
    workspace._add_primitive("rectangle")
    child = workspace.model["geometry"]["regions"][1]
    assert child["parent_id"] == parent["id"]

    workspace._set_region_value(parent["id"], "mechanical", True)
    interface = next(
        item for item in workspace.model["boundary_conditions"]
        if item.get("type") == "transmission_interface"
    )
    assert all(
        interface["id"] in next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == edge_id)["boundary_condition_ids"]
        for edge_id in child["edge_ids"]
    )

    workspace._set_region_value(child["id"], "mechanical", True)
    assert all(
        next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == edge_id).get("mechanical_boundary_locked") is True
        and interface["id"] not in next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == edge_id)["boundary_condition_ids"]
        for edge_id in child["edge_ids"]
    )
    workspace.model["boundary_conditions"].append({
        "id": "boundary-fixed-shared", "name": "Fixed", "type": "mechanical_fixed"
    })
    shared_edge_id = child["edge_ids"][1]
    workspace._set_edge_condition(shared_edge_id, "mechanical", "boundary-fixed-shared")
    assert "boundary-fixed-shared" not in next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == shared_edge_id)["boundary_condition_ids"]
    assert "locked" in workspace.message.lower()
    workspace.active_section = "geometry"
    workspace.selected_edge_ids = [shared_edge_id]
    workspace.selected_edge_id = shared_edge_id
    workspace._build_geometry_inspector()
    workspace._update_geometry_inspector()
    assert workspace._edge_mech_select.ui_disable is True
    assert workspace._edge_em_select.ui_disable is False

    project, _ = workspace._canvas_projection()
    workspace._move_region_from_canvas_drag(
        child["id"], project((0.005, 0.0)), project((0.045, 0.0))
    )

    assert child["parent_id"] is None
    assert all(
        interface["id"] in next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == edge_id)["boundary_condition_ids"]
        and not next(edge for edge in workspace.model["geometry"]["edges"] if edge["id"] == edge_id).get("mechanical_boundary_locked")
        for edge_id in child["edge_ids"]
    )


def test_loading_pre_axis_model_adds_automatic_axis_boundary(standalone_components):
    source = SolveWorkspace()
    source._primitive_values[("rectangle", "r_min")] = 0.0
    source._add_primitive("rectangle")
    source._set_region_value(
        source.model["geometry"]["regions"][0]["id"], "material_id", "material-air"
    )
    legacy_axis_model = copy.deepcopy(source.model)
    legacy_axis_model["boundary_conditions"] = []
    for edge in legacy_axis_model["geometry"]["edges"]:
        edge["boundary_condition_ids"] = []

    workspace = SolveWorkspace()
    workspace.set_model(legacy_axis_model)

    axis_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"])
    )
    axis_condition = next(
        condition for condition in workspace.model["boundary_conditions"]
        if condition["type"] == "axis_of_symmetry"
    )
    assert axis_edge["boundary_condition_ids"] == [axis_condition["id"]]
    assert axis_condition["automatic_for_axis"] is True


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


def test_moving_nested_region_refreshes_parent_cutout_without_redrawing(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    outer = workspace.model["geometry"]["regions"][0]
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.003,
        ("rectangle", "z_min"): 0.003,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.005,
    })
    workspace._add_primitive("rectangle")
    inner = workspace.model["geometry"]["regions"][1]
    assert inner["parent_id"] == outer["id"]
    vertices_before = copy.deepcopy(inner["vertices"])
    edges_before = {
        item["id"]: copy.deepcopy(item["vertices"])
        for item in workspace.model["geometry"]["edges"]
        if item["id"] in inner["edge_ids"]
    }
    path_before = workspace._canvas_region_path_data(outer)
    scripts = []
    workspace_js = SimpleNamespace(eval=lambda script: scripts.append(script))
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda _self: workspace_js))
    rendered = []
    monkeypatch.setattr(workspace, "render_canvas", lambda: rendered.append(True))
    monkeypatch.setattr(workspace, "_refresh_model_tree", lambda: None)
    monkeypatch.setattr(workspace, "_render_inspector", lambda: None)
    project, _ = workspace._canvas_projection()

    moved = workspace._move_region_from_canvas_drag(
        inner["id"], project((0.004, 0.004)), project((0.005, 0.004)),
        preserve_canvas=True,
    )

    path_after = workspace._canvas_region_path_data(outer)
    assert moved is True
    assert path_after != path_before
    assert inner["parent_id"] == outer["id"]
    assert inner["vertices"] != vertices_before
    for vertex, before in zip(inner["vertices"], vertices_before):
        assert vertex == pytest.approx([before[0] + 0.001, before[1]])
    for edge in workspace.model["geometry"]["edges"]:
        if edge["id"] not in edges_before:
            continue
        for point, before in zip(edge["vertices"], edges_before[edge["id"]]):
            assert point == pytest.approx([before[0] + 0.001, before[1]])
    assert any("__ngsolveCommitSketchRegionMove" in script for script in scripts)
    assert any(json.dumps({"id": outer["id"], "d": path_after}) in script for script in scripts)

    # Moving fully outside the container must remove its now-stale cutout too.
    moved_outside = workspace._move_region_from_canvas_drag(
        inner["id"], project((0.005, 0.004)), project((0.022, 0.004)),
        preserve_canvas=True,
    )
    path_outside = workspace._canvas_region_path_data(outer)
    assert moved_outside is True
    assert inner["parent_id"] is None
    assert path_outside.count("M") == 1
    assert json.dumps({"id": outer["id"], "d": path_outside}) in scripts[-1]
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
    assert "interiors cannot overlap" in workspace.message


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





def test_hp_layer_targets_can_be_removed_individually_including_stale_ids(standalone_components):
    workspace = SolveWorkspace()
    workspace.model["mesh"]["hp_region_ids"] = ["deleted-region-id"]
    workspace.model["mesh"]["hp_edge_ids"] = ["deleted-edge-id"]
    workspace._sync_canvas_selection = lambda: None
    workspace._render_inspector = lambda: None

    workspace._remove_hp_layer_target("hp_region_ids", "deleted-region-id")
    assert workspace.model["mesh"]["hp_region_ids"] == []
    assert workspace.model["mesh"]["hp_edge_ids"] == ["deleted-edge-id"]

    workspace._remove_hp_layer_target("hp_edge_ids", "deleted-edge-id")
    assert workspace.model["mesh"]["hp_edge_ids"] == []


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
    assert set(workspace.selected_edge_ids) == set(region["edge_ids"])
    workspace._select_edges_in_canvas_box(
        (left, top), (right, bottom), additive=True
    )
    assert set(workspace.selected_edge_ids) == {
        item["id"] for item in workspace.model["geometry"]["edges"]
    }


def test_line_drag_finishes_even_when_browser_movement_flag_is_stale(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    completed = [
        "draw", "line", 100.0, 100.0, 180.0, 100.0, "",
        False, False, 1.0, 0.0, 0.0, 0,
        "", [], "", "", False,
    ]
    fake_js = SimpleNamespace(eval=lambda _script: completed)
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda _self: fake_js), raising=False)
    monkeypatch.setattr(workspace, "_commit_browser_canvas_view", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(
        workspace, "_sketch_screen_points_to_world",
        lambda points, snap=True: [(0.004, 0.002), (0.008, 0.002)],
    )
    finished = []
    monkeypatch.setattr(
        workspace, "_finish_line_sketch_chain",
        lambda *, select_tool=False: finished.append(select_tool),
    )

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert finished == [True]


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


def test_pressing_a_canvas_region_selects_it_for_editing(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    first_region_id = workspace.model["geometry"]["regions"][0]["id"]
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.003,
        ("rectangle", "z_min"): 0.003,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.005,
    })
    workspace._add_primitive("rectangle")
    second_region_id = workspace.model["geometry"]["regions"][1]["id"]
    assert workspace.selected_region_id == second_region_id

    monkeypatch.setattr(workspace, "_read_canvas_region_id", lambda _value: first_region_id)
    monkeypatch.setattr(workspace, "_read_canvas_edge_id", lambda _value: None)
    workspace._canvas_event_point = lambda _event, refresh_transform=False: (450.0, 320.0)

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))

    assert workspace.selected_region_id == first_region_id
    assert workspace._region_name_input.ui_model_value == "square 1"
    assert workspace._canvas._callbacks["mousedown"] == [workspace._on_canvas_mouse_down]


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

    assert workspace._canvas_drag["operation"] == "marquee"
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


def test_middle_drag_pans_without_leaving_the_radial_axis_margin(standalone_components):
    workspace = SolveWorkspace()
    center_before, width_before, _ = workspace._current_canvas_world_view()
    start, end = (100.0, 120.0), (80.0, 146.0)
    points = iter((start, end, end))
    workspace._canvas_event_point = lambda event, refresh_transform=False: next(points)

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 1}))
    assert workspace._canvas_drag["operation"] == "pan"
    workspace._on_canvas_mouse_move(SimpleNamespace(value={}))
    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    center_after, width_after, _ = workspace._current_canvas_world_view()
    pixels_per_world = (workspace._canvas_plot_bounds()[2] - workspace._canvas_plot_bounds()[0]) / width_before
    assert center_after[0] == pytest.approx(center_before[0] + 20.0 / pixels_per_world)
    assert center_after[1] == pytest.approx(center_before[1] + 26.0 / pixels_per_world)
    assert width_after == pytest.approx(width_before)

    points = iter(((160.0, 180.0), (190.0, 210.0), (190.0, 210.0)))
    workspace._canvas_event_point = lambda event, refresh_transform=False: next(points)
    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))
    assert workspace._canvas_drag["operation"] == "marquee"
    workspace._on_canvas_mouse_move(SimpleNamespace(value={}))
    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))
    assert workspace._canvas_drag is None


def test_middle_drag_keeps_z_axis_labels_clear_at_negative_r_limit(standalone_components):
    workspace = SolveWorkspace()
    plot = workspace._canvas_plot_bounds()
    buffer_px = min(96.0, max(72.0, (plot[2] - plot[0]) * 0.08))
    workspace._pan_canvas_by_pixels((100.0, 120.0), (10000.0, 120.0))

    project, _ = workspace._canvas_projection()
    assert project((0.0, 0.0))[0] == pytest.approx(buffer_px)


def test_browser_pan_commit_clamps_negative_radius_when_requested(standalone_components):
    workspace = SolveWorkspace()
    plot = workspace._canvas_plot_bounds()
    buffer_px = min(96.0, max(72.0, (plot[2] - plot[0]) * 0.08))

    assert workspace._commit_browser_canvas_view(
        (1.0, 10000.0, 0.0, 1), render=False, clamp_negative_r=True
    )

    project, _ = workspace._canvas_projection()
    assert project((0.0, 0.0))[0] == pytest.approx(buffer_px)


def test_zooming_out_keeps_negative_r_within_the_axis_buffer(standalone_components):
    workspace = SolveWorkspace()
    plot = workspace._canvas_plot_bounds()
    buffer_px = min(96.0, max(72.0, (plot[2] - plot[0]) * 0.08))
    _, initial_width, _ = workspace._current_canvas_world_view()

    workspace.zoom_canvas(
        2.0,
        ((plot[0] + plot[2]) / 2, (plot[1] + plot[3]) / 2),
        render=False,
    )

    project, _ = workspace._canvas_projection()
    assert workspace._current_canvas_world_view()[1] == pytest.approx(initial_width * 2)
    assert project((0.0, 0.0))[0] == pytest.approx(buffer_px)


def test_browser_zoom_out_commit_clamps_axis_to_negative_r_buffer(standalone_components):
    workspace = SolveWorkspace()
    plot = workspace._canvas_plot_bounds()
    buffer_px = min(96.0, max(72.0, (plot[2] - plot[0]) * 0.08))
    center_x = (plot[0] + plot[2]) / 2
    center_y = (plot[1] + plot[3]) / 2
    _, initial_width, _ = workspace._current_canvas_world_view()

    assert workspace._commit_browser_canvas_view(
        (0.5, center_x * 0.5, center_y * 0.5, 1),
        render=False,
        clamp_negative_r=True,
    )

    project, _ = workspace._canvas_projection()
    assert workspace._current_canvas_world_view()[1] == pytest.approx(initial_width * 2)
    assert project((0.0, 0.0))[0] == pytest.approx(buffer_px)


def test_pan_release_snapshots_view_before_redrawing(standalone_components, monkeypatch):
    import webgpu.platform as platform

    workspace = SolveWorkspace()
    completed = [
        "pan", "select", 0.0, 0.0, 50.0, 25.0, "", False, True,
        1.0, 50.0, 25.0, 1, "",
    ]
    operations = []

    class FakeJS:
        def eval(self, *_args, **_kwargs):
            return completed

    monkeypatch.setattr(platform, "js", FakeJS())
    workspace._prepare_canvas_view_commit_snapshot = lambda: operations.append("snapshot")
    workspace.render_canvas = lambda: operations.append("render")

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert operations == ["snapshot", "render"]


def test_middle_drag_pans_even_when_a_shape_tool_is_selected(standalone_components):
    workspace = SolveWorkspace()
    workspace.set_sketch_tool("rectangle", announce=False)
    workspace._canvas_event_point = lambda event, refresh_transform=False: (120.0, 180.0)

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 1}))

    assert workspace._canvas_drag["operation"] == "pan"
    assert workspace._canvas_drag["tool"] == "rectangle"


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


def test_drawing_tool_cursor_remains_crosshair_over_existing_geometry(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    region_node = workspace._canvas_region_nodes[region["id"]]
    edge_hit = workspace._canvas_edge_nodes[region["edge_ids"][0]][-1]

    workspace.set_sketch_tool("rectangle", announce=False)

    assert region_node._props["style"] == "cursor:crosshair;"
    assert edge_hit._props["style"].startswith("cursor:crosshair;")

    workspace.set_sketch_tool("select", announce=False)

    assert region_node._props["style"] == "cursor:pointer;"
    assert edge_hit._props["style"].startswith("cursor:pointer;")


def test_selecting_geometry_restores_its_inspector_without_material_assignments(standalone_components):
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
    labels = [getattr(item, "ui_label", None) for item in workspace._geometry_inspector_children[1].ui_children]
    assert "Material" not in labels
    assert "Include in mechanics" not in labels

    workspace.select_section("materials")
    workspace.select_edge(edge_id)
    assert workspace._inspector_view == "geometry"
    assert workspace._region_panel.ui_hidden
    assert not workspace._edge_panel.ui_hidden

def test_boundary_stage_accumulates_region_edges_without_duplicates(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.005,
        ("rectangle", "z_min"): 0.005,
        ("rectangle", "width"): 0.005,
        ("rectangle", "height"): 0.005,
    })
    workspace._add_primitive("rectangle")
    first, second = workspace.model["geometry"]["regions"]
    first_edges = first["edge_ids"]
    second["edge_ids"] = [first_edges[1], *second["edge_ids"]]
    workspace.select_section("boundaries")
    project, _ = workspace._canvas_projection()
    for world_point in ((0.017, 0.017), (0.0075, 0.0075)):
        point = project(world_point)
        workspace._canvas_event_point = lambda event, refresh_transform=False, point=point: point
        workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))
        workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert workspace.active_section == "boundaries"
    assert workspace.selected_edge_ids == list(dict.fromkeys([*first_edges, *second["edge_ids"]]))
    assert len(workspace.selected_edge_ids) == len(set(workspace.selected_edge_ids))


def test_boundary_stage_edge_toggle_removes_only_the_clicked_edge(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace.select_section("boundaries")
    edges = [edge["id"] for edge in workspace.model["geometry"]["edges"]]
    workspace._set_boundary_stage_selection(edges[:3])

    workspace.toggle_boundary_stage_edge(edges[1])
    assert workspace.selected_edge_ids == [edges[0], edges[2]]
    workspace.toggle_boundary_stage_edge(edges[3])
    assert workspace.selected_edge_ids == [edges[0], edges[2], edges[3]]
    assert workspace.active_section == "boundaries"


def test_boundary_stage_edge_hit_has_priority_over_region_hit(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace.select_section("boundaries")
    region = workspace.model["geometry"]["regions"][0]
    edge = workspace.model["geometry"]["edges"][1]
    project, _ = workspace._canvas_projection()
    first, second = [project(point) for point in edge["vertices"]]
    point = ((first[0] + second[0]) / 2, (first[1] + second[1]) / 2)
    workspace._canvas_event_point = lambda event, refresh_transform=False: point

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))
    assert workspace._canvas_drag["operation"] == "edge"
    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))
    # The native click follows pointer release in the browser and must not
    # toggle the same edge a second time.
    workspace._on_canvas_edge_click(edge["id"])

    assert workspace.selected_edge_ids == [edge["id"]]
    assert workspace.active_section == "boundaries"
    assert workspace.selected_region_id is None


def test_boundary_stage_assigns_active_condition_on_click_and_toggle_keeps_assignment(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    condition = {"id": "boundary-test", "name": "Outer reference", "type": "magnetic_potential_zero"}
    workspace.model["boundary_conditions"].append(condition)
    workspace.select_boundary(condition["id"])
    workspace._boundary_properties()

    assert not hasattr(workspace, "_boundary_apply_selection_button")
    non_axis_edges = [
        edge["id"] for edge in workspace.model["geometry"]["edges"]
        if not all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"])
    ]
    edge_id = non_axis_edges[0]
    workspace.toggle_boundary_stage_edge(edge_id)
    assert workspace.selected_edge_ids == [edge_id]
    edge = next(item for item in workspace.model["geometry"]["edges"] if item["id"] == edge_id)
    assert condition["id"] in edge["boundary_condition_ids"]

    workspace.toggle_boundary_stage_edge(edge_id)
    assert workspace.selected_edge_ids == []
    assert condition["id"] in edge["boundary_condition_ids"]

    workspace.toggle_boundary_stage_region(region["id"])
    assert set(workspace.selected_edge_ids) == set(region["edge_ids"])
    assert workspace.active_section == "boundaries"
    workspace.toggle_boundary_stage_region(region["id"])
    assert workspace.selected_edge_ids == []

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
    toolbar = workspace._sketch_toolbar

    assert workspace.ui_children.index(toolbar) < workspace.ui_children.index(workspace._content)
    assert workspace._canvas_panel.ui_children == [workspace._canvas_host]

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
    assert "![0, 1].includes(event.button)" in scripts[0]
    assert "const middlePan = event.button === 1" in scripts[0]
    assert "? (dragRegionId ? 'region' : (dragEdgeId ? 'edge' : 'marquee'))" in scripts[0]
    assert "gridCacheFor" in scripts[0]
    assert "liveGrid.replaceChildren();" in scripts[0]
    assert "while (liveGrid.firstChild)" not in scripts[0]
    assert "data-sketch-grid-revision" in scripts[0]
    assert "snapshot.__ngsolveHiddenLayers" in scripts[0]
    assert "layer.setAttribute('visibility', 'hidden')" in scripts[0]
    assert "layer.setAttribute('visibility', entry.visibility)" in scripts[0]
    reset_body = scripts[0].split("window.__ngsolveResetSketchPreview =", 1)[1]
    assert reset_body.index("removeViewCommitSnapshot(current)") < reset_body.index("clearPreview(current)")
    preview_body = scripts[0].split(
        "const updatePreview = (svg, point, screenPoint = null) => {", 1
    )[1].split("const beginGesture", 1)[0]
    assert "regionElements(svg, dragRegionId)" in preview_body
    assert "querySelectorAll('[data-sketch-region-id]')" not in preview_body


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


def test_blank_canvas_click_clears_selected_boundary_regions_and_edges(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace.select_section("boundaries")
    region = workspace.model["geometry"]["regions"][0]
    edge_ids = [edge["id"] for edge in workspace.model["geometry"]["edges"]]
    workspace.selected_region_id = region["id"]
    workspace.selected_region_ids = [region["id"]]
    workspace.selected_edge_ids = edge_ids[:2]
    workspace.selected_edge_id = edge_ids[1]

    completed = ["marquee", "select", 120, 180, 120, 180, "", False, False, 1, 0, 0, 1, ""]
    fake_js = SimpleNamespace(eval=lambda _script: completed)
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: fake_js), raising=False)
    workspace._commit_browser_canvas_view = lambda *args, **kwargs: False

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert workspace.selected_region_id is None
    assert workspace.selected_region_ids == []
    assert workspace.selected_edge_id is None
    assert workspace.selected_edge_ids == []
    assert workspace.active_section == "boundaries"


def test_blank_canvas_click_clears_geometry_selection(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    edge_id = workspace.model["geometry"]["edges"][0]["id"]
    workspace.selected_region_id = region["id"]
    workspace.selected_region_ids = [region["id"]]
    workspace.selected_edge_id = edge_id
    workspace.selected_edge_ids = [edge_id]

    completed = ["marquee", "select", 120, 180, 120, 180, "", False, False, 1, 0, 0, 1, ""]
    fake_js = SimpleNamespace(eval=lambda _script: completed)
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: fake_js), raising=False)
    workspace._commit_browser_canvas_view = lambda *args, **kwargs: False

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert workspace.selected_region_id is None
    assert workspace.selected_region_ids == []
    assert workspace.selected_edge_id is None
    assert workspace.selected_edge_ids == []


def test_canvas_render_keeps_background_and_layer_components_mounted(standalone_components):
    workspace = SolveWorkspace()
    root_layers = tuple(workspace._canvas.ui_children)

    workspace._add_primitive("rectangle")

    assert tuple(workspace._canvas.ui_children) == root_layers
    assert root_layers == (
        workspace._canvas_background,
        workspace._canvas_grid_layer,
        workspace._canvas_scene,
        workspace._canvas_dimensions,
        workspace._canvas_preview,
        workspace._canvas_inference,
    )
    assert tuple(workspace._canvas_grid_layer.ui_children) == (
        workspace._canvas_grid,
        workspace._canvas_live_grid,
    )


def test_model_tree_groups_sections_and_places_children_under_their_parent(standalone_components):
    workspace = SolveWorkspace()
    assert workspace.model["materials"] == []
    assert workspace.model["boundary_conditions"] == []

    assert [button.ui_label for button in workspace._section_buttons.values()] == [
        "Geometry",
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
    assert model_group.ui_children[2].ui_children[0] is workspace._section_buttons["materials"]
    assert "parameters" not in workspace._section_buttons
    assert analysis_group.ui_children[1].ui_children[0] is workspace._section_buttons["mesh"]
    assert results_group.ui_children[1].ui_children[0] is workspace._section_buttons["runs"]
    assert workspace._tree_subsection("materials") == ("MATERIALS · 0", [])
    assert workspace._tree_subsection("boundaries") == ("CONDITIONS · 0", [])
    assert [option["label"] for option in workspace._material_options()] == [
        "Unassigned",
        "Air",
        "Copper",
        "Main coil (effective composite)",
        "Copper (gradient coil)",
        "Stainless steel (4 K / OVC)",
        "Aluminium (77 K shield)",
        "Epoxy",
        "Shim",
        "Analytical test material",
    ]
    assert workspace._tree_splitter.ui_slot_before == [workspace._tree]
    assert workspace._tree_splitter.ui_slot_after == [workspace._properties_splitter]
    assert workspace._properties_splitter.ui_slot_before == [workspace._canvas_panel]
    assert workspace._properties_splitter.ui_slot_after == [workspace._right]
    assert "flex:1 1 auto" in workspace._tree_splitter._props["style"]
    assert "flex:1 1 auto" in workspace._properties_splitter._props["style"]


def test_blank_axisymmetric_view_reserves_a_z_label_gutter(standalone_components):
    workspace = SolveWorkspace()
    project, unproject = workspace._canvas_projection()
    plot = workspace._canvas_plot_bounds()
    center = ((plot[0] + plot[2]) / 2, (plot[1] + plot[3]) / 2)
    buffer_px = min(96.0, max(72.0, (plot[2] - plot[0]) * 0.08))

    assert plot == (0.0, 0.0, 900.0, 640.0)
    assert buffer_px == pytest.approx(72.0)
    assert project((0.0, 0.0))[0] == pytest.approx(buffer_px)
    assert unproject((plot[0], 320.0))[0] < 0.0
    assert unproject(center)[1] == pytest.approx(0.0)
    assert unproject((center[0], plot[1]))[1] == pytest.approx(-unproject((center[0], plot[3]))[1])
    ticks = workspace._coordinate_ticks(0.0, 0.022)
    assert ticks[0] == pytest.approx(0.0)
    steps = {round(b - a, 10) for a, b in zip(ticks, ticks[1:])}
    assert len(steps) == 1
    svg_text = [item for item in workspace._canvas_grid.ui_children if item._component_name == "text"]
    assert any(item._props.get("textContent") == "Axis of rotation  ·  r = 0" for item in svg_text)
    tick_labels = [
        item for item in svg_text
        if item._props.get("textContent") not in {
            "Axis of rotation  ·  r = 0", "z = 0", "r  [m]", "z  [m]",
        }
    ]
    assert tick_labels
    assert all(item._props.get("font-size") == "16" for item in tick_labels)
    assert all(not item.ui_children for item in svg_text)


def test_narrow_canvas_keeps_a_full_z_axis_label_gutter(standalone_components):
    workspace = SolveWorkspace()
    workspace._canvas_width = 406
    workspace._canvas_height = 390

    project, _ = workspace._canvas_projection()

    assert project((0.0, 0.0))[0] == pytest.approx(72.0)


def test_sketch_axes_and_grid_fill_the_responsive_viewport(standalone_components):
    workspace = SolveWorkspace()
    plot = workspace._canvas_plot_bounds()

    assert workspace._canvas._props["viewBox"] == "0 0 900 640"
    assert workspace._canvas._props["preserveAspectRatio"] == "xMidYMid meet"
    assert "width:100%" in workspace._canvas._props["style"]
    assert "height:100%" in workspace._canvas._props["style"]
    assert plot == (0.0, 0.0, 900.0, 640.0)

    grid_lines = [
        item for item in workspace._canvas_grid.ui_children
        if item._component_name == "line"
    ]
    rotation_axis = next(item for item in grid_lines if item._props.get("stroke-dasharray") == "6 4")
    zero_height_axis = next(
        item for item in grid_lines
        if item._props.get("stroke") == "#557187"
        and item._props.get("stroke-width") == "1.5"
    )
    buffer_px = min(96.0, max(72.0, (plot[2] - plot[0]) * 0.08))
    assert (rotation_axis._props["x1"], rotation_axis._props["x2"]) == pytest.approx(
        (buffer_px, buffer_px)
    )
    assert (rotation_axis._props["y1"], rotation_axis._props["y2"]) == (plot[1], plot[3])
    assert (zero_height_axis._props["x1"], zero_height_axis._props["x2"]) == (plot[0], plot[2])


def test_sketch_grid_has_five_snap_subdivisions_per_major_cell(standalone_components):
    workspace = SolveWorkspace()
    plot = workspace._canvas_plot_bounds()
    _, world_width, _ = workspace._current_canvas_world_view()
    pixels_per_world = (plot[2] - plot[0]) / world_width
    major_step = workspace._canvas_grid_step()
    major_spacing_px = major_step * pixels_per_world
    snap_spacing_px = workspace._canvas_snap_step() * pixels_per_world
    _, unproject = workspace._canvas_projection()
    minor_lines = [
        item for item in workspace._canvas_grid.ui_children
        if item._component_name == "line"
        and item._props.get("stroke") == "var(--grid-minor, #e8edf2)"
    ]

    assert snap_spacing_px == pytest.approx(major_spacing_px / 5)
    vertical_coordinates = [
        unproject((item._props["x1"], (plot[1] + plot[3]) / 2))[0]
        for item in minor_lines if item._props["x1"] == item._props["x2"]
    ]
    horizontal_coordinates = [
        unproject(((plot[0] + plot[2]) / 2, item._props["y1"]))[1]
        for item in minor_lines if item._props["y1"] == item._props["y2"]
    ]
    assert vertical_coordinates and horizontal_coordinates
    for coordinate in vertical_coordinates + horizontal_coordinates:
        major_fraction = coordinate / major_step
        assert major_fraction * 5 == pytest.approx(round(major_fraction * 5))
        assert major_fraction != pytest.approx(round(major_fraction))


def test_sketch_reprojects_to_resized_viewport_without_distorting_geometry(standalone_components):
    workspace = SolveWorkspace()
    workspace._on_canvas_resize(SimpleNamespace(value={"width": 1200.4, "height": 600.2}))

    assert workspace._canvas_width == 1200
    assert workspace._canvas_height == 600
    assert workspace._canvas._props["viewBox"] == "0 0 1200 600"
    assert workspace._canvas_background._props["width"] == 1200
    assert workspace._canvas_background._props["height"] == 600
    assert workspace._canvas_plot_bounds() == (0.0, 0.0, 1200.0, 600.0)

    project, _ = workspace._canvas_projection()
    radial_scale = (project((0.01, 0.0))[0] - project((0.0, 0.0))[0]) / 0.01
    axial_scale = (project((0.0, 0.01))[1] - project((0.0, 0.0))[1]) / 0.01
    assert radial_scale == pytest.approx(-axial_scale)


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


def test_sketch_keeps_motion_browser_local_and_sends_discrete_pointer_events(standalone_components):
    workspace = SolveWorkspace()
    callbacks = workspace._canvas._callbacks

    assert "mousedown" in callbacks
    assert "mouseup" in callbacks
    assert "wheel" in callbacks
    assert "mousemove" not in callbacks


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

    buffer_px = min(96.0, max(72.0, workspace._canvas_plot_bounds()[2] * 0.08))
    assert project((0.0, 0.0))[0] == pytest.approx(buffer_px)
    assert project((0.01, 0.0))[0] > buffer_px
    assert unproject((0.0, 320.0))[0] < 0.0


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

    workspace.zoom_canvas(0.8, anchor)

    project_after, unproject_after = workspace._canvas_projection()
    span_after = project_after((0.020, 0.0))[0] - project_after((0.0, 0.0))[0]
    assert unproject_after(anchor) == pytest.approx(anchor_world)
    assert span_after == pytest.approx(span_before / 0.8)
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
    snap_step = workspace._canvas_snap_step()
    assert snap_step == pytest.approx(grid_step / 5)
    unsnapped = project((grid_step * 1.37, grid_step * 2.62))
    snapped = workspace._snap_canvas_point(unsnapped)
    radial, axial = unproject(snapped)
    assert radial == pytest.approx(round(radial / snap_step) * snap_step)
    assert axial == pytest.approx(round(axial / snap_step) * snap_step)

    workspace.toggle_snap_to_grid()
    assert not workspace.snap_to_grid
    assert workspace._snap_canvas_point(unsnapped) == unsnapped


def test_grid_snap_button_is_present_with_the_sketch_tools(standalone_components):
    workspace = SolveWorkspace()
    toolbar = workspace._sketch_toolbar
    slots = toolbar.ui_slots["default"]
    snap_index = next(index for index, item in enumerate(slots) if getattr(item, "ui_icon", None) == "mdi-magnet")
    fit_view_index = next(index for index, item in enumerate(slots) if getattr(item, "ui_label", None) == "Fit view")
    snap_button = slots[snap_index]

    assert workspace._snap_button is snap_button
    assert snap_button.ui_icon == "mdi-magnet"
    assert snap_button.ui_label == "Snap"
    assert "flex:0 0 auto" in snap_button.ui_style
    assert snap_index < fit_view_index


def test_parameters_are_hidden_from_the_model_tree_but_legacy_values_are_preserved(standalone_components):
    workspace = SolveWorkspace()
    workspace.add_parameter()

    assert "parameters" not in workspace._section_buttons
    assert "parameters" not in workspace._tree_entry_lists
    assert workspace.active_section == "geometry"
    assert workspace.model["parameters"][0]["name"] == "length_1"

    workspace.set_model(
        workspace.model,
        layout={"schema_version": 1, "active_section": "parameters", "camera": "fit"},
    )
    assert workspace.active_section == "geometry"
    assert workspace.model["parameters"][0]["name"] == "length_1"


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


def test_model_tree_material_section_lists_regions_and_boundary_counts(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    edges = workspace.model["geometry"]["edges"]
    axis_edge = next(edge for edge in edges if all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"]))
    axis_condition = next(condition for condition in workspace.model["boundary_conditions"] if condition["type"] == "axis_of_symmetry")
    assert axis_edge["boundary_condition_ids"] == [axis_condition["id"]]
    assert all(edge["boundary_condition_ids"] == [] for edge in edges if edge is not axis_edge)

    workspace._set_region_value(region["id"], "material_id", "material-air")
    label = workspace._tree_subsection("materials")[1][0][1]
    assert region["name"] in label and "Air" in label
    workspace._set_region_value(region["id"], "material_id", "material-copper")
    entries = workspace._tree_subsection("materials")[1]
    assert len(entries) == 1 and region["name"] in entries[0][1] and "Copper" in entries[0][1]

    condition = {"id": "boundary-fixed", "name": "Fixed support", "type": "mechanical_fixed"}
    workspace.model["boundary_conditions"].append(condition)
    workspace._refresh_model_tree()
    labels = {entry[0]: entry[1] for entry in workspace._tree_subsection("boundaries")[1]}
    assert labels["boundary-fixed"] == "Fixed support · 0 edges"
    workspace._set_edge_condition(edges[1]["id"], "mechanical", condition["id"])
    labels = {entry[0]: entry[1] for entry in workspace._tree_subsection("boundaries")[1]}
    assert labels["boundary-fixed"] == "Fixed support · 1 edge"

def test_unused_builtin_material_can_be_removed_from_the_model(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._set_region_value(region["id"], "material_id", "material-air")
    workspace._set_region_value(region["id"], "material_id", None)
    workspace.remove_material("material-air")

    assert workspace.model["materials"] == []
    entries = workspace._tree_subsection("materials")[1]
    assert len(entries) == 1 and "Unassigned" in entries[0][1]

def test_region_material_drawer_assigns_custom_material_and_saves_it_locally(standalone_components):
    from types import SimpleNamespace

    def components(entries):
        for entry in entries:
            yield entry
            yield from components(getattr(entry, "ui_children", []))

    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    properties = workspace._material_properties()
    assert any("#fff4cc" in getattr(item, "ui_style", "") for item in properties)

    workspace._open_region_material_editor(region["id"])
    controls = list(components(workspace._editor_drawer_children()))
    material_select = next(item for item in controls if getattr(item, "ui_label", None) == "Material")
    assert "Stainless steel (4 K / OVC)" in {option["label"] for option in material_select.ui_options}
    assert any(option["value"] == "__custom__" for option in material_select.ui_options)
    workspace._set_editor_material_choice(SimpleNamespace(value="__custom__"))
    controls = list(components(workspace._editor_drawer_children()))
    labels = [getattr(item, "ui_label", None) for item in controls]
    assert "Material name" in labels
    assert labels.index("Material name") < labels.index("Relative permeability") < labels.index("Electrical conductivity")
    assert "Include in mechanics" in labels

    draft = workspace._editor_drawer["draft"]
    draft["custom_material"]["name"] = "My alloy"
    draft["custom_material"]["properties"]["electrical_conductivity"] = "2.4e6"
    draft["mechanical"] = True
    footer_labels = [getattr(item, "ui_label", None) for item in workspace._editor_drawer_children()[-1].ui_children]
    assert "Apply" in footer_labels and "Save + apply" in footer_labels and "Cancel" in footer_labels
    workspace._commit_region_material(True)

    assert region["material_id"] == workspace.model["materials"][0]["id"]
    assert region["mechanical"] is True
    assert workspace.model["materials"][0]["name"] == "My alloy"
    assert workspace.model["materials"][0]["properties"]["electrical_conductivity"] == "2.4e6"
    assert any(item["id"] == region["material_id"] for item in workspace._user_materials)

def test_problem_material_preset_is_copied_to_model_when_assigned(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]

    workspace._set_region_value(region["id"], "material_id", "material-cryogenic-steel")

    expected = next(
        item for item in builtin_materials()
        if item["id"] == "material-cryogenic-steel"
    )
    assert workspace.model["materials"] == [expected]
    assert "Stainless steel (4 K / OVC)" in workspace._tree_subsection("materials")[1][0][1]


def test_custom_materials_persist_locally_across_new_models(standalone_components):
    from types import SimpleNamespace

    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._open_region_material_editor(region["id"])
    workspace._set_editor_material_choice(SimpleNamespace(value="__custom__"))
    custom = workspace._editor_drawer["draft"]["custom_material"]
    custom["name"] = "Persistent alloy"
    custom["properties"]["electrical_conductivity"] = "2.4e6"
    workspace._commit_region_material(True)
    material_id = region["material_id"]

    next_workspace = SolveWorkspace()
    assert next_workspace.model["materials"] == []
    option = next(item for item in next_workspace._material_options() if item["value"] == material_id)
    assert option["label"] == "Persistent alloy"
    assert next_workspace._material_library.path.name == "material_library.json"
    assert "site-packages" not in str(next_workspace._material_library.path)

def test_custom_material_can_be_removed_from_local_library(standalone_components):
    workspace = SolveWorkspace()
    workspace.add_material()
    custom = workspace._editor_drawer["draft"]
    workspace._commit_editor()
    custom_id = workspace.model["materials"][0]["id"]

    assert workspace.remove_material_from_library(custom_id)
    assert any(item["id"] == custom_id for item in workspace.model["materials"])
    assert all(item["id"] != custom_id for item in workspace._user_materials)
    assert all(option["value"] != custom_id for option in SolveWorkspace()._material_options())

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


def test_sources_are_defined_then_assigned_from_the_viewport(standalone_components):
    def component_labels(nodes):
        labels = []
        for node in nodes:
            label = getattr(node, "ui_label", None)
            if label:
                labels.append(label)
            labels.extend(component_labels(getattr(node, "ui_children", [])))
        return labels

    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    first = workspace.model["geometry"]["regions"][0]
    workspace._primitive_values[("rectangle", "r_min")] = 0.05
    workspace._add_primitive("rectangle")
    second = workspace.model["geometry"]["regions"][1]

    source_labels = component_labels(workspace._sources_properties())
    assert source_labels.count("Add source/load") == 1
    assert workspace._tree_subsection("sources") == ("REGIONAL LOADS · 0", [])
    assert workspace.model["regional_loads"] == []

    workspace._open_sources_editor()
    assert workspace._editor_drawer["kind"] == "regional_load"
    labels = component_labels(workspace._editor_drawer_children())
    assert "Source/load name" in labels
    assert "DC current density" in labels
    assert "Radial body force" in labels
    assert "Select regions" not in labels
    assert "Select region in viewport" not in labels
    workspace._editor_drawer["draft"].update({
        "name": "Coil excitation",
        "dc_current_density": "1000",
        "ac_current_density_real": "2000",
    })
    workspace._commit_editor()
    load = workspace.model["regional_loads"][0]
    assert load["name"] == "Coil excitation"
    assert load["region_ids"] == []
    assert first["sources"]["dc_current_density"] == "0"

    workspace.select_regional_load(load["id"])
    workspace._toggle_active_source_region(first["id"])
    workspace._toggle_active_source_region(second["id"])
    assert load["region_ids"] == [first["id"], second["id"]]
    assert first["sources"]["dc_current_density"] == "1000"
    assert second["sources"]["ac_current_density_real"] == "2000"
    workspace._toggle_active_source_region(first["id"])
    assert load["region_ids"] == [second["id"]]
    assert first["sources"]["dc_current_density"] == "0"

    workspace._open_sources_editor(load["id"])
    assert workspace._editor_drawer["draft"]["name"] == "Coil excitation"
    workspace._editor_drawer["draft"]["dc_current_density"] = "3000"
    workspace._commit_editor()
    assert second["sources"]["dc_current_density"] == "3000"
    assert workspace.delete_regional_load(load["id"]) is True
    assert workspace.model["regional_loads"] == []
    assert second["sources"]["dc_current_density"] == "0"


def test_deleting_region_removes_regional_load_assignment(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._open_sources_editor()
    workspace._editor_drawer["draft"].update(
        {"name": "Coil current", "dc_current_density": "1000"}
    )
    workspace._commit_editor()
    load = workspace.model["regional_loads"][0]
    workspace.select_regional_load(load["id"])
    workspace._toggle_active_source_region(region["id"])
    workspace.selected_region_ids = [region["id"]]

    assert load["region_ids"] == [region["id"]]
    assert region["sources"]["dc_current_density"] == "1000"

    workspace.delete_region(region["id"])

    assert workspace.model["regional_loads"][0]["region_ids"] == []
    assert workspace.selected_region_ids == []
    assert workspace.model["geometry"]["regions"] == []


def test_model_and_properties_panels_can_be_collapsed_and_restored(standalone_components):
    workspace = SolveWorkspace()
    toolbar_labels = [getattr(button, "ui_label", None) for button in workspace._sketch_toolbar.ui_children]

    assert "Model" not in toolbar_labels
    assert "Properties" not in toolbar_labels
    assert workspace._tree_reopen_tab.ui_label == "Model Tree"
    assert workspace._tree_reopen_tab.ui_icon == "mdi-chevron-right"
    assert workspace._properties_reopen_tab.ui_label == "Properties"
    assert workspace._properties_reopen_tab.ui_icon == "mdi-chevron-left"
    assert workspace._tree_reopen_tab.ui_hidden
    assert workspace._properties_reopen_tab.ui_hidden

    workspace._toggle_tree_panel()
    workspace._toggle_properties_panel()
    assert workspace._tree_splitter.ui_model_value == 0
    assert workspace._properties_splitter.ui_model_value == 0
    assert workspace._tree_splitter.ui_limits == [0, 420]
    assert workspace._properties_splitter.ui_limits == [0, 500]
    assert not workspace._tree_reopen_tab.ui_hidden
    assert not workspace._properties_reopen_tab.ui_hidden

    workspace._toggle_tree_panel()
    workspace._toggle_properties_panel()
    assert workspace._tree_splitter.ui_model_value == 240
    assert workspace._properties_splitter.ui_model_value == 340
    assert workspace._tree_splitter.ui_limits == [180, 420]
    assert workspace._properties_splitter.ui_limits == [240, 500]
    assert workspace._tree_reopen_tab.ui_hidden
    assert workspace._properties_reopen_tab.ui_hidden


def test_setup_validation_checks_actual_study_frequency_list(standalone_components):
    workspace = SolveWorkspace()
    workspace.studies["studies"][0]["frequency_hz"] = ["0"]

    assert any("Study frequency" in error for error in workspace.validation_errors())
    assert workspace._validation_issue_section("Study frequency must be positive") == "studies"


def test_setup_issue_navigation_selects_the_named_model_item(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace.model["materials"] = builtin_materials()
    copper = next(item for item in workspace.model["materials"] if item["name"] == "Copper")
    region["material_id"] = copper["id"]
    condition = {"id": "condition-fixed", "name": "Fixed support", "type": "mechanical_fixed"}
    workspace.model["boundary_conditions"] = [condition]
    edge = workspace.model["geometry"]["edges"][0]
    edge["name"] = "left edge"

    workspace._navigate_to_validation_issue("Material 'Copper' electrical_conductivity must be positive")
    assert workspace.active_section == "materials"
    assert workspace.selected_region_id == region["id"]
    assert workspace._editor_drawer["kind"] == "region_material"

    workspace._navigate_to_validation_issue("Boundary 'Fixed support' has an invalid value")
    assert workspace.active_section == "boundaries"
    assert workspace.selected_boundary_id == condition["id"]

    workspace._navigate_to_validation_issue("Geometry edge 'left edge' duplicates another edge")
    assert workspace.active_section == "geometry"
    assert workspace.selected_edge_id == edge["id"]

    workspace._navigate_to_validation_issue("Region 'square 1' dc_current_density has an incompatible unit")
    assert workspace.active_section == "sources"
    assert workspace.selected_region_id == region["id"]

    workspace._navigate_to_validation_issue("Region 'square 1' has invalid geometry")
    assert workspace.active_section == "geometry"
    assert workspace.selected_region_id == region["id"]

def test_canvas_event_reads_pointer_cache_when_ngapp_omits_coordinates(standalone_components):
    workspace = SolveWorkspace()
    workspace._screen_to_svg = (1.25, 0.0, 0.0, 1.25, 100.0, 200.0)
    workspace._read_canvas_pointer = lambda value: (125.0, 300.0)

    point = workspace._canvas_event_point(
        SimpleNamespace(value={"type": "mousedown", "button": 0, "timeStamp": 12.5})
    )

    assert point == pytest.approx((20.0, 80.0))


@pytest.mark.parametrize("section", ["geometry", "boundaries"])
def test_middle_pan_release_clamps_negative_radius_in_every_solve_section(
    standalone_components, monkeypatch, section
):
    import webgpu.platform as platform

    workspace = SolveWorkspace()
    workspace.active_section = section
    completed = [
        "pan", "select", 0.0, 0.0, 50.0, 25.0, "", False, True,
        1.0, 50.0, 25.0, 1, "",
    ]
    commits = []
    renders = []

    class FakeJS:
        def eval(self, *_args, **_kwargs):
            return completed

    monkeypatch.setattr(platform, "js", FakeJS())
    workspace._commit_browser_canvas_view = (
        lambda view=None, **kwargs: commits.append(kwargs) or True
    )
    workspace._render_canvas_after_interaction = lambda: renders.append("render")

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))

    assert commits == [{"render": False, "clamp_negative_r": True}]
    assert renders == ["render"]


def test_committed_pan_projection_matches_the_visible_browser_transform(standalone_components):
    workspace = SolveWorkspace()
    before, _ = workspace._canvas_projection()
    points = ((0.0, 0.0), (0.012, 0.004), (0.028, -0.009))
    visible_before = [before(point) for point in points]
    scale, tx, ty = 1.1, -10.0, -18.0

    assert workspace._commit_browser_canvas_view(
        (scale, tx, ty, 1),
        render=False,
        clamp_negative_r=True,
    )

    after, _ = workspace._canvas_projection()
    visible_after = [after(point) for point in points]
    for old, new in zip(visible_before, visible_after):
        assert new == pytest.approx((scale * old[0] + tx, scale * old[1] + ty))



def test_adjacent_mechanical_regions_share_locked_default_boundary(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.0,
        ("rectangle", "z_min"): 0.0,
        ("rectangle", "width"): 0.01,
        ("rectangle", "height"): 0.01,
    })
    workspace._add_primitive("rectangle")
    first = workspace.model["geometry"]["regions"][0]

    # This rectangle shares only part of the first region's right edge.
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.01,
        ("rectangle", "z_min"): 0.002,
        ("rectangle", "width"): 0.006,
        ("rectangle", "height"): 0.004,
    })
    workspace._add_primitive("rectangle")
    second = workspace.model["geometry"]["regions"][1]
    first["mechanical"] = True
    second["mechanical"] = True
    workspace._rebuild_edges()

    shared_ids = set(first["edge_ids"]) & set(second["edge_ids"])
    shared = [
        edge for edge in workspace.model["geometry"]["edges"]
        if edge["id"] in shared_ids
    ]
    assert len(shared) == 1
    assert shared[0]["vertices"] == [[0.01, 0.002], [0.01, 0.006]]
    assert shared[0]["mechanical_boundary_locked"] is True
    assert not any(
        condition.get("type", "").startswith("mechanical_")
        or condition.get("type") == "transmission_interface"
        for condition in workspace.model["boundary_conditions"]
        if condition.get("id") in shared[0]["boundary_condition_ids"]
    )

    workspace.model["boundary_conditions"].append({
        "id": "manual-fixed", "name": "Fixed", "type": "mechanical_fixed"
    })
    workspace._set_edge_condition(shared[0]["id"], "mechanical", "manual-fixed")
    assert not any(
        condition_id == "manual-fixed"
        for condition_id in shared[0]["boundary_condition_ids"]
    )


def test_concave_touching_faces_are_not_misclassified_as_overlapping(standalone_components):
    from ngsolve_gui.solve_workspace import _polygons_overlap_interior

    left_face = [
        (0.004, 0.0), (0.014, 0.0), (0.014, 0.005),
        (0.009, 0.005), (0.009, 0.01), (0.004, 0.01),
    ]
    overlap_face = [(0.009, 0.005), (0.014, 0.005), (0.014, 0.01), (0.009, 0.01)]
    assert not _polygons_overlap_interior(left_face, overlap_face)
    assert _polygons_overlap_interior(overlap_face, overlap_face)


def test_partial_rectangle_overlap_is_split_into_editable_subregions(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.004, ("rectangle", "z_min"): 0.0,
        ("rectangle", "width"): 0.010, ("rectangle", "height"): 0.010,
    })
    workspace._add_primitive("rectangle")
    first = workspace.model["geometry"]["regions"][0]
    first["material_id"] = "material-copper"
    first["sources"]["dc_current_density"] = "1e6"

    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.009, ("rectangle", "z_min"): 0.005,
        ("rectangle", "width"): 0.010, ("rectangle", "height"): 0.010,
    })
    workspace._add_primitive("rectangle")

    regions = workspace.model["geometry"]["regions"]
    from ngsolve_gui.axisymmetric_model import find_closed_sketch_loops
    loops = find_closed_sketch_loops(workspace.model["geometry"].get("sketch_entities", []))
    assert len(regions) == 3, {"message": workspace.message, "regions": [(item["name"], item["vertices"], item.get("shape")) for item in regions]}
    assert all(region["shape"]["type"] == "sketch" for region in regions)
    assert len({region["id"] for region in regions}) == 3
    assert any(region["material_id"] == "material-copper" for region in regions)
    assert any(region["name"].startswith("square 2") for region in regions)
    assert workspace.message.startswith("Created square 2")
    assert all(len(region["edge_ids"]) >= 3 for region in regions)


def test_partial_circle_overlap_is_split_and_keeps_curved_edges(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("circle", "r_center"): 0.010, ("circle", "z_center"): 0.0,
        ("circle", "radius"): 0.004,
    })
    workspace._add_primitive("circle")
    workspace._primitive_values.update({
        ("circle", "r_center"): 0.013, ("circle", "z_center"): 0.0,
        ("circle", "radius"): 0.004,
    })
    workspace._add_primitive("circle")

    regions = workspace.model["geometry"]["regions"]
    assert len(regions) == 3
    assert all(region["shape"]["type"] == "sketch" for region in regions)
    assert any(edge.get("curve", {}).get("type") == "arc"
               for edge in workspace.model["geometry"]["edges"])


def test_circle_crossing_square_creates_clean_curved_regions(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.004,
        ("rectangle", "z_min"): -0.010,
        ("rectangle", "width"): 0.016,
        ("rectangle", "height"): 0.020,
    })
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("circle", "r_center"): 0.017,
        ("circle", "z_center"): 0.0,
        ("circle", "radius"): 0.006,
    })
    workspace._add_primitive("circle")

    regions = workspace.model["geometry"]["regions"]
    assert len(regions) == 3
    assert all(region["shape"]["type"] == "sketch" for region in regions)
    arc_edges = [
        edge for edge in workspace.model["geometry"]["edges"]
        if edge.get("curve", {}).get("type") == "arc"
    ]
    assert arc_edges
    for edge in arc_edges:
        curve = edge["curve"]
        center = curve["center"]
        radius = curve["radius"]
        for point in curve["sampled_points"]:
            assert math.hypot(point[0] - center[0], point[1] - center[1]) == pytest.approx(
                radius, abs=1e-12
            )

    expected_intersection_z = math.sqrt(0.006**2 - 0.003**2)
    intersection_heights = sorted({
        round(point[1], 12)
        for edge in arc_edges
        for point in (edge["curve"]["sampled_points"][0], edge["curve"]["sampled_points"][-1])
        if math.isclose(point[0], 0.020, abs_tol=1e-12)
    })
    assert intersection_heights == pytest.approx(
        [-expected_intersection_z, expected_intersection_z], abs=1e-12
    )

    # Adjacent faces reference the same split arc interval. Its sampled path
    # must remain ordered and both faces must reuse one selectable edge.
    shared_intervals = {}
    entities_by_id = {
        entity["id"]: entity
        for entity in workspace.model["geometry"].get("sketch_entities", [])
    }
    for region in regions:
        boundary = region.get("shape", {}).get("boundary", [])
        loop_edges = region.get("boundary_loops", [{}])[0].get("edges", [])
        for reference, loop_edge in zip(boundary, loop_edges):
            entity = entities_by_id.get(reference.get("entity_id"), {})
            if entity.get("type") != "arc":
                continue
            interval = tuple(sorted((
                round(float(reference.get("start_t", 0.0)), 12),
                round(float(reference.get("end_t", 1.0)), 12),
            )))
            key = (reference["entity_id"], interval)
            edge = next(
                edge for edge in workspace.model["geometry"]["edges"]
                if edge["id"] == loop_edge["edge_id"]
            )
            if key in shared_intervals:
                assert edge["id"] == shared_intervals[key]["id"]
            else:
                shared_intervals[key] = edge
            samples = edge["curve"].get("sampled_points", [])
            assert all(
                math.dist(first, second) < 0.001
                for first, second in zip(samples, samples[1:])
            )

    # Intersecting faces touch at their shared boundaries, so none should be
    # misclassified as a nested child. Curved faces still render their sampled
    # arc contour rather than a chord between intersection points.
    assert all(region.get("parent_id") is None for region in regions)
    contours = [
        workspace._canvas_region_path_data(
            region, project=lambda point: (point[0] * 1e6, point[1] * 1e6)
        )
        for region in regions
    ]
    assert max(contour.count("L") for contour in contours) >= 40


@pytest.mark.parametrize("curve_kind", ["three-point-arc", "tangent-arc", "spline3", "spline"])
def test_sketch_curve_dividers_intersect_the_exact_curve(curve_kind):
    from ngsolve_gui.axisymmetric_model import evaluate_sketch_entity, find_closed_sketch_loops

    start = (0.002, 0.002)
    if curve_kind == "three-point-arc":
        end = (0.008, 0.002)
        entity = {
            "id": "curve", "type": "arc",
            "points": [start, (0.005, 0.005), end],
            "center": (0.005, 0.002), "radius": 0.003, "clockwise": True,
        }
    elif curve_kind == "tangent-arc":
        end = (0.005, 0.005)
        entity = {
            "id": "curve", "type": "arc",
            "points": [start, (0.00412132034356, 0.00287867965644), end],
            "center": (0.002, 0.005), "radius": 0.003, "clockwise": False,
        }
    elif curve_kind == "spline3":
        end = (0.008, 0.002)
        entity = {"id": "curve", "type": "spline3", "points": [start, (0.005, 0.009), end]}
    else:
        end = (0.008, 0.002)
        entity = {
            "id": "curve", "type": "spline",
            "points": [start, (0.003, 0.006), (0.006, 0.007), end],
        }

    subdivisions = 256 if entity["type"] in {"arc", "spline3"} else 5 * (len(entity["points"]) - 1)
    entity["sampled_points"] = [
        list(evaluate_sketch_entity(entity, index / subdivisions))
        for index in range(subdivisions + 1)
    ]
    expected_t = 0.37
    expected_cut = evaluate_sketch_entity(entity, expected_t)
    base_midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
    divider_end = (
        expected_cut[0] + 0.5 * (expected_cut[0] - base_midpoint[0]),
        expected_cut[1] + 0.5 * (expected_cut[1] - base_midpoint[1]),
    )
    base = {
        "id": "base", "type": "line", "points": [end, start],
        "sampled_points": [end, start],
    }
    divider = {
        "id": "divider", "type": "line",
        "points": [base_midpoint, divider_end],
        "sampled_points": [base_midpoint, divider_end],
    }

    loops = find_closed_sketch_loops([entity, base, divider])
    assert len(loops) == 2
    cut_vertices = [
        point for loop in loops for point in loop["vertices"]
        if math.dist(point, expected_cut) < 1e-12
    ]
    assert cut_vertices
    assert min(math.dist(point, expected_cut) for point in cut_vertices) < 1e-12
    cut_parameters = [
        value
        for loop in loops
        for reference in loop["boundary"]
        if reference["entity_id"] == "curve"
        for value in (reference["start_t"], reference["end_t"])
        if math.isclose(value, expected_t, abs_tol=1e-9)
    ]
    assert cut_parameters


def test_sketch_diameter_splits_a_primitive_circle_into_two_regions(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("circle", "r_center"): 0.010,
        ("circle", "z_center"): 0.0,
        ("circle", "radius"): 0.004,
    })
    workspace._add_primitive("circle")
    original = workspace.model["geometry"]["regions"][0]
    original_id = original["id"]
    original["material_id"] = "material-copper"
    original["mechanical"] = True
    original["sources"]["dc_current_density"] = "2e6"

    angle = math.radians(33.0)
    half_chord = (0.004 * math.cos(angle), 0.004 * math.sin(angle))
    line_start = (0.010 - half_chord[0], -half_chord[1])
    line_end = (0.010 + half_chord[0], half_chord[1])
    assert workspace._add_sketch_entity("line", [line_start, line_end])

    regions = workspace.model["geometry"]["regions"]
    assert len(regions) == 2
    assert all(region["shape"]["type"] == "sketch" for region in regions)
    assert original_id in {region["id"] for region in regions}
    assert all(region["material_id"] == "material-copper" for region in regions)
    assert all(region["mechanical"] is True for region in regions)
    assert all(region["sources"]["dc_current_density"] == "2e6" for region in regions)

    line_entity = next(entity for entity in workspace.model["geometry"]["sketch_entities"]
                       if entity.get("type") == "line")
    line_edges = [edge for edge in workspace.model["geometry"]["edges"]
                  if edge.get("sketch_entity_id") == line_entity["id"]]
    assert len(line_edges) == 1
    assert all(line_edges[0]["id"] in region["edge_ids"] for region in regions)
    assert all(any(reference["entity_id"] == line_entity["id"]
                   for reference in region["shape"]["boundary"]) for region in regions)
    assert any(edge.get("curve", {}).get("type") == "arc"
               for edge in workspace.model["geometry"]["edges"])


def _add_rectangle_with_crossing_divider(workspace):
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.002,
        ("rectangle", "z_min"): 0.002,
        ("rectangle", "width"): 0.006,
        ("rectangle", "height"): 0.006,
    })
    workspace._add_primitive("rectangle")
    assert workspace._add_sketch_entity("line", [(0.001, 0.005), (0.009, 0.005)])
    divider = next(
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == "line"
        and entity.get("points", [])[0][1] == pytest.approx(0.005)
        and entity.get("points", [])[-1][1] == pytest.approx(0.005)
        and min(point[0] for point in entity["points"]) == pytest.approx(0.001)
    )
    return divider


def test_click_selects_and_deletes_the_complete_divider_without_deleting_its_faces(standalone_components):
    workspace = SolveWorkspace()
    divider = _add_rectangle_with_crossing_divider(workspace)
    geometry = workspace.model["geometry"]
    faces = [region for region in geometry["regions"] if region.get("shape", {}).get("type") == "sketch"]
    assert len(faces) == 2
    divider_edges = [edge for edge in geometry["edges"] if edge.get("sketch_entity_id") == divider["id"]]
    assert len(divider_edges) >= 3

    workspace.select_edge(divider_edges[0]["id"])
    assert set(workspace.selected_edge_ids) == {edge["id"] for edge in divider_edges}
    workspace.select_edge(divider_edges[1]["id"], additive=True)
    assert workspace.selected_edge_ids == []
    workspace.select_edge(divider_edges[1]["id"])
    assert workspace.delete_selected_sketch_edges()

    geometry = workspace.model["geometry"]
    remaining_faces = [region for region in geometry["regions"] if region.get("shape", {}).get("type") == "sketch"]
    assert len(remaining_faces) == 1
    assert not any(entity.get("id") == divider["id"] for entity in geometry["sketch_entities"])
    assert len(geometry["sketch_entities"]) == 4
    vertices = remaining_faces[0]["vertices"]
    area = abs(sum(
        vertices[index][0] * vertices[(index + 1) % len(vertices)][1]
        - vertices[(index + 1) % len(vertices)][0] * vertices[index][1]
        for index in range(len(vertices))
    ) / 2.0)
    assert area == pytest.approx(0.006 * 0.006)


def test_power_trim_removes_each_external_divider_tail_but_preserves_both_faces(standalone_components):
    workspace = SolveWorkspace()
    divider = _add_rectangle_with_crossing_divider(workspace)
    assert len([region for region in workspace.model["geometry"]["regions"]
                if region.get("shape", {}).get("type") == "sketch"]) == 2

    assert workspace._trim_sketch_entities_along_stroke([(0.0015, 0.0045), (0.0015, 0.0055)])
    faces = [region for region in workspace.model["geometry"]["regions"] if region.get("shape", {}).get("type") == "sketch"]
    assert len(faces) == 2
    horizontal = [
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == "line"
        and len(entity.get("points", [])) == 2
        and all(abs(point[1] - 0.005) < 1e-10 for point in entity["points"])
    ]
    spans = sorted(tuple(sorted(point[0] for point in entity["points"])) for entity in horizontal)
    assert len(spans) == 2
    assert spans[0] == pytest.approx((0.002, 0.008))
    assert spans[1] == pytest.approx((0.008, 0.009))

    right_tail = next(entity for entity in horizontal if entity["id"] != divider["id"])
    midpoint = sum(point[0] for point in right_tail["points"]) / 2
    assert workspace._trim_sketch_entities_along_stroke([(midpoint, 0.0045), (midpoint, 0.0055)])
    faces = [region for region in workspace.model["geometry"]["regions"] if region.get("shape", {}).get("type") == "sketch"]
    assert len(faces) == 2
    horizontal = [
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == "line"
        and len(entity.get("points", [])) == 2
        and all(abs(point[1] - 0.005) < 1e-10 for point in entity["points"])
    ]
    assert len(horizontal) == 1
    assert tuple(sorted(point[0] for point in horizontal[0]["points"])) == pytest.approx((0.002, 0.008))


def test_deleting_a_simple_region_removes_its_outline(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region_id = workspace.model["geometry"]["regions"][0]["id"]
    assert workspace.delete_regions([region_id])
    assert workspace.model["geometry"]["regions"] == []
    assert workspace.model["geometry"]["edges"] == []


def test_sketch_tangent_and_spline3_arcs_close_regions(standalone_components):
    for variant in ("tangent", "spline3"):
        workspace = SolveWorkspace()
        start, end = (0.006, 0.0), (0.006, 0.004)
        assert workspace._add_sketch_entity("line", [(0.002, 0.0), start])
        if variant == "tangent":
            workspace._accept_arc_sketch_point(start, variant="tangent")
            assert workspace._accept_arc_sketch_point(end, variant="tangent")
        else:
            assert workspace._accept_arc_sketch_point(start, variant="spline3")
            assert workspace._accept_arc_sketch_point(end, variant="spline3")
            assert workspace._accept_arc_sketch_point((0.008, 0.002), variant="spline3")
        assert workspace._add_sketch_entity("line", [end, (0.002, 0.004)])
        assert workspace._add_sketch_entity("line", [(0.002, 0.004), (0.002, 0.0)])
        regions = [item for item in workspace.model["geometry"]["regions"]
                   if item.get("shape", {}).get("type") == "sketch"]
        assert len(regions) == 1
        arc_type = "arc" if variant == "tangent" else "spline3"
        assert any(edge.get("curve", {}).get("type") == arc_type
                   for edge in workspace.model["geometry"]["edges"])


def test_circle_three_point_preview_uses_three_circumference_points(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    scripts = []
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: SimpleNamespace(eval=scripts.append)), raising=False)
    workspace._install_canvas_pointer_capture()
    script = scripts[0]
    assert "circlePoints" in script
    assert "solve-sketch-drag-circle" in script
    assert "const denominator = 2 * (first[0]" in script
    assert 'previewCircle.setAttribute("cx"' in script
    assert 'previewCircle.setAttribute("cy"' in script
    assert 'previewCircle.setAttribute("r"' in script


def test_delete_region_after_select_removes_orphaned_sketch_curves(standalone_components):
    workspace = SolveWorkspace()
    for first, second in (
        ((0.002, 0.0), (0.008, 0.0)),
        ((0.008, 0.0), (0.008, 0.006)),
        ((0.008, 0.006), (0.002, 0.006)),
        ((0.002, 0.006), (0.002, 0.0)),
    ):
        assert workspace._add_sketch_entity("line", [first, second])
    region_id = next(item["id"] for item in workspace.model["geometry"]["regions"]
                     if item.get("shape", {}).get("type") == "sketch")
    assert workspace.delete_regions([region_id])
    assert not workspace.model["geometry"]["regions"]
    assert not workspace.model["geometry"]["edges"]
    assert not workspace.model["geometry"]["sketch_entities"]


def test_boundary_stage_blank_drag_is_marquee_and_shape_tool_remains_drawable(standalone_components):
    workspace = SolveWorkspace()
    workspace.select_section("boundaries")
    workspace.set_sketch_tool("rectangle", announce=False)
    project, _ = workspace._canvas_projection()
    start, end = project((0.003, 0.002)), project((0.008, 0.007))
    points = iter((start, (start[0] + 20.0, start[1] + 20.0), end))
    workspace._canvas_event_point = lambda event, refresh_transform=False: next(points)
    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))
    assert workspace._canvas_drag["operation"] == "draw"
    workspace._on_canvas_mouse_move(SimpleNamespace(value={}))
    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))
    assert len(workspace.model["geometry"]["regions"]) == 1

    workspace.set_sketch_tool("select", announce=False)
    workspace._canvas_event_point = lambda event, refresh_transform=False: (10.0, 10.0)
    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))
    assert workspace._canvas_drag["operation"] == "marquee"



def test_static_electromagnetic_study_has_no_frequency_input(standalone_components):
    workspace = SolveWorkspace()
    workspace.model["physics"]["dc_magnetic"]["enabled"] = True
    workspace.model["physics"]["harmonic_electromagnetic"]["enabled"] = False

    def walk(component):
        yield component
        for child in getattr(component, "ui_children", []) or []:
            yield from walk(child)

    labels = [
        str(getattr(component, "ui_label", ""))
        for component in walk(Div(*workspace._study_properties()))
    ]
    assert "Frequency points (comma separated)" not in labels
    assert any(
        getattr(component, "ui_children", None)
        == ["Static electromagnetic study; no frequency is required."]
        for component in walk(Div(*workspace._study_properties()))
    )



def test_marquee_selects_multiple_regions_in_one_window(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.02,
        ("rectangle", "z_min"): 0.02,
        ("rectangle", "width"): 0.006,
        ("rectangle", "height"): 0.007,
    })
    workspace._add_primitive("rectangle")
    regions = workspace.model["geometry"]["regions"]
    project, _ = workspace._canvas_projection()
    projected = [
        project(point)
        for region in regions
        for point in region["vertices"]
    ]
    box = (
        min(point[0] for point in projected) - 2,
        min(point[1] for point in projected) - 2,
        max(point[0] for point in projected) + 2,
        max(point[1] for point in projected) + 2,
    )

    workspace._select_edges_in_canvas_box(
        (box[0], box[1]), (box[2], box[3])
    )

    assert set(workspace.selected_region_ids) == {
        region["id"] for region in regions
    }
    assert len(workspace.selected_edge_ids) == 8
    assert len(workspace.selected_edge_ids) == len(set(workspace.selected_edge_ids))


def test_boundary_condition_add_button_is_at_the_top_of_properties(standalone_components):
    workspace = SolveWorkspace()
    properties = workspace._boundary_properties()
    add_button = next(
        component for component in properties
        if getattr(component, "ui_label", None) == "Add boundary condition"
    )

    assert properties.index(add_button) <= 1
    assert not hasattr(workspace, "_boundary_apply_selection_button")


def test_setup_validation_survives_malformed_nested_sections(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._set_region_value(region["id"], "material_id", "material-copper")
    region["mechanical"] = True
    workspace.model["physics"]["mechanics"]["enabled"] = True
    workspace.model["parameters"] = None
    workspace.model["materials"] = None
    workspace.model["boundary_conditions"] = None
    workspace.model["geometry"]["edges"] = None

    errors = workspace.validation_errors()
    assert any("Parameters must be a list" in error for error in errors)
    assert any("Materials must be a list" in error for error in errors)
    assert any("Boundary conditions must be a list" in error for error in errors)
    assert any("Geometry edges must be a list" in error for error in errors)
    assert workspace.mesh_validation_errors()

    workspace.model["physics"]["dc_magnetic"] = "malformed"
    errors = workspace.validation_errors()
    results = workspace._setup_check_results(errors)
    assert any("Physics setting" in error for error in errors)
    assert len(results) == 13


def test_boundary_condition_fields_follow_selected_type_and_save_values(standalone_components):
    def components(items):
        for item in items:
            yield item
            yield from components(getattr(item, "ui_children", []))

    def labels(items):
        return [
            item.ui_label for item in components(items)
            if getattr(item, "ui_label", None)
        ]

    workspace = SolveWorkspace()
    workspace._open_boundary_editor()
    assert workspace._editor_drawer["kind"] == "boundary"

    workspace._set_editor_draft("type", SimpleNamespace(value="mechanical_prescribed"))
    controls = list(components(workspace._inspector.ui_children))
    field_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in controls if getattr(item, "ui_label", None)
    }
    assert field_units["Radial displacement"] == "m"
    assert field_units["Axial displacement"] == "m"

    workspace._set_editor_draft("type", SimpleNamespace(value="mechanical_traction"))
    controls = list(components(workspace._inspector.ui_children))
    field_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in controls if getattr(item, "ui_label", None)
    }
    assert field_units["Radial traction"] == "N/m" + chr(178)
    assert field_units["Axial traction"] == "N/m" + chr(178)

    workspace._set_editor_draft("type", SimpleNamespace(value="mechanical_robin"))
    controls = list(components(workspace._inspector.ui_children))
    field_units = {
        item.ui_label: getattr(item, "ui_suffix", None)
        for item in controls if getattr(item, "ui_label", None)
    }
    assert field_units["Normal stiffness"] == "N/m" + chr(179)
    assert field_units["Tangential stiffness"] == "N/m" + chr(179)

    for condition_type in (
        "mechanical_fixed",
        "axis_of_symmetry",
        "magnetic_potential_zero",
        "natural",
    ):
        workspace._set_editor_draft("type", SimpleNamespace(value=condition_type))
        labels_for_type = labels(workspace._inspector.ui_children)
        assert "Radial displacement" not in labels_for_type
        assert "Axial displacement" not in labels_for_type
        assert "Radial traction" not in labels_for_type
        assert "Axial traction" not in labels_for_type
        assert "Normal stiffness" not in labels_for_type
        assert "Tangential stiffness" not in labels_for_type

    for condition_type, values in (
        ("mechanical_prescribed", {"displacement_r": 0, "displacement_z": "-2e-4"}),
        ("mechanical_traction", {"traction_r": "12.5", "traction_z": "-3"}),
        ("mechanical_robin", {"stiffness_normal": "4e6", "stiffness_tangential": "2e6"}),
    ):
        if workspace.model["boundary_conditions"]:
            workspace._open_boundary_editor()
        workspace._set_editor_draft("type", SimpleNamespace(value=condition_type))
        for key, value in values.items():
            workspace._set_editor_draft(key, SimpleNamespace(value=value))
        workspace._commit_editor()
        condition = workspace.model["boundary_conditions"][-1]
        assert condition["type"] == condition_type
        for key, value in values.items():
            assert condition[key] == str(value)


def _workspace_with_two_regions():
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    first = workspace.model["geometry"]["regions"][0]
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.02,
        ("rectangle", "z_min"): 0.01,
        ("rectangle", "width"): 0.006,
        ("rectangle", "height"): 0.007,
    })
    workspace._add_primitive("rectangle")
    second = workspace.model["geometry"]["regions"][1]
    return workspace, first, second


def test_ctrl_or_shift_region_selection_toggles_regions_and_expands_groups(standalone_components):
    workspace, first, second = _workspace_with_two_regions()

    workspace.select_region(first["id"])
    assert workspace.selected_region_ids == [first["id"]]
    workspace.select_region(second["id"], additive=True)
    assert set(workspace.selected_region_ids) == {first["id"], second["id"]}
    workspace.select_region(first["id"], additive=True)
    assert workspace.selected_region_ids == [second["id"]]
    workspace.select_region(first["id"], additive=True)
    assert set(workspace.selected_region_ids) == {first["id"], second["id"]}

    assert workspace.group_selected_regions()
    group = workspace.model["region_groups"][0]
    assert set(group["region_ids"]) == {first["id"], second["id"]}
    workspace.select_region(first["id"])
    assert set(workspace.selected_region_ids) == {first["id"], second["id"]}
    assert workspace.ungroup_selected_regions()
    workspace.select_region(first["id"])
    assert workspace.selected_region_ids == [first["id"]]


def test_canvas_context_menu_offers_group_delete_ungroup_for_current_selection(standalone_components, monkeypatch):
    workspace, first, second = _workspace_with_two_regions()
    workspace.select_region(first["id"])
    workspace.select_region(second["id"], additive=True)
    workspace.render_canvas()

    context_menu = workspace._canvas_context_menu
    assert context_menu in workspace._canvas_host.ui_children
    assert context_menu.__class__.__name__ == "Div"
    assert context_menu._id == "solve-sketch-context-menu"
    assert context_menu._props["class"] == "solve-sketch-context-menu"
    assert "display:none" in context_menu._props["style"]

    def strings(component):
        if isinstance(component, str):
            return [component]
        return [text for child in getattr(component, "ui_children", []) for text in strings(child)]

    labels = strings(context_menu)
    assert "Delete selected regions" in labels
    assert "Group selected regions" in labels
    assert "Ungroup selected regions" in labels
    assert set(workspace.selected_region_ids) == {first["id"], second["id"]}
    for region_id in (first["id"], second["id"]):
        assert workspace._canvas_region_nodes[region_id]._props["data-sketch-region-selected"] == "true"

    scripts = []
    monkeypatch.setattr(SolveWorkspace, "js", property(lambda self: SimpleNamespace(eval=scripts.append)), raising=False)
    workspace._install_canvas_pointer_capture()
    assert "document.addEventListener('contextmenu', openSketchContextMenu, true)" in scripts[0]
    assert "[data-sketch-region-selected=\"true\"]" in scripts[0]
    assert "event.clientX - bounds.left" in scripts[0]
    assert "event.key === 'Escape'" in scripts[0]
    assert "__ngsolveSketchCompletedGesture" in scripts[0]
    assert "new MouseEvent('mouseup'" in scripts[0]
    assert "svg.dispatchEvent(forwarded)" in scripts[0]
    assert "svg.focus({preventScroll: true})" in scripts[0]


def test_group_deletion_removes_all_selected_regions_and_stale_assignments(standalone_components):
    workspace, first, second = _workspace_with_two_regions()
    workspace.model["regional_loads"] = [{"id": "load-test", "name": "Coil current", "region_ids": [first["id"], second["id"]]}]
    workspace.model["mesh"]["region_element_sizes"] = {first["id"]: "0.001", second["id"]: "0.002"}
    workspace.model["mesh"]["hp_region_ids"] = [first["id"], second["id"]]
    workspace._sync_region_sources_from_loads()
    workspace.select_region(first["id"])
    workspace.select_region(second["id"], additive=True)
    workspace.group_selected_regions()

    assert workspace.delete_selected_regions()
    assert workspace.model["geometry"]["regions"] == []
    assert workspace.model["region_groups"] == []
    assert workspace.model["regional_loads"][0]["region_ids"] == []
    assert workspace.model["mesh"]["region_element_sizes"] == {}
    assert workspace.model["mesh"]["hp_region_ids"] == []


def test_group_selection_applies_material_and_mechanical_setting_in_one_action(standalone_components):
    workspace, first, second = _workspace_with_two_regions()
    workspace.select_region(first["id"])
    workspace.select_region(second["id"], additive=True)
    workspace.group_selected_regions()
    workspace.select_section("materials")
    workspace._bulk_material_choice = "material-copper"
    workspace._bulk_mechanical_choice = "true"

    assert workspace._apply_bulk_region_material()
    assert all(region["material_id"] == "material-copper" for region in (first, second))
    assert all(region["mechanical"] for region in (first, second))
    bulk_controls = workspace._material_properties()
    assert any(getattr(item, "ui_label", "").startswith("Material for 2 selected") for item in bulk_controls)


def test_grouped_region_toggles_source_assignment_for_every_member(standalone_components):
    workspace, first, second = _workspace_with_two_regions()
    workspace.select_region(first["id"])
    workspace.select_region(second["id"], additive=True)
    workspace.group_selected_regions()
    workspace.model["regional_loads"] = [{
        "id": "load-test", "name": "Coil current", "region_ids": [],
        "dc_current_density": "1200000", "ac_current_density_real": "0",
        "ac_current_density_imaginary": "0", "mechanical_body_force": {"r": "0", "z": "0"},
    }]
    workspace.active_section = "sources"
    workspace.selected_source_id = "load-test"

    assert workspace._toggle_active_source_region(first["id"])
    assert set(workspace.model["regional_loads"][0]["region_ids"]) == {first["id"], second["id"]}
    assert workspace._toggle_active_source_region(second["id"])
    assert workspace.model["regional_loads"][0]["region_ids"] == []



def test_delete_key_removes_only_marquee_selected_edge(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    edge = next(item for item in workspace.model["geometry"]["edges"] if item["id"] == region["edge_ids"][0])
    project, _ = workspace._canvas_projection()
    first, second = [project(point) for point in edge["vertices"]]
    box = (
        min(first[0], second[0]) - 2,
        min(first[1], second[1]) - 2,
        max(first[0], second[0]) + 2,
        max(first[1], second[1]) + 2,
    )

    workspace._select_edges_in_canvas_box(
        (box[0], box[1]), (box[2], box[3])
    )

    assert workspace.selected_region_ids == []
    assert workspace.selected_edge_ids == [edge["id"]]
    assert workspace.delete_selected_geometry()

    geometry = workspace.model["geometry"]
    assert geometry["regions"] == []
    assert len(geometry["edges"]) == 3
    assert all(item.get("sketch_entity_id") for item in geometry["edges"])
    assert edge["id"] not in {item["id"] for item in geometry["edges"]}


def test_delete_inner_region_preserves_a_valid_hole(standalone_components):
    from ngsolve_gui.axisymmetric_model import validate_model

    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    outer = workspace.model["geometry"]["regions"][0]
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.003,
        ("rectangle", "z_min"): 0.003,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.005,
    })
    workspace._add_primitive("rectangle")
    inner = workspace.model["geometry"]["regions"][1]
    assert inner["parent_id"] == outer["id"]

    workspace.select_region(inner["id"])
    assert workspace.delete_selected_geometry()

    geometry = workspace.model["geometry"]
    assert [region["id"] for region in geometry["regions"]] == [outer["id"]]
    assert len(geometry["voids"]) == 1
    hole = geometry["voids"][0]
    assert hole["parent_id"] == outer["id"]
    assert set(hole["edge_ids"]).issubset({edge["id"] for edge in geometry["edges"]})
    outer = geometry["regions"][0]
    assert any(
        loop.get("role") == "hole"
        and loop.get("void_id") == hole["id"]
        for loop in outer.get("boundary_loops", [])
    )
    assert validate_model(workspace.model, scope="mesh") == []



def test_delete_key_removes_all_selected_regions(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    first = workspace.model["geometry"]["regions"][0]
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.02,
        ("rectangle", "z_min"): 0.0,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.005,
    })
    workspace._add_primitive("rectangle")
    second = workspace.model["geometry"]["regions"][1]

    workspace.select_region(first["id"])
    workspace.select_region(second["id"], additive=True)

    assert set(workspace._selected_region_ids_live()) == {first["id"], second["id"]}
    assert workspace.delete_selected_geometry()
    assert workspace.model["geometry"]["regions"] == []



def test_run_history_uses_configured_shared_vm_output_root(monkeypatch, tmp_path):
    shared_root = tmp_path / "shared-results"
    monkeypatch.setenv("COUPLED_SOLVER_OUTPUT_DIR", str(shared_root))
    run_root = shared_root / "gui_runs"
    run_directory = run_root / "imported_model_20261014"
    run_directory.mkdir(parents=True)
    (run_directory / "run_manifest.json").write_text(
        json.dumps({
            "schema": "mm-fem.run",
            "kind": "study",
            "status": "complete",
            "model_name": "Imported model",
        }),
        encoding="utf-8",
    )

    assert default_run_history_root() == run_root
    runs = _load_saved_run_history()
    assert [run["output_path"] for run in runs] == [str(run_directory)]


def test_run_history_defaults_to_home_results_directory_without_vm_override(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("COUPLED_SOLVER_OUTPUT_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert default_run_history_root() == tmp_path / "NGSolve_results"


def test_centerpoint_arc_snaps_onto_existing_endpoint_without_open_gap(standalone_components):
    workspace = SolveWorkspace()
    start, end = (0.00942, -0.00175), (0.01241, -0.00175)
    center = (0.01092, -0.00175)

    assert workspace._add_sketch_entity("line", [start, end])
    assert workspace._accept_arc_sketch_point(center, variant="centerpoint")
    assert workspace._accept_arc_sketch_point(start, variant="centerpoint")
    assert workspace._accept_arc_sketch_point(end, variant="centerpoint")

    arc = next(
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == "arc"
    )
    assert arc["points"][-1] == list(end)
    assert math.dist(arc["center"], start) == pytest.approx(math.dist(arc["center"], end))
    assert len(workspace.model["geometry"]["regions"]) == 1


def test_power_trim_of_both_tails_on_arc_divider_preserves_both_faces(standalone_components):
    workspace = SolveWorkspace()
    start, end = (0.00942, -0.00175), (0.01241, -0.00175)
    center = (0.01092, -0.00175)
    assert workspace._add_sketch_entity("line", [start, end])
    assert workspace._accept_arc_sketch_point(center, variant="centerpoint")
    assert workspace._accept_arc_sketch_point(start, variant="centerpoint")
    assert workspace._accept_arc_sketch_point(end, variant="centerpoint")
    arc = next(entity for entity in workspace.model["geometry"]["sketch_entities"] if entity.get("type") == "arc")
    cx, cz = arc["center"]
    assert workspace._add_sketch_entity("line", [(cx, cz + 0.001), (cx, cz - 0.0025)])
    assert len(workspace.model["geometry"]["regions"]) == 2

    assert workspace._trim_sketch_entities_along_stroke([(cx - 0.0005, cz + 0.0005), (cx + 0.0005, cz + 0.0005)])
    assert len(workspace.model["geometry"]["regions"]) == 2
    assert workspace._trim_sketch_entities_along_stroke([(cx - 0.0005, cz - 0.002), (cx + 0.0005, cz - 0.002)])
    assert len(workspace.model["geometry"]["regions"]) == 2


def test_power_trim_preserves_both_faces_at_a_spline_divider_intersection(standalone_components):
    from ngsolve_gui.axisymmetric_model import evaluate_sketch_entity

    workspace = SolveWorkspace()
    start, end, control = (0.006, 0.003), (0.012, 0.003), (0.009, 0.006)
    assert workspace._add_sketch_entity("line", [start, end])
    assert workspace._add_sketch_entity("spline3", [start, control, end])
    spline = next(entity for entity in workspace.model["geometry"]["sketch_entities"]
                  if entity.get("type") == "spline3")
    hit = evaluate_sketch_entity(spline, 0.371)
    divider_x = hit[0]
    assert workspace._add_sketch_entity("line", [(divider_x, 0.002), (divider_x, 0.005)])
    assert len(workspace.model["geometry"]["regions"]) == 2

    assert workspace._trim_sketch_entities_along_stroke([(divider_x - 0.0005, 0.0025), (divider_x + 0.0005, 0.0025)])
    assert len(workspace.model["geometry"]["regions"]) == 2
    assert workspace._trim_sketch_entities_along_stroke([(divider_x - 0.0005, 0.0048), (divider_x + 0.0005, 0.0048)])
    assert len(workspace.model["geometry"]["regions"]) == 2


def _make_trim_test_split_rectangle(workspace):
    lower_left = (0.004, 0.002)
    divider_bottom = (0.012, 0.002)
    lower_right = (0.020, 0.002)
    upper_right = (0.020, 0.007)
    divider_top = (0.012, 0.007)
    upper_left = (0.004, 0.007)
    definitions = [
        ("trim-bottom", lower_left, lower_right),
        ("trim-right", lower_right, upper_right),
        ("trim-top", upper_right, upper_left),
        ("trim-left", upper_left, lower_left),
        ("trim-divider", divider_bottom, divider_top),
    ]
    entities = [
        {"id": entity_id, "type": "line", "points": [list(start), list(end)],
         "sampled_points": [list(start), list(end)]}
        for entity_id, start, end in definitions
    ]
    workspace.model["geometry"]["regions"] = []
    workspace.model["geometry"]["edges"] = []
    workspace.model["geometry"]["sketch_entities"] = entities
    workspace._refresh_sketch_generated_regions()
    assert len(workspace.model["geometry"]["regions"]) == 2
    return {entity["id"]: entity for entity in entities}


def test_trim_right_side_of_split_base_stops_at_the_divider(standalone_components):
    workspace = SolveWorkspace()
    entities = _make_trim_test_split_rectangle(workspace)
    right_base_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge.get("sketch_entity_id") == "trim-bottom"
        and sum(point[0] for point in edge.get("vertices", [])) / 2 > 0.012
    )

    assert workspace._trim_sketch_entities_along_stroke(
        [(0.016, 0.0015), (0.016, 0.0025)], target_edge_id=right_base_edge["id"]
    )

    remaining = {entity["id"]: entity for entity in workspace.model["geometry"]["sketch_entities"]}
    assert remaining["trim-bottom"]["points"] == [[0.004, 0.002], [0.012, 0.002]]
    assert "trim-divider" in remaining
    assert len(workspace.model["geometry"]["regions"]) == 1
    remaining_edges = {edge.get("sketch_entity_id") for edge in workspace.model["geometry"]["edges"]}
    assert {"trim-right", "trim-top", "trim-left", "trim-divider"} <= remaining_edges


def test_trim_internal_vertical_divider_preserves_the_outer_geometry(standalone_components):
    workspace = SolveWorkspace()
    entities = _make_trim_test_split_rectangle(workspace)
    divider_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge.get("sketch_entity_id") == "trim-divider"
    )

    assert workspace._trim_sketch_entities_along_stroke(
        [(0.011, 0.0045), (0.013, 0.0045)], target_edge_id=divider_edge["id"]
    )

    remaining_ids = {entity["id"] for entity in workspace.model["geometry"]["sketch_entities"]}
    assert "trim-divider" not in remaining_ids
    assert {"trim-bottom", "trim-right", "trim-top", "trim-left"} <= remaining_ids
    assert len(workspace.model["geometry"]["regions"]) == 1
    region_entity_ids = set(workspace.model["geometry"]["regions"][0]["shape"]["entity_ids"])
    assert region_entity_ids == {"trim-bottom", "trim-right", "trim-top", "trim-left"}



def test_scoped_sketch_refresh_ignores_unrelated_closed_curves(standalone_components):
    workspace = SolveWorkspace()
    entities = _make_trim_test_split_rectangle(workspace)
    outer_ids = {"trim-bottom", "trim-right", "trim-top", "trim-left"}
    triangle = [
        {"id": "loose-a", "type": "line", "points": [[0.030, 0.002], [0.032, 0.002]],
         "sampled_points": [[0.030, 0.002], [0.032, 0.002]]},
        {"id": "loose-b", "type": "line", "points": [[0.032, 0.002], [0.031, 0.004]],
         "sampled_points": [[0.032, 0.002], [0.031, 0.004]]},
        {"id": "loose-c", "type": "line", "points": [[0.031, 0.004], [0.030, 0.002]],
         "sampled_points": [[0.031, 0.004], [0.030, 0.002]]},
    ]
    workspace.model["geometry"]["sketch_entities"].extend(triangle)
    workspace.model["geometry"]["regions"] = [
        region for region in workspace.model["geometry"]["regions"]
        if set(region.get("shape", {}).get("entity_ids", [])) <= outer_ids
    ]

    workspace._refresh_sketch_generated_regions(entity_ids=outer_ids)

    assert len(workspace.model["geometry"]["regions"]) == 1
    assert set(workspace.model["geometry"]["regions"][0]["shape"]["entity_ids"]) == outer_ids
    assert {item["id"] for item in workspace.model["geometry"]["sketch_entities"]} >= outer_ids | {
        "loose-a", "loose-b", "loose-c"
    }


def test_trim_ignores_near_coincident_curve_tessellation_crossings(standalone_components, monkeypatch):
    workspace = SolveWorkspace()
    count = 65
    start_x, divider_x, end_x = 0.004, 0.012, 0.020
    base_y = 0.003
    target_points = [
        [start_x + (end_x - start_x) * index / (count - 1), base_y]
        for index in range(count)
    ]
    duplicate_points = [
        [point[0], point[1] + 2e-5 * math.sin(8.0 * math.pi * index / (count - 1))]
        for index, point in enumerate(target_points)
    ]
    entities = [
        {"id": "near-target", "type": "spline", "points": target_points,
         "sampled_points": target_points},
        {"id": "near-copy", "type": "spline", "points": duplicate_points,
         "sampled_points": duplicate_points},
        {"id": "trim-left-stop", "type": "line", "points": [[start_x, 0.001], [start_x, 0.005]],
         "sampled_points": [[start_x, 0.001], [start_x, 0.005]]},
        {"id": "trim-divider-stop", "type": "line", "points": [[divider_x, 0.001], [divider_x, 0.005]],
         "sampled_points": [[divider_x, 0.001], [divider_x, 0.005]]},
        {"id": "trim-right-stop", "type": "line", "points": [[end_x, 0.001], [end_x, 0.005]],
         "sampled_points": [[end_x, 0.001], [end_x, 0.005]]},
    ]
    workspace.model["geometry"]["sketch_entities"] = entities
    workspace.model["geometry"]["edges"] = [{
        "id": "near-target-edge", "sketch_entity_id": "near-target",
        "sketch_t0": 0.5, "sketch_t1": 1.0,
        "vertices": [target_points[count // 2], target_points[-1]],
        "curve": {"type": "spline", "sampled_points": target_points[count // 2:]},
    }]
    workspace.model["geometry"]["regions"] = [{
        "id": "near-region", "name": "Near-overlap test",
        "vertices": [[start_x, 0.001], [end_x, 0.001], [end_x, 0.005], [start_x, 0.005]],
        "shape": {"type": "sketch", "entity_ids": [
            "near-target", "trim-left-stop", "trim-divider-stop", "trim-right-stop"
        ], "boundary": [
            {"entity_id": "near-target", "start_t": 0.0, "end_t": 1.0},
            {"entity_id": "trim-left-stop", "start_t": 0.0, "end_t": 1.0},
            {"entity_id": "trim-divider-stop", "start_t": 0.0, "end_t": 1.0},
            {"entity_id": "trim-right-stop", "start_t": 0.0, "end_t": 1.0},
        ]},
    }]
    monkeypatch.setattr(workspace, "_refresh_sketch_generated_regions", lambda **_kwargs: None)

    assert workspace._trim_sketch_entities_along_stroke(
        [(0.016, 0.002), (0.016, 0.004)],
        target_edge_id="near-target-edge",
    )

    remaining = {
        entity["id"]: entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("id") in {"near-target", "near-copy"}
    }
    assert set(remaining) == {"near-target", "near-copy"}
    for entity in remaining.values():
        xs = [point[0] for point in entity["sampled_points"]]
        assert min(xs) == pytest.approx(start_x)
        assert max(xs) == pytest.approx(divider_x, abs=1e-10)
@pytest.mark.parametrize("use_click", [False, True])
@pytest.mark.parametrize("arc_variant", ["centerpoint", "tangent"])
def test_trimming_centerpoint_and_tangent_arc_caps(
    standalone_components, arc_variant, use_click
):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.005,
        ("rectangle", "z_min"): -0.015,
        ("rectangle", "width"): 0.021,
        ("rectangle", "height"): 0.030,
    })
    workspace._add_primitive("rectangle")
    rectangle_region = workspace.model["geometry"]["regions"][0]
    workspace._materialize_primitive_as_sketch(rectangle_region)
    workspace._refresh_sketch_generated_regions()

    rectangle_right = 0.026
    if arc_variant == "centerpoint":
        center, start, end = (0.018, 0.0), (0.018, -0.010), (0.018, 0.010)
        assert workspace._accept_arc_sketch_point(center, variant="centerpoint")
        assert workspace._accept_arc_sketch_point(start, variant="centerpoint")
        assert workspace._accept_arc_sketch_point(end, variant="centerpoint")
    else:
        tangent_start, tangent_end = (0.018, -0.010), (0.018, 0.010)
        assert workspace._add_sketch_entity("line", [tangent_start, tangent_end])
        assert workspace._accept_arc_sketch_point(tangent_end, variant="tangent")
        assert workspace._accept_arc_sketch_point((0.028, 0.006), variant="tangent")

    arc_entity = next(
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == "arc"
    )
    assert max(point[0] for point in arc_entity["sampled_points"]) > rectangle_right

    if use_click:
        candidate_edges = [
            edge for edge in workspace.model["geometry"]["edges"]
            if edge.get("sketch_entity_id") == arc_entity["id"]
        ]
        assert candidate_edges
        target_edge = max(
            candidate_edges,
            key=lambda edge: max(
                point[0]
                for point in (
                    (edge.get("curve") or {}).get("sampled_points")
                    or edge.get("vertices", [])
                )
            ),
        )
        samples = (
            (target_edge.get("curve") or {}).get("sampled_points")
            or target_edge.get("vertices", [])
        )
        trim_point = max(samples, key=lambda point: point[0])
        project, _ = workspace._canvas_projection()
        assert workspace._trim_sketch_edge_at_canvas_point(
            target_edge["id"], project(trim_point)
        )
    else:
        samples = arc_entity["sampled_points"]
        outside_index = max(
            range(1, len(samples) - 1),
            key=lambda index: samples[index][0],
        )
        point = samples[outside_index]
        before, after = samples[outside_index - 1], samples[outside_index + 1]
        tangent = (after[0] - before[0], after[1] - before[1])
        tangent_length = math.hypot(*tangent)
        normal = (-tangent[1] / tangent_length, tangent[0] / tangent_length)
        reach = 0.001
        stroke = [
            (point[0] - reach * normal[0], point[1] - reach * normal[1]),
            (point[0] + reach * normal[0], point[1] + reach * normal[1]),
        ]
        assert workspace._trim_sketch_entities_along_stroke(stroke)

    remaining_arcs = [
        entity for entity in workspace.model["geometry"]["sketch_entities"]
        if entity.get("type") == "arc"
    ]
    assert remaining_arcs
    assert all(
        point[0] <= rectangle_right + 1e-8
        for entity in remaining_arcs
        for point in entity.get("sampled_points", [])
    )
