"""Helpers for turning a saved solver run into navigable result entries."""

import json
from pathlib import Path


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _execution_status_label(manifest):
    """Name process completion and the numerical outcome separately."""
    execution_status = str(
        manifest.get("execution_status", manifest.get("status", "complete"))
    ).casefold()
    status = execution_status.replace("_", " ").title()
    if status == "Running":
        return "Interrupted"
    if status != "Complete":
        return status

    raw_scientific_status = manifest.get("scientific_status")
    scientific_status = (
        str(raw_scientific_status).casefold()
        if isinstance(raw_scientific_status, str) and raw_scientific_status.strip()
        else "not_reported"
    )
    if scientific_status in {"nonconverged", "nonconverged_diagnostic_data"}:
        return "Execution finished · not converged"
    if scientific_status in {"converged", "all_frequencies_converged"}:
        return "Execution finished · converged"
    if scientific_status == "partial":
        return "Execution finished · partial results"
    if scientific_status == "unavailable":
        return "Execution finished · no valid results"
    if scientific_status == "not_reported":
        return "Execution finished · numerical status unavailable"
    return status


def _output_file(output_root, base_directory, value):
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if not path.is_absolute():
        path = base_directory / path
    try:
        path = path.resolve()
        path.relative_to(output_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return path if path.is_file() else None


def _diagnostic_summary(manifest):
    """Build a compact, truthful run-history summary from persisted diagnostics."""
    rom_settings = manifest.get("mechanical_rom")
    rom_diagnostics = manifest.get("rom_diagnostics")
    rom_text = ""
    if isinstance(rom_settings, dict) and rom_settings.get("enabled") is True:
        requested = rom_settings.get("modes")
        actual = rom_diagnostics.get("actual_modes") if isinstance(rom_diagnostics, dict) else None
        count = (
            f"{actual}/{requested} modes"
            if isinstance(actual, int) and isinstance(requested, int)
            else f"{requested} requested modes"
            if isinstance(requested, int)
            else "modal basis"
        )
        rom_text = f"Mechanical modal ROM · {count}"
        setup_time = rom_diagnostics.get("basis_construction_seconds") if isinstance(rom_diagnostics, dict) else None
        if isinstance(setup_time, (int, float)) and setup_time >= 0:
            rom_text += f" · basis {setup_time:.2f} s"
        online_time = (
            rom_diagnostics.get("online_harmonic_solve_seconds")
            if isinstance(rom_diagnostics, dict) else None
        )
        if isinstance(online_time, (int, float)) and online_time >= 0:
            rom_text += f" · harmonic solve {online_time:.2f} s"
        pinvit = rom_diagnostics.get("pinvit") if isinstance(rom_diagnostics, dict) else None
        if isinstance(pinvit, dict):
            iterations = pinvit.get("iterations")
            if pinvit.get("converged") is True and isinstance(iterations, int):
                rom_text += f" · PINVIT converged in {iterations} iterations"
            elif pinvit.get("converged") is False:
                rom_text += " · PINVIT did not converge"

    def with_rom_summary(summary):
        return f"{rom_text} · {summary}" if rom_text and summary else rom_text or summary

    frequency_points = manifest.get("frequency_points")
    if isinstance(frequency_points, list) and frequency_points:
        labels = []
        coupling = manifest.get("coupling")
        if not isinstance(coupling, dict):
            coupling = {}
        for point in frequency_points:
            if not isinstance(point, dict):
                continue
            frequency = point.get("frequency_hz")
            try:
                frequency_label = f"{float(frequency):g} Hz"
            except (TypeError, ValueError, OverflowError):
                frequency_label = f"Point {int(point.get('index', len(labels))) + 1}"
            execution = str(point.get("execution_status", "pending")).casefold()
            scientific = str(point.get("scientific_status", "pending")).casefold()
            if execution == "complete" and scientific == "converged":
                state = "converged"
            elif execution == "complete" and scientific == "nonconverged":
                state = "not converged"
            elif execution in {"failed", "cancelled", "not_started", "running", "pending"}:
                state = execution.replace("_", " ")
            else:
                state = "completed"
            detail = ""
            if isinstance(point.get("iterations"), int) and isinstance(
                coupling.get("maximum_iterations"), int
            ):
                detail = f" ({point['iterations']}/{coupling['maximum_iterations']} iterations"
                residual_a = point.get("residual_A")
                residual_u = point.get("residual_u")
                if isinstance(residual_a, (int, float)) and isinstance(residual_u, (int, float)):
                    detail += f"; ΔA {residual_a:.2g}, Δu {residual_u:.2g}"
                detail += ")"
            elif state == "not converged" and isinstance(point.get("termination_reason"), str):
                detail = f" ({point['termination_reason'].replace('_', ' ')})"
            labels.append(f"{frequency_label}: {state}{detail}")
        progress = manifest.get("progress", {})
        completed = progress.get("completed_frequencies")
        total = progress.get("total_frequencies", len(frequency_points))
        count = (
            f"{completed}/{total} frequency points completed · "
            if isinstance(completed, int) and isinstance(total, int)
            else ""
        )
        dc_source = manifest.get("dc_background_field_source")
        dc_text = " · DC field reused" if dc_source == "reused" else (
            " · DC field calculated" if dc_source == "computed" else ""
        )
        return with_rom_summary(count + " · ".join(labels) + dc_text)

    convergence = manifest.get("coupling_convergence")
    if isinstance(convergence, dict):
        points = convergence.get("frequencies")
        point = points[0] if isinstance(points, list) and points else {}
        if not isinstance(point, dict):
            point = {}
        converged = point.get("converged") is True and convergence.get("all_converged") is True
        if converged:
            state = "Converged"
        elif point.get("termination_reason") == "maximum_iterations_reached":
            state = "Not converged · maximum iterations reached · diagnostic fields only"
        else:
            state = "Not converged · diagnostic fields only"
        iteration_count = point.get("iterations")
        maximum_iterations = convergence.get("maximum_iterations")
        iteration_text = ""
        if isinstance(iteration_count, int) and isinstance(maximum_iterations, int):
            iteration_text = f" · {iteration_count}/{maximum_iterations} iterations"
        residual_text = ""
        residual_a = point.get("residual_A")
        residual_u = point.get("residual_u")
        tolerance = point.get("relative_tolerance", convergence.get("relative_tolerance"))
        if all(isinstance(value, (int, float)) for value in (residual_a, residual_u, tolerance)):
            residual_text = (
                f" · ΔA {residual_a:.3g}, Δu {residual_u:.3g} / tol {tolerance:.3g}"
            )
        dc_source = manifest.get("dc_background_field_source")
        dc_text = ""
        if dc_source == "reused":
            dc_text = " · DC field reused"
        elif dc_source == "computed":
            check = manifest.get("dc_background_field_reuse_check", {})
            check_status = check.get("status") if isinstance(check, dict) else None
            if check_status == "missing":
                dc_text = " · DC field calculated (none saved)"
            elif check_status == "no_compatible_saved_field":
                dc_text = " · DC field recalculated (no compatible saved field)"
            else:
                dc_text = " · DC field calculated"
        return with_rom_summary(state + iteration_text + residual_text + dc_text)

    status = str(manifest.get("status", "")).casefold()
    if status == "failed":
        error = str(manifest.get("error") or "Solver failed")
        return with_rom_summary(f"Failure · {error[:180]}")
    if status == "cancelled":
        return with_rom_summary("Cancelled · partial diagnostic files may be present")
    if status == "interrupted":
        return with_rom_summary("Interrupted · inspect the solver log and process diagnostics")
    return rom_text or None


def load_run_result_tree(run):
    """Return one history record enriched with its existing result files.

    Solver metadata paths are resolved relative to their owning output folder
    and constrained to that run. This keeps a stale or malformed manifest from
    turning a navigator action into an arbitrary file load.
    """
    output_value = run.get("output_path") if isinstance(run, dict) else None
    if not output_value:
        return {**(run or {}), "default_path": None, "results": []}

    try:
        output_root = Path(output_value).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {**run, "default_path": None, "results": []}
    manifest = _read_json(output_root / "run_manifest.json")
    diagnostic_summary = _diagnostic_summary(manifest)
    gui_directory = output_root / "ngsolve_gui"
    metadata = _read_json(gui_directory / "metadata.json")

    fields = []
    seen_paths = set()
    field_files = metadata.get("field_files")
    if not isinstance(field_files, list):
        field_files = []
    for item in field_files:
        if not isinstance(item, dict):
            continue
        path = _output_file(output_root, gui_directory, item.get("file"))
        if path is None:
            continue
        key = str(path).casefold()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        fields.append({
            "kind": "field",
            "path": str(path),
            "label": str(item.get("label") or item.get("field_key") or path.name),
            "field_key": item.get("field_key"),
            "snapshot_index": item.get("snapshot_index"),
        })

    if not fields:
        raw_fields = manifest.get("fields")
        if isinstance(raw_fields, list):
            for value in raw_fields:
                path = _output_file(output_root, output_root, value)
                if path is None:
                    continue
                key = str(path).casefold()
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                fields.append({
                    "kind": "field",
                    "path": str(path),
                    "label": path.name,
                    "field_key": None,
                    "snapshot_index": None,
                })

    default_path = _output_file(
        output_root, gui_directory, metadata.get("default_solution_file")
    )
    default_field = metadata.get("default_field")
    if not isinstance(default_field, dict):
        default_field = {}
    default_key = default_field.get("field_key")
    default_snapshot = default_field.get("snapshot_index")

    results = []
    matched_default = False
    for field in fields:
        matches_default = (
            default_path is not None
            and (
                str(default_path).casefold() == field["path"].casefold()
                or (
                    default_key is not None
                    and field.get("field_key") == default_key
                    and field.get("snapshot_index") == default_snapshot
                )
            )
        )
        if matches_default:
            default_path = Path(field["path"])
            results.append({
                **field,
                "id": field["path"],
                "label": f"{field['label']} (default)",
                "is_default": True,
            })
            matched_default = True
            continue
        results.append({**field, "id": field["path"], "is_default": False})

    if default_path is not None and not matched_default:
        default_label = default_field.get("label") or "Default solution"
        results.insert(0, {
            "id": str(default_path),
            "kind": "field",
            "path": str(default_path),
            "label": f"{default_label} (default)",
            "field_key": default_key,
            "snapshot_index": default_snapshot,
            "is_default": True,
        })
    elif default_path is None and fields:
        default_path = Path(fields[0]["path"])
        results[0]["is_default"] = True
        results[0]["label"] = f"{results[0]['label']} (default)"

    mesh_data = manifest.get("mesh")
    mesh_value = mesh_data.get("file") if isinstance(mesh_data, dict) else None
    mesh_path = _output_file(output_root, output_root, mesh_value)
    if mesh_path is None:
        mesh_path = _output_file(output_root, gui_directory, metadata.get("mesh_file"))
    if mesh_path is None:
        for candidate in (output_root / "mesh.vol", gui_directory / "mesh.vol"):
            mesh_path = _output_file(output_root, output_root, str(candidate))
            if mesh_path is not None:
                break
    if mesh_path is not None:
        results.append({
            "id": str(mesh_path),
            "kind": "mesh",
            "path": str(mesh_path),
            "label": "Generated mesh",
            "is_default": False,
        })

    return {
        **run,
        "default_path": str(default_path) if default_path is not None else None,
        "results": results,
        "scientific_status": manifest.get("scientific_status"),
        "diagnostic_summary": diagnostic_summary,
    }
