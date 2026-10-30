import json

from ngsolve_gui.run_history import load_run_result_tree


def test_run_result_tree_exposes_default_field_alternates_and_mesh(tmp_path):
    output = tmp_path / "run"
    gui = output / "ngsolve_gui"
    fields = gui / "fields"
    fields.mkdir(parents=True)
    default_path = gui / "solution.pkl"
    potential_path = fields / "potential.pkl"
    flux_path = fields / "flux_density.pkl"
    mesh_path = output / "mesh.vol"
    for path in (default_path, potential_path, flux_path, mesh_path):
        path.write_bytes(b"result")
    (output / "run_manifest.json").write_text(
        json.dumps({"schema": "mm-fem.run", "mesh": {"file": "mesh.vol"}}),
        encoding="utf-8",
    )
    (gui / "metadata.json").write_text(
        json.dumps({
            "default_solution_file": "solution.pkl",
            "default_field": {
                "field_key": "gfAAC",
                "snapshot_index": 0,
                "label": "AC magnetic vector potential [Wb/m]",
            },
            "mesh_file": "mesh.vol",
            "field_files": [
                {
                    "file": "fields/potential.pkl",
                    "field_key": "gfAAC",
                    "snapshot_index": 0,
                    "label": "AC magnetic vector potential [Wb/m]",
                },
                {
                    "file": "fields/flux_density.pkl",
                    "field_key": "gfBAC",
                    "snapshot_index": 0,
                    "label": "AC magnetic flux density [T]",
                },
            ],
        }),
        encoding="utf-8",
    )

    tree = load_run_result_tree({
        "id": "run-1",
        "name": "Study: Copper coil",
        "status": "Complete",
        "output_path": str(output),
    })

    assert tree["default_path"] == str(potential_path.resolve())
    assert [item["label"] for item in tree["results"]] == [
        "AC magnetic vector potential [Wb/m] (default)",
        "AC magnetic flux density [T]",
        "Generated mesh",
    ]
    assert [item["kind"] for item in tree["results"]] == ["field", "field", "mesh"]


def test_run_result_tree_ignores_missing_and_outside_files(tmp_path):
    output = tmp_path / "run"
    gui = output / "ngsolve_gui"
    fields = gui / "fields"
    fields.mkdir(parents=True)
    outside_path = tmp_path / "outside.pkl"
    outside_path.write_bytes(b"do not open")
    (gui / "metadata.json").write_text(
        json.dumps({
            "default_solution_file": "../../outside.pkl",
            "field_files": [
                {"file": "../../outside.pkl", "label": "Outside"},
                {"file": "fields/missing.pkl", "label": "Missing"},
            ],
        }),
        encoding="utf-8",
    )

    tree = load_run_result_tree({"output_path": str(output)})

    assert tree["default_path"] is None
    assert tree["results"] == []


def test_run_result_tree_resolves_axisymmetric_mesh_beside_metadata(tmp_path):
    output = tmp_path / "axisymmetric-run"
    gui = output / "ngsolve_gui"
    fields = gui / "fields"
    fields.mkdir(parents=True)
    field_path = fields / "Displacement_AC.pkl"
    mesh_path = gui / "mesh.vol"
    field_path.write_bytes(b"field")
    mesh_path.write_bytes(b"mesh")
    (gui / "metadata.json").write_text(
        json.dumps({
            "default_solution_file": "fields/Displacement_AC.pkl",
            "mesh_file": "mesh.vol",
            "field_files": [{
                "file": "fields/Displacement_AC.pkl",
                "label": "Time-harmonic displacement",
                "field_key": "gfuAC",
                "snapshot_index": 0,
            }],
        }),
        encoding="utf-8",
    )

    tree = load_run_result_tree({"output_path": str(output)})

    assert tree["default_path"] == str(field_path.resolve())
    assert [item["label"] for item in tree["results"]] == [
        "Time-harmonic displacement (default)",
        "Generated mesh",
    ]
    assert tree["results"][1]["path"] == str(mesh_path.resolve())
