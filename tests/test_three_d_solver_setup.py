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
    build_time_harmonic_em_problem,
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


def test_study_setup_uses_project_material_snapshots_and_single_frequency():
    project = _study_project()

    validated = validate_time_harmonic_em_setup(project)

    assert validated["frequency_hz"] == 500
    assert validated["polynomial_order"] == 1
    assert validated["air_padding"] == 2
    assert validated["materials"]["Stainless steel (4 K / OVC)"]["sigma"] == 1.4e6
    assert validated["materials"]["Aluminium (77 K shield)"]["sigma"] == 33e6
    assert "Air" in validated["materials"]


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
    assert problem["murdict"][coarse_object] == pytest.approx(
        float(materials[4]["properties"]["relative_permeability"])
    )
    assert inputs["material_display_names"][refined_object] == materials[5]["name"]
    assert inputs["material_display_names"][coarse_object] == materials[4]["name"]


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
        frequency_hz=50.0,
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
    assert manifest["frequency_hz"] == 50.0
    assert manifest["polynomial_order"] == 1
    assert 0 < manifest["mesh"]["elements"] < 50_000
    assert manifest["fields"]
    flux_field = next(field for field in manifest["fields"] if "gfBAC" in field)
    import ngsolve as ngs

    solver_mesh = ngs.Mesh(str(output_dir / "mesh.vol"))
    with (output_dir / flux_field).open("rb") as stream:
        flux_density = pickle.load(stream)
    value = flux_density(solver_mesh(0.011, 0.005, 0.005))
    magnitude = math.sqrt(sum(abs(component) ** 2 for component in value))
    assert math.isfinite(magnitude) and magnitude > 1e-12
    print(
        "Bounded scoped-boundary run: "
        f"{solver_mesh.ne} volume elements, |B|={magnitude:.6g} T"
    )


@pytest.mark.skipif(
    not solver_checkout_available(),
    reason=SOLVER_UNAVAILABLE_REASON,
)
def test_small_3d_mechanical_solver_run_exports_nonzero_displacement(tmp_path):
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
        frequency_hz=50.0,
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
    assert manifest["physics"] == "Time-harmonic mechanics"
    assert manifest["mechanical_body_force_n_m3"] == [1e8, 0.0, 0.0]
    assert "air_padding" not in manifest
    displacement_file = next(
        field for field in manifest["fields"] if "gfuAC" in field
    )
    with (output_dir / displacement_file).open("rb") as stream:
        displacement = pickle.load(stream)
    displacement_values = displacement.vec.FV().NumPy()
    assert displacement_values.size > 0
    assert all(math.isfinite(float(value.real)) for value in displacement_values)
    assert float(abs(displacement_values).max()) > 0
    free_dofs = displacement.space.FreeDofs()
    constrained_dofs = [
        index for index in range(displacement.space.ndof)
        if not free_dofs[index]
    ]
    assert constrained_dofs, "The selected mechanical support constrained no DOFs."
    assert float(abs(displacement_values[constrained_dofs]).max()) <= 1e-12
    print(
        f"Bounded mechanical run: {manifest['mesh']['elements']} elements, "
        f"max |u|={float(abs(displacement_values).max()):.6g} m"
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
