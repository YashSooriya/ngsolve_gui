import io
import json
import zipfile

import pytest

from ngsolve_gui.axisymmetric_model import (
    builtin_materials,
    evaluate_expression,
    find_closed_sketch_loops,
    new_model,
    migrate_regional_loads,
    migrate_legacy_model,
    new_studies,
    package_model,
    unpack_model,
    validate_model,
    validate_studies,
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
    model["boundary_conditions"] = [{
        "id": "axis-boundary", "name": "Axis of symmetry", "type": "axis_of_symmetry",
        "automatic_for_axis": True,
    }]
    model["geometry"]["edges"][3]["boundary_condition_ids"] = ["axis-boundary"]
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


def test_legacy_sketch_migration_adds_stable_points_and_oriented_nested_loops():
    model = _nested_model()
    geometry = model["geometry"]
    geometry.pop("topology_version", None)
    geometry.pop("points", None)
    for region in geometry["regions"]:
        region.pop("boundary_loops", None)
    for edge in geometry["edges"]:
        edge.pop("curve", None)
        edge.pop("point_ids", None)

    migrated = migrate_legacy_model(model)
    geometry = migrated["geometry"]
    point_ids = {point["id"] for point in geometry["points"]}
    assert geometry["topology_version"] == 1
    assert len(point_ids) == len(geometry["points"])
    assert all(set(edge["point_ids"]) <= point_ids for edge in geometry["edges"])
    assert all(edge["curve"]["type"] == "line" for edge in geometry["edges"])

    outer = next(region for region in geometry["regions"] if region["id"] == "outer")
    coil = next(region for region in geometry["regions"] if region["id"] == "coil")
    hole = next(loop for loop in outer["boundary_loops"] if loop["role"] == "hole")
    assert hole["region_id"] == "coil"
    assert {edge["edge_id"] for edge in hole["edges"]} == set(coil["edge_ids"])
    assert all(
        reverse["forward"] is not forward["forward"]
        for reverse, forward in zip(hole["edges"], reversed(coil["boundary_loops"][0]["edges"]))
    )


def test_legacy_circle_migration_keeps_curved_edges_exact():
    import math
    from ngsolve_gui.axisymmetric_model import migrate_legacy_model

    model = new_model()
    center = (0.01, 0.02)
    radius = 0.004
    vertices = [
        [center[0] + radius * math.cos(2 * math.pi * index / 48),
         center[1] + radius * math.sin(2 * math.pi * index / 48)]
        for index in range(48)
    ]
    edges = []
    edge_ids = []
    for index, start in enumerate(vertices):
        edge_id = f"arc-{index}"
        edge_ids.append(edge_id)
        edges.append({
            "id": edge_id,
            "vertices": [start, vertices[(index + 1) % len(vertices)]],
            "boundary_condition_ids": [],
        })
    model["geometry"]["regions"] = [{
        "id": "circle-region",
        "name": "circle",
        "shape": {
            "type": "circle",
            "r_center": center[0],
            "z_center": center[1],
            "radius": radius,
            "segments": 48,
            "dimension_expressions": {
                "r_center": "0.01", "z_center": "0.02", "radius": "0.004",
            },
        },
        "vertices": vertices,
        "edge_ids": edge_ids,
        "parent_id": None,
    }]
    model["geometry"]["edges"] = edges

    migrated = migrate_legacy_model(model)
    migrated_edges = migrated["geometry"]["edges"]
    assert all(edge["curve"]["type"] == "arc" for edge in migrated_edges)
    assert all(edge["curve"]["center"] == list(center) for edge in migrated_edges)
    assert all(edge["curve"]["radius"] == pytest.approx(radius) for edge in migrated_edges)
    assert migrate_legacy_model(migrated) == migrated


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


def test_transmission_interface_is_a_supported_auto_boundary_type():
    model = _nested_model()
    model["boundary_conditions"].append({
        "id": "automatic-interface",
        "name": "Transmission interface",
        "type": "transmission_interface",
        "automatic_for_mechanics": True,
    })
    model["geometry"]["edges"][0]["boundary_condition_ids"].append("automatic-interface")

    assert validate_model(model) == []


def test_validate_model_allows_one_em_and_one_mechanical_condition():
    model = _nested_model()
    model["boundary_conditions"].extend([
        {"id": "em-condition", "name": "EM", "type": "magnetic_potential_zero"},
        {"id": "mechanical-condition", "name": "Fixed", "type": "mechanical_fixed"},
    ])
    model["geometry"]["edges"][1]["boundary_condition_ids"] = ["em-condition", "mechanical-condition"]
    assert validate_model(model) == []


def test_validate_model_rejects_multiple_conditions_of_one_physics():
    model = _nested_model()
    model["boundary_conditions"].extend([
        {"id": "mechanical-one", "name": "Fixed", "type": "mechanical_fixed"},
        {"id": "mechanical-two", "name": "Traction", "type": "mechanical_traction"},
    ])
    model["geometry"]["edges"][1]["boundary_condition_ids"] = ["mechanical-one", "mechanical-two"]
    assert any("multiple mechanical conditions" in error for error in validate_model(model))


def test_axis_condition_can_coexist_with_one_mechanical_condition():
    model = _nested_model()
    model["geometry"]["edges"][3]["boundary_condition_ids"] = ["axis-boundary", "mechanical-one"]
    model["boundary_conditions"].append({"id": "mechanical-one", "name": "Fixed", "type": "mechanical_fixed"})
    assert not any(
        "multiple" in error or "must have only the axis-of-symmetry" in error
        for error in validate_model(model)
    )


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


def test_regional_mesh_sizes_and_hp_layer_targets_are_validated():
    model = _nested_model()
    model["mesh"].update({
        "region_element_sizes": {"coil": "0.001"},
        "hp_layers": 2,
        "hp_grading_factor": 0.3,
        "hp_region_ids": ["coil"],
        "hp_edge_ids": [model["geometry"]["edges"][0]["id"]],
    })
    assert validate_model(model) == []

    model["mesh"]["region_element_sizes"]["coil"] = "-0.001"
    model["mesh"]["hp_edge_ids"] = ["missing-edge"]
    errors = validate_model(model)
    assert any("Region 'Coil' mesh size" in error for error in errors)
    assert any("missing boundary 'missing-edge'" in error for error in errors)


def test_hp_layers_require_a_valid_mark_and_safe_grading():
    model = _nested_model()
    model["mesh"].update({"hp_layers": 2, "hp_grading_factor": 1.0})
    errors = validate_model(model)
    assert any("Select at least one region or boundary" in error for error in errors)
    assert any("hp grading factor must be between 0 and 1" in error for error in errors)


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
    assert any("region interiors must not overlap" in error for error in errors)

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
    assert restored_model == migrate_legacy_model(model)
    assert restored_model["geometry"]["topology_version"] == 1
    assert restored_studies == studies
    assert restored_layout["active_section"] == "geometry"

    malicious = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive), "r") as source, zipfile.ZipFile(malicious, "w") as target:
        for info in source.infolist():
            target.writestr(info.filename, source.read(info.filename))
        target.writestr("execute.py", "raise RuntimeError('must not execute')")
    with pytest.raises(ValueError, match="unsupported files"):
        unpack_model(malicious.getvalue())


def test_drafts_can_save_multiple_studies_but_solver_reports_unsupported_count():
    model = new_model()
    studies = new_studies()
    studies["studies"].append(dict(studies["studies"][0], id="second", name="Second study"))

    package_model(model, studies)
    errors = validate_studies(studies, physics=model["physics"], for_solve=True)
    assert any("one study at a time" in error for error in errors)


def test_mesh_scope_ignores_incomplete_physics_materials_and_boundary_data():
    model = _nested_model()
    model["materials"] = "unfinished"
    model["physics"] = "unfinished"
    model["boundary_conditions"] = None
    for region in model["geometry"]["regions"]:
        region["material_id"] = None

    assert validate_model(model, scope="mesh") == []


def test_harmonic_mechanics_can_be_solved_without_electromagnetics():
    model = _nested_model()
    model["physics"] = {
        "dc_magnetic": {"enabled": False},
        "harmonic_electromagnetic": {"enabled": False},
        "mechanics": {"enabled": True},
        "coupling": {"enabled": False},
    }
    model["geometry"]["regions"][1]["mechanical"] = True
    model["materials"][1]["properties"].update({
        "youngs_modulus": "110e9",
        "poissons_ratio": "0.34",
        "density": "8960",
    })
    studies = new_studies()
    studies["studies"][0]["frequency_hz"] = ["500"]
    assert validate_model(model) == []
    assert validate_studies(studies, physics=model["physics"], for_solve=True) == []

    studies["studies"][0]["frequency_hz"] = []
    assert any(
        "positive frequency" in error
        for error in validate_studies(studies, physics=model["physics"], for_solve=True)
    )


def test_static_only_study_does_not_require_frequency():
    model = new_model()
    model["physics"]["dc_magnetic"]["enabled"] = True
    model["physics"]["harmonic_electromagnetic"]["enabled"] = False
    model["physics"]["harmonic_electromagnetic"]["frequency_hz"] = "invalid-unused-frequency"
    studies = new_studies()
    studies["studies"][0]["frequency_hz"] = []

    assert validate_studies(studies, physics=model["physics"]) == []
    assert not any("Default frequency" in error for error in validate_model(model))
    package_model(model, studies)





def _rebuild_test_edges(model):
    edge_by_key = {}
    edges = []
    for region in model["geometry"]["regions"]:
        region["edge_ids"] = []
        points = region["vertices"]
        for index, first in enumerate(points):
            second = points[(index + 1) % len(points)]
            key = tuple(sorted((
                tuple(round(float(value), 12) for value in first),
                tuple(round(float(value), 12) for value in second),
            )))
            edge = edge_by_key.get(key)
            if edge is None:
                edge = {
                    "id": f"edge-{len(edges) + 1}",
                    "name": f"Edge {len(edges) + 1}",
                    "vertices": [list(first), list(second)],
                    "boundary_condition_ids": [],
                }
                edge_by_key[key] = edge
                edges.append(edge)
            region["edge_ids"].append(edge["id"])
    axis_id = next(
        item["id"] for item in model["boundary_conditions"]
        if item["type"] == "axis_of_symmetry"
    )
    for edge in edges:
        if all(abs(float(point[0])) <= 1e-12 for point in edge["vertices"]):
            edge["boundary_condition_ids"] = [axis_id]
    model["geometry"]["edges"] = edges

def test_tangent_adjacent_regions_validate_with_no_parent():
    model = _nested_model()
    coil = model["geometry"]["regions"][1]
    coil["vertices"] = [
        [0.02, 0.003], [0.024, 0.003], [0.024, 0.008], [0.02, 0.008]
    ]
    coil["shape"].update({
        "r_min": 0.02, "z_min": 0.003, "width": 0.004, "height": 0.005
    })
    coil["shape"]["dimension_expressions"] = {
        "r_min": "0.02", "z_min": "0.003", "width": "0.004", "height": "0.005"
    }
    coil["parent_id"] = None
    _rebuild_test_edges(model)

    assert validate_model(model) == []


def test_tangent_nested_region_may_touch_its_container():
    model = _nested_model()
    coil = model["geometry"]["regions"][1]
    coil["vertices"] = [
        [0.0, 0.003], [0.004, 0.003], [0.004, 0.008], [0.0, 0.008]
    ]
    coil["shape"].update({
        "r_min": 0.0, "z_min": 0.003, "width": 0.004, "height": 0.005
    })
    coil["shape"]["dimension_expressions"] = {
        "r_min": "0", "z_min": "0.003", "width": "0.004", "height": "0.005"
    }
    _rebuild_test_edges(model)

    assert validate_model(model) == []

def test_legacy_region_sources_migrate_to_named_regional_loads():
    model = _nested_model()
    coil = model["geometry"]["regions"][1]
    coil["sources"] = {
        "dc_current_density": "1e6",
        "ac_current_density_real": "2e6",
        "ac_current_density_imaginary": "3e6",
        "mechanical_body_force": {"r": "4", "z": "5"},
    }

    migrated = migrate_regional_loads(model)
    loads = migrated["regional_loads"]
    assert len(loads) == 1
    assert loads[0]["name"] == "Coil load"
    assert loads[0]["region_ids"] == ["coil"]
    assert migrated["geometry"]["regions"][1]["sources"] == coil["sources"]
    assert validate_model(migrated) == []
    assert len(migrate_regional_loads(migrated)["regional_loads"]) == 1


def test_validate_studies_handles_malformed_physics_sections():
    studies = new_studies()
    assert validate_studies(
        studies,
        physics={"harmonic_electromagnetic": "invalid", "mechanics": None},
        for_solve=True,
    ) == []


def test_closed_sketch_loop_detection_finds_only_bounded_faces():
    entities = [
        {"id": "bottom", "type": "line", "sampled_points": [[0, 0], [2, 0]]},
        {"id": "right", "type": "line", "sampled_points": [[2, 0], [2, 1]]},
        {"id": "top", "type": "line", "sampled_points": [[2, 1], [0, 1]]},
        {"id": "left", "type": "line", "sampled_points": [[0, 1], [0, 0]]},
    ]
    loops = find_closed_sketch_loops(entities)
    assert len(loops) == 1
    assert len(loops[0]["vertices"]) == 4
    assert set(loops[0]["entity_ids"]) == {"bottom", "right", "top", "left"}


def test_closed_sketch_loop_detection_supports_curved_samples_and_open_chains():
    entities = [
        {"id": "arc", "type": "arc", "sampled_points": [[0, 0], [1, 0.5], [2, 0]]},
        {"id": "base", "type": "line", "sampled_points": [[2, 0], [0, 0]]},
        {"id": "open", "type": "line", "sampled_points": [[3, 0], [4, 0]]},
    ]
    loops = find_closed_sketch_loops(entities)
    assert len(loops) == 1
    assert len(loops[0]["vertices"]) == 3
    assert set(loops[0]["entity_ids"]) == {"arc", "base"}
    assert len(loops[0]["boundary"]) == 2
    assert {item["entity_id"] for item in loops[0]["boundary"]} == {"arc", "base"}


def test_new_and_migrated_models_have_a_sketch_entity_collection():
    model = new_model()
    assert model["geometry"]["sketch_entities"] == []
    legacy = _nested_model()
    legacy["geometry"].pop("sketch_entities", None)
    migrated = migrate_legacy_model(legacy)
    assert migrated["geometry"]["sketch_entities"] == []


def test_sketch_curve_geometry_survives_problem_file_round_trip():
    model = new_model()
    model["geometry"]["sketch_entities"] = [{
        "id": "curve-arc",
        "type": "arc",
        "points": [[0.01, 0.0], [0.015, 0.005], [0.02, 0.0]],
        "center": [0.015, 0.0],
        "radius": 0.005,
        "clockwise": False,
        "sampled_points": [[0.01, 0.0], [0.015, 0.005], [0.02, 0.0]],
    }]
    restored, _, _ = unpack_model(package_model(model))
    assert restored["geometry"]["sketch_entities"] == model["geometry"]["sketch_entities"]
