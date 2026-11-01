import hashlib
import io
import zipfile

import pytest

from ngsolve_gui.three_d_project import (
    add_step_geometry,
    add_wire_trace,
    build_3d_solver_inputs,
    build_wire_trace_sets,
    detect_step_unit,
    materialize_3d_assets,
    new_3d_project,
    UNASSIGNED_MATERIAL,
    package_3d_project,
    parse_wire_trace,
    unpack_3d_project,
    step_unit_scale,
    validate_3d_project,
)


STEP = (
    b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n"
    b"#1=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));\n"
    b"ENDSEC;\nEND-ISO-10303-21;\n"
)


def test_step_import_does_not_assume_a_physical_material():
    project = new_3d_project()
    assets = {}

    geometry = add_step_geometry(project, assets, "part.step", STEP, units="mm")

    assert geometry["material"] == UNASSIGNED_MATERIAL


def test_step_region_tags_follow_geometry_not_import_order_or_assembly_scale():
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_project import step_geometry_tags

    first = Box(Pnt(0, 0, 0), Pnt(10, 10, 10))
    second = Box(Pnt(25, 0, 0), Pnt(45, 10, 5))
    unrelated_large = Box(Pnt(-200, 0, 0), Pnt(-20, 25, 30))

    tags, face_refs = step_geometry_tags([first, second])
    reversed_tags, reversed_faces = step_geometry_tags([second, first])
    expanded_tags, _ = step_geometry_tags([first, second, unrelated_large])

    assert tags[0] == reversed_tags[1] == expanded_tags[0]
    assert tags[1] == reversed_tags[0] == expanded_tags[1]
    assert {tag for tag, _solid, _face in face_refs} == {
        tag for tag, _solid, _face in reversed_faces
    }


def test_step_region_tags_change_when_solid_geometry_changes():
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_project import step_geometry_tags

    original = Box(Pnt(0, 0, 0), Pnt(10, 10, 10))
    edited = Box(Pnt(0, 0, 0), Pnt(11, 10, 10))

    assert step_geometry_tags([original])[0] != step_geometry_tags([edited])[0]


def test_step_region_tags_survive_solver_unit_scaling():
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_project import step_geometry_tags

    source = Box(Pnt(0, 0, 0), Pnt(10, 10, 10))
    scaled = source.Scale(Pnt(0, 0, 0), 0.001)

    assert step_geometry_tags([source])[0] == step_geometry_tags([scaled])[0]


def test_indistinguishable_duplicate_solids_fail_closed():
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_project import step_geometry_tags

    duplicate_a = Box(Pnt(0, 0, 0), Pnt(10, 10, 10))
    duplicate_b = Box(Pnt(0, 0, 0), Pnt(10, 10, 10))

    with pytest.raises(ValueError, match="indistinguishable overlapping solids"):
        step_geometry_tags([duplicate_a, duplicate_b])


def test_project_round_trip_preserves_stable_solid_and_face_assignments():
    project = new_3d_project("Stable selections")
    assets = {}
    geometry = add_step_geometry(project, assets, "shield.step", STEP)
    geometry["domain_selections"] = [{
        "id": "core",
        "name": "Core",
        "domain_ids": [0],
        "domain_tags": ["MMFEM_SOLID_000000000000000000000001"],
        "material": "Copper",
        "mesh_maxh_mm": 2.0,
    }]
    geometry["boundary_selections"] = [{
        "id": "support",
        "name": "Support",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000002"],
        "boundary_condition": "magnetic_potential_zero",
    }]

    restored, _assets = unpack_3d_project(package_3d_project(project, assets))

    assert restored["geometries"][0]["domain_selections"] == geometry["domain_selections"]
    assert restored["geometries"][0]["boundary_selections"] == geometry["boundary_selections"]


def test_wire_trace_parser_converts_csv_and_whitespace_units_to_metres():
    assert parse_wire_trace("0, 0, 0\n10, 20, 30\n", "mm") == [
        (0.0, 0.0, 0.0),
        (0.01, 0.02, 0.03),
    ]
    assert parse_wire_trace("x,y,z\n0,0,0\n1,2,3\n") == [
        (0.0, 0.0, 0.0),
        (1.0, 2.0, 3.0),
    ]


def test_step_unit_detection_handles_common_si_and_inch_declarations():
    assert detect_step_unit(b"SI_UNIT(.MILLI.,.METRE.)") == "mm"
    assert detect_step_unit(b"SI_UNIT($,.METRE.)") == "m"
    assert detect_step_unit(b"CONVERSION_BASED_UNIT('INCH',#42)") == "in"
    assert detect_step_unit(b"ISO-10303-21; no unit declaration") is None
    assert step_unit_scale(b"SI_UNIT(.MILLI.,.METRE.)", units="mm") == 0.001
    assert step_unit_scale(b"SI_UNIT($,.METRE.)", units="m") == 0.001
    assert step_unit_scale(b"SI_UNIT(.MILLI.,.METRE.)", units="m") == 1.0
    assert step_unit_scale(b"no unit declaration", units="m") == 1.0


def test_nan_rows_are_preserved_as_disconnected_trace_segments():
    points = parse_wire_trace(b"0 0 0\n1 0 0\nnan nan nan\n2 0 0\n3 0 0\n")

    assert points == [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        None,
        (2.0, 0.0, 0.0),
        (3.0, 0.0, 0.0),
    ]
    assert parse_wire_trace("# x y z\n0 0 0\n1 2 3\n", "cm") == [
        (0.0, 0.0, 0.0),
        (0.01, 0.02, 0.03),
    ]


@pytest.mark.parametrize(
    "contents, message",
    [
        ("0 0\n1 1\n", "exactly three"),
        ("0 0 0\n1 nope 1\n", "non-numeric"),
        ("0 0 0\ninf 1 1\n", "non-finite"),
        ("0 0 0\n0 0 0\n", "non-zero length"),
    ],
)
def test_wire_trace_parser_rejects_bad_or_empty_traces(contents, message):
    with pytest.raises(ValueError, match=message):
        parse_wire_trace(contents)


def test_project_round_trip_keeps_step_trace_metadata_and_solver_filenames(tmp_path):
    project = new_3d_project("Shielded magnet")
    assets = {}
    geometry = add_step_geometry(project, assets, "shield.stp", STEP, units="mm")
    geometry["mesh_maxh_mm"] = 30.0
    geometry["domain_selections"] = [{
        "id": "domain-selection-1",
        "name": "Shield volume",
        "domain_ids": [0],
        "mesh_maxh_mm": 2.5,
    }]
    geometry["boundary_selections"] = [{"id": "boundary-selection-1", "name": "Cryostat ends", "boundary_ids": [0, 2]}]
    trace_set = add_wire_trace(
        project, assets, "x_gradient.csv", b"0,0,0\n0,0.1,0\n", units="cm"
    )
    trace_set["current"] = 2.5
    trace_set["direction"] = "y"
    trace_set["target_gradient"] = 0.001

    restored, restored_assets = unpack_3d_project(package_3d_project(project, assets))

    assert restored["name"] == "Shielded magnet"
    assert restored["geometries"][0]["units"] == "mm"
    assert restored["geometries"][0]["mesh_maxh_mm"] == 30.0
    assert restored["geometries"][0]["domain_selections"] == geometry["domain_selections"]
    assert restored["geometries"][0]["boundary_selections"] == geometry["boundary_selections"]
    assert restored_assets[geometry["asset"]] == STEP
    points = parse_wire_trace(restored_assets[trace_set["traces"][0]["filename"]])
    assert points == [(0.0, 0.0, 0.0), (0.0, 0.001, 0.0)]
    config = build_wire_trace_sets(restored, {trace_set["traces"][0]["filename"]: "winding.txt"})
    assert config[0]["name"] == "x_gradient"
    assert config[0]["direction"] == "y"
    assert config[0]["current"] == 2.5
    assert config[0]["target_gradient"] == 0.001
    assert config[0]["dsv"] == 0.4
    assert config[0]["samples"] == 41
    assert config[0]["traces"][0]["filename"] == "winding.txt"
    paths = materialize_3d_assets(restored, restored_assets, tmp_path / "run")
    solver_inputs = build_3d_solver_inputs(restored, paths)
    assert solver_inputs["geometries"][0]["unit_scale"] == 0.001
    assert solver_inputs["wire_trace_sets"][0]["traces"][0]["filename"].endswith(".txt")


@pytest.mark.parametrize("maxh", [0, -1, float("nan"), float("inf"), "invalid"])
def test_project_rejects_invalid_persisted_mesh_size(maxh):
    project = new_3d_project()
    assets = {}
    geometry = add_step_geometry(project, assets, "shield.step", STEP)
    geometry["mesh_maxh_mm"] = maxh

    with pytest.raises(ValueError, match="Invalid maximum mesh size"):
        package_3d_project(project, assets)


@pytest.mark.parametrize("maxh", [0, -1, float("nan"), float("inf"), "invalid"])
def test_project_rejects_invalid_domain_mesh_size(maxh):
    project = new_3d_project()
    assets = {}
    geometry = add_step_geometry(project, assets, "shield.step", STEP)
    geometry["domain_selections"] = [{
        "id": "domain-1",
        "name": "Shield volume",
        "domain_ids": [0],
        "mesh_maxh_mm": maxh,
    }]

    with pytest.raises(ValueError, match="Invalid local mesh size|Local maximum element size"):
        package_3d_project(project, assets)


def test_project_rejects_conflicting_local_mesh_sizes_on_overlapping_selections():
    project = new_3d_project()
    assets = {}
    geometry = add_step_geometry(project, assets, "shield.step", STEP)
    geometry["domain_selections"] = [
        {"id": "domain-1", "name": "Core", "domain_ids": [0], "mesh_maxh_mm": 2.0},
        {"id": "domain-2", "name": "Refined core", "domain_ids": [0], "mesh_maxh_mm": 1.0},
    ]

    with pytest.raises(ValueError, match="conflicting local mesh sizes"):
        package_3d_project(project, assets)


def test_older_geometry_without_named_selection_fields_remains_compatible():
    project = new_3d_project()
    assets = {}
    geometry = add_step_geometry(project, assets, "shield.step", STEP)
    geometry.pop("domain_selections")
    geometry.pop("boundary_selections")

    restored, _assets = unpack_3d_project(package_3d_project(project, assets))

    assert restored["geometries"][0]["domain_selections"] == []
    assert restored["geometries"][0]["boundary_selections"] == []


@pytest.mark.parametrize(
    "key,selection,error",
    [
        ("boundary_selections", {"id": "s1", "name": "", "boundary_ids": [0]}, "needs a name"),
        ("boundary_selections", {"id": "s1", "name": "Outside", "boundary_ids": [True]}, "non-negative integers"),
        ("domain_selections", {"id": "s1", "name": "Core", "domain_ids": []}, "must include"),
        ("domain_selections", {"id": "s1", "name": "Core", "domain_ids": [0, 0]}, "must be unique"),
    ],
)
def test_project_rejects_invalid_named_region_selections(key, selection, error):
    project = new_3d_project()
    assets = {}
    geometry = add_step_geometry(project, assets, "shield.step", STEP)
    geometry[key] = [selection]

    with pytest.raises(ValueError, match=error):
        package_3d_project(project, assets)


@pytest.mark.parametrize(
    "condition,tags,error",
    [
        ("unsupported_surface_load", ["MMFEM_FACE_0_0"], "Unsupported 3D boundary condition"),
        ("magnetic_potential_zero", None, "needs saved STEP face identifiers"),
        ("magnetic_potential_zero", ["missing-face-id"], "valid STEP face identifiers"),
        (
            "magnetic_potential_zero",
            ["MMFEM_FACE_0_0", "MMFEM_FACE_0_1"],
            "one-to-one",
        ),
    ],
)
def test_project_rejects_unsupported_or_unmapped_3d_boundary_conditions(condition, tags, error):
    project = new_3d_project()
    assets = {}
    geometry = add_step_geometry(project, assets, "shield.step", STEP)
    selection = {
        "id": "fixed-face",
        "name": "Fixed magnetic boundary",
        "boundary_ids": [0],
        "boundary_condition": condition,
    }
    if tags is not None:
        selection["boundary_tags"] = tags
    geometry["boundary_selections"] = [selection]

    with pytest.raises(ValueError, match=error):
        validate_3d_project(project, assets)


def test_project_archive_rejects_corrupt_asset_checksum():
    project = new_3d_project()
    assets = {}
    geometry = add_step_geometry(project, assets, "shield.step", STEP)
    project["geometries"][0]["sha256"] = hashlib.sha256(b"wrong").hexdigest()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        import json

        archive.writestr("project.json", json.dumps(project))
        archive.writestr(geometry["asset"], assets[geometry["asset"]])
    with pytest.raises(ValueError, match="checksum"):
        unpack_3d_project(output.getvalue())


def test_project_archive_rejects_unexpected_or_unsafe_entries():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("project.json", "{}")
        archive.writestr("../outside.txt", "bad")
    with pytest.raises(ValueError, match="Unsafe path"):
        unpack_3d_project(output.getvalue())


@pytest.mark.parametrize("filename", ["golay_x_trace.txt", "golay_y_trace.txt"])
def test_grouped_golay_trace_fixtures_parse_as_si_coordinates(filename):
    from pathlib import Path

    path = Path(__file__).parent / "fixtures" / filename
    points = parse_wire_trace(path.read_bytes())

    assert len(points) > 1000
    assert None in points
    assert all(len(point) == 3 for point in points if point is not None)
