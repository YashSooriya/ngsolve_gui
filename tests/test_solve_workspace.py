import copy
import os
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


def test_centerpoint_arc_projects_end_to_start_radius(standalone_components):
    workspace = SolveWorkspace()
    center, start, cursor = (0.006, 0.001), (0.010, 0.001), (0.006, 0.005)
    workspace._accept_arc_sketch_point(center, variant="centerpoint")
    workspace._accept_arc_sketch_point(start, variant="centerpoint")
    assert workspace._accept_arc_sketch_point(cursor, variant="centerpoint")

    entity = workspace.model["geometry"]["sketch_entities"][0]
    assert math.dist(center, start) == pytest.approx(math.dist(center, entity["points"][-1]))
    assert math.dist(center, start) == pytest.approx(entity["radius"])
    assert math.dist(start, entity["points"][-1]) > 0


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
    assert "spline3Path" in script
    assert "arcPathTangent" in script
    assert "const cancelArc" in script
    assert "solve-sketch-construction-points" in script
    assert "window.__ngsolveRefreshConstructionPreview" in script
    assert "hasPending" in script and "updateConstructionPreview" in script

def test_trim_pencil_stroke_removes_only_curves_it_crosses(standalone_components):
    workspace = SolveWorkspace()
    assert workspace._add_sketch_entity("line", [(0.002, 0.0), (0.008, 0.0)])
    assert workspace._add_sketch_entity("line", [(0.020, 0.004), (0.026, 0.004)])

    assert workspace._trim_sketch_entities_along_stroke([(0.005, -0.001), (0.005, 0.001)])
    remaining = workspace.model["geometry"]["sketch_entities"]
    assert len(remaining) == 1
    assert remaining[0]["points"] == [[0.02, 0.004], [0.026, 0.004]]


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
        workspace._canvas_grid,
        workspace._canvas_live_grid,
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


def test_tangent_regions_are_allowed_and_interior_overlap_is_rejected(standalone_components):
    workspace = SolveWorkspace()
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.0,
        ("rectangle", "z_min"): 0.0,
        ("rectangle", "width"): 0.01,
        ("rectangle", "height"): 0.01,
    })
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.01,
        ("rectangle", "z_min"): 0.003,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.004,
    })
    workspace._add_primitive("rectangle")
    first, second = workspace.model["geometry"]["regions"]
    assert set(first["edge_ids"]) & set(second["edge_ids"])

    overlapping_vertices = [
        [0.009, 0.003], [0.013, 0.003], [0.013, 0.007], [0.009, 0.007]
    ]
    assert workspace._has_unsupported_overlap(overlapping_vertices, None)
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 0.009,
        ("rectangle", "z_min"): 0.003,
        ("rectangle", "width"): 0.004,
        ("rectangle", "height"): 0.004,
    })
    workspace._add_primitive("rectangle")
    assert len(workspace.model["geometry"]["regions"]) == 2
    assert "interiors cannot overlap" in workspace.message


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
    assert "new KeyboardEvent('keydown'" in scripts[0]
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
