import pytest

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
