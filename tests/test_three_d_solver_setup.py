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
    trace_set = add_wire_trace(project, assets, "coil.txt", trace_path.read_bytes())

    inputs = build_time_harmonic_em_problem(
        project,
        {
            geometry["asset"]: step_path,
            trace_set["traces"][0]["filename"]: trace_path,
        },
    )

    problem = inputs["problem"]
    assert set(problem["objects"]) == {
        "_mmfem_solid_0",
        "_mmfem_solid_1",
        "_mmfem_air",
    }
    assert inputs["domain_maxh"]["_mmfem_solid_0"] == pytest.approx(0.002)
    assert inputs["domain_maxh"]["_mmfem_solid_1"] == pytest.approx(0.01)
    assert problem["murdict"][materials[5]["name"]] == pytest.approx(
        float(materials[5]["properties"]["relative_permeability"])
    )
    assert problem["murdict"][materials[4]["name"]] == pytest.approx(
        float(materials[4]["properties"]["relative_permeability"])
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
        "boundary_tags": ["MMFEM_FACE_99_99"],
        "boundary_condition": "magnetic_potential_zero",
    }]
    trace_set = add_wire_trace(project, assets, trace_path.name, trace_path.read_bytes())

    with pytest.raises(ValueError, match="no longer match its STEP faces"):
        build_time_harmonic_em_problem(
            project,
            {
                geometry["asset"]: step_path,
                trace_set["traces"][0]["filename"]: trace_path,
            },
        )
