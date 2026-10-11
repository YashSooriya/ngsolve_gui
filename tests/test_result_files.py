import json
from pathlib import Path

import pytest

from ngsolve_gui.result_files import (
    extract_solution_bundle,
    make_mesh_bundle,
    make_solution_bundle,
    problem_fingerprint,
    read_mesh_bundle,
)


MODEL = {
    "schema": "test",
    "name": "Nested model",
    "geometry": {"regions": [{"id": "outer", "width": 0.02}]},
    "mesh": {"element_size": "0.001"},
}


def test_mesh_bundle_round_trips_for_the_matching_problem():
    mesh_bytes = b"mesh bytes for test"

    saved = make_mesh_bundle(mesh_bytes, MODEL)

    assert read_mesh_bundle(saved, MODEL) == mesh_bytes


def test_mesh_bundle_rejects_a_different_problem():
    saved = make_mesh_bundle(b"mesh", MODEL)
    changed_model = {**MODEL, "name": "Different model"}

    with pytest.raises(ValueError, match="different problem file"):
        read_mesh_bundle(saved, changed_model)


def test_mesh_bundle_rejects_mesh_corruption():
    saved = make_mesh_bundle(b"mesh", MODEL)
    import io
    import zipfile

    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(saved), "r") as source:
        entries = {name: source.read(name) for name in source.namelist()}
    entries["mesh.vol"] = b"corrupted"
    with zipfile.ZipFile(output, "w") as target:
        for name, data in entries.items():
            target.writestr(name, data)

    with pytest.raises(ValueError, match="integrity check"):
        read_mesh_bundle(output.getvalue(), MODEL)


def test_solution_bundle_round_trips_problem_mesh_and_field_files(tmp_path):
    output = tmp_path / "run"
    (output / "ngsolve_gui" / "fields").mkdir(parents=True)
    (output / "problem.json").write_text(json.dumps(MODEL), encoding="utf-8")
    (output / "studies.json").write_text('{"studies": []}', encoding="utf-8")
    (output / "mesh.vol").write_bytes(b"mesh data")
    (output / "run_manifest.json").write_text(json.dumps({
        "schema": "mm-fem.run", "status": "complete", "kind": "study",
    }), encoding="utf-8")
    (output / "ngsolve_gui" / "metadata.json").write_text(json.dumps({
        "problem_domain": "axisymmetric",
        "field_files": [{"file": "fields/B_DC.pkl", "label": "DC magnetic flux density"}],
    }), encoding="utf-8")
    (output / "ngsolve_gui" / "fields" / "B_DC.pkl").write_bytes(b"fake pickled field")

    bundle, filename = make_solution_bundle(output, MODEL)
    extracted_dir, fields = extract_solution_bundle(bundle)

    assert filename == "Nested model.mmfem-solutions"
    assert Path(fields[0]).read_bytes() == b"fake pickled field"
    assert Path(extracted_dir, "mesh.vol").read_bytes() == b"mesh data"
    assert json.loads(Path(extracted_dir, "problem.json").read_text()) == MODEL


def test_solution_bundle_requires_complete_run_and_exact_problem(tmp_path):
    output = tmp_path / "run"
    (output / "ngsolve_gui" / "fields").mkdir(parents=True)
    (output / "problem.json").write_text(json.dumps(MODEL), encoding="utf-8")
    (output / "run_manifest.json").write_text(json.dumps({
        "status": "failed", "kind": "study",
    }), encoding="utf-8")
    (output / "ngsolve_gui" / "fields" / "B_DC.pkl").write_bytes(b"field")

    with pytest.raises(ValueError, match="completed study"):
        make_solution_bundle(output, MODEL)

    (output / "run_manifest.json").write_text(json.dumps({
        "status": "complete", "kind": "study",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="different problem"):
        make_solution_bundle(output, {**MODEL, "name": "Another model"})


def test_problem_fingerprint_is_key_order_independent():
    assert problem_fingerprint({"a": 1, "b": 2}) == problem_fingerprint({"b": 2, "a": 1})
