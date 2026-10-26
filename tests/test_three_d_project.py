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
    package_3d_project,
    parse_wire_trace,
    unpack_3d_project,
    step_unit_scale,
)


STEP = (
    b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\n"
    b"#1=(LENGTH_UNIT() NAMED_UNIT(*) SI_UNIT(.MILLI.,.METRE.));\n"
    b"ENDSEC;\nEND-ISO-10303-21;\n"
)


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
    trace_set = add_wire_trace(
        project, assets, "x_gradient.csv", b"0,0,0\n0,0.1,0\n", units="cm"
    )
    trace_set["current"] = 2.5
    trace_set["direction"] = "y"
    trace_set["target_gradient"] = 0.001

    restored, restored_assets = unpack_3d_project(package_3d_project(project, assets))

    assert restored["name"] == "Shielded magnet"
    assert restored["geometries"][0]["units"] == "mm"
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
