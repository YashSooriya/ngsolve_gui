import math

import pytest
from types import SimpleNamespace

from ngapp import utils

from ngsolve_gui.solve_workspace import SolveWorkspace


@pytest.fixture
def standalone_components(monkeypatch):
    environment = utils.Environment(utils.EnvironmentType.STANDALONE, have_backend=False)
    environment.frontend.update_component = lambda *args, **kwargs: None
    monkeypatch.setattr(utils, "_environment", environment)


def test_workspace_builds_nested_regions_and_recomputes_dimension_parameters(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    outer = workspace.model["geometry"]["regions"][0]

    workspace._primitive_values.update({
        ("rectangle", "r_min"): 3.0,
        ("rectangle", "z_min"): 3.0,
        ("rectangle", "width"): 4.0,
        ("rectangle", "height"): 5.0,
    })
    workspace._add_primitive("rectangle")
    coil = workspace.model["geometry"]["regions"][1]
    assert coil["parent_id"] == outer["id"]

    parameter = {"id": "outer-width", "name": "outer_width", "expression": "0.02", "unit": "m"}
    workspace.model["parameters"].append(parameter)
    workspace._set_region_dimension(outer["id"], "width", "outer_width*1000")

    assert outer["shape"]["width"] == 0.02
    assert workspace.validation_errors() == []


def test_same_edge_can_have_independent_em_and_mechanical_conditions(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    workspace._primitive_values.update({
        ("rectangle", "r_min"): 3.0,
        ("rectangle", "z_min"): 3.0,
        ("rectangle", "width"): 4.0,
        ("rectangle", "height"): 5.0,
    })
    workspace._add_primitive("rectangle")
    coil = workspace.model["geometry"]["regions"][1]
    coil["material_id"] = "material-copper"
    coil["mechanical"] = True
    workspace.model["physics"]["mechanics"]["enabled"] = True

    outer_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if "boundary-outer" in edge["boundary_condition_ids"]
    )
    workspace._set_edge_condition(outer_edge["id"], "mechanical", "boundary-fixed")

    assert set(outer_edge["boundary_condition_ids"]) == {"boundary-outer", "boundary-fixed"}
    assert workspace.validation_errors() == []


def test_electromagnetic_run_requires_an_exterior_reference_boundary(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    for edge in list(workspace.model["geometry"]["edges"]):
        if "boundary-outer" in edge["boundary_condition_ids"]:
            workspace._set_edge_condition(edge["id"], "electromagnetic", None)

    assert any("anchor the electromagnetic solution" in error for error in workspace.validation_errors())


def test_drag_sketches_dimensioned_rectangle_and_circle_regions(standalone_components):
    workspace = SolveWorkspace()
    project, _ = workspace._canvas_projection()
    workspace._create_region_from_canvas_drag(
        "rectangle", project((0.0, 0.0)), project((0.020, 0.020))
    )
    rectangle = workspace.model["geometry"]["regions"][0]
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
    assert circle["parent_id"] == rectangle["id"]
    assert circle["shape"]["radius"] == pytest.approx(0.002)
    assert {item["type"] for item in circle["constraints"]} >= {
        "radial_position", "axial_position", "radius"
    }


def test_mouse_drawn_geometry_uses_hundredth_mm_precision(standalone_components):
    workspace = SolveWorkspace()
    project, _ = workspace._canvas_projection()
    workspace._create_region_from_canvas_drag(
        "rectangle",
        project((0.0034567, -0.0023467)),
        project((0.0101234, 0.0078912)),
    )
    rectangle = workspace.model["geometry"]["regions"][0]["shape"]
    assert rectangle["r_min"] * 1000 == pytest.approx(3.46)
    assert rectangle["z_min"] * 1000 == pytest.approx(-2.35)
    assert rectangle["width"] * 1000 == pytest.approx(6.66)
    assert rectangle["height"] * 1000 == pytest.approx(10.24)
    assert rectangle["dimension_expressions"] == {
        "r_min": "3.46",
        "z_min": "-2.35",
        "width": "6.66",
        "height": "10.24",
    }

    circle_workspace = SolveWorkspace()
    project, _ = circle_workspace._canvas_projection()
    circle_workspace._create_region_from_canvas_drag(
        "circle",
        project((0.0200034567, -0.0023467)),
        project((0.0251234, 0.0019879)),
    )
    circle = circle_workspace.model["geometry"]["regions"][0]["shape"]
    assert circle["r_center"] * 1000 == pytest.approx(20.00)
    assert circle["z_center"] * 1000 == pytest.approx(-2.35)
    assert circle["radius"] * 1000 == pytest.approx(6.71)
    assert circle["dimension_expressions"] == {
        "r_center": "20.0",
        "z_center": "-2.35",
        "radius": "6.71",
    }


def test_manual_region_dimensions_keep_precision_beyond_hundredth_mm(standalone_components):
    workspace = SolveWorkspace()
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]

    workspace._set_region_dimension(region["id"], "width", "20.1234")

    assert region["shape"]["width"] == pytest.approx(0.0201234)
    assert region["shape"]["dimension_expressions"]["width"] == "20.1234"


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

    workspace._on_canvas_mouse_down(SimpleNamespace(value={"button": 0}))
    workspace._on_canvas_mouse_move(SimpleNamespace(value={}))
    assert renders == []

    workspace._on_canvas_mouse_up(SimpleNamespace(value={}))
    assert renders == []
    assert len(workspace.selected_edge_ids) == 4


def test_region_creation_updates_canvas_before_side_panels(standalone_components, monkeypatch):
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
    assert all(component in updated_components for component in (
        workspace._left_items,
        workspace._canvas_grid,
        workspace._canvas_scene,
        workspace._canvas_dimensions,
        workspace._region_panel,
        workspace._region_name_input,
    ))
    positions = {id(component): index for index, component in enumerate(updated_components)}
    assert max(positions[id(component)] for component in (
        workspace._canvas_grid,
        workspace._canvas_scene,
        workspace._canvas_dimensions,
    )) < positions[id(workspace._region_panel)]
    assert len(workspace.model["geometry"]["regions"]) == 1


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
    assert workspace._region_name_input.ui_model_value == "Region 1"
    dimension_labels = [
        child._props["textContent"]
        for child in workspace._canvas_dimensions.ui_children
        if child._props.get("textContent")
    ]
    assert dimension_labels == ["W 20 mm", "H 20 mm"]

    edge_id = workspace.model["geometry"]["edges"][0]["id"]
    workspace.select_edge(edge_id)
    assert tuple(workspace._left_items.ui_children) == tree_children
    assert tuple(workspace._inspector.ui_children) == inspector_children
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
    assert workspace._tree_splitter.ui_slot_before == [workspace._tree]
    assert workspace._tree_splitter.ui_slot_after == [workspace._properties_splitter]
    assert workspace._properties_splitter.ui_slot_before == [workspace._canvas_panel]
    assert workspace._properties_splitter.ui_slot_after == [workspace._right]
    assert "flex:1 1 auto" in workspace._tree_splitter._props["style"]
    assert "flex:1 1 auto" in workspace._properties_splitter._props["style"]


def test_blank_axisymmetric_view_starts_at_r_zero_with_world_space_ticks(standalone_components):
    workspace = SolveWorkspace()
    project, unproject = workspace._canvas_projection()

    assert project((0.0, 0.0))[0] == pytest.approx(72.0)
    assert unproject((72.0, 320.0))[0] == pytest.approx(0.0)
    ticks = workspace._coordinate_ticks(0.0, 0.022)
    assert ticks[0] == pytest.approx(0.0)
    steps = {round(b - a, 10) for a, b in zip(ticks, ticks[1:])}
    assert len(steps) == 1
    svg_text = [item for item in workspace._canvas_grid.ui_children if item._component_name == "text"]
    assert any(item._props.get("textContent") == "Axis of rotation  ·  r = 0" for item in svg_text)
    assert all(not item.ui_children for item in svg_text)


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
    workspace.zoom_canvas = lambda factor, point=None: calls.append((factor, point))

    workspace._on_canvas_wheel(SimpleNamespace(value={"deltaY": 120.0}))

    assert calls == [(pytest.approx(math.exp(0.18)), anchor)]


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


def test_grid_snap_button_is_right_aligned_in_the_sketch_toolbar(standalone_components):
    workspace = SolveWorkspace()
    toolbar = workspace._canvas_panel.ui_children[0]
    snap_button = toolbar.ui_slots["default"][-1]

    assert snap_button.ui_icon == "mdi-magnet"
    assert "position:absolute" in snap_button.ui_style
    assert "right:8px" in snap_button.ui_style


def test_added_model_parameter_is_listed_in_tree(standalone_components):
    workspace = SolveWorkspace()
    workspace.select_section("parameters")

    workspace.add_parameter()

    model_group = workspace._left_items.ui_children[0]
    parameter_row = model_group.ui_children[2]
    parameter_list = parameter_row.ui_children[1]
    assert any(child.ui_label == "length_1" for child in parameter_list.ui_children[1:])
    assert workspace.selected_parameter_id == workspace.model["parameters"][0]["id"]


def test_parameter_removal_is_available_and_blocked_while_referenced(standalone_components):
    workspace = SolveWorkspace()
    workspace.add_parameter()
    parameter = workspace.model["parameters"][0]
    workspace._add_primitive("rectangle")
    region = workspace.model["geometry"]["regions"][0]
    workspace._set_region_dimension(region["id"], "width", "length_1*1000")

    workspace.remove_parameter(parameter["id"])

    assert workspace.model["parameters"] == [parameter]
    assert "Cannot remove 'length_1'" in workspace.message
    assert "region 'Region 1' width" in workspace.message

    workspace._set_region_dimension(region["id"], "width", "20")
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
