import io
import zipfile

import pytest

from ngsolve_gui.axisymmetric_model import (
    builtin_materials,
    evaluate_expression,
    new_model,
    new_studies,
    package_model,
    unpack_model,
    validate_model,
)


def _nested_model():
    model = new_model()
    model["materials"] = builtin_materials()
    model["parameters"] = [
        {"id": "parameter-radius", "name": "coil_radius", "expression": "0.004", "unit": "m"}
    ]
    model["geometry"]["regions"] = [
        {
            "id": "outer", "name": "Air", "material_id": "material-air", "parent_id": None,
            "vertices": [[0, 0], [0.02, 0], [0.02, 0.02], [0, 0.02]],
            "shape": {"type": "rectangle", "r_min": 0, "z_min": 0, "width": 0.02, "height": 0.02,
                      "dimension_expressions": {"r_min": "0", "z_min": "0", "width": "20", "height": "20"}},
            "sources": {},
        },
        {
            "id": "coil", "name": "Coil", "material_id": "material-copper", "parent_id": "outer",
            "vertices": [[0.003, 0.003], [0.007, 0.003], [0.007, 0.008], [0.003, 0.008]],
            "shape": {"type": "rectangle", "r_min": 0.003, "z_min": 0.003, "width": 0.004, "height": 0.005,
                      "dimension_expressions": {"r_min": "3", "z_min": "3", "width": "coil_radius*1000", "height": "5"}},
            "sources": {"dc_current_density": "1e6", "ac_current_density_real": "0", "ac_current_density_imaginary": "0"},
        },
    ]
    edges = []
    for region in model["geometry"]["regions"]:
        region["edge_ids"] = []
        vertices = region["vertices"]
        for index, start in enumerate(vertices):
            edge_id = f"edge-{len(edges) + 1}"
            edges.append({
                "id": edge_id,
                "name": edge_id,
                "vertices": [start, vertices[(index + 1) % len(vertices)]],
                "boundary_condition_ids": [],
            })
            region["edge_ids"].append(edge_id)
    model["geometry"]["edges"] = edges
    return model


def test_parameter_expressions_resolve_safely_and_reject_cycles():
    params = {
        "radius": {"expression": "4e-3", "unit": "m"},
        "width": {"expression": "2*radius", "unit": "m"},
    }
    assert evaluate_expression("sqrt(width**2)", params) == pytest.approx(0.008)
    with pytest.raises(ValueError, match="Circular parameter"):
        evaluate_expression("first", {"first": {"expression": "second"}, "second": {"expression": "first"}})
    with pytest.raises(ValueError, match="Only|unsupported|unknown"):
        evaluate_expression("__import__('os').system('echo unsafe')")


def test_nested_geometry_and_parameter_driven_dimensions_validate():
    model = _nested_model()
    assert validate_model(model) == []
    model["geometry"]["regions"][1]["shape"]["dimension_expressions"]["width"] = "coil_radius*2000"
    assert any("expression and saved dimension disagree" in error for error in validate_model(model))


def test_overlapping_domains_and_wrong_parent_are_rejected():
    model = _nested_model()
    model["geometry"]["regions"][1]["vertices"][0][0] = 0.021
    errors = validate_model(model)
    assert any("cross or touch" in error for error in errors)

    model = _nested_model()
    model["geometry"]["regions"][1]["parent_id"] = None
    assert any("nearest containing region" in error for error in validate_model(model))


def test_malformed_parent_and_boundary_values_are_reported_without_crashing():
    model = _nested_model()
    model["geometry"]["regions"][1]["parent_id"] = {"invalid": "id"}
    model["boundary_conditions"].append({"id": "broken", "name": "Broken", "type": ["invalid"]})

    errors = validate_model(model)

    assert any("parent id must be text" in error for error in errors)
    assert any("unsupported type" in error for error in errors)


def test_ngsmodel_round_trip_is_data_only_and_rejects_extra_files():
    model = _nested_model()
    studies = new_studies()
    studies["studies"][0]["frequency_hz"] = ["500", "1000"]
    archive = package_model(model, studies, {"active_section": "geometry"})
    restored_model, restored_studies, restored_layout = unpack_model(archive)
    assert restored_model == model
    assert restored_studies == studies
    assert restored_layout["active_section"] == "geometry"

    malicious = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive), "r") as source, zipfile.ZipFile(malicious, "w") as target:
        for info in source.infolist():
            target.writestr(info.filename, source.read(info.filename))
        target.writestr("execute.py", "raise RuntimeError('must not execute')")
    with pytest.raises(ValueError, match="unsupported files"):
        unpack_model(malicious.getvalue())


def test_sweeps_are_single_study_frequency_lists():
    studies = new_studies()
    studies["studies"].append(dict(studies["studies"][0], id="second"))
    with pytest.raises(ValueError, match="Exactly one"):
        package_model(new_model(), studies)
