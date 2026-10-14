import math

import pytest

from ngapp import utils
from ngsolve_gui.axisymmetric_model import package_model, serialize_model_json, unpack_model, validate_model
from ngsolve_gui.legacy_problem_import import LegacySplineGeometryCapture, legacy_problem_to_model
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
