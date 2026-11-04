"""Run bounded solver-level coupling-term controls for the 3D GUI model."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np


def _relative_difference(left, right):
    left = np.asarray(left)
    right = np.asarray(right)
    scale = max(float(np.linalg.norm(left)), float(np.linalg.norm(right)), 1e-300)
    return float(np.linalg.norm(left - right) / scale)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--solver-root", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--loaded-model", type=Path, required=True)
    parser.add_argument("--direction-model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    solver_root = args.solver_root.resolve()
    for entry in (solver_root, solver_root / "src"):
        if str(entry) not in sys.path:
            sys.path.insert(0, str(entry))

    from main import main as solver_main
    from ngsolve_gui.three_d_project import (
        materialize_3d_assets,
        unpack_3d_project,
    )
    from ngsolve_gui.three_d_solver import build_3d_study_problem

    project, assets = unpack_3d_project(args.model.read_bytes())
    asset_paths = materialize_3d_assets(
        project, assets, args.output.parent / "coupling-isolation-assets"
    )
    inputs = build_3d_study_problem(project, asset_paths)
    loaded_project, loaded_assets = unpack_3d_project(args.loaded_model.read_bytes())
    loaded_asset_paths = materialize_3d_assets(
        loaded_project, loaded_assets, args.output.parent / "loaded-coupling-assets"
    )
    loaded_inputs = build_3d_study_problem(loaded_project, loaded_asset_paths)
    direction_project, direction_assets = unpack_3d_project(
        args.direction_model.read_bytes()
    )
    direction_asset_paths = materialize_3d_assets(
        direction_project, direction_assets,
        args.output.parent / "direction-coupling-assets",
    )
    direction_inputs = build_3d_study_problem(direction_project, direction_asset_paths)

    variants = [
        ("uncoupled", inputs, 0, 0, 1.0, False),
        ("em_to_mechanics", inputs, 1, 0, 1.0, True),
        ("double_excitation", inputs, 1, 0, 2.0, True),
        ("quadrature_excitation", inputs, 1, 0, 1j, True),
        ("one_way_loaded", loaded_inputs, 1, 0, 1.0, True),
        ("two_way", loaded_inputs, 1, 1, 1.0, True),
        ("two_way_rotated_dc", direction_inputs, 1, 1, 1.0, False),
    ]
    fields = {}
    report = {
        "model": project.get("name", "3D project"),
        "polynomial_order": int(inputs["polynomial_order"]),
        "variants": {},
        "mesh_elements": None,
        "ndof": None,
        "dc_transfer": "computed once and reused on the same mesh and H(div) space",
    }
    mesh = None
    dc_coefficients = None
    for index, (
        name, model_inputs, k_coupling, c_coupling, source_scale, reuse_dc
    ) in enumerate(variants):
        model_problem = model_inputs["problem"]
        problem = dict(model_problem)
        problem["srcdictAC"] = dict(model_problem["srcdictAC"])
        if source_scale != 1.0:
            problem["srcdictAC"] = {
                material: source * source_scale
                for material, source in problem["srcdictAC"].items()
            }
        problem["kCoupCoef"] = k_coupling
        problem["cCoupCoef"] = c_coupling
        if reuse_dc:
            problem["kACCoefDC"] = 0
        else:
            from src._3D.EM.winding_current_source import set_winding_current_source_dc

            set_winding_current_source_dc(problem)

        result = solver_main(
            h_global=model_inputs["h_global"],
            h_local=model_inputs.get("h_local", 0.1),
            p=model_inputs["polynomial_order"],
            problem_name=f"gui_3d_coupling_isolation_{name}",
            problem_data=problem,
            mesh_override=mesh,
            dc_background_field_data=dc_coefficients if reuse_dc else None,
            output_formats=[],
            saveMesh=False,
            out_raw=False,
            plot_vtk=False,
            plot_stress_strain=False,
            compute_stress_strain=False,
            compute_energy=False,
            prevMechSol=False,
            Nfixpoint=30,
            fp_tol=1e-5,
            noconv_cont=True,
            timeHarmonicApproach="FullyCoupled",
            anderson_depth=3,
            anderson_beta=1.0,
            plot_coupled_quants=False,
            outPowerKE=False,
            useIntegrate=False,
        )
        if index == 0:
            mesh = result["mesh"]
            dc_coefficients = np.asarray(result["gfBDCdata"], dtype=np.float64).copy()
            report["mesh_elements"] = int(mesh.ne)
            report["ndof"] = int(result["NDOF"])

        solution_fields = {
            "A": np.asarray(result["gfAACMDdata"][:, 0]).copy(),
            "B": np.asarray(result["gfBACMDdata"][:, 0]).copy(),
            "BDC": np.asarray(result["gfBDCdata"]).copy(),
            "u": np.asarray(result["gfuACMDdata"][:, 0]).copy(),
            "JL": np.asarray(result["JL"][0].vec.FV().NumPy()).copy(),
        }
        fields.update({f"{name}_{field}": values for field, values in solution_fields.items()})
        iterations = int(np.asarray(result["iterations"]).reshape(-1)[0])
        convergence = np.asarray(result["fixed_point_converged"], dtype=bool).reshape(-1)
        converged = bool(convergence[0]) if convergence.size else None
        residual_a = np.asarray(result["residA"], dtype=float)
        residual_u = np.asarray(result["residu"], dtype=float)
        report["variants"][name] = {
            "kCoupCoef": k_coupling,
            "cCoupCoef": c_coupling,
            "excitation_multiplier": str(source_scale),
            "fixed_point_converged": converged,
            "iterations": iterations,
            "residual_A": float(residual_a[0, iterations - 1]),
            "residual_u": float(residual_u[0, iterations - 1]),
            "residual_history_A": [float(value) for value in residual_a[0, :iterations]],
            "residual_history_u": [float(value) for value in residual_u[0, :iterations]],
            "max_abs_displacement_m": float(np.max(np.abs(solution_fields["u"]))),
            "max_abs_lorentz_current": float(np.max(np.abs(solution_fields["JL"]))),
            "dc_coefficients_reused": reuse_dc,
            "dc_solution_mode": "reused" if reuse_dc else "computed",
        }

    report["comparisons"] = {
        "mechanics_from_maxwell_stress": _relative_difference(
            fields["em_to_mechanics_u"], fields["uncoupled_u"]
        ),
        "em_unchanged_when_mechanical_feedback_is_off": _relative_difference(
            fields["em_to_mechanics_B"], fields["uncoupled_B"]
        ),
        "two_way_feedback_changes_ac_field": _relative_difference(
            fields["two_way_B"], fields["one_way_loaded_B"]
        ),
        "double_excitation_field_scaling_error": _relative_difference(
            fields["double_excitation_B"], 2.0 * fields["em_to_mechanics_B"]
        ),
        "double_excitation_displacement_scaling_error": _relative_difference(
            fields["double_excitation_u"], 2.0 * fields["em_to_mechanics_u"]
        ),
        "quadrature_field_phase_error": _relative_difference(
            fields["quadrature_excitation_B"], 1j * fields["em_to_mechanics_B"]
        ),
        "quadrature_displacement_phase_error": _relative_difference(
            fields["quadrature_excitation_u"], 1j * fields["em_to_mechanics_u"]
        ),
        "direction_changed_dc_field": _relative_difference(
            fields["two_way_rotated_dc_BDC"], fields["two_way_BDC"]
        ),
        "direction_changed_ac_field": _relative_difference(
            fields["two_way_rotated_dc_B"], fields["two_way_B"]
        ),
        "direction_changed_displacement": _relative_difference(
            fields["two_way_rotated_dc_u"], fields["two_way_u"]
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    np.savez_compressed(args.output.with_suffix(".npz"), **fields)
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
