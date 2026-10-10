import io
import json
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
                      "dimension_expressions": {"r_min": "0", "z_min": "0", "width": "0.02", "height": "0.02"}},
            "sources": {},
        },
        {
            "id": "coil", "name": "Coil", "material_id": "material-copper", "parent_id": "outer",
            "vertices": [[0.003, 0.003], [0.007, 0.003], [0.007, 0.008], [0.003, 0.008]],
            "shape": {"type": "rectangle", "r_min": 0.003, "z_min": 0.003, "width": 0.004, "height": 0.005,
                      "dimension_expressions": {"r_min": "0.003", "z_min": "0.003", "width": "coil_radius", "height": "0.005"}},
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
    model["geometry"]["regions"][1]["shape"]["dimension_expressions"]["width"] = "coil_radius*2"
    assert any("expression and saved dimension disagree" in error for error in validate_model(model))


def test_parameter_dimensions_reject_values_used_in_incompatible_fields():
    model = _nested_model()
    model["parameters"].append({
        "id": "parameter-current",
        "name": "coil_current_density",
        "expression": "2e6",
        "unit": "A/m^2",
    })
    coil = model["geometry"]["regions"][1]
    coil["sources"]["dc_current_density"] = "coil_current_density"
    coil["shape"]["dimension_expressions"]["width"] = "sqrt(coil_radius**2)"
    assert validate_model(model) == []

    coil["sources"]["dc_current_density"] = "coil_radius"
    errors = validate_model(model)
    assert any("dc_current_density: has unit m; expected A/m^2" in error for error in errors)


def test_parameter_declaration_and_explicit_si_suffixes_are_checked():
    model = _nested_model()
    model["parameters"].append({
        "id": "parameter-frequency",
        "name": "bad_frequency",
        "expression": "2*coil_radius",
        "unit": "Hz",
    })
    errors = validate_model(model)
    assert any("expression has unit m but parameter is declared as Hz" in error for error in errors)

    model = _nested_model()
    model["mesh"]["element_size"] = "0.01 A/m^2"
    errors = validate_model(model)
    assert any("Mesh element size: has unit A/m^2; expected m" in error for error in errors)


def test_legacy_millimetre_project_migrates_expressions_to_si_metres():
    model = _nested_model()
    model["geometry"].pop("dimension_expression_unit")
    model["geometry"]["regions"][0]["shape"]["dimension_expressions"] = {
        "r_min": "0", "z_min": "0", "width": "20 mm", "height": "20"
    }
    model["geometry"]["regions"][1]["shape"]["dimension_expressions"] = {
        "r_min": "3", "z_min": "3", "width": "coil_radius*1000", "height": "5"
    }
    studies = new_studies()
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem.json", json.dumps(model))
        archive.writestr("studies.json", json.dumps(studies))

    restored, _, _ = unpack_model(archive_bytes.getvalue())

    assert restored["geometry"]["dimension_expression_unit"] == "m"
    assert restored["geometry"]["regions"][0]["shape"]["dimension_expressions"]["width"] == "(20) / 1000"
    assert restored["geometry"]["regions"][1]["shape"]["dimension_expressions"]["width"] == "(coil_radius*1000) / 1000"
    assert validate_model(restored) == []


def test_legacy_empty_project_is_marked_for_si_geometry():
    model = new_model()
    model["geometry"].pop("dimension_expression_unit")
    model["geometry"].pop("regions")
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("problem.json", json.dumps(model))
        archive.writestr("studies.json", json.dumps(new_studies()))

    restored, _, _ = unpack_model(archive_bytes.getvalue())

    assert restored["geometry"]["dimension_expression_unit"] == "m"


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
