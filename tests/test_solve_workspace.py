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


def test_canvas_render_keeps_background_and_layer_components_mounted(standalone_components):
    workspace = SolveWorkspace()
    root_layers = tuple(workspace._canvas.ui_children)

    workspace._add_primitive("rectangle")

    assert tuple(workspace._canvas.ui_children) == root_layers
    assert root_layers == (
        workspace._canvas_background,
        workspace._canvas_grid,
        workspace._canvas_scene,
        workspace._canvas_preview,
    )


def test_canvas_event_reads_pointer_cache_when_ngapp_omits_coordinates(standalone_components):
    workspace = SolveWorkspace()
    workspace._screen_to_svg = (1.25, 0.0, 0.0, 1.25, 100.0, 200.0)
    workspace._read_canvas_pointer = lambda value: (125.0, 300.0)

    point = workspace._canvas_event_point(
        SimpleNamespace(value={"type": "mousedown", "button": 0, "timeStamp": 12.5})
    )

    assert point == pytest.approx((20.0, 80.0))
