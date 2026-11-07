from __future__ import annotations

import copy
import json
import math
import os
import pickle
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ngsolve_gui.axisymmetric_model import builtin_materials
from ngsolve_gui.three_d_project import (
    add_step_geometry,
    add_wire_trace,
    new_3d_project,
    package_3d_project,
)
from ngsolve_gui.three_d_solver import (
    build_3d_study_problem,
    build_time_harmonic_em_problem,
    dc_input_fingerprint_payload,
    material_properties_for_study,
    validate_3d_study_setup,
    validate_time_harmonic_em_setup,
)
from .solver_test_support import SOLVER_UNAVAILABLE_REASON, solver_checkout_available


def _study_project():
    materials = builtin_materials()
    project = new_3d_project("EM setup")
    project["geometries"] = [{
        "id": "geometry-1",
        "name": "shield.step",
        "material": "Aluminium (77 K shield)",
        "mesh_maxh_mm": 10.0,
        "domain_selections": [{
            "id": "domain-1",
            "name": "Inner shield",
            "domain_ids": [0],
            "domain_tags": ["MMFEM_SOLID_000000000000000000000001"],
            "material": "Stainless steel (4 K / OVC)",
        }],
    }]
    project["wire_trace_sets"] = [{
        "id": "trace-1",
        "name": "Coil",
        "material": "Air",
        "traces": [{"material": "Air"}],
    }]
    project["materials"] = [materials[4], materials[5]]
    return project


def _run_packaged_solver_model(
    project, assets, tmp_path, name, *, expected_status="complete", measure_peak_rss=False
):
    gui_root = Path(__file__).resolve().parents[1]
    solver_candidates = []
    configured_root = os.environ.get("COUPLED_SOLVER_ROOT")
    if configured_root:
        solver_candidates.append(Path(configured_root).expanduser())
    solver_candidates.extend([
        gui_root / "coupled_solver",
        gui_root.parent / "ngsolve-ui" / "coupled_solver",
        Path.cwd() / "coupled_solver",
    ])
    solver_root = next(
        root.resolve()
        for root in solver_candidates
        if (root / "main.py").is_file()
        and (root / "scripts" / "run_gui_3d_model.py").is_file()
    )
    model_path = tmp_path / f"{name}.mmfem3d"
    model_path.write_bytes(package_3d_project(project, assets))
    output_dir = tmp_path / f"{name}-output"
    environment = os.environ.copy()
    environment.pop("COUPLED_SOLVER_OUTPUT_DIR", None)
    environment.pop("COUPLED_SOLVER_SHARED_FILES_DIR", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(gui_root / "src"), *filter(None, [environment.get("PYTHONPATH")])]
    )
    command = [
        sys.executable,
        str(solver_root / "scripts" / "run_gui_3d_model.py"),
        "--model", str(model_path),
        "--output-dir", str(output_dir),
    ]
    peak_rss_bytes = None
    if measure_peak_rss:
        import threading
        import time

        import psutil

        process = subprocess.Popen(
            command,
            cwd=solver_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )
        process_output = []

        def collect_output():
            process_output.extend(process.communicate())

        reader = threading.Thread(target=collect_output, daemon=True)
        reader.start()
        deadline = time.monotonic() + 90
        peak_rss_bytes = 0
        measured_process = psutil.Process(process.pid)
        while process.poll() is None:
            try:
                peak_rss_bytes = max(
                    peak_rss_bytes, measured_process.memory_info().rss
                )
            except psutil.NoSuchProcess:
                break
            except psutil.AccessDenied:
                pass
            if time.monotonic() >= deadline:
                process.kill()
                reader.join(timeout=5)
                raise subprocess.TimeoutExpired(command, 90)
            time.sleep(0.02)
        reader.join(timeout=5)
        completed = subprocess.CompletedProcess(
            command,
            process.returncode,
            process_output[0] if process_output else "",
            process_output[1] if len(process_output) > 1 else "",
        )
    else:
        completed = subprocess.run(
            command,
            cwd=solver_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            env=environment,
            check=False,
        )
    solver_output = re.sub(
        re.escape(str(tmp_path)), "<temporary-run>",
        f"{completed.stdout}\n{completed.stderr}", flags=re.IGNORECASE,
    )
    assert completed.returncode == 0, solver_output[-5000:]
    manifest = json.loads(
        (output_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["status"] == expected_status, solver_output[-5000:]
    if measure_peak_rss:
        manifest["_test_peak_rss_bytes"] = peak_rss_bytes
    return manifest, output_dir


def _load_snapshot_coefficients(output_dir, manifest, field_key, snapshot_index):
    import numpy as np

    marker = f"snapshot_{snapshot_index:04d}_{field_key}"
    field_path = next(path for path in manifest["fields"] if marker in path)
    with (output_dir / field_path).open("rb") as stream:
        field = pickle.load(stream)
    return np.asarray(field.vec.FV().NumPy()).copy()


def _assert_solver_meshes_equivalent(left_output, right_output):
    """Compare the discrete meshes, not Netgen's serialized OCC metadata."""
    import numpy as np
    import ngsolve

    left = ngsolve.Mesh(str(left_output / "mesh.vol"))
    right = ngsolve.Mesh(str(right_output / "mesh.vol"))
    assert (left.nv, left.ne) == (right.nv, right.ne)
    assert tuple(left.GetBoundaries()) == tuple(right.GetBoundaries())
    assert tuple(left.GetMaterials()) == tuple(right.GetMaterials())

    left_points = np.asarray(
        [tuple(float(value) for value in point) for point in left.ngmesh.Points()]
    )
    right_points = np.asarray(
        [tuple(float(value) for value in point) for point in right.ngmesh.Points()]
    )
    np.testing.assert_allclose(left_points, right_points, rtol=0.0, atol=1e-12)

    def element_signature(mesh, elements):
        return tuple(
            (int(element.index), tuple(int(vertex.nr) for vertex in element.vertices))
            for element in elements(mesh.ngmesh)
        )

    assert element_signature(left, lambda mesh: mesh.Elements3D()) == element_signature(
        right, lambda mesh: mesh.Elements3D()
    )
    assert element_signature(left, lambda mesh: mesh.Elements2D()) == element_signature(
        right, lambda mesh: mesh.Elements2D()
    )


def test_study_setup_uses_project_material_snapshots_and_single_frequency():
    project = _study_project()

    validated = validate_time_harmonic_em_setup(project)

    assert validated["frequency_hz"] == 500
    assert validated["polynomial_order"] == 1
    assert validated["air_padding"] == 2
    assert validated["materials"]["Stainless steel (4 K / OVC)"]["sigma"] == 1.4e6
    assert validated["materials"]["Aluminium (77 K shield)"]["sigma"] == 33e6
    assert "Air" in validated["materials"]


def test_magnetostatic_validation_requires_only_dc_inputs_and_permeability():
    project = _study_project()
    project["geometries"][0]["material"] = "DC test alloy"
    project["geometries"][0]["domain_selections"] = []
    project["materials"] = [{
        "id": "dc-test-alloy",
        "name": "DC test alloy",
        "properties": {
            "relative_permeability": "2.4",
            "electrical_conductivity": "unused by magnetostatics",
            "youngs_modulus": "unused by magnetostatics",
            "poissons_ratio": "unused by magnetostatics",
            "density": "unused by magnetostatics",
        },
    }]
    project["wire_trace_sets"] = []
    project["study"].update(
        physics="magnetostatic_dc",
        dc_background_field_t=[0.0, 0.0, 0.025],
        frequency_hz="not a DC input",
        em_source_mode="not applicable",
    )

    validated = validate_3d_study_setup(project)

    assert validated["frequency_hz"] is None
    assert validated["em_source_mode"] is None
    assert validated["dc_background_field_t"] == [0.0, 0.0, 0.025]
    assert validated["materials"]["DC test alloy"]["mur"] == 2.4
    assert validated["materials"]["DC test alloy"]["sigma"] == 0.0
    assert material_properties_for_study(
        project["materials"][0], "magnetostatic_dc"
    )["mur"] == 2.4


def test_magnetostatic_builder_maps_real_dc_sources_and_uniform_field_boundary(tmp_path):
    from netgen.occ import Box, OCCGeometry, Pnt
    from ngsolve_gui.three_d_project import step_geometry_tags

    step_path = tmp_path / "dc_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    materials = builtin_materials()
    project = new_3d_project("Static DC block")
    alloy = {
        "id": "dc-alloy",
        "name": "DC test alloy",
        "properties": {
            "relative_permeability": 2.0,
            "electrical_conductivity": 5e7,
            "youngs_modulus": 0.0,
            "poissons_ratio": 0.3,
            "density": 0.0,
        },
    }
    project["materials"] = [alloy]
    project["study"].update(
        physics="magnetostatic_dc",
        dc_background_field_t=[0.0, 0.0, 0.02],
        air_padding=2.5,
    )
    project["wire_trace_sets"] = []
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=alloy["name"],
    )
    geometry["mesh_maxh_mm"] = 5.0
    solid_tags, face_refs = step_geometry_tags(
        list(OCCGeometry(str(step_path)).shape.solids)
    )
    face_tag = next(tag for tag, _solid_tag, _face in face_refs if tag is not None)
    geometry["domain_selections"] = [{
        "id": "dc-current-selection",
        "name": "Driven solid",
        "domain_ids": [0],
        "domain_tags": [solid_tags[0]],
        "dc_current_density_a_per_m2": [1e5, 0.0, 0.0],
        "dc_current_density_suppressed": False,
    }]
    geometry["boundary_selections"] = [{
        "id": "a-zero-selection",
        "name": "Vector potential zero",
        "boundary_ids": [0],
        "boundary_tags": [face_tag],
        "boundary_condition": "magnetic_potential_zero",
    }]

    inputs = build_3d_study_problem(project, {geometry["asset"]: step_path})
    problem = inputs["problem"]

    assert inputs["physics"] == "magnetostatic_dc"
    assert inputs["frequency_hz"] is None
    assert problem["temp_disc"] == "static"
    assert problem["kACCoefDC"] == 1
    assert problem["cACCoef"] == problem["kACCoef"] == 0
    assert problem["statCond"] is True
    assert problem["dirkeysADC"].startswith("DirEM")
    assert "DirEM" in problem["dirconditionADC"]
    assert f"DirEM_G0_{face_tag}" in problem["dirkeysADC"]
    assert "omega" not in problem
    assert problem["murdict"]["_mmfem_g0_" + solid_tags[0]] == 2.0
    assert problem["srcdictDC"]["_mmfem_air"].dim == 3
    assert problem["Aux"]["dc_current_density_by_domain"] == {
        f"{geometry['id']}:{solid_tags[0]}": [1e5, 0.0, 0.0]
    }
    assert inputs["dc_input_fingerprint_payload"]["applied_background_field_t"] == [0.0, 0.0, 0.02]


def test_dc_fingerprint_tracks_only_inputs_that_change_the_background(tmp_path):
    from netgen.occ import Box, Pnt

    step_path = tmp_path / "fingerprint_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    material = {
        "id": "ferrite",
        "name": "Ferrite",
        "properties": {"relative_permeability": 2.0},
    }
    project = new_3d_project("Fingerprint")
    project["materials"] = [material]
    project["study"].update(
        physics="magnetostatic_dc", dc_background_field_t=[0.0, 0.0, 0.01]
    )
    project["wire_trace_sets"] = []
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=material["name"],
    )
    paths = {geometry["asset"]: step_path}

    first = dc_input_fingerprint_payload(project, paths)
    project["study"]["frequency_hz"] = 1e99
    project["study"]["mechanical_body_force_n_m3"] = [8e8, 0.0, 0.0]
    project["study"]["mechanical_rayleigh_beta"] = 0.001
    unrelated = dc_input_fingerprint_payload(project, paths)
    assert first == unrelated
    project["study"]["mesh_geometry_order"] = 4
    changed_geometry_order = dc_input_fingerprint_payload(project, paths)
    assert changed_geometry_order != first
    project["materials"][0]["properties"]["relative_permeability"] = 2.2
    changed_material = dc_input_fingerprint_payload(project, paths)
    assert changed_material != first


def test_coupled_validation_requires_dc_ac_and_mechanical_supports():
    project = _study_project()
    project["wire_trace_sets"] = []
    project["study"].update(
        physics="coupled_magneto_mechanical",
        em_source_mode="domain_current_density",
        dc_background_field_t=[0.0, 0.0, 0.025],
    )
    domain = project["geometries"][0]["domain_selections"][0]
    domain["current_density_amplitude_a_per_m2"] = [1e6, 0.0, 0.0]
    domain["current_density_phase_deg"] = [0.0, 0.0, 0.0]
    project["geometries"][0]["boundary_selections"] = [{
        "id": "support",
        "name": "Fixed support",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000003"],
        "boundary_condition": "mechanical_fixed",
    }]

    validated = validate_3d_study_setup(project)

    assert validated["physics"] == "coupled_magneto_mechanical"
    assert validated["frequency_hz"] == 500.0
    assert validated["dc_background_field_t"] == [0.0, 0.0, 0.025]
    assert validated["coupling_max_iterations"] == 15
    assert validated["coupling_tolerance"] == 1e-4
    assert validated["current_density_by_domain"]

    project["geometries"][0]["boundary_selections"] = []
    with pytest.raises(ValueError, match="fully restrained spring support"):
        validate_3d_study_setup(project)

    project["geometries"][0]["boundary_selections"] = [{
        "id": "support",
        "name": "Fixed support",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000003"],
        "boundary_condition": "mechanical_fixed",
    }]
    project["study"]["dc_background_field_t"] = [0.0, 0.0, 0.0]
    with pytest.raises(ValueError, match="non-zero DC current density"):
        validate_3d_study_setup(project)


def test_coupled_numerical_settings_validate_anderson_requirements():
    project = _study_project()
    project["wire_trace_sets"] = []
    project["study"].update(
        physics="coupled_magneto_mechanical",
        em_source_mode="domain_current_density",
        dc_background_field_t=[0.0, 0.0, 0.025],
    )
    domain = project["geometries"][0]["domain_selections"][0]
    domain["current_density_amplitude_a_per_m2"] = [1e6, 0.0, 0.0]
    domain["current_density_phase_deg"] = [0.0, 0.0, 0.0]
    project["geometries"][0]["boundary_selections"] = [{
        "id": "support",
        "name": "Fixed support",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000003"],
        "boundary_condition": "mechanical_fixed",
    }]

    project["study"].update(coupling_max_iterations=1, coupling_anderson_depth=3)
    with pytest.raises(ValueError, match="Anderson acceleration requires at least 2"):
        validate_3d_study_setup(project)

    project["study"].update(coupling_anderson_depth=0)
    validated = validate_3d_study_setup(project)
    assert validated["coupling_anderson_depth"] == 0
    assert validated["coupling_anderson_relaxation"] == 1.0


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_mesh_geometry_order_and_rayleigh_damping_reach_a_real_3d_solve(tmp_path):
    import numpy as np
    import ngsolve as ngs
    from netgen.occ import Box, OCCGeometry, Pnt, Sphere
    from ngsolve_gui.three_d_project import step_geometry_tags

    step_path = tmp_path / "curved_mechanical_sphere.step"
    Sphere(Pnt(0, 0, 0), 5).WriteStep(str(step_path))
    solid_tags, face_refs = step_geometry_tags(
        list(OCCGeometry(str(step_path)).solids)
    )
    support_tag = next(
        tag for tag, solid_tag, _face in face_refs if solid_tag == solid_tags[0]
    )

    material = builtin_materials()[4]
    project = new_3d_project("Curved mesh and damping verification")
    project["materials"] = [material]
    project["study"].update(
        physics="harmonic_mechanical",
        frequency_hz=50.0,
        polynomial_order=1,
        mechanical_body_force_n_m3=[1e8, 0.0, 0.0],
    )
    assets = {}
    geometry = add_step_geometry(
        project,
        assets,
        step_path.name,
        step_path.read_bytes(),
        units="mm",
        material=material["name"],
    )
    geometry["mesh_maxh_mm"] = 10.0
    geometry["boundary_selections"] = [{
        "id": "fixed-sphere-boundary",
        "name": "Fixed support",
        "boundary_ids": [0],
        "boundary_tags": [support_tag],
        "boundary_condition": "mechanical_fixed",
    }]

    linear_project = copy.deepcopy(project)
    curved_project = copy.deepcopy(project)
    curved_project["study"]["mesh_geometry_order"] = 4
    linear_manifest, linear_dir = _run_packaged_solver_model(
        linear_project, assets, tmp_path, "sphere-geometry-order-1"
    )
    curved_manifest, curved_dir = _run_packaged_solver_model(
        curved_project, assets, tmp_path, "sphere-geometry-order-4"
    )

    # Use a block for damping: unlike a STEP sphere's single topological face,
    # a box gives us a local support face and free displacement degrees of
    # freedom on the remaining faces.
    block_path = tmp_path / "supported_mechanical_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(block_path))
    _block_tags, block_faces = step_geometry_tags(
        list(OCCGeometry(str(block_path)).solids)
    )
    block_support_tag = next(
        tag for tag, solid_tag, _face in block_faces if solid_tag == _block_tags[0]
    )
    damped_project = new_3d_project("Rayleigh damping verification")
    damped_project["materials"] = [material]
    damped_project["study"].update(
        physics="harmonic_mechanical",
        frequency_hz=50.0,
        polynomial_order=1,
        mechanical_body_force_n_m3=[1e8, 0.0, 0.0],
    )
    damped_assets = {}
    block_geometry = add_step_geometry(
        damped_project,
        damped_assets,
        block_path.name,
        block_path.read_bytes(),
        units="mm",
        material=material["name"],
    )
    block_geometry["mesh_maxh_mm"] = 10.0
    block_geometry["boundary_selections"] = [{
        "id": "fixed-block-face",
        "name": "Fixed support",
        "boundary_ids": [0],
        "boundary_tags": [block_support_tag],
        "boundary_condition": "mechanical_fixed",
    }]
    damped_project["study"]["mechanical_rayleigh_beta"] = 0.001
    undamped_project = copy.deepcopy(damped_project)
    undamped_project["study"]["mechanical_rayleigh_beta"] = 0.0
    undamped_manifest, undamped_dir = _run_packaged_solver_model(
        undamped_project, damped_assets, tmp_path, "block-rayleigh-undamped"
    )
    damped_manifest, damped_dir = _run_packaged_solver_model(
        damped_project, damped_assets, tmp_path, "block-rayleigh-damped"
    )

    linear_mesh = ngs.Mesh(str(linear_dir / "mesh.vol"))
    curved_mesh = ngs.Mesh(str(curved_dir / "mesh.vol"))
    assert linear_manifest["mesh_geometry_order"] == linear_mesh.GetCurveOrder() == 1
    assert curved_manifest["mesh_geometry_order"] == curved_mesh.GetCurveOrder() == 4
    exact_sphere_volume = (4.0 / 3.0) * math.pi * 0.005**3
    linear_volume = float(ngs.Integrate(1, linear_mesh))
    curved_volume = float(ngs.Integrate(1, curved_mesh))
    assert abs(curved_volume - exact_sphere_volume) < abs(
        linear_volume - exact_sphere_volume
    ), (linear_volume, curved_volume, exact_sphere_volume)

    reference_displacement = _load_snapshot_coefficients(
        undamped_dir, undamped_manifest, "gfuAC", 0
    )
    damped_displacement = _load_snapshot_coefficients(
        damped_dir, damped_manifest, "gfuAC", 0
    )
    response_ratio = float(
        np.linalg.norm(damped_displacement) / np.linalg.norm(reference_displacement)
    )
    assert np.isfinite([np.linalg.norm(reference_displacement), np.linalg.norm(damped_displacement)]).all()
    assert np.linalg.norm(reference_displacement) > 0
    assert 0 < response_ratio < 1.0
    assert np.linalg.norm(damped_displacement.imag) > 0
    assert damped_manifest["rayleigh_damping"] == {
        "mass_coefficient_per_second": 0.0,
        "stiffness_coefficient_seconds": 0.001,
    }
    print(
        "Curved sphere numerical check: volume order 1/4 "
        f"{linear_volume:.8e}/{curved_volume:.8e} m³ vs "
        f"{exact_sphere_volume:.8e} m³; beta=0.001 s reduced displacement "
        f"to {response_ratio:.6f} of the undamped value."
    )


def test_coupled_builder_configures_existing_solver_for_all_three_physics(tmp_path):
    from netgen.occ import Box, OCCGeometry, Pnt
    from ngsolve_gui.three_d_project import step_geometry_tags

    step_path = tmp_path / "coupled_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    solids = list(OCCGeometry(str(step_path)).solids)
    solid_tags, face_refs = step_geometry_tags(solids)
    support_tag = next(
        tag for tag, solid_tag, _face in face_refs if solid_tag == solid_tags[0]
    )
    material = builtin_materials()[4]
    project = new_3d_project("Coupled block")
    project["materials"] = [material]
    project["study"].update(
        physics="coupled_magneto_mechanical",
        em_source_mode="domain_current_density",
        frequency_hz=50.0,
        polynomial_order=1,
        mesh_geometry_order=4,
        air_padding=0.25,
        dc_background_field_t=[0.0, 0.0, 0.025],
        coupling_max_iterations=12,
        coupling_tolerance=2e-5,
        coupling_anderson_depth=2,
        coupling_anderson_relaxation=0.7,
        mechanical_rayleigh_alpha=1.25,
        mechanical_rayleigh_beta=0.00025,
    )
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=material["name"],
    )
    geometry["mesh_maxh_mm"] = 10.0
    geometry["domain_selections"] = [{
        "id": "ac-source",
        "name": "AC current domain",
        "domain_ids": [0],
        "domain_tags": [solid_tags[0]],
        "current_density_amplitude_a_per_m2": [1e6, 0.0, 0.0],
        "current_density_phase_deg": [0.0, 90.0, 0.0],
    }]
    geometry["boundary_selections"] = [{
        "id": "support",
        "name": "Fixed support",
        "boundary_ids": [0],
        "boundary_tags": [support_tag],
        "boundary_condition": "mechanical_fixed",
    }]

    inputs = build_3d_study_problem(project, {geometry["asset"]: step_path})
    problem = inputs["problem"]
    solid_key = f"_mmfem_g0_{solid_tags[0]}"

    assert inputs["physics"] == "coupled_magneto_mechanical"
    assert inputs["frequency_points_hz"] == [50.0]
    assert problem["omega"] == pytest.approx([2 * math.pi * 50.0])
    assert inputs["coupling_max_iterations"] == 12
    assert inputs["coupling_tolerance"] == pytest.approx(2e-5)
    assert inputs["coupling_anderson_depth"] == 2
    assert inputs["coupling_anderson_relaxation"] == pytest.approx(0.7)
    assert inputs["mesh_geometry_order"] == 4
    assert problem["mesh_curve_order"] == 4
    assert problem["dampingalpha"] == pytest.approx(1.25)
    assert problem["dampingbeta"] == pytest.approx(0.00025)
    assert problem["Aux"]["rayleigh_damping"] == {
        "mass_coefficient_per_second": 1.25,
        "stiffness_coefficient_seconds": 0.00025,
    }
    assert problem["Aux"]["coupling_iteration"] == {
        "method": "block_gauss_seidel",
        "acceleration": "anderson",
        "anderson_depth": 2,
        "anderson_relaxation": 0.7,
        "maximum_iterations": 12,
        "relative_tolerance": pytest.approx(2e-5),
    }
    assert problem["temp_disc"] == "time_harmonic"
    assert problem["cACCoef"] == problem["kACCoef"] == 1
    assert problem["kACCoefDC"] == 1
    assert problem["mMechCoef"] == problem["kMechCoef"] == 1
    assert problem["cCoupCoef"] == problem["kCoupCoef"] == 1
    assert problem["current_source"] == "domain_current_density"
    assert problem["dirkeysMech"]
    assert problem["srcdictAC"][solid_key] != 0
    assert problem["Aux"]["dc_input_fingerprint_payload"]["physics"] == "magnetostatic_dc"
    assert inputs["dc_input_fingerprint_payload"]

    project["study"].update(
        frequency_mode="points",
        frequency_points_hz=[50.0, 100.0],
    )
    sweep = build_3d_study_problem(project, {geometry["asset"]: step_path})
    assert sweep["frequency_hz"] is None
    assert sweep["frequency_points_hz"] == [50.0, 100.0]
    assert sweep["problem"]["omega"] == pytest.approx([100 * math.pi, 200 * math.pi])
    assert sweep["problem"]["Aux"]["frequency_points_hz"] == [50.0, 100.0]


def test_mechanical_setup_accepts_body_force_without_electromagnetic_trace():
    project = _study_project()
    project["wire_trace_sets"] = []
    project["study"].update(
        physics="harmonic_mechanical",
        mechanical_body_force_n_m3=[1e6, 0.0, -2e6],
    )

    validated = validate_time_harmonic_em_setup(project)

    assert validated["physics"] == "harmonic_mechanical"
    assert validated["mechanical_body_force_n_m3"] == [1e6, 0.0, -2e6]
    assert "Air" not in validated["materials"]


def test_em_materials_require_only_em_properties():
    project = _study_project()
    project["geometries"][0]["material"] = "EM-only alloy"
    project["geometries"][0]["domain_selections"] = []
    project["materials"] = [{
        "id": "em-only-alloy",
        "name": "EM-only alloy",
        "properties": {
            "relative_permeability": "1.2",
            "electrical_conductivity": "3.4e6",
            "youngs_modulus": "not used by EM",
            "poissons_ratio": "not used by EM",
            "density": "not used by EM",
        },
    }]

    validated = validate_time_harmonic_em_setup(project)

    assert validated["materials"]["EM-only alloy"] == {
        "name": "EM-only alloy",
        "mur": pytest.approx(1.2),
        "sigma": pytest.approx(3.4e6),
        "youngs": 0.0,
        "poisson": 0.3,
        "density": 0.0,
    }


def test_mechanics_materials_require_only_mechanical_properties():
    project = _study_project()
    project["geometries"][0]["material"] = "Elastic-only material"
    project["geometries"][0]["domain_selections"] = []
    project["materials"] = [{
        "id": "elastic-only",
        "name": "Elastic-only material",
        "properties": {
            "youngs_modulus": "2.1e9",
            "poissons_ratio": "0.29",
            "density": "1200",
            "relative_permeability": "not used by mechanics",
            "electrical_conductivity": "not used by mechanics",
        },
    }]
    project["wire_trace_sets"] = []
    project["study"].update(
        physics="harmonic_mechanical",
        mechanical_body_force_n_m3=[1.0, 0.0, 0.0],
    )

    validated = validate_time_harmonic_em_setup(project)

    assert validated["materials"]["Elastic-only material"] == {
        "name": "Elastic-only material",
        "mur": 1.0,
        "sigma": 0.0,
        "youngs": pytest.approx(2.1e9),
        "poisson": pytest.approx(0.29),
        "density": pytest.approx(1200),
    }


def test_mechanics_rejects_air_as_a_solid_material():
    project = _study_project()
    project["geometries"][0]["material"] = "Air"
    project["geometries"][0]["domain_selections"] = []
    project["materials"] = [builtin_materials()[0]]
    project["wire_trace_sets"] = []
    project["study"].update(
        physics="harmonic_mechanical",
        mechanical_body_force_n_m3=[1.0, 0.0, 0.0],
    )

    with pytest.raises(ValueError, match="Air cannot be assigned to a mechanics solid"):
        validate_time_harmonic_em_setup(project)


@pytest.mark.parametrize(
    ("body_force", "material_change", "message"),
    [
        ([0, 0, 0], None, "non-zero body force"),
        ([1, 0], None, "X, Y and Z"),
        ([1, 0, 0], ("youngs_modulus", 0), "positive Young's modulus"),
        ([1, 0, 0], ("density", -1), "physical range"),
    ],
)
def test_mechanical_setup_rejects_missing_or_invalid_inputs(body_force, material_change, message):
    project = _study_project()
    project["wire_trace_sets"] = []
    project["study"].update(physics="harmonic_mechanical", mechanical_body_force_n_m3=body_force)
    if material_change:
        key, value = material_change
        project["materials"][0]["properties"][key] = value

    with pytest.raises(ValueError, match=message):
        validate_time_harmonic_em_setup(project)


def test_mechanical_builder_sets_structural_sources_supports_and_no_air_domain(tmp_path):
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_mesh import generate_step_mesh

    step_path = tmp_path / "mechanics_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    mesh_result = generate_step_mesh(step_path.read_bytes(), maxh_mm=10.0)
    support_face = mesh_result["boundary_regions"][0]
    materials = builtin_materials()
    project = new_3d_project("Mechanical block")
    project["study"].update(
        physics="harmonic_mechanical",
        frequency_hz=50.0,
        polynomial_order=1,
        mechanical_body_force_n_m3=[1e8, 0.0, 0.0],
    )
    project["materials"] = [materials[4]]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(), units="mm",
        material=materials[4]["name"],
    )
    geometry["material"] = materials[4]["name"]
    geometry["mesh_maxh_mm"] = 10.0
    geometry["boundary_selections"] = [{
        "id": "fixed-face",
        "name": "Fixed support",
        "boundary_ids": [support_face["id"]],
        "boundary_tags": [support_face["boundary_tag"]],
        "boundary_condition": "mechanical_fixed",
    }]

    inputs = build_time_harmonic_em_problem(project, {geometry["asset"]: step_path})
    problem = inputs["problem"]

    assert inputs["physics"] == "harmonic_mechanical"
    assert len(problem["objects"]) == 1
    assert "_mmfem_air" not in problem["objects"]
    assert problem["cACCoef"] == problem["kACCoef"] == 0
    assert problem["mMechCoef"] == problem["kMechCoef"] == 1
    assert problem["electromagnetic_only"] is False
    assert problem["dirkeysMech"].startswith("DirMech_G0_MMFEM_FACE_")
    assert set(problem["dirconditionMech"]) == set(problem["dirkeysMech"].split("|"))
    assert inputs["mechanical_body_force_n_m3"] == [1e8, 0.0, 0.0]


def test_mechanical_builder_maps_domain_force_displacement_and_spring_values(tmp_path):
    from netgen.occ import Box, OCCGeometry, Pnt
    from ngsolve_gui.three_d_project import step_geometry_tags

    step_path = tmp_path / "scoped_mechanical_assignments.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    solids = list(OCCGeometry(str(step_path)).solids)
    solid_tags, face_refs = step_geometry_tags(solids)
    face_tags = [tag for tag, solid_tag, _face in face_refs if solid_tag == solid_tags[0]]
    material = builtin_materials()[4]
    project = new_3d_project("Scoped mechanical assignments")
    project["study"].update(
        physics="harmonic_mechanical",
        frequency_hz=50.0,
        mechanical_body_force_n_m3=[2e5, 0.0, 0.0],
        mechanical_body_force_phase_deg=[0.0, 0.0, 0.0],
    )
    project["materials"] = [material]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=material["name"],
    )
    geometry["domain_selections"] = [{
        "id": "domain-load",
        "name": "Local force",
        "domain_ids": [0],
        "domain_tags": [solid_tags[0]],
        "mechanical_body_force_n_m3": [0.0, 3e5, 0.0],
        "mechanical_body_force_phase_deg": [90.0, 0.0, 0.0],
    }]
    geometry["boundary_selections"] = [
        {
            "id": "prescribed",
            "name": "Driven face",
            "boundary_ids": [0],
            "boundary_tags": [face_tags[0]],
            "boundary_condition": "mechanical_prescribed_displacement",
            "prescribed_displacement_amplitude_m": [1e-6, 0.0, 0.0],
            "prescribed_displacement_phase_deg": [90.0, 0.0, 0.0],
        },
        {
            "id": "spring",
            "name": "Compliant face",
            "boundary_ids": [1],
            "boundary_tags": [face_tags[1]],
            "boundary_condition": "mechanical_spring_support",
            "spring_normal_stiffness_n_per_m3": 2e8,
            "spring_tangential_stiffness_n_per_m3": 5e7,
        },
    ]

    inputs = build_time_harmonic_em_problem(project, {geometry["asset"]: step_path})
    problem = inputs["problem"]
    solid_key = f"_mmfem_g0_{solid_tags[0]}"
    driven_key = f"DirMech_G0_{face_tags[0]}"
    spring_key = f"RobinMech_G0_{face_tags[1]}"

    assert isinstance(problem["srcMech"], dict)
    assert set(problem["srcMech"]) == {solid_key}
    assert driven_key in problem["dirconditionMech"]
    assert problem["robin_bcs"] == [spring_key]
    assert problem["robin_k_normal"][spring_key] == 2e8
    assert problem["robin_k_tangential"][spring_key] == 5e7
    local_force = problem["Aux"]["mechanical_body_force_by_domain"][
        f"{geometry['id']}:{solid_tags[0]}"
    ]
    assert local_force[0] == pytest.approx([0.0, 0.0])
    assert local_force[1] == pytest.approx([3e5, 0.0])
    assert local_force[2] == pytest.approx([0.0, 0.0])
    assert problem["Aux"]["boundary_assignments"][f"{geometry['id']}:{face_tags[0]}"]["value"][0] == [
        pytest.approx(0.0), pytest.approx(1e-6)
    ]


def test_domain_current_density_mode_uses_only_selected_solid_without_wire_trace(tmp_path):
    from netgen.occ import Box, OCCGeometry, Pnt
    from ngsolve_gui.three_d_project import step_geometry_tags

    step_path = tmp_path / "current_density_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    solid_tags, _face_refs = step_geometry_tags(
        list(OCCGeometry(str(step_path)).solids)
    )
    material = builtin_materials()[4]
    project = new_3d_project("Domain current density")
    project["study"].update(em_source_mode="domain_current_density")
    project["materials"] = [material]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=material["name"],
    )
    geometry["domain_selections"] = [{
        "id": "current-domain",
        "name": "Driven solid",
        "domain_ids": [0],
        "domain_tags": [solid_tags[0]],
        "current_density_amplitude_a_per_m2": [1e6, 1e6, -2e6],
        "current_density_phase_deg": [0.0, 90.0, -45.0],
    }]

    validated = validate_time_harmonic_em_setup(project)
    inputs = build_time_harmonic_em_problem(project, {geometry["asset"]: step_path})
    problem = inputs["problem"]
    solid_key = f"_mmfem_g0_{solid_tags[0]}"

    assert validated["em_source_mode"] == "domain_current_density"
    assert inputs["em_source_mode"] == "domain_current_density"
    assert project["wire_trace_sets"] == []
    assert problem["current_source"] == "domain_current_density"
    assert problem["wire_trace_sets"] == []
    assert set(problem["srcdictAC"]) == set(problem["objects"])
    current_y = problem["Aux"]["current_density_by_domain"][
        f"{geometry['id']}:{solid_tags[0]}"
    ][1]
    assert current_y == pytest.approx([0.0, 1e6], abs=1e-9)


def test_inactive_physics_assignments_do_not_require_irrelevant_sources_or_loads():
    project = _study_project()
    project["study"].update(
        physics="harmonic_mechanical",
        em_source_mode="domain_current_density",
        mechanical_body_force_n_m3=[1e6, 0.0, 0.0],
    )
    project["wire_trace_sets"] = []
    project["geometries"][0]["boundary_selections"] = [{
        "id": "magnetic-only",
        "name": "Inactive EM boundary",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000002"],
        "boundary_condition": "magnetic_potential_zero",
    }]

    validated = validate_time_harmonic_em_setup(project)

    assert validated["boundary_assignments"] == {}
    assert validated["mechanical_body_force_n_m3"] == [1e6, 0.0, 0.0]


@pytest.mark.parametrize("supported_solids", [0, 1, 2])
def test_mechanical_builder_accepts_free_and_partially_supported_solids(
    tmp_path, supported_solids
):
    from netgen.occ import Box, Glue, OCCGeometry, Pnt
    from ngsolve_gui.three_d_project import step_geometry_tags

    step_path = tmp_path / "two_free_or_supported_blocks.step"
    Glue([
        Box(Pnt(0, 0, 0), Pnt(10, 10, 10)),
        Box(Pnt(20, 0, 0), Pnt(30, 10, 10)),
    ]).WriteStep(str(step_path))
    materials = builtin_materials()
    project = new_3d_project("Two-solid mechanics setup")
    project["study"].update(
        physics="harmonic_mechanical",
        frequency_hz=50.0,
        mechanical_body_force_n_m3=[1e5, 0.0, 0.0],
    )
    project["materials"] = [materials[4]]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(), units="mm",
        material=materials[4]["name"],
    )
    geometry["material"] = materials[4]["name"]
    geometry["mesh_maxh_mm"] = 20.0
    solids = list(OCCGeometry(str(step_path)).solids)
    _solid_tags, face_refs = step_geometry_tags(solids)
    tags_by_solid = {}
    for face_tag, solid_tag, _face in face_refs:
        if face_tag:
            tags_by_solid.setdefault(solid_tag, face_tag)
    selected_tags = list(tags_by_solid.values())[:supported_solids]
    if selected_tags:
        geometry["boundary_selections"] = [{
            "id": "partial-support",
            "name": "Partial support",
            "boundary_ids": list(range(len(selected_tags))),
            "boundary_tags": selected_tags,
            "boundary_condition": "mechanical_fixed",
        }]

    inputs = build_time_harmonic_em_problem(
        project, {geometry["asset"]: step_path}
    )
    problem = inputs["problem"]

    assert len([key for key in problem["objects"] if key.startswith("_mmfem_g0_")]) == 2
    assert len([key for key in problem["dirkeysMech"].split("|") if key]) == supported_solids
    assert len(problem["dirconditionMech"]) == supported_solids


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda project: project["study"].update(frequency_hz=0), "frequency"),
        (lambda project: project["study"].update(polynomial_order=5), "order"),
        (lambda project: project["study"].update(air_padding=0.1), "padding"),
        (lambda project: project["geometries"][0].update(material="Unassigned"), "physical material"),
        (lambda project: project["geometries"][0].update(material="Shield"), "not embedded"),
        (lambda project: project.update(wire_trace_sets=[]), "wire trace"),
    ],
)
def test_study_setup_rejects_invalid_or_incomplete_inputs(change, message):
    project = copy.deepcopy(_study_project())
    change(project)

    with pytest.raises(ValueError, match=message):
        validate_time_harmonic_em_setup(project)


def test_study_setup_rejects_legacy_order_based_active_domain_assignment():
    project = _study_project()
    project["geometries"][0]["domain_selections"][0].pop("domain_tags")

    with pytest.raises(ValueError, match="order-based geometry ids"):
        validate_time_harmonic_em_setup(project)


def test_study_setup_rejects_conflicting_overlapping_domain_materials():
    project = _study_project()
    project["geometries"][0]["domain_selections"].append({
        "id": "domain-conflict",
        "name": "Conflicting override",
        "domain_ids": [0],
        "domain_tags": ["MMFEM_SOLID_000000000000000000000001"],
        "material": "Aluminium (77 K shield)",
    })

    with pytest.raises(ValueError, match="conflicting material assignments"):
        validate_time_harmonic_em_setup(project)


def test_study_setup_rejects_non_numeric_material_properties():
    project = _study_project()
    project["materials"][0]["properties"]["electrical_conductivity"] = "10 megasiemens"

    with pytest.raises(ValueError, match="numeric electrical conductivity"):
        validate_time_harmonic_em_setup(project)


def test_solver_problem_preserves_local_domain_sizes_and_physical_materials(tmp_path):
    from netgen.occ import Box, Glue, Pnt

    step_path = tmp_path / "two_domains.step"
    Glue([
        Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).mat("First"),
        Box(Pnt(20, 0, 0), Pnt(30, 10, 10)).mat("Second"),
    ]).WriteStep(str(step_path))
    trace_path = tmp_path / "coil.txt"
    trace_path.write_text("0 0 0\n0 0.1 0\n", encoding="utf-8")

    materials = builtin_materials()
    project = new_3d_project("Local mesh setup")
    materials[5]["properties"]["electrical_conductivity"] = "2.75e5"
    project["materials"] = [materials[4], materials[5]]
    assets = {}
    geometry = add_step_geometry(
        project,
        assets,
        step_path.name,
        step_path.read_bytes(),
        units="mm",
        material=materials[4]["name"],
    )
    geometry["material"] = materials[4]["name"]
    geometry["mesh_maxh_mm"] = 10.0
    geometry["domain_selections"] = [{
        "id": "domain-0",
        "name": "Fine domain",
        "domain_ids": [0],
        "material": materials[5]["name"],
        "mesh_maxh_mm": 2.0,
    }]
    from ngsolve_gui.three_d_project import step_geometry_tags
    from netgen.occ import OCCGeometry

    geometry["domain_selections"][0]["domain_tags"] = step_geometry_tags(
        list(OCCGeometry(str(step_path)).solids)
    )[0][:1]
    trace_set = add_wire_trace(project, assets, "coil.txt", trace_path.read_bytes())

    inputs = build_time_harmonic_em_problem(
        project,
        {
            geometry["asset"]: step_path,
            trace_set["traces"][0]["filename"]: trace_path,
        },
    )

    problem = inputs["problem"]
    assert len(problem["objects"]) == 3
    solid_objects = [name for name in problem["objects"] if name.startswith("_mmfem_g0_")]
    assert len(solid_objects) == 2
    assert "_mmfem_air" in problem["objects"]
    refined_object = next(name for name in solid_objects if inputs["domain_maxh"][name] == pytest.approx(0.002))
    coarse_object = next(name for name in solid_objects if inputs["domain_maxh"][name] == pytest.approx(0.01))
    assert refined_object != coarse_object
    assert problem["murdict"][refined_object] == pytest.approx(
        float(materials[5]["properties"]["relative_permeability"])
    )
    assert problem["sigmadict"][refined_object] == pytest.approx(2.75e5)
    assert problem["murdict"][coarse_object] == pytest.approx(
        float(materials[4]["properties"]["relative_permeability"])
    )
    assert inputs["material_display_names"][refined_object] == materials[5]["name"]
    assert inputs["material_display_names"][coarse_object] == materials[4]["name"]


def test_mesh_maps_boundary_regions_to_stable_step_solids(tmp_path):
    from netgen.occ import Box, Glue, OCCGeometry, Pnt
    from ngsolve_gui.three_d_mesh import generate_step_mesh
    from ngsolve_gui.three_d_project import step_geometry_tags

    step_path = tmp_path / "two_named_bodies.step"
    Glue([
        Box(Pnt(0, 0, 0), Pnt(10, 10, 10)),
        Box(Pnt(20, 0, 0), Pnt(30, 10, 10)),
    ]).WriteStep(str(step_path))
    expected_tags, _face_refs = step_geometry_tags(
        list(OCCGeometry(str(step_path)).solids)
    )

    result = generate_step_mesh(step_path.read_bytes(), maxh_mm=10.0)

    assert {item["domain_tag"] for item in result["domain_regions"]} == set(expected_tags)
    assert len(result["boundary_domain_tags"]) == len(result["boundary_region_ids"])
    assert set(result["boundary_domain_tags"]) == set(expected_tags)
    assert all(
        item["domain_tag"] in expected_tags
        for item in result["boundary_regions"]
    )


def test_named_surface_condition_reaches_solver_mesh_and_dirichlet_boundaries(tmp_path):
    import ngsolve as ngs
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_mesh import generate_step_mesh

    step_path = tmp_path / "small_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    trace_path = tmp_path / "coil.txt"
    trace_path.write_text("0 0 0\n0 0.1 0\n", encoding="utf-8")

    mesh_setup = generate_step_mesh(step_path.read_bytes(), maxh_mm=10.0)
    selected_region = mesh_setup["boundary_regions"][0]
    materials = builtin_materials()
    project = new_3d_project("Boundary condition integration")
    project["study"].update(air_padding=0.25, frequency_hz=50.0)
    project["materials"] = [materials[4]]
    assets = {}
    geometry = add_step_geometry(
        project,
        assets,
        step_path.name,
        step_path.read_bytes(),
        units="mm",
        material=materials[4]["name"],
    )
    geometry["material"] = materials[4]["name"]
    geometry["mesh_maxh_mm"] = 10.0
    geometry["boundary_selections"] = [{
        "id": "fixed-face",
        "name": "Fixed magnetic face",
        "boundary_ids": [selected_region["id"]],
        "boundary_tags": [selected_region["boundary_tag"]],
        "boundary_condition": "magnetic_potential_zero",
    }]
    trace_set = add_wire_trace(project, assets, trace_path.name, trace_path.read_bytes())

    inputs = build_time_harmonic_em_problem(
        project,
        {
            geometry["asset"]: step_path,
            trace_set["traces"][0]["filename"]: trace_path,
        },
    )
    problem = inputs["problem"]
    scoped_boundary = f"DirEM_G0_{selected_region['boundary_tag']}"

    assert scoped_boundary in problem["dirkeysAAC"].split("|")
    assert set(problem["dirconditionAAC"]) == set(problem["dirkeysAAC"].split("|"))
    assert problem["Neukeys"] == ["Neu_0", "Transmission_Interface"]
    mesh = ngs.Mesh(problem["geometry"].GenerateMesh(maxh=0.01))
    boundary_names = list(mesh.GetBoundaries())
    scoped_ids = [
        region_id for region_id, name in enumerate(boundary_names)
        if name == scoped_boundary
    ]
    assert len(scoped_ids) == 1
    assert any(
        int(element.index) == scoped_ids[0]
        for element in mesh.Elements(ngs.BND)
    )
    outer_only = ngs.HCurl(mesh, order=1, dirichlet="DirEM", complex=True)
    assigned_boundary = ngs.HCurl(
        mesh,
        order=1,
        dirichlet=problem["dirkeysAAC"],
        complex=True,
    )
    assert (
        assigned_boundary.FreeDofs().NumSet()
        < outer_only.FreeDofs().NumSet()
    )


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_3d_solver_run_completes_with_scoped_surface_condition(tmp_path):
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_mesh import generate_step_mesh

    solver_roots = []
    configured_root = os.environ.get("COUPLED_SOLVER_ROOT")
    if configured_root:
        solver_roots.append(Path(configured_root).expanduser())
    gui_root = Path(__file__).resolve().parents[1]
    solver_roots.extend([
        gui_root / "coupled_solver",
        gui_root.parent / "ngsolve-ui" / "coupled_solver",
        Path.cwd() / "coupled_solver",
    ])
    solver_root = next(
        root.resolve()
        for root in solver_roots
        if (root / "main.py").is_file()
        and (root / "scripts" / "run_gui_3d_model.py").is_file()
    )

    step_path = tmp_path / "small_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    step_data = step_path.read_bytes()
    mesh_setup = generate_step_mesh(step_data, maxh_mm=10.0)
    assert 0 < mesh_setup["volume_elements"] < 50_000
    selected_region = mesh_setup["boundary_regions"][0]
    trace_path = tmp_path / "coil.txt"
    trace_path.write_text(
        "0.005 -0.02 0.005\n0.005 0.03 0.005\n",
        encoding="utf-8",
    )

    materials = builtin_materials()
    project = new_3d_project("Small scoped-boundary solver check")
    project["study"].update(
        frequency_mode="points",
        frequency_points_hz=[50.0, 100.0],
        polynomial_order=1,
        air_padding=0.25,
    )
    project["materials"] = [materials[4]]
    assets = {}
    geometry = add_step_geometry(
        project,
        assets,
        step_path.name,
        step_data,
        units="mm",
        material=materials[4]["name"],
    )
    geometry["material"] = materials[4]["name"]
    geometry["mesh_maxh_mm"] = 10.0
    geometry["boundary_selections"] = [{
        "id": "fixed-face",
        "name": "Fixed magnetic face",
        "boundary_ids": [selected_region["id"]],
        "boundary_tags": [selected_region["boundary_tag"]],
        "boundary_condition": "magnetic_potential_zero",
    }]
    add_wire_trace(project, assets, trace_path.name, trace_path.read_bytes())
    model_path = tmp_path / "small_scoped_boundary.mmfem3d"
    model_path.write_bytes(package_3d_project(project, assets))
    output_dir = tmp_path / "solver-output"
    environment = os.environ.copy()
    environment.pop("COUPLED_SOLVER_OUTPUT_DIR", None)
    environment.pop("COUPLED_SOLVER_SHARED_FILES_DIR", None)
    python_paths = [str(gui_root / "src")]
    if environment.get("PYTHONPATH"):
        python_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)

    completed = subprocess.run(
        [
            sys.executable,
            str(solver_root / "scripts" / "run_gui_3d_model.py"),
            "--model",
            str(model_path),
            "--output-dir",
            str(output_dir),
        ],
        cwd=solver_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        env=environment,
        check=False,
    )
    solver_output = re.sub(
        re.escape(str(tmp_path)),
        "<temporary-run>",
        f"{completed.stdout}\n{completed.stderr}",
        flags=re.IGNORECASE,
    )
    assert completed.returncode == 0, solver_output[-5000:]
    if solver_output.strip():
        print(solver_output.rstrip())
    manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["execution_status"] == "complete"
    assert manifest["frequency_points_hz"] == [50.0, 100.0]
    assert all(
        point["execution_status"] == "complete"
        and point["scientific_status"] == "not_applicable"
        for point in manifest["frequency_points"]
    )
    assert manifest["polynomial_order"] == 1
    assert 0 < manifest["mesh"]["elements"] < 50_000
    assert manifest["fields"]
    flux_field = next(field for field in manifest["fields"] if "gfBAC" in field)
    import ngsolve as ngs

    solver_mesh = ngs.Mesh(str(output_dir / "mesh.vol"))
    with (output_dir / flux_field).open("rb") as stream:
        flux_density = pickle.load(stream)
    probe_point = (0.0025, 0.005, 0.005)
    value = flux_density(solver_mesh(*probe_point))
    magnitude = math.sqrt(sum(abs(component) ** 2 for component in value))
    assert math.isfinite(magnitude) and magnitude > 1e-12

    import numpy as np

    em_sweep_differences = {}
    for snapshot_index, frequency_hz in enumerate((50.0, 100.0)):
        single_project = copy.deepcopy(project)
        single_project["study"].update(
            frequency_mode="single",
            frequency_hz=frequency_hz,
        )
        single_manifest, single_dir = _run_packaged_solver_model(
            single_project,
            assets,
            tmp_path,
            f"em-single-{frequency_hz:g}-hz",
        )
        assert single_manifest["frequency_points_hz"] == [frequency_hz]
        assert (single_dir / "mesh.vol").read_bytes() == (
            output_dir / "mesh.vol"
        ).read_bytes()
        sweep_values = _load_snapshot_coefficients(
            output_dir, manifest, "gfBAC", snapshot_index
        )
        single_values = _load_snapshot_coefficients(
            single_dir, single_manifest, "gfBAC", 0
        )
        relative_l2 = np.linalg.norm(sweep_values - single_values) / np.linalg.norm(
            single_values
        )
        assert relative_l2 < 1e-8
        em_sweep_differences[frequency_hz] = relative_l2
    print(
        "EM sweep versus same-mesh single-frequency solves: "
        + ", ".join(
            f"{frequency:g} Hz relative L2={difference:.3e}"
            for frequency, difference in em_sweep_differences.items()
        )
    )

    changed_project = copy.deepcopy(project)
    changed_project["study"].update(
        frequency_mode="single",
        frequency_hz=50.0,
    )
    changed_material = copy.deepcopy(materials[4])
    changed_material["properties"]["electrical_conductivity"] = "0"
    changed_project["materials"] = [changed_material]
    changed_model_path = tmp_path / "small_scoped_boundary_insulating.mmfem3d"
    changed_model_path.write_bytes(package_3d_project(changed_project, assets))
    changed_trace = changed_project["wire_trace_sets"][0]["traces"][0]
    adapted_problem = build_time_harmonic_em_problem(
        changed_project,
        {
            geometry["asset"]: step_path,
            changed_trace["filename"]: trace_path,
        },
    )["problem"]
    solid_solver_name = next(
        name for name in adapted_problem["murdict"] if name.startswith("_mmfem_g0_")
    )
    assert adapted_problem["sigmadict"][solid_solver_name] == 0
    assert adapted_problem["inoroutdict"][solid_solver_name] == 0
    changed_output_dir = tmp_path / "solver-output-insulating"
    changed_run = subprocess.run(
        [
            sys.executable,
            str(solver_root / "scripts" / "run_gui_3d_model.py"),
            "--model",
            str(changed_model_path),
            "--output-dir",
            str(changed_output_dir),
        ],
        cwd=solver_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        env=environment,
        check=False,
    )
    changed_output = re.sub(
        re.escape(str(tmp_path)),
        "<temporary-run>",
        f"{changed_run.stdout}\n{changed_run.stderr}",
        flags=re.IGNORECASE,
    )
    assert changed_run.returncode == 0, changed_output[-5000:]
    changed_manifest = json.loads(
        (changed_output_dir / "run_manifest.json").read_text(encoding="utf-8")
    )
    assert changed_manifest["status"] == "complete"
    solver_configuration = json.loads(
        (changed_output_dir / changed_manifest["problem_file"]).read_text(encoding="utf-8")
    )
    solid_material_name = next(
        name for name in solver_configuration["materials"] if name.startswith("_mmfem_g0_")
    )
    assert solver_configuration["materials"][solid_material_name][
        "electrical_conductivity"
    ] == 0
    changed_flux_field = next(
        field for field in changed_manifest["fields"] if "gfBAC" in field
    )
    changed_mesh = ngs.Mesh(str(changed_output_dir / "mesh.vol"))
    with (changed_output_dir / changed_flux_field).open("rb") as stream:
        changed_flux_density = pickle.load(stream)
    changed_value = changed_flux_density(changed_mesh(*probe_point))
    changed_magnitude = math.sqrt(
        sum(abs(component) ** 2 for component in changed_value)
    )
    assert math.isfinite(changed_magnitude)
    material_response_delta = changed_magnitude - magnitude
    assert not math.isclose(magnitude, changed_magnitude, rel_tol=0, abs_tol=1e-12)
    print(
        "Bounded material-response comparison: "
        f"{solver_mesh.ne} volume elements at probe {probe_point}, "
        f"|B| sigma=1.4e6 S/m {magnitude:.12g} T; "
        f"sigma=0 S/m {changed_magnitude:.12g} T; "
        f"delta={material_response_delta:.6e} T"
    )


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_3d_mechanical_solver_run_exports_nonzero_displacement(tmp_path):
    import numpy as np

    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_mesh import generate_step_mesh

    solver_roots = []
    configured_root = os.environ.get("COUPLED_SOLVER_ROOT")
    if configured_root:
        solver_roots.append(Path(configured_root).expanduser())
    gui_root = Path(__file__).resolve().parents[1]
    solver_roots.extend([
        gui_root / "coupled_solver",
        gui_root.parent / "ngsolve-ui" / "coupled_solver",
        Path.cwd() / "coupled_solver",
    ])
    solver_root = next(
        root.resolve()
        for root in solver_roots
        if (root / "main.py").is_file()
        and (root / "scripts" / "run_gui_3d_model.py").is_file()
    )

    step_path = tmp_path / "small_mechanical_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    mesh_setup = generate_step_mesh(step_path.read_bytes(), maxh_mm=10.0)
    assert 0 < mesh_setup["volume_elements"] < 50_000
    support_face = mesh_setup["boundary_regions"][0]

    materials = builtin_materials()
    project = new_3d_project("Small mechanical support check")
    project["study"].update(
        physics="harmonic_mechanical",
        frequency_mode="points",
        frequency_points_hz=[50.0, 100.0],
        polynomial_order=1,
        mechanical_body_force_n_m3=[1e8, 0.0, 0.0],
    )
    project["materials"] = [materials[4]]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=materials[4]["name"],
    )
    geometry["material"] = materials[4]["name"]
    geometry["mesh_maxh_mm"] = 10.0
    geometry["boundary_selections"] = [{
        "id": "fixed-face",
        "name": "Fixed support",
        "boundary_ids": [support_face["id"]],
        "boundary_tags": [support_face["boundary_tag"]],
        "boundary_condition": "mechanical_fixed",
    }]

    model_path = tmp_path / "small_mechanics.mmfem3d"
    model_path.write_bytes(package_3d_project(project, assets))
    output_dir = tmp_path / "mechanical-solver-output"
    environment = os.environ.copy()
    environment.pop("COUPLED_SOLVER_OUTPUT_DIR", None)
    environment.pop("COUPLED_SOLVER_SHARED_FILES_DIR", None)
    python_paths = [str(gui_root / "src")]
    if environment.get("PYTHONPATH"):
        python_paths.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(python_paths)

    completed = subprocess.run(
        [
            sys.executable,
            str(solver_root / "scripts" / "run_gui_3d_model.py"),
            "--model", str(model_path),
            "--output-dir", str(output_dir),
        ],
        cwd=solver_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        env=environment,
        check=False,
    )
    solver_output = re.sub(
        re.escape(str(tmp_path)), "<temporary-run>",
        f"{completed.stdout}\n{completed.stderr}", flags=re.IGNORECASE,
    )
    assert completed.returncode == 0, solver_output[-5000:]
    manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["frequency_points_hz"] == [50.0, 100.0]
    assert all(
        point["execution_status"] == "complete"
        and point["scientific_status"] == "not_applicable"
        for point in manifest["frequency_points"]
    )
    assert manifest["physics"] == "Time-harmonic mechanics"
    assert manifest["mechanical_body_force_n_m3"] == [1e8, 0.0, 0.0]
    assert "air_padding" not in manifest
    with (output_dir / next(
        field for field in manifest["fields"] if "snapshot_0000_gfuAC" in field
    )).open("rb") as stream:
        displacement = pickle.load(stream)
    displacement_values = displacement.vec.FV().NumPy()
    assert displacement_values.size > 0
    assert all(math.isfinite(float(value.real)) for value in displacement_values)
    assert float(abs(displacement_values).max()) > 0
    second_displacement = _load_snapshot_coefficients(output_dir, manifest, "gfuAC", 1)
    assert second_displacement.size == displacement_values.size
    assert float(abs(second_displacement).max()) > 0
    free_dofs = displacement.space.FreeDofs()
    constrained_dofs = [
        index for index in range(displacement.space.ndof)
        if not free_dofs[index]
    ]
    assert constrained_dofs, "The selected mechanical support constrained no DOFs."
    assert float(abs(displacement_values[constrained_dofs]).max()) <= 1e-12
    for snapshot_index, frequency_hz in enumerate((50.0, 100.0)):
        project["study"].update(frequency_mode="single", frequency_hz=frequency_hz)
        single_manifest, single_dir = _run_packaged_solver_model(
            project, assets, tmp_path, f"mechanical-single-{frequency_hz:g}-hz"
        )
        assert (single_dir / "mesh.vol").read_bytes() == (output_dir / "mesh.vol").read_bytes()
        sweep_values = _load_snapshot_coefficients(
            output_dir, manifest, "gfuAC", snapshot_index
        )
        single_values = _load_snapshot_coefficients(
            single_dir, single_manifest, "gfuAC", 0
        )
        relative_l2 = np.linalg.norm(sweep_values - single_values) / np.linalg.norm(single_values)
        assert relative_l2 < 1e-8
    print(
        f"Bounded mechanical run: {manifest['mesh']['elements']} elements, "
        f"max |u|={float(abs(displacement_values).max()):.6g} m"
    )


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_3d_modal_rom_reduces_mechanics_and_matches_full_order_sweep(tmp_path):
    import numpy as np
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_mesh import generate_step_mesh

    step_path = tmp_path / "small_rom_mechanical_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    mesh_setup = generate_step_mesh(step_path.read_bytes(), maxh_mm=10.0)
    assert 0 < mesh_setup["volume_elements"] < 1_000

    material = builtin_materials()[4]
    project = new_3d_project("Small full-order versus mechanical ROM")
    project["study"].update(
        physics="harmonic_mechanical",
        frequency_mode="points",
        frequency_points_hz=[50.0, 100.0],
        polynomial_order=1,
        mechanical_body_force_n_m3=[1e8, 0.0, 0.0],
    )
    project["materials"] = [material]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=material["name"],
    )
    geometry["mesh_maxh_mm"] = 10.0
    support_face = mesh_setup["boundary_regions"][0]
    geometry["boundary_selections"] = [{
        "id": "fixed-face",
        "name": "Fixed support",
        "boundary_ids": [support_face["id"]],
        "boundary_tags": [support_face["boundary_tag"]],
        "boundary_condition": "mechanical_fixed",
    }]

    full, full_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "small-rom-full-order"
    )
    project["study"].update(
        mechanical_solution_method="modal_rom",
        mechanical_rom={
            "modes": 6,
            "eigen_solver": "scipy",
            "pinvit_max_iterations": 80,
            "pinvit_tolerance": 1e-8,
            "pinvit_residual_tolerance": 1e-4,
            "static_enrichment": True,
        },
    )
    reduced_6, reduced_6_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "small-rom-6-modes"
    )
    project["study"]["mechanical_rom"]["modes"] = 13
    reduced_13, reduced_13_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "small-rom-13-modes"
    )
    pinvit_project = copy.deepcopy(project)
    pinvit_project["study"]["mechanical_rom"].update(
        modes=3,
        eigen_solver="pinvit",
        pinvit_max_iterations=100,
        pinvit_tolerance=1e-8,
        pinvit_residual_tolerance=1e-4,
        static_enrichment=True,
    )
    pinvit, pinvit_dir = _run_packaged_solver_model(
        pinvit_project, assets, tmp_path, "small-rom-pinvit"
    )

    _assert_solver_meshes_equivalent(full_dir, reduced_6_dir)
    _assert_solver_meshes_equivalent(full_dir, reduced_13_dir)
    _assert_solver_meshes_equivalent(full_dir, pinvit_dir)
    assert full["mechanical_rom"]["enabled"] is False
    assert reduced_6["mechanical_rom"]["enabled"] is True
    assert reduced_6["mechanical_rom"]["basis_lifecycle"].startswith("built at run start")
    assert reduced_6["rom_diagnostics"]["method"] == "mechanical_modal"
    assert reduced_6["rom_diagnostics"]["actual_modes"] == 6
    assert reduced_6["rom_diagnostics"]["static_enrichment"] is True
    assert reduced_13["rom_diagnostics"]["actual_modes"] == 13
    assert reduced_13["rom_diagnostics"]["free_mechanical_dofs"] > 13
    assert reduced_6["rom_diagnostics"]["online_harmonic_solve_seconds"] > 0
    assert reduced_13["rom_diagnostics"]["online_harmonic_solve_seconds"] > 0
    assert pinvit["mechanical_rom"]["eigen_solver"] == "pinvit"
    assert pinvit["rom_diagnostics"]["actual_modes"] == 3
    assert pinvit["rom_diagnostics"]["pinvit"]["converged"] is True
    assert pinvit["rom_diagnostics"]["pinvit"]["iterations"] >= 1
    assert len(reduced_6["frequency_points"]) == len(reduced_13["frequency_points"]) == 2

    errors_by_modes = {6: {}, 13: {}}
    for index, frequency_hz in enumerate((50.0, 100.0)):
        full_values = _load_snapshot_coefficients(full_dir, full, "gfuAC", index)
        for modes, run_manifest, run_dir in (
            (6, reduced_6, reduced_6_dir),
            (13, reduced_13, reduced_13_dir),
        ):
            reduced_values = _load_snapshot_coefficients(run_dir, run_manifest, "gfuAC", index)
            errors_by_modes[modes][frequency_hz] = float(
                np.linalg.norm(reduced_values - full_values) / np.linalg.norm(full_values)
            )
        assert errors_by_modes[13][frequency_hz] < errors_by_modes[6][frequency_hz]

    assert reduced_6["performance"]["solver_wall_seconds"] > reduced_6["rom_diagnostics"]["basis_construction_seconds"]
    assert reduced_13["performance"]["solver_wall_seconds"] > reduced_13["rom_diagnostics"]["basis_construction_seconds"]

    from ngsolve_gui.three_d_project import unpack_3d_project

    saved_project, _ = unpack_3d_project(
        (reduced_13_dir / reduced_13["project_snapshot"]).read_bytes()
    )
    assert saved_project["study"]["mechanical_solution_method"] == "modal_rom"
    assert saved_project["study"]["mechanical_rom"]["modes"] == 13
    print(
        "Small 3D mechanical ROM convergence: "
        f"{reduced_13['mesh']['elements']} elements, basis setup "
        f"6 modes {reduced_6['rom_diagnostics']['basis_construction_seconds']:.3f} s / "
        f"13 modes {reduced_13['rom_diagnostics']['basis_construction_seconds']:.3f} s, "
        f"solver wall full {full['performance']['solver_wall_seconds']:.3f} s / "
        f"ROM 6 {reduced_6['performance']['solver_wall_seconds']:.3f} s / "
        f"ROM 13 {reduced_13['performance']['solver_wall_seconds']:.3f} s; "
        + "; ".join(
            f"{frequency:g} Hz error 6 modes={errors_by_modes[6][frequency]:.3e}, "
            f"13 modes={errors_by_modes[13][frequency]:.3e}"
            for frequency in errors_by_modes[6]
        )
    )


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_bounded_mid_size_3d_rom_repeated_sweep_resource_benchmark(tmp_path):
    """Measure a few-hundred-element model without treating timings as a gate."""
    import numpy as np
    from netgen.occ import Box, OCCGeometry, Pnt

    from ngsolve_gui.three_d_project import step_geometry_tags

    step_path = tmp_path / "mid_size_rom_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    solid_tags, face_refs = step_geometry_tags(
        list(OCCGeometry(str(step_path)).solids)
    )
    support_tag = next(
        tag for tag, solid_tag, _face in face_refs if solid_tag == solid_tags[0]
    )
    material = builtin_materials()[4]
    project = new_3d_project("Bounded mid-size ROM performance check")
    project["materials"] = [material]
    project["study"].update(
        physics="harmonic_mechanical",
        frequency_mode="points",
        frequency_points_hz=[50.0, 100.0, 150.0],
        polynomial_order=1,
        mechanical_body_force_n_m3=[1e8, 0.0, 0.0],
    )
    assets = {}
    geometry = add_step_geometry(
        project,
        assets,
        step_path.name,
        step_path.read_bytes(),
        units="mm",
        material=material["name"],
    )
    geometry["mesh_maxh_mm"] = 2.5
    geometry["boundary_selections"] = [{
        "id": "fixed-benchmark-face",
        "name": "Fixed support",
        "boundary_ids": [0],
        "boundary_tags": [support_tag],
        "boundary_condition": "mechanical_fixed",
    }]

    full, full_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "mid-size-rom-full-order", measure_peak_rss=True
    )
    project["study"].update(
        mechanical_solution_method="modal_rom",
        mechanical_rom={
            "modes": 6,
            "eigen_solver": "scipy",
            "pinvit_max_iterations": 80,
            "pinvit_tolerance": 1e-8,
            "pinvit_residual_tolerance": 1e-4,
            "static_enrichment": True,
        },
    )
    first_rom, first_rom_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "mid-size-rom-first-run", measure_peak_rss=True
    )
    repeated_rom, repeated_rom_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "mid-size-rom-repeated-run", measure_peak_rss=True
    )

    assert 100 <= full["mesh"]["elements"] < 2_000
    _assert_solver_meshes_equivalent(full_dir, first_rom_dir)
    _assert_solver_meshes_equivalent(full_dir, repeated_rom_dir)
    assert len(full["frequency_points"]) == 3
    assert first_rom["mechanical_rom"]["enabled"] is True
    assert repeated_rom["mechanical_rom"]["basis_lifecycle"].startswith(
        "built at run start"
    )
    for run in (full, first_rom, repeated_rom):
        assert run["_test_peak_rss_bytes"] > 0
        assert run["performance"]["solver_wall_seconds"] > 0
    for run in (first_rom, repeated_rom):
        assert run["rom_diagnostics"]["basis_construction_seconds"] > 0
        assert run["rom_diagnostics"]["online_harmonic_solve_seconds"] > 0
        assert len(run["frequency_points"]) == 3

    frequency_errors = []
    for index, frequency_hz in enumerate((50.0, 100.0, 150.0)):
        full_values = _load_snapshot_coefficients(full_dir, full, "gfuAC", index)
        rom_values = _load_snapshot_coefficients(first_rom_dir, first_rom, "gfuAC", index)
        relative_error = float(
            np.linalg.norm(rom_values - full_values) / np.linalg.norm(full_values)
        )
        assert np.isfinite(relative_error)
        frequency_errors.append({
            "frequency_hz": frequency_hz,
            "rom_relative_displacement_error": relative_error,
        })

    free_dofs = first_rom["rom_diagnostics"]["free_mechanical_dofs"]
    actual_modes = first_rom["rom_diagnostics"]["actual_modes"]
    dense_basis_storage_estimate = free_dofs * actual_modes * 8
    evidence = {
        "mesh_elements": full["mesh"]["elements"],
        "mesh_vertices": full["mesh"]["vertices"],
        "polynomial_order": full["polynomial_order"],
        "frequencies_hz": [50.0, 100.0, 150.0],
        "full_order_solver_wall_seconds": full["performance"]["solver_wall_seconds"],
        "rom_first_solver_wall_seconds": first_rom["performance"]["solver_wall_seconds"],
        "rom_repeat_solver_wall_seconds": repeated_rom["performance"]["solver_wall_seconds"],
        "rom_basis_build_seconds": first_rom["rom_diagnostics"]["basis_construction_seconds"],
        "rom_repeat_basis_build_seconds": repeated_rom["rom_diagnostics"]["basis_construction_seconds"],
        "rom_online_harmonic_solve_seconds": first_rom["rom_diagnostics"]["online_harmonic_solve_seconds"],
        "free_mechanical_dofs": free_dofs,
        "actual_modes": actual_modes,
        "dense_basis_storage_estimate_bytes_float64": dense_basis_storage_estimate,
        "peak_rss_bytes_full_order": full["_test_peak_rss_bytes"],
        "peak_rss_bytes_rom_first": first_rom["_test_peak_rss_bytes"],
        "peak_rss_bytes_rom_repeat": repeated_rom["_test_peak_rss_bytes"],
        "frequency_displacement_errors": frequency_errors,
        "basis_persisted_between_runs": False,
    }
    benchmark_path = Path(
        os.environ.get(
            "MAGMECHSIM_ROM_BENCHMARK_REPORT",
            str(tmp_path / "rom_resource_benchmark.json"),
        )
    )
    benchmark_path.parent.mkdir(parents=True, exist_ok=True)
    benchmark_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print("Bounded mid-size ROM resource benchmark: " + json.dumps(evidence, sort_keys=True))


def _bounded_coupled_project(
    tmp_path,
    name="Bounded coupled integration check",
    *,
    material_name="Stainless steel (4 K / OVC)",
    mechanical_body_force_n_m3=(0.0, 0.0, 0.0),
):
    from netgen.occ import Box, OCCGeometry, Pnt
    from ngsolve_gui.three_d_project import step_geometry_tags

    step_path = tmp_path / f"{name.replace(' ', '_').casefold()}_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    materials = {item["name"]: item for item in builtin_materials()}
    material = materials[material_name]
    solid_tags, face_refs = step_geometry_tags(list(OCCGeometry(str(step_path)).solids))
    support_tag = next(
        tag for tag, solid_tag, _face in face_refs if solid_tag == solid_tags[0]
    )

    project = new_3d_project(name)
    project["materials"] = [material]
    project["study"].update(
        physics="coupled_magneto_mechanical",
        em_source_mode="domain_current_density",
        frequency_hz=50.0,
        polynomial_order=1,
        air_padding=0.25,
        dc_background_field_t=[0.0, 0.0, 0.025],
        mechanical_body_force_n_m3=list(mechanical_body_force_n_m3),
        coupling_max_iterations=12,
        coupling_tolerance=1e-4,
    )
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=material["name"],
    )
    geometry["mesh_maxh_mm"] = 10.0
    geometry["domain_selections"] = [{
        "id": "ac-excitation",
        "name": "AC current source",
        "domain_ids": [0],
        "domain_tags": [solid_tags[0]],
        "current_density_amplitude_a_per_m2": [1e6, 0.0, 0.0],
        "current_density_phase_deg": [0.0, 0.0, 0.0],
    }]
    geometry["boundary_selections"] = [{
        "id": "mechanical-support",
        "name": "Fixed support",
        "boundary_ids": [0],
        "boundary_tags": [support_tag],
        "boundary_condition": "mechanical_fixed",
    }]
    return project, assets


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_coupled_rom_reduces_only_mechanics_and_retains_converged_feedback(tmp_path):
    import numpy as np

    project, assets = _bounded_coupled_project(
        tmp_path, "Bounded coupled ROM comparison"
    )
    full, full_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "bounded-coupled-rom-full-order"
    )
    # Reuse the just-computed DC background so the full-order timing is
    # comparable with the later ROM runs, which also reuse that field.
    full_reused, full_reused_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "bounded-coupled-rom-full-order-reused-dc"
    )
    project["study"].update(
        mechanical_solution_method="modal_rom",
        mechanical_rom={
            "modes": 6,
            "eigen_solver": "scipy",
            "pinvit_max_iterations": 80,
            "pinvit_tolerance": 1e-8,
            "pinvit_residual_tolerance": 1e-4,
            "static_enrichment": True,
        },
    )
    reduced_6, reduced_6_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "bounded-coupled-rom-6-modes"
    )
    project["study"]["mechanical_rom"]["modes"] = 13
    reduced_13, reduced_13_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "bounded-coupled-rom-13-modes"
    )

    for reduced in (reduced_6, reduced_13):
        assert reduced["dc_input_fingerprint"] == full["dc_input_fingerprint"]
        assert reduced["dc_background_field_source"] == "reused"
        assert reduced["mechanical_rom"]["enabled"] is True
        assert reduced["mechanical_rom"]["scope"] == (
            "mechanics block only; electromagnetics remains full order"
        )
        assert reduced["coupling_convergence"]["all_converged"] is True
    assert full_reused["dc_input_fingerprint"] == full["dc_input_fingerprint"]
    assert full_reused["dc_background_field_source"] == "reused"
    assert full_reused["coupling_convergence"]["all_converged"] is True
    assert reduced_6["rom_diagnostics"]["actual_modes"] == 6
    assert reduced_13["rom_diagnostics"]["actual_modes"] == 13
    _assert_solver_meshes_equivalent(full_dir, full_reused_dir)
    _assert_solver_meshes_equivalent(full_dir, reduced_6_dir)
    _assert_solver_meshes_equivalent(full_dir, reduced_13_dir)

    relative_errors = {6: {}, 13: {}}
    for field_key in ("gfAAC", "gfBAC", "gfuAC"):
        full_values = _load_snapshot_coefficients(
            full_reused_dir, full_reused, field_key, 0
        )
        for modes, run_manifest, run_dir in (
            (6, reduced_6, reduced_6_dir),
            (13, reduced_13, reduced_13_dir),
        ):
            reduced_values = _load_snapshot_coefficients(run_dir, run_manifest, field_key, 0)
            relative_errors[modes][field_key] = float(
                np.linalg.norm(reduced_values - full_values) / np.linalg.norm(full_values)
            )
    assert relative_errors[13]["gfuAC"] < relative_errors[6]["gfuAC"]
    # The EM block stays full order. Its coupled changes should remain within
    # the configured fixed-point tolerance, but need not improve monotonically
    # as the mechanical basis grows once differences are near solver noise.
    for field_key in ("gfAAC", "gfBAC"):
        assert max(relative_errors[6][field_key], relative_errors[13][field_key]) < (
            project["study"]["coupling_tolerance"]
        )
    error_summary = "; ".join(
        f"{key} error 6={relative_errors[6][key]:.3e}, "
        f"13={relative_errors[13][key]:.3e}"
        for key in relative_errors[6]
    )
    print(
        "Bounded coupled ROM: "
        f"{reduced_13['mesh']['elements']} elements, "
        f"{reduced_6['coupling_convergence']['frequencies'][0]['iterations']}/"
        f"{reduced_13['coupling_convergence']['frequencies'][0]['iterations']} iterations, "
        f"basis setup 6/13 modes "
        f"{reduced_6['rom_diagnostics']['basis_construction_seconds']:.3f}/"
        f"{reduced_13['rom_diagnostics']['basis_construction_seconds']:.3f} s; "
        f"{error_summary}; solver wall full reused-DC "
        f"{full_reused['performance']['solver_wall_seconds']:.3f} s / "
        f"ROM 6 {reduced_6['performance']['solver_wall_seconds']:.3f} s / "
        f"ROM 13 {reduced_13['performance']['solver_wall_seconds']:.3f} s"
    )


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_bounded_coupled_solver_uses_dc_and_reuses_only_the_matching_mesh(tmp_path):
    import numpy as np

    project, assets = _bounded_coupled_project(tmp_path)
    project["study"].update(
        coupling_anderson_depth=2,
        coupling_anderson_relaxation=0.75,
    )

    first, first_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "bounded-coupled-first"
    )
    assert first["physics"] == "Fully coupled magneto-mechanics"
    assert first["mesh"]["elements"] < 1_000
    assert first["dc_background_field_source"] == "computed"
    assert first["dc_background_field_reuse_check"]["status"] == "missing"
    assert first["coupling_convergence"]["all_converged"] is True
    assert first["coupling"]["acceleration"] == "anderson"
    assert first["coupling"]["anderson_depth"] == 2
    assert first["coupling"]["anderson_relaxation"] == 0.75
    assert first["coupling_convergence"]["anderson_depth"] == 2
    assert first["coupling_convergence"]["anderson_relaxation"] == 0.75
    assert first["solver_configuration"]["coupling_iteration"]["anderson_depth"] == 2
    convergence = first["coupling_convergence"]["frequencies"][0]
    assert convergence["converged"] is True
    assert 1 <= convergence["iterations"] <= 12
    assert convergence["residual_A"] < 1e-4
    assert convergence["residual_u"] < 1e-4
    assert (first_dir / "dc_background_field.npz").is_file()
    assert any("gfBDC" in path for path in first["fields"])
    assert any("gfBAC" in path for path in first["fields"])
    displacement_path = next(path for path in first["fields"] if "gfuAC" in path)
    with (first_dir / displacement_path).open("rb") as stream:
        displacement = pickle.load(stream)
    displacement_values = displacement.vec.FV().NumPy()
    assert np.isfinite(displacement_values).all()
    first_displacement_magnitude = float(np.abs(displacement_values).max())
    assert first_displacement_magnitude > 0

    # Frequency is harmonic-only; the exact-mesh DC state remains valid.
    project["study"].update(
        frequency_mode="points",
        frequency_points_hz=[50.0, 60.0],
    )
    second, second_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "bounded-coupled-second"
    )
    assert second["status"] == "complete"
    assert second["frequency_points_hz"] == [50.0, 60.0]
    assert [point["scientific_status"] for point in second["frequency_points"]] == [
        "converged", "converged"
    ]
    assert second["dc_input_fingerprint"] == first["dc_input_fingerprint"]
    assert second["dc_background_field_source"] == "reused"
    assert second["dc_background_field_reuse_check"]["status"] == "reused"
    assert Path(second["dc_background_field_reused_from"]).resolve() == first_dir.resolve()
    assert second["mesh"]["elements"] == first["mesh"]["elements"]
    def assert_within_coupling_tolerance(reference, candidate):
        relative_l2 = np.linalg.norm(candidate - reference) / np.linalg.norm(reference)
        assert relative_l2 < project["study"]["coupling_tolerance"]
        return relative_l2

    first_frequency_differences = {}
    for field_key in ("gfAAC", "gfBAC", "gfuAC"):
        first_frequency_differences[field_key] = assert_within_coupling_tolerance(
            _load_snapshot_coefficients(first_dir, first, field_key, 0),
            _load_snapshot_coefficients(second_dir, second, field_key, 0),
        )

    project["study"].update(frequency_mode="single", frequency_hz=60.0)
    single_60, single_60_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "bounded-coupled-60-hz-single"
    )
    assert single_60["dc_background_field_source"] == "reused"
    second_frequency_differences = {}
    for field_key in ("gfAAC", "gfBAC", "gfuAC"):
        second_frequency_differences[field_key] = assert_within_coupling_tolerance(
            _load_snapshot_coefficients(single_60_dir, single_60, field_key, 0),
            _load_snapshot_coefficients(second_dir, second, field_key, 1),
        )
    print(
        "Sweep versus separate coupled solves: "
        "50 Hz relative L2="
        + ", ".join(f"{key} {value:.3e}" for key, value in first_frequency_differences.items())
        + "; "
        "60 Hz relative L2="
        + ", ".join(f"{key} {value:.3e}" for key, value in second_frequency_differences.items())
    )

    # A DC-only input change invalidates that cache and affects the coupled
    # mechanical response through the existing Lorentz/Maxwell load path.
    project["study"].update(
        dc_background_field_t=[0.0, 0.0, 0.05],
        coupling_max_iterations=12,
        coupling_tolerance=1e-4,
    )
    changed_dc, changed_dc_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "bounded-coupled-changed-dc"
    )
    assert changed_dc["status"] == "complete"
    assert changed_dc["dc_input_fingerprint"] != first["dc_input_fingerprint"]
    assert changed_dc["dc_background_field_source"] == "computed"
    assert (
        changed_dc["dc_background_field_reuse_check"]["status"]
        == "no_compatible_saved_field"
    )
    changed_displacement_path = next(
        path for path in changed_dc["fields"] if "gfuAC" in path
    )
    with (changed_dc_dir / changed_displacement_path).open("rb") as stream:
        changed_displacement = pickle.load(stream)
    changed_displacement_magnitude = float(
        np.abs(changed_displacement.vec.FV().NumPy()).max()
    )
    assert math.isfinite(changed_displacement_magnitude)
    assert not math.isclose(
        first_displacement_magnitude,
        changed_displacement_magnitude,
        rel_tol=1e-4,
        abs_tol=1e-12,
    )

    # A strict, two-iteration limit verifies the separate diagnostic outcome.
    project["study"].update(
        coupling_max_iterations=2,
        coupling_tolerance=1e-12,
    )
    nonconverged, _nonconverged_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "bounded-coupled-nonconverged",
        expected_status="complete",
    )
    assert nonconverged["execution_status"] == "complete"
    assert nonconverged["scientific_status"] == "nonconverged"
    assert nonconverged["coupling_convergence"]["all_converged"] is False
    point = nonconverged["coupling_convergence"]["frequencies"][0]
    assert point["iterations"] == 2
    assert max(point["residual_A"], point["residual_u"]) >= 1e-12
    print(
        "Bounded coupled verification: "
        f"{first['mesh']['elements']} elements; converged in "
        f"{convergence['iterations']} iterations with residuals "
        f"({convergence['residual_A']:.3e}, {convergence['residual_u']:.3e}); "
        f"max |u|={first_displacement_magnitude:.6e} m; "
        f"harmonic-only change DC cache={second['dc_background_field_source']}; "
        f"changed DC source max |u|={changed_displacement_magnitude:.6e} m; "
        f"bounded nonconvergence residuals=({point['residual_A']:.3e}, "
        f"{point['residual_u']:.3e})"
    )


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_3d_model_isolates_both_coupling_terms_and_complex_response(tmp_path):
    """Exercise the actual GUI-built problem with the two existing terms isolated."""
    import copy

    project, assets = _bounded_coupled_project(
        tmp_path,
        "Bounded coupling-term isolation",
        material_name="Stainless steel (4 K / OVC)",
    )
    loaded_project = copy.deepcopy(project)
    loaded_project["name"] = "Bounded coupling-term isolation with body force"
    loaded_project["study"]["mechanical_body_force_n_m3"] = [0.0, 2e10, 0.0]
    direction_project = copy.deepcopy(loaded_project)
    direction_project["name"] = "Bounded coupling-term isolation with rotated DC field"
    direction_project["study"]["dc_background_field_t"] = [0.025, 0.0, 0.0]
    gui_root = Path(__file__).resolve().parents[1]
    solver_candidates = []
    configured_root = os.environ.get("COUPLED_SOLVER_ROOT")
    if configured_root:
        solver_candidates.append(Path(configured_root).expanduser())
    solver_candidates.extend([
        gui_root / "coupled_solver",
        gui_root.parent / "ngsolve-ui" / "coupled_solver",
        Path.cwd() / "coupled_solver",
    ])
    solver_root = next(
        root.resolve()
        for root in solver_candidates
        if (root / "main.py").is_file()
        and (root / "scripts" / "run_gui_3d_model.py").is_file()
    )
    model_path = tmp_path / "coupling-term-isolation.mmfem3d"
    model_path.write_bytes(package_3d_project(project, assets))
    loaded_model_path = tmp_path / "coupling-term-isolation-loaded.mmfem3d"
    loaded_model_path.write_bytes(package_3d_project(loaded_project, assets))
    direction_model_path = tmp_path / "coupling-term-isolation-rotated-dc.mmfem3d"
    direction_model_path.write_bytes(package_3d_project(direction_project, assets))
    report_path = tmp_path / "coupling-term-isolation.json"
    helper = gui_root / "tests" / "fixtures" / "run_3d_coupling_isolation.py"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(gui_root / "src"), *filter(None, [environment.get("PYTHONPATH")])]
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(helper),
            "--solver-root", str(solver_root),
            "--model", str(model_path),
            "--loaded-model", str(loaded_model_path),
            "--direction-model", str(direction_model_path),
            "--output", str(report_path),
        ],
        cwd=solver_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        env=environment,
        check=False,
    )
    output = re.sub(
        re.escape(str(tmp_path)), "<temporary-run>",
        f"{completed.stdout}\n{completed.stderr}", flags=re.IGNORECASE,
    )
    assert completed.returncode == 0, output[-7000:]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    variants = report["variants"]
    comparisons = report["comparisons"]

    assert report["mesh_elements"] < 1_000
    assert report["ndof"] < 6_000
    assert variants["em_to_mechanics"]["fixed_point_converged"] is True
    assert variants["two_way"]["fixed_point_converged"] is True
    assert variants["two_way_rotated_dc"]["fixed_point_converged"] is True
    assert variants["two_way"]["max_abs_displacement_m"] > 0.0
    assert variants["two_way"]["max_abs_lorentz_current"] > 0.0
    assert variants["two_way_rotated_dc"]["dc_solution_mode"] == "computed"
    assert variants["two_way"]["dc_solution_mode"] == "reused"

    # Maxwell stress creates displacement, while the EM solution stays
    # unchanged when the reverse Lorentz-current term is explicitly disabled.
    assert comparisons["mechanics_from_maxwell_stress"] > 1e-6
    assert comparisons["em_unchanged_when_mechanical_feedback_is_off"] < 1e-10
    # Enabling displacement feedback changes the AC solution on the same mesh.
    assert variants["one_way_loaded"]["max_abs_displacement_m"] > 1e-7
    assert comparisons["two_way_feedback_changes_ac_field"] > 1e-8
    # Rotating the imposed DC vector changes the solved H(div) background and
    # its coupled AC/mechanical response, while the mesh and other inputs stay fixed.
    assert comparisons["direction_changed_dc_field"] > 0.5
    assert comparisons["direction_changed_ac_field"] > 1e-8
    assert comparisons["direction_changed_displacement"] > 1e-8

    # Linear phasor scaling checks complex-valued excitation propagation and
    # the expected magnitude and 90-degree phase response with cCoupCoef=0.
    assert comparisons["double_excitation_field_scaling_error"] < 1e-7
    assert comparisons["double_excitation_displacement_scaling_error"] < 1e-7
    assert comparisons["quadrature_field_phase_error"] < 1e-7
    assert comparisons["quadrature_displacement_phase_error"] < 1e-7
    assert all(
        variants[name]["dc_coefficients_reused"]
        for name in (
            "em_to_mechanics", "two_way", "double_excitation",
            "quadrature_excitation",
        )
    )
    print(
        f"Bounded coupled-term evidence: {report['mesh_elements']} elements, "
        f"{report['ndof']} DOFs; {json.dumps(comparisons, sort_keys=True)}"
    )


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
@pytest.mark.parametrize("frequency_hz", [50.0, 100.0])
def test_free_body_harmonic_displacement_matches_inertial_solution(
    tmp_path, frequency_hz
):
    """A freely translating body has exact u = -f/(rho*omega^2) at f > 0."""
    from netgen.occ import Box, Pnt
    from ngsolve import Integrate, VOL

    gui_root = Path(__file__).resolve().parents[1]
    solver_roots = []
    configured_solver_root = os.environ.get("COUPLED_SOLVER_ROOT")
    if configured_solver_root:
        solver_roots.append(Path(configured_solver_root).expanduser())
    solver_roots.extend([
        gui_root / "coupled_solver",
        gui_root.parent / "ngsolve-ui" / "coupled_solver",
        Path.cwd() / "coupled_solver",
    ])
    solver_root = next(
        root.resolve()
        for root in solver_roots
        if (root / "scripts" / "run_gui_3d_model.py").is_file()
    )
    step_path = tmp_path / "free_body_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))

    materials = builtin_materials()
    material = materials[4]
    body_force = 1.0e5
    project = new_3d_project("Free body harmonic check")
    project["study"].update(
        physics="harmonic_mechanical",
        frequency_hz=frequency_hz,
        polynomial_order=1,
        mechanical_body_force_n_m3=[body_force, 0.0, 0.0],
    )
    project["materials"] = [material]
    assets = {}
    geometry = add_step_geometry(
        project,
        assets,
        step_path.name,
        step_path.read_bytes(),
        units="mm",
        material=material["name"],
    )
    geometry["material"] = material["name"]
    geometry["mesh_maxh_mm"] = 10.0

    model_path = tmp_path / "free_body.mmfem3d"
    model_path.write_bytes(package_3d_project(project, assets))
    output_dir = tmp_path / "free_body_output"
    environment = os.environ.copy()
    environment.pop("COUPLED_SOLVER_OUTPUT_DIR", None)
    environment.pop("COUPLED_SOLVER_SHARED_FILES_DIR", None)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(gui_root / "src"), *filter(None, [environment.get("PYTHONPATH")])]
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(solver_root / "scripts" / "run_gui_3d_model.py"),
            "--model", str(model_path),
            "--output-dir", str(output_dir),
        ],
        cwd=solver_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        env=environment,
        check=False,
    )
    solver_output = re.sub(
        re.escape(str(tmp_path)), "<temporary-run>",
        f"{completed.stdout}\n{completed.stderr}", flags=re.IGNORECASE,
    )
    assert completed.returncode == 0, solver_output[-5000:]
    manifest = json.loads((output_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["mesh"]["elements"] < 50_000
    displacement_file = next(
        field for field in manifest["fields"] if "gfuAC" in field
    )
    with (output_dir / displacement_file).open("rb") as stream:
        displacement = pickle.load(stream)
    displacement_volume = Integrate(1, displacement.space.mesh, VOL)
    mean_x = Integrate(displacement[0], displacement.space.mesh, VOL) / displacement_volume
    mean_y = Integrate(displacement[1], displacement.space.mesh, VOL) / displacement_volume
    mean_z = Integrate(displacement[2], displacement.space.mesh, VOL) / displacement_volume
    density = float(material["properties"]["density"])
    omega = 2.0 * math.pi * frequency_hz
    expected_x = -body_force / (density * omega**2)
    assert mean_x.real == pytest.approx(expected_x, rel=1e-5, abs=1e-10)
    assert mean_x.imag == pytest.approx(0.0, abs=1e-10)
    assert mean_y == pytest.approx(0.0, abs=1e-10)
    assert mean_z == pytest.approx(0.0, abs=1e-10)
    print(
        f"Free-body harmonic check: f={frequency_hz:g} Hz, "
        f"{manifest['mesh']['elements']} elements, mean ux={mean_x.real:.9g} m, "
        f"analytic ux={expected_x:.9g} m"
    )


def test_solver_rejects_boundary_tags_that_do_not_match_step_topology(tmp_path):
    from netgen.occ import Box, Pnt

    step_path = tmp_path / "small_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    trace_path = tmp_path / "coil.txt"
    trace_path.write_text("0 0 0\n0 0.1 0\n", encoding="utf-8")
    materials = builtin_materials()
    project = new_3d_project("Stale boundary mapping")
    project["materials"] = [materials[4]]
    assets = {}
    geometry = add_step_geometry(
        project,
        assets,
        step_path.name,
        step_path.read_bytes(),
        units="mm",
        material=materials[4]["name"],
    )
    geometry["material"] = materials[4]["name"]
    geometry["boundary_selections"] = [{
        "id": "stale-face",
        "name": "Stale face",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_000000000000000000000099"],
        "boundary_condition": "magnetic_potential_zero",
    }]
    trace_set = add_wire_trace(project, assets, trace_path.name, trace_path.read_bytes())

    with pytest.raises(ValueError, match="no longer match unique STEP faces"):
        build_time_harmonic_em_problem(
            project,
            {
                geometry["asset"]: step_path,
                trace_set["traces"][0]["filename"]: trace_path,
            },
        )


def test_solver_rejects_active_order_based_boundary_references(tmp_path):
    from netgen.occ import Box, Pnt

    step_path = tmp_path / "legacy_boundary.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    trace_path = tmp_path / "coil.txt"
    trace_path.write_text("0 0 0\n0 0.1 0\n", encoding="utf-8")
    materials = builtin_materials()
    project = new_3d_project("Legacy boundary mapping")
    project["materials"] = [materials[4]]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=materials[4]["name"],
    )
    geometry["material"] = materials[4]["name"]
    geometry["boundary_selections"] = [{
        "id": "old-face",
        "name": "Old boundary reference",
        "boundary_ids": [0],
        "boundary_tags": ["MMFEM_FACE_0_0"],
        "boundary_condition": "magnetic_potential_zero",
    }]
    trace_set = add_wire_trace(project, assets, trace_path.name, trace_path.read_bytes())

    with pytest.raises(ValueError, match="order-based STEP references"):
        build_time_harmonic_em_problem(
            project,
            {
                geometry["asset"]: step_path,
                trace_set["traces"][0]["filename"]: trace_path,
            },
        )


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_3d_domain_current_density_run_propagates_complex_source_and_scales_field(tmp_path):
    import numpy as np
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_mesh import generate_step_mesh

    step_path = tmp_path / "domain_current_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    mesh_setup = generate_step_mesh(step_path.read_bytes(), maxh_mm=10.0)
    assert 0 < mesh_setup["volume_elements"] < 50_000
    material = builtin_materials()[4]
    project = new_3d_project("Small domain current-density check")
    project["study"].update(
        em_source_mode="domain_current_density",
        frequency_hz=50.0,
        polynomial_order=1,
        air_padding=0.25,
    )
    project["materials"] = [material]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=material["name"],
    )
    geometry["mesh_maxh_mm"] = 10.0
    domain = mesh_setup["domain_regions"][0]
    geometry["domain_selections"] = [{
        "id": "current-source",
        "name": "Applied current density",
        "domain_ids": [domain["id"]],
        "domain_tags": [domain["domain_tag"]],
        "current_density_amplitude_a_per_m2": [0.0, 0.0, 1e6],
        "current_density_phase_deg": [0.0, 0.0, 90.0],
    }]

    first_manifest, first_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "current-density-1x"
    )
    changed_project = copy.deepcopy(project)
    changed_project["geometries"][0]["domain_selections"][0][
        "current_density_amplitude_a_per_m2"
    ][2] = 2e6
    second_manifest, second_dir = _run_packaged_solver_model(
        changed_project, assets, tmp_path, "current-density-2x"
    )

    assert first_manifest["em_source_mode"] == "domain_current_density"
    assert second_manifest["solver_configuration"]["current_density_by_domain"][
        f"{geometry['id']}:{domain['domain_tag']}"
    ][2] == pytest.approx([0.0, 2e6], abs=1e-8)
    import ngsolve as ngs

    sample_points = [
        (0.011, 0.004, 0.006),
        (0.011, 0.006, 0.004),
        (0.006, 0.011, 0.004),
    ]
    sampled_fields = []
    for output_dir in (first_dir, second_dir):
        manifest = json.loads(
            (output_dir / "run_manifest.json").read_text(encoding="utf-8")
        )
        field_path = next(
            output_dir / name for name in manifest["fields"] if "gfBAC" in name
        )
        solver_mesh = ngs.Mesh(str(output_dir / "mesh.vol"))
        with field_path.open("rb") as stream:
            flux_density = pickle.load(stream)
        sampled_fields.append(
            [
                np.asarray(flux_density(solver_mesh(*point)), dtype=complex)
                for point in sample_points
            ]
        )
    response = max(float(np.linalg.norm(value)) for value in sampled_fields[0])
    assert math.isfinite(response) and response > 1e-12
    for baseline, doubled in zip(*sampled_fields):
        if np.linalg.norm(baseline) > 1e-12:
            relative_error = float(
                np.linalg.norm(doubled - 2.0 * baseline)
                / np.linalg.norm(2.0 * baseline)
            )
            # This is a proportionality check across separate native solver
            # processes; parallel assembly introduces a few parts per million
            # of repeat-to-repeat variation on the same mesh.
            assert relative_error < 1e-5
            break
    else:
        pytest.fail("No sampled point had a non-zero magnetic response to the current-density source")


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_3d_mechanical_domain_load_prescribed_motion_and_spring_run(tmp_path):
    import numpy as np
    from netgen.occ import Box, Pnt

    from ngsolve_gui.three_d_mesh import generate_step_mesh

    step_path = tmp_path / "scoped_mechanical_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    mesh_setup = generate_step_mesh(step_path.read_bytes(), maxh_mm=10.0)
    assert 0 < mesh_setup["volume_elements"] < 50_000
    material = builtin_materials()[4]
    project = new_3d_project("Small scoped mechanical check")
    project["study"].update(
        physics="harmonic_mechanical",
        frequency_hz=50.0,
        polynomial_order=1,
        mechanical_body_force_n_m3=[0.0, 0.0, 0.0],
    )
    project["materials"] = [material]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=material["name"],
    )
    geometry["mesh_maxh_mm"] = 10.0
    domain = mesh_setup["domain_regions"][0]
    geometry["domain_selections"] = [{
        "id": "local-mechanical-load",
        "name": "Oscillating load domain",
        "domain_ids": [domain["id"]],
        "domain_tags": [domain["domain_tag"]],
        "mechanical_body_force_n_m3": [1e8, 0.0, 0.0],
        "mechanical_body_force_phase_deg": [90.0, 0.0, 0.0],
    }]
    driven_face, spring_face, fixed_face = mesh_setup["boundary_regions"][:3]
    geometry["boundary_selections"] = [
        {
            "id": "driven-face",
            "name": "Prescribed displacement",
            "boundary_ids": [driven_face["id"]],
            "boundary_tags": [driven_face["boundary_tag"]],
            "boundary_condition": "mechanical_prescribed_displacement",
            "prescribed_displacement_amplitude_m": [1e-6, 0.0, 0.0],
            "prescribed_displacement_phase_deg": [90.0, 0.0, 0.0],
        },
        {
            "id": "spring-face",
            "name": "Compliant support",
            "boundary_ids": [spring_face["id"]],
            "boundary_tags": [spring_face["boundary_tag"]],
            "boundary_condition": "mechanical_spring_support",
            "spring_normal_stiffness_n_per_m3": 2e8,
            "spring_tangential_stiffness_n_per_m3": 5e7,
        },
        {
            "id": "fixed-face",
            "name": "Fixed support",
            "boundary_ids": [fixed_face["id"]],
            "boundary_tags": [fixed_face["boundary_tag"]],
            "boundary_condition": "mechanical_fixed",
        },
    ]

    manifest, output_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "scoped-mechanical"
    )

    assert manifest["physics"] == "Time-harmonic mechanics"
    configuration = manifest["solver_configuration"]
    assert configuration["mechanical_body_force_by_domain"][
        f"{geometry['id']}:{domain['domain_tag']}"
    ][0] == pytest.approx([0.0, 1e8], abs=1e-8)
    boundary_assignments = configuration["boundary_assignments"]
    assert boundary_assignments[f"{geometry['id']}:{driven_face['boundary_tag']}"]["value"][0] == pytest.approx(
        [0.0, 1e-6], abs=1e-12
    )
    assert boundary_assignments[f"{geometry['id']}:{spring_face['boundary_tag']}"] == {
        "type": "mechanical_spring_support",
        "normal": 2e8,
        "tangential": 5e7,
    }
    assert boundary_assignments[f"{geometry['id']}:{fixed_face['boundary_tag']}"] == {
        "type": "mechanical_fixed"
    }

    displacement_path = next(
        output_dir / name for name in manifest["fields"] if "gfuAC" in name
    )
    with displacement_path.open("rb") as stream:
        displacement = pickle.load(stream)
    free_dofs = displacement.space.FreeDofs()
    constrained = [
        index for index in range(displacement.space.ndof)
        if not free_dofs[index]
    ]
    values = np.asarray(displacement.vec.FV().NumPy())[constrained]
    assert constrained
    assert float(np.abs(values.imag).max()) == pytest.approx(1e-6, rel=1e-6)
    assert float(np.abs(values.real).max()) < 1e-12


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_3d_dc_sphere_matches_uniform_field_and_permeability_effect(tmp_path):
    import numpy as np
    from netgen.occ import Pnt, Sphere

    step_path = tmp_path / "small_dc_sphere.step"
    Sphere(Pnt(0, 0, 0), r=5.0).WriteStep(str(step_path))
    project = new_3d_project("Small DC magnetic sphere")
    project["study"].update(
        physics="magnetostatic_dc",
        dc_background_field_t=[0.0, 0.0, 0.02],
        polynomial_order=1,
        air_padding=1.0,
    )
    project["study"].pop("frequency_hz", None)
    project["wire_trace_sets"] = []
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material="DC sphere alloy",
    )
    geometry["mesh_maxh_mm"] = 2.0

    measured = {}
    mesh_elements = {}
    for relative_permeability in (1.0, 2.0):
        mesh_sizes = (2.0,) if relative_permeability == 1.0 else (2.0, 1.5)
        measured[relative_permeability] = {}
        mesh_elements[relative_permeability] = {}
        project["materials"] = [{
            "id": "dc-sphere-alloy",
            "name": "DC sphere alloy",
            "properties": {
                "relative_permeability": relative_permeability,
                "electrical_conductivity": 4e6,
                "youngs_modulus": 0.0,
                "poissons_ratio": 0.3,
                "density": 0.0,
            },
        }]
        for mesh_maxh_mm in mesh_sizes:
            geometry["mesh_maxh_mm"] = mesh_maxh_mm
            run_name = (
                f"dc-sphere-mur-{relative_permeability:g}-h-{mesh_maxh_mm:g}"
            )
            manifest, output_dir = _run_packaged_solver_model(
                project, assets, tmp_path, run_name
            )
            assert manifest["physics"] == "Static/DC magnetics"
            assert "frequency_hz" not in manifest
            assert manifest["status"] == "complete"
            assert manifest["mesh"]["elements"] < 50_000
            artifact = output_dir / manifest["dc_background_field"]["file"]
            assert artifact.is_file()
            with np.load(artifact, allow_pickle=False) as archive:
                stored_coefficients = archive["coefficients"]
                stored_metadata = json.loads(str(archive["metadata"].item()))
            assert stored_coefficients.size == stored_metadata["dof_count"]
            assert stored_metadata["mesh_sha256"]
            assert stored_metadata["input_fingerprint"] == manifest["dc_input_fingerprint"]
            field_path = next(
                output_dir / name for name in manifest["fields"] if "gfBDC" in name
            )
            with field_path.open("rb") as stream:
                flux_density = pickle.load(stream)
            mesh = __import__("ngsolve").Mesh(str(output_dir / "mesh.vol"))
            value = np.asarray(flux_density(mesh(0.0, 0.0, 0.0)), dtype=float)
            assert np.isfinite(value).all()
            measured[relative_permeability][mesh_maxh_mm] = float(value[2])
            mesh_elements[relative_permeability][mesh_maxh_mm] = manifest[
                "mesh"
            ]["elements"]

    # For a sphere in a uniform applied field, the infinite-domain interior
    # solution is B_in = 3*mu_r/(mu_r+2)*B0. The finite air box and coarse
    # first-order mesh introduce a bounded approximation error.
    assert measured[1.0][2.0] == pytest.approx(0.02, rel=0.03, abs=5e-4)
    assert measured[2.0][2.0] == pytest.approx(0.03, rel=0.05, abs=5e-4)
    assert measured[2.0][1.5] == pytest.approx(0.03, rel=0.05, abs=5e-4)
    coarse_error = abs(measured[2.0][2.0] - 0.03)
    refined_error = abs(measured[2.0][1.5] - 0.03)
    assert refined_error < coarse_error, (
        f"The refined 1.5 mm mesh did not improve the analytic sphere error: "
        f"coarse={coarse_error}, refined={refined_error}, "
        f"measured={measured[2.0]!r}"
    )
    assert measured[2.0][2.0] > measured[1.0][2.0] * 1.25
    print(json.dumps({
        "reference": "uniform-field permeable sphere",
        "applied_Bz_T": 0.02,
        "analytic_Bz_center_T": {"mur_1": 0.02, "mur_2": 0.03},
        "measured_Bz_center_T_by_mur_and_mesh_maxh_mm": {
            f"mur_{mur:g}": {
                f"h_{mesh_maxh_mm:g}_mm": field
                for mesh_maxh_mm, field in fields.items()
            }
            for mur, fields in measured.items()
        },
        "mesh_tetrahedra_by_mur_and_mesh_maxh_mm": {
            f"mur_{mur:g}": {
                f"h_{mesh_maxh_mm:g}_mm": count
                for mesh_maxh_mm, count in fields.items()
            }
            for mur, fields in mesh_elements.items()
        },
        "analytic_error_reduced_by_mesh_refinement": True,
    }, sort_keys=True))


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_3d_dc_domain_current_density_produces_right_hand_field(tmp_path):
    import numpy as np
    from netgen.occ import Box, Pnt
    import ngsolve as ngs

    from ngsolve_gui.three_d_mesh import generate_step_mesh

    step_path = tmp_path / "small_dc_current_block.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(step_path))
    mesh_setup = generate_step_mesh(step_path.read_bytes(), maxh_mm=5.0)
    assert 0 < mesh_setup["volume_elements"] < 50_000
    material = builtin_materials()[4]
    project = new_3d_project("Small DC current-density check")
    project["study"].update(
        physics="magnetostatic_dc",
        dc_background_field_t=[0.0, 0.0, 0.0],
        polynomial_order=1,
        air_padding=0.5,
    )
    project["study"].pop("frequency_hz", None)
    project["wire_trace_sets"] = []
    project["materials"] = [material]
    assets = {}
    geometry = add_step_geometry(
        project, assets, step_path.name, step_path.read_bytes(),
        units="mm", material=material["name"],
    )
    geometry["mesh_maxh_mm"] = 5.0
    domain = mesh_setup["domain_regions"][0]
    geometry["domain_selections"] = [{
        "id": "dc-current-source",
        "name": "DC current source",
        "domain_ids": [domain["id"]],
        "domain_tags": [domain["domain_tag"]],
        "dc_current_density_a_per_m2": [0.0, 0.0, 1e6],
    }]

    manifest, output_dir = _run_packaged_solver_model(
        project, assets, tmp_path, "small-dc-current-density"
    )
    configuration = manifest["solver_configuration"]
    assert manifest["physics"] == "Static/DC magnetics"
    assert configuration["dc_current_density_by_domain"][
        f"{geometry['id']}:{domain['domain_tag']}"
    ] == [0.0, 0.0, 1e6]
    field_path = next(
        output_dir / name for name in manifest["fields"] if "gfBDC" in name
    )
    with field_path.open("rb") as stream:
        flux_density = pickle.load(stream)
    mesh = ngs.Mesh(str(output_dir / "mesh.vol"))
    # Sample just outside the +X face, where the right-hand rule predicts +Y.
    measured = np.asarray(flux_density(mesh(0.012, 0.005, 0.005)), dtype=float)
    assert np.isfinite(measured).all()
    assert measured[1] > 1e-6
    assert abs(measured[1]) > 5 * max(abs(measured[0]), abs(measured[2]))
    print(json.dumps({
        "reference": "right-hand rule for +Z current density",
        "Jz_a_per_m2": 1e6,
        "sample_xyz_m": [0.012, 0.005, 0.005],
        "measured_B_t": measured.tolist(),
        "tetrahedra": manifest["mesh"]["elements"],
    }, sort_keys=True))
