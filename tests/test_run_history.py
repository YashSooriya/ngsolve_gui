import json

from ngsolve_gui.run_history import _diagnostic_summary, load_run_result_tree


def test_run_history_identifies_reduced_mechanical_basis_and_build_cost():
    summary = _diagnostic_summary({
        "status": "complete",
        "mechanical_rom": {"enabled": True, "modes": 6},
        "rom_diagnostics": {
            "actual_modes": 6,
            "basis_construction_seconds": 0.125,
            "online_harmonic_solve_seconds": 1.25,
        },
        "frequency_points": [{
            "frequency_hz": 50.0,
            "execution_status": "complete",
            "scientific_status": "not_applicable",
        }],
        "progress": {"completed_frequencies": 1, "total_frequencies": 1},
    })

    assert summary.startswith("Mechanical modal ROM · 6/6 modes · basis 0.12 s")
    assert "harmonic solve 1.25 s" in summary
    assert "50 Hz: completed" in summary


def test_run_history_reports_pinvit_convergence_separately_from_job_status():
    summary = _diagnostic_summary({
        "status": "complete",
        "mechanical_rom": {"enabled": True, "modes": 4},
        "rom_diagnostics": {
            "actual_modes": 4,
            "basis_construction_seconds": 0.25,
            "pinvit": {"converged": True, "iterations": 7},
        },
    })

    assert "PINVIT converged in 7 iterations" in summary


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


def test_run_result_tree_exposes_coupled_convergence_and_dc_reuse(tmp_path):
    output = tmp_path / "coupled-run"
    output.mkdir()
    (output / "run_manifest.json").write_text(json.dumps({
        "schema": "mm-fem.run",
        "status": "complete",
        "scientific_status": "all_frequencies_converged",
        "dc_background_field_source": "reused",
        "coupling_convergence": {
            "all_converged": True,
            "maximum_iterations": 12,
            "relative_tolerance": 1e-4,
            "frequencies": [{
                "converged": True,
                "iterations": 3,
                "residual_A": 2.3e-6,
                "residual_u": 5.1e-7,
                "relative_tolerance": 1e-4,
            }],
        },
    }), encoding="utf-8")

    tree = load_run_result_tree({
        "id": "coupled-run",
        "status": "Complete",
        "output_path": str(output),
    })

    assert tree["scientific_status"] == "all_frequencies_converged"
    assert tree["diagnostic_summary"] == (
        "Converged · 3/12 iterations · ΔA 2.3e-06, Δu 5.1e-07 / tol 0.0001 · DC field reused"
    )


def test_run_result_tree_keeps_nonconvergence_distinct_from_process_failure(tmp_path):
    output = tmp_path / "diagnostic-run"
    output.mkdir()
    (output / "run_manifest.json").write_text(json.dumps({
        "schema": "mm-fem.run",
        "status": "nonconverged",
        "scientific_status": "nonconverged_diagnostic_data",
        "coupling_convergence": {
            "all_converged": False,
            "maximum_iterations": 2,
            "relative_tolerance": 1e-8,
            "frequencies": [{
                "converged": False,
                "termination_reason": "maximum_iterations_reached",
                "iterations": 2,
                "residual_A": 1e-5,
                "residual_u": 2e-5,
                "relative_tolerance": 1e-8,
            }],
        },
    }), encoding="utf-8")

    tree = load_run_result_tree({
        "id": "diagnostic-run",
        "status": "Nonconverged",
        "output_path": str(output),
    })

    assert tree["status"] == "Nonconverged"
    assert tree["scientific_status"] == "nonconverged_diagnostic_data"
    assert tree["diagnostic_summary"].startswith(
        "Not converged · maximum iterations reached · diagnostic fields only · 2/2 iterations"
    )


def test_run_result_tree_lists_each_sweep_point_status(tmp_path):
    output = tmp_path / "sweep-run"
    output.mkdir()
    (output / "run_manifest.json").write_text(json.dumps({
        "schema": "mm-fem.run",
        "status": "complete",
        "execution_status": "complete",
        "scientific_status": "nonconverged",
        "frequency_points_hz": [50.0, 100.0],
        "frequency_points": [
            {"index": 0, "frequency_hz": 50.0, "execution_status": "complete", "scientific_status": "converged", "iterations": 2},
            {"index": 1, "frequency_hz": 100.0, "execution_status": "complete", "scientific_status": "nonconverged", "iterations": 12},
        ],
        "progress": {"completed_frequencies": 2, "total_frequencies": 2},
        "coupling": {"maximum_iterations": 12},
    }), encoding="utf-8")

    tree = load_run_result_tree({
        "id": "sweep-run",
        "status": "Execution finished · not converged",
        "output_path": str(output),
    })

    assert tree["scientific_status"] == "nonconverged"
    assert tree["diagnostic_summary"] == (
        "2/2 frequency points completed · 50 Hz: converged (2/12 iterations) · "
        "100 Hz: not converged (12/12 iterations)"
    )
