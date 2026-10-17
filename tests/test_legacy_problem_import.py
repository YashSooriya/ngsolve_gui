import copy
import math
from types import SimpleNamespace

import pytest

from ngapp import utils
from ngsolve_gui.axisymmetric_model import package_model, serialize_model_json, unpack_model, validate_model
from ngsolve_gui.legacy_problem_import import (
    LegacySplineGeometryCapture,
    import_legacy_problem_file,
    legacy_problem_to_model,
)
from ngsolve_gui.solve_workspace import SolveWorkspace


@pytest.fixture
def standalone_components(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ngapp-config"))
    monkeypatch.setattr(utils, "user_config_dir", lambda app_name: str(tmp_path / "user-config" / app_name))
    environment = utils.Environment(utils.EnvironmentType.STANDALONE, have_backend=False)
    environment.frontend.update_component = lambda *args, **kwargs: None
    monkeypatch.setattr(utils, "_environment", environment)


def _rectangle(geometry, r0, z0, r1, z1, domain, parent, conditions, *, hpref=False):
    points = [
        geometry.AppendPoint(r0, z0, hpref=hpref),
        geometry.AppendPoint(r1, z0, hpref=hpref),
        geometry.AppendPoint(r1, z1, hpref=hpref),
        geometry.AppendPoint(r0, z1, hpref=hpref),
    ]
    for index, point in enumerate(points):
        options = {
            "leftdomain": domain,
            "rightdomain": parent,
            "hpref": hpref,
        }
        if conditions[index]:
            options["bc"] = conditions[index]
        geometry.Append(["line", point, points[(index + 1) % 4]], **options)


def _legacy_test_problem():
    geometry = LegacySplineGeometryCapture()
    _rectangle(geometry, 0, -1, 2, 1, 1, 0, ["DirEM", "DirEM", "DirEM", "Neu_0"])
    _rectangle(geometry, 0.5, -0.5, 1, 0.5, 2, 1,
               ["DirMechFix", "Transmission_Interface", "DirMechFix", "Transmission_Interface"],
               hpref=True)
    geometry.SetMaterial(1, "Air")
    geometry.SetMaterial(2, "Copper")
    return {
        "geometry": geometry,
        "mechSubdom": "Copper",
        "dirkeysMech": "DirMechFix",
        "Edict": {"Air": 0, "Copper": 110e9},
        "nudict": {"Air": 0.3, "Copper": 0.34},
        "rhodict": {"Air": 0, "Copper": 8960},
        "murdict": {"Air": 1, "Copper": 1},
        "sigmadict": {"Air": 0, "Copper": 5.8e7},
        "srcdictDC": {"Air": 0, "Copper": 1e6},
        "srcdictAC": {"Air": 0, "Copper": 2e6 + 3e6j},
        "omega": [2 * math.pi * 500],
        "dirkeysADC": "DirEM",
        "dirkeysAAC": "DirEM",
        "dirconditionADC": {"DirEM": 0},
        "dirconditionAAC": {"DirEM": 0},
        "HpLayers": 2,
        "sigmaG": 0.35,
        "kACCoefDC": 1,
        "kACCoef": 1,
        "cACCoef": 1,
        "mMechCoef": 1,
        "kMechCoef": 1,
        "kCoupCoef": 1,
        "cCoupCoef": 1,
        "dampingalpha": 12.5,
        "dampingbeta": 4e-8,
        "solver": "direct",
    }


def test_legacy_problem_conversion_preserves_supported_model_data(standalone_components):
    model, studies, layout = legacy_problem_to_model(
        _legacy_test_problem(), element_size=0.1, polynomial_order=3
    )

    assert validate_model(model) == []
    assert len(model["geometry"]["regions"]) == 2
    outer, coil = model["geometry"]["regions"]
    assert outer["parent_id"] is None
    assert coil["parent_id"] == outer["id"]
    assert coil["mechanical"] is True
    assert len(model["geometry"]["edges"]) == 8
    assert len(model["boundary_conditions"]) == 4
    assert model["mesh"]["hp_layers"] == 2
    assert len(model["mesh"]["hp_edge_ids"]) == 4
    assert coil["sources"]["dc_current_density"] == "1000000"
    assert coil["sources"]["ac_current_density_real"] == "2000000"
    assert coil["sources"]["ac_current_density_imaginary"] == "3000000"
    assert studies["studies"][0]["frequency_hz"] == ["500"]
    assert model["physics"]["mechanics"]["rayleigh_alpha"] == "12.5"
    assert model["physics"]["mechanics"]["rayleigh_beta"] == "4e-08"
    assert model["solver"]["maximum_iterations"] == 10
    assert layout["active_section"] == "geometry"

    workspace = SolveWorkspace()
    workspace.set_model(model, studies, layout)
    conditions = {item["id"]: item for item in workspace.model["boundary_conditions"]}
    assert not any(item.get("automatic_for_mechanics") for item in conditions.values())
    coil_edges = {
        edge["id"]: edge for edge in workspace.model["geometry"]["edges"]
        if edge["id"] in coil["edge_ids"]
    }
    assert all(edge["boundary_condition_ids"] for edge in coil_edges.values())


def test_legacy_solver_ramp_and_transmission_settings_are_preserved(standalone_components):
    problem = _legacy_test_problem()
    problem.update({
        "transmissionExteriorSubdom": "Vac",
        "ramp_time": 1.25,
        "ramp_type": "sigmoid",
        "source_type": "sine",
        "coupled_approach": "linearised",
    })
    model, _studies, _layout = legacy_problem_to_model(problem)
    assert model["legacy_import"]["solver_parameters"] == {
        "transmission_exterior_subdomain": "Vac",
        "ramp_time": 1.25,
        "ramp_type": "sigmoid",
        "source_type": "sine",
        "coupled_approach": "linearised",
    }


def test_empty_legacy_dirichlet_keys_remain_natural_and_axis_is_preserved(standalone_components):
    problem = _legacy_test_problem()
    problem["dirkeysADC"] = ""
    problem["dirkeysAAC"] = ""
    model, _studies, _layout = legacy_problem_to_model(problem)

    conditions = {condition["id"]: condition for condition in model["boundary_conditions"]}
    magnetic_zero_ids = {
        condition_id for condition_id, condition in conditions.items()
        if condition["type"] == "magnetic_potential_zero"
    }
    outer = model["geometry"]["regions"][0]
    outer_edges = [
        edge for edge in model["geometry"]["edges"]
        if edge["id"] in outer["edge_ids"]
    ]
    axis_edges = [
        edge for edge in outer_edges
        if all(abs(point[0]) <= 1e-12 for point in edge["vertices"])
    ]
    assert len(axis_edges) == 1
    assert [conditions[item]["type"] for item in axis_edges[0]["boundary_condition_ids"]] == ["axis_of_symmetry"]
    assert not any(
        magnetic_zero_ids & set(edge["boundary_condition_ids"])
        for edge in outer_edges if edge not in axis_edges
    )


def test_explicit_legacy_local_mesh_size_survives_global_target_changes(standalone_components):
    problem = _legacy_test_problem()
    problem["geometry"].SetDomainMaxH(2, 0.01)

    model, _studies, _layout = legacy_problem_to_model(problem, element_size=0.01)

    coil = model["geometry"]["regions"][1]
    assert model["mesh"]["element_size"] == "0.01"
    assert model["mesh"]["region_element_sizes"][coil["id"]] == "0.01"


def test_legacy_file_import_uses_solver_default_mesh_controls():
    defaults = import_legacy_problem_file.__kwdefaults__
    assert defaults["element_size"] == 1.0
    assert defaults["legacy_h_local"] == 0.1
    assert defaults["polynomial_order"] == 3
    model, _studies, _layout = legacy_problem_to_model(_legacy_test_problem())
    assert model["mesh"]["curve_order"] == 3


def test_legacy_domain_mesh_overrides_are_preserved_per_region(standalone_components):
    problem = _legacy_test_problem()
    problem["geometry"].SetDomainMaxH(1, 0.125)
    problem["geometry"].SetDomainMaxH(2, 0.025)
    model, _studies, _layout = legacy_problem_to_model(problem, element_size=0.5)
    regions = {region["legacy_domain"]: region for region in model["geometry"]["regions"]}

    assert model["mesh"]["element_size"] == "0.5"
    assert model["mesh"]["region_element_sizes"][regions[1]["id"]] == "0.125"
    assert model["mesh"]["region_element_sizes"][regions[2]["id"]] == "0.025"


def test_legacy_dc_and_ac_dirichlet_selections_must_match(standalone_components):
    problem = _legacy_test_problem()
    problem["dirkeysAAC"] = ""
    problem["dirconditionAAC"] = {}

    with pytest.raises(ValueError, match="different magnetic Dirichlet boundary selections"):
        legacy_problem_to_model(problem)


def test_json_problem_file_roundtrips_and_ngsmodel_remains_supported(standalone_components):
    model, studies, layout = legacy_problem_to_model(_legacy_test_problem())

    json_data = serialize_model_json(model, studies, layout)
    json_model, json_studies, json_layout = unpack_model(json_data)
    assert json_model == model
    assert json_studies == studies
    assert json_layout == layout

    archive_data = package_model(model, studies, layout)
    archive_model, archive_studies, archive_layout = unpack_model(archive_data)
    assert archive_model == model
    assert archive_studies == studies
    assert archive_layout == layout


def test_legacy_spline3_import_preserves_control_curve_for_render_and_solver(standalone_components):
    problem = _legacy_test_problem()
    geometry = problem["geometry"]
    control = geometry.AppendPoint(1.0, -1.5)
    segment = geometry.segments[0]
    segment.update({"type": "spline3", "control": control})

    model, studies, layout = legacy_problem_to_model(problem)
    spline_edge = next(
        edge for edge in model["geometry"]["edges"]
        if edge["curve"].get("type") == "spline3"
    )

    assert model["legacy_import"]["topology_mode"] == "direct_curve_edges"
    assert spline_edge["curve"]["control_point"] == [1.0, -1.5]
    assert len(spline_edge["curve"]["sampled_points"]) == 33
    assert spline_edge["vertices"] == [[0.0, -1.0], [2.0, -1.0]]
    assert set(spline_edge["legacy_domain_sides"]) == {"left_region_id", "right_region_id"}

    workspace = SolveWorkspace()
    workspace.set_model(model, studies, layout)
    reloaded_edge = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge["id"] == spline_edge["id"]
    )
    assert reloaded_edge["curve"]["type"] == "spline3"
    assert reloaded_edge["curve"]["control_point"] == [1.0, -1.5]


def test_dragging_nested_legacy_contour_preserves_spline_topology_and_updates_parent(standalone_components, monkeypatch):
    problem = _legacy_test_problem()
    geometry = problem["geometry"]
    segment = next(item for item in geometry.segments if item["leftdomain"] == 2)
    start = geometry.points[segment["start"]]["coordinates"]
    end = geometry.points[segment["end"]]["coordinates"]
    control = geometry.AppendPoint(
        (start[0] + end[0]) / 2,
        (start[1] + end[1]) / 2 + 0.03,
    )
    segment.update({"type": "spline3", "control": control})
    model, studies, layout = legacy_problem_to_model(problem)
    workspace = SolveWorkspace()
    workspace.set_model(model, studies, layout)
    outer = next(item for item in workspace.model["geometry"]["regions"] if item["legacy_domain"] == 1)
    inner = next(item for item in workspace.model["geometry"]["regions"] if item["legacy_domain"] == 2)
    spline = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge["curve"].get("type") == "spline3"
    )
    spline_id = spline["id"]
    edge_ids = list(inner["edge_ids"])
    edge_count = len(workspace.model["geometry"]["edges"])
    original_curve = copy.deepcopy(spline["curve"])
    original_condition_ids = list(spline["boundary_condition_ids"])
    original_domain_sides = copy.deepcopy(spline["legacy_domain_sides"])
    original_control_id = original_curve["control_point_id"]
    original_control_record = next(
        item for item in workspace.model["geometry"]["points"]
        if item["id"] == original_control_id
    )
    original_control_coordinates = list(original_control_record["coordinates"])
    path_before = workspace._canvas_region_path_data(outer)
    scripts = []
    workspace.js = SimpleNamespace(eval=lambda script: scripts.append(script))
    rendered = []
    monkeypatch.setattr(workspace, "render_canvas", lambda: rendered.append(True))
    project, _ = workspace._canvas_projection()

    moved = workspace._move_region_from_canvas_drag(
        inner["id"], project((0.75, 0.0)), project((0.77, 0.02)),
        preserve_canvas=True,
    )

    moved_spline = next(
        edge for edge in workspace.model["geometry"]["edges"]
        if edge["id"] == spline_id
    )
    assert moved is True
    assert inner["shape"]["type"] == "imported_contour"
    assert inner["parent_id"] == outer["id"]
    assert inner["edge_ids"] == edge_ids
    assert len(workspace.model["geometry"]["edges"]) == edge_count
    assert moved_spline["curve"]["type"] == "spline3"
    assert moved_spline["curve"]["control_point"] == pytest.approx([
        original_curve["control_point"][0] + 0.02,
        original_curve["control_point"][1] + 0.02,
    ])
    assert moved_spline["curve"]["sampled_points"][0] == pytest.approx([
        original_curve["sampled_points"][0][0] + 0.02,
        original_curve["sampled_points"][0][1] + 0.02,
    ])
    assert moved_spline["boundary_condition_ids"] == original_condition_ids
    assert moved_spline["legacy_domain_sides"] == original_domain_sides
    assert moved_spline["curve"]["control_point_id"] == original_control_id
    assert original_control_record["coordinates"] == pytest.approx([
        original_control_coordinates[0] + 0.02,
        original_control_coordinates[1] + 0.02,
    ])
    moved_control_record = next(
        item for item in workspace.model["geometry"]["points"]
        if item["id"] == moved_spline["curve"]["control_point_id"]
    )
    assert moved_control_record is original_control_record
    assert len(workspace.model["geometry"]["points"]) == len(model["geometry"]["points"])
    parent_hole = next(
        loop for loop in outer["boundary_loops"]
        if loop.get("role") == "hole" and loop.get("region_id") == inner["id"]
    )
    assert {item["edge_id"] for item in parent_hole["edges"]} == set(inner["edge_ids"])
    path_after = workspace._canvas_region_path_data(outer)
    assert path_after != path_before
    assert scripts and json.dumps({"id": outer["id"], "d": path_after}) in scripts[0]
    assert rendered == []


def test_legacy_capture_preserves_rational_spline3_control_points():
    geometry = LegacySplineGeometryCapture()
    first = geometry.AppendPoint(0.5, -0.5)
    control = geometry.AppendPoint(1.0, 0.5)
    end = geometry.AppendPoint(1.5, -0.5)

    geometry.Append(["spline3", first, control, end], leftdomain=2, rightdomain=1, bc="default")

    segment = geometry.segments[0]
    assert segment["type"] == "spline3"
    assert segment["start"] == first
    assert segment["control"] == control
    assert segment["end"] == end


def test_imported_contour_bounds_and_spline_points_are_editable(standalone_components, monkeypatch):
    problem = _legacy_test_problem()
    geometry = problem["geometry"]
    segment = next(item for item in geometry.segments if item["leftdomain"] == 2)
    start = geometry.points[segment["start"]]["coordinates"]
    end = geometry.points[segment["end"]]["coordinates"]
    control = geometry.AppendPoint(
        (start[0] + end[0]) / 2,
        (start[1] + end[1]) / 2 + 0.03,
    )
    segment.update({"type": "spline3", "control": control})
    model, studies, layout = legacy_problem_to_model(problem)
    workspace = SolveWorkspace()
    workspace.set_model(model, studies, layout)
    monkeypatch.setattr(workspace, "render_canvas", lambda: None)
    inner = next(item for item in workspace.model["geometry"]["regions"] if item["legacy_domain"] == 2)
    spline = next(edge for edge in workspace.model["geometry"]["edges"] if edge["curve"].get("type") == "spline3")
    old_vertices = copy.deepcopy(inner["vertices"])
    old_curve = copy.deepcopy(spline["curve"])
    old_r_min = min(point[0] for point in old_vertices)
    old_r_max = max(point[0] for point in old_vertices)

    workspace._set_region_dimension(inner["id"], "width", "0.8")

    new_r = [point[0] for point in inner["vertices"]]
    assert max(new_r) - min(new_r) == pytest.approx(0.8)
    assert spline["curve"]["type"] == "spline3"
    assert spline["curve"]["control_point"][0] == pytest.approx(
        min(new_r) + (old_curve["control_point"][0] - old_r_min) * 0.8 / (old_r_max - old_r_min)
    )
    assert len(spline["curve"]["sampled_points"]) == len(old_curve["sampled_points"])

    workspace.active_section = "geometry"
    workspace.selected_edge_id = spline["id"]
    workspace.selected_edge_ids = [spline["id"]]
    workspace._render_inspector()
    assert "imported_contour" in workspace._region_dimension_groups
    assert workspace._imported_edge_geometry.ui_hidden is False
    old_control_z = spline["curve"]["control_point"][1]
    workspace._set_imported_edge_coordinate(spline["id"], "control", "z", str(old_control_z + 0.01))
    assert spline["curve"]["control_point"][1] == pytest.approx(old_control_z + 0.01)
    assert len(spline["curve"]["sampled_points"]) == 33


def test_imported_edge_endpoint_edit_updates_connected_edges_and_rejects_negative_radius(standalone_components, monkeypatch):
    model, studies, layout = legacy_problem_to_model(_legacy_test_problem())
    workspace = SolveWorkspace()
    workspace.set_model(model, studies, layout)
    monkeypatch.setattr(workspace, "render_canvas", lambda: None)
    edge = next(item for item in workspace.model["geometry"]["edges"] if item.get("legacy_domain_sides"))
    point_id = edge["point_ids"][0]
    point = next(item for item in workspace.model["geometry"]["points"] if item["id"] == point_id)
    old_point = copy.deepcopy(point["coordinates"])
    workspace._set_imported_edge_coordinate(edge["id"], "start", "z", str(old_point[1] + 0.01))
    assert point["coordinates"] == pytest.approx([old_point[0], old_point[1] + 0.01])
    for connected in workspace.model["geometry"]["edges"]:
        for index, connected_id in enumerate(connected.get("point_ids", [])):
            if connected_id == point_id:
                assert connected["vertices"][index] == pytest.approx(point["coordinates"])

    before_invalid_edit = copy.deepcopy(point["coordinates"])
    workspace._set_imported_edge_coordinate(edge["id"], "start", "r", "-1")
    restored_point = next(item for item in workspace.model["geometry"]["points"] if item["id"] == point_id)
    assert restored_point["coordinates"] == pytest.approx(before_invalid_edit)
    assert workspace.message_is_error
