"""Build the supported 3D time-harmonic electromagnetic input for MagMechSim."""

from __future__ import annotations

import math
from pathlib import Path

from .axisymmetric_model import builtin_materials
from .three_d_project import (
    UNASSIGNED_MATERIAL,
    build_3d_solver_inputs,
    build_wire_trace_sets,
    domain_mesh_size_overrides,
    step_face_tags,
)


def _finite_material_property(material, key, *, default=None):
    properties = material.get("properties", {})
    value = properties.get(key, default)
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(
            f"Material {material.get('name', '')!r} needs a numeric {key.replace('_', ' ')} value."
        ) from error
    if not math.isfinite(value):
        raise ValueError(
            f"Material {material.get('name', '')!r} has a non-finite {key.replace('_', ' ')} value."
        )
    return value


def _point_coordinates(point):
    coordinates = []
    for axis in "xyz":
        value = getattr(point, axis, None)
        if value is None:
            getter = getattr(point, axis.upper(), None)
            value = getter() if callable(getter) else getter
        coordinates.append(float(value))
    return coordinates


def _shape_bounds(shape):
    bounds = getattr(shape, "bounding_box", None)
    if callable(bounds):
        bounds = bounds()
    if bounds is None:
        bounds = getattr(shape, "bbox", None)
        if callable(bounds):
            bounds = bounds()
    if not isinstance(bounds, (tuple, list)) or len(bounds) != 2:
        raise ValueError("Could not determine the imported STEP model bounds.")
    return _point_coordinates(bounds[0]), _point_coordinates(bounds[1])


def validate_time_harmonic_em_setup(project):
    """Check the current 3D study and its project materials without NGSolve."""
    study = project.get("study", {})
    try:
        frequency_hz = float(study.get("frequency_hz", 500.0))
        polynomial_order = int(study.get("polynomial_order", 1))
        air_padding = float(study.get("air_padding", 2.0))
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"Invalid 3D study settings: {error}") from error
    if not math.isfinite(frequency_hz) or frequency_hz <= 0:
        raise ValueError("The study frequency must be positive and finite.")
    if polynomial_order not in {1, 2, 3, 4}:
        raise ValueError("The finite-element order must be between 1 and 4.")
    if not math.isfinite(air_padding) or not 0.25 <= air_padding <= 20:
        raise ValueError("Air-domain padding must be between 0.25 and 20 model spans.")
    if not project.get("geometries"):
        raise ValueError("Import at least one closed STEP solid before running a study.")
    if not project.get("wire_trace_sets"):
        raise ValueError("Import at least one wire trace before running a study.")

    records = {item["name"].casefold(): item for item in builtin_materials()}
    records.update({item["name"].casefold(): item for item in project.get("materials", [])})
    if "air" not in records:
        raise ValueError("The Air material is unavailable; restore the built-in material library.")

    material_names = set()
    for geometry in project.get("geometries", []):
        default_material = str(geometry.get("material", "")).strip()
        if not default_material:
            raise ValueError(f"Assign a material to {geometry.get('name', 'Geometry')!r} before running.")
        if default_material.casefold() == UNASSIGNED_MATERIAL.casefold():
            raise ValueError(
                f"Assign a physical material to {geometry.get('name', 'Geometry')!r} before running."
            )
        names = [default_material]
        names.extend(
            selection.get("material", "")
            for selection in geometry.get("domain_selections", [])
            if selection.get("material")
        )
        mesh_size = float(geometry.get("mesh_maxh_mm", 30.0))
        if not math.isfinite(mesh_size) or mesh_size <= 0:
            raise ValueError(f"{geometry.get('name', 'Geometry')!r} needs a positive mesh size.")
        domain_mesh_size_overrides(geometry)
        material_names.update(str(name).strip() for name in names if str(name).strip())
    for trace_set in project.get("wire_trace_sets", []):
        for trace in trace_set.get("traces", []):
            material_names.add(str(trace.get("material", trace_set.get("material", "Air"))).strip())

    resolved = {}
    for name in material_names | {"Air"}:
        record = records.get(name.casefold())
        if record is None:
            if name.casefold() == UNASSIGNED_MATERIAL.casefold():
                raise ValueError(
                    "Assign a physical material from the library to every solid and region before running."
                )
            raise ValueError(
                f"Material {name!r} is not embedded in this project. Choose it from the material library."
            )
        mur = _finite_material_property(record, "relative_permeability")
        sigma = _finite_material_property(record, "electrical_conductivity")
        youngs = _finite_material_property(record, "youngs_modulus", default=0)
        poisson = _finite_material_property(record, "poissons_ratio", default=0.3)
        density = _finite_material_property(record, "density", default=0)
        if mur <= 0 or sigma < 0 or youngs < 0 or density < 0 or not -0.99 < poisson < 0.5:
            raise ValueError(f"Material {name!r} has values outside the supported physical range.")
        resolved[record["name"]] = {
            "name": record["name"],
            "mur": mur,
            "sigma": sigma,
            "youngs": youngs,
            "poisson": poisson,
            "density": density,
        }
    return {
        "frequency_hz": frequency_hz,
        "polynomial_order": polynomial_order,
        "air_padding": air_padding,
        "materials": resolved,
    }


def build_time_harmonic_em_problem(project, asset_paths):
    """Translate a 3D project into the coupled solver's supported input shape.

    This vertical slice solves magnetic vector potential and derived magnetic
    flux density at one frequency. Mechanical degrees of freedom remain off.
    Imported STEP solids use their geometry material as the default; saved
    domain selections can override that material for individual solid ids.
    """
    validated = validate_time_harmonic_em_setup(project)
    from ngsolve import CoefficientFunction
    from netgen.occ import Box, Glue, OCCGeometry, Pnt

    frequency_hz = validated["frequency_hz"]
    polynomial_order = validated["polynomial_order"]
    air_padding = validated["air_padding"]
    material_records = {name.casefold(): {"name": name, "properties": {
        "relative_permeability": item["mur"],
        "electrical_conductivity": item["sigma"],
        "youngs_modulus": item["youngs"],
        "poissons_ratio": item["poisson"],
        "density": item["density"],
    }} for name, item in validated["materials"].items()}
    air_record = material_records.get("air")

    def resolve_material(name):
        record = material_records.get(str(name).strip().casefold())
        if record is None:
            raise ValueError(
                f"Material {name!r} is not embedded in this project. Choose it from the material library."
            )
        properties = record.get("properties", {})
        mur = _finite_material_property(record, "relative_permeability")
        sigma = _finite_material_property(record, "electrical_conductivity")
        youngs = _finite_material_property(record, "youngs_modulus", default=0)
        poisson = _finite_material_property(record, "poissons_ratio", default=0.3)
        density = _finite_material_property(record, "density", default=0)
        if mur <= 0 or sigma < 0 or youngs < 0 or density < 0 or not -0.99 < poisson < 0.5:
            raise ValueError(f"Material {name!r} has values outside the supported physical range.")
        return {
            "name": record["name"],
            "mur": mur,
            "sigma": sigma,
            "youngs": youngs,
            "poisson": poisson,
            "density": density,
        }

    geometry_inputs = build_3d_solver_inputs(project, asset_paths)["geometries"]
    objects = {}
    domain_maxh = {}
    all_solids = []
    object_index = 0
    bounds_min = [math.inf, math.inf, math.inf]
    bounds_max = [-math.inf, -math.inf, -math.inf]
    used_materials = {}
    dirichlet_boundary_names = []
    seen_dirichlet_boundary_names = set()

    for geometry_index, (geometry_input, geometry) in enumerate(
        zip(geometry_inputs, project["geometries"])
    ):
        default_material = str(geometry.get("material", "")).strip()
        if not default_material:
            raise ValueError(f"Assign a material to {geometry['name']!r} before running.")
        resolve_material(default_material)
        source = OCCGeometry(geometry_input["filename"])
        shape = source.shape
        scaled_shape = shape.Scale(Pnt(0, 0, 0), geometry_input["unit_scale"])
        if scaled_shape is not None:
            shape = scaled_shape
        solids = list(shape.solids)
        if not solids:
            raise ValueError(f"{geometry['name']!r} does not contain a closed STEP solid.")

        selected_boundary_conditions = {}
        for selection in geometry.get("boundary_selections", []):
            condition = selection.get("boundary_condition")
            if condition is None:
                continue
            if condition != "magnetic_potential_zero":
                raise ValueError(
                    f"Boundary selection {selection.get('name', '')!r} uses an unsupported condition."
                )
            tags = selection.get("boundary_tags")
            if not tags:
                raise ValueError(
                    f"Boundary selection {selection.get('name', '')!r} has no saved STEP face mapping. "
                    "Regenerate the mesh and save the selection again."
                )
            for tag in tags:
                selected_boundary_conditions[tag] = condition

        faces_by_tag = dict(step_face_tags(solids))
        stale_tags = sorted(set(selected_boundary_conditions) - set(faces_by_tag))
        if stale_tags:
            raise ValueError(
                f"Boundary selections for {geometry['name']!r} no longer match its STEP faces. "
                "Regenerate the mesh and update the selections."
            )
        for tag, face in faces_by_tag.items():
            if tag in selected_boundary_conditions:
                boundary_name = f"DirEM_G{geometry_index}_{tag}"
                face.bc(boundary_name)
                if boundary_name not in seen_dirichlet_boundary_names:
                    dirichlet_boundary_names.append(boundary_name)
                    seen_dirichlet_boundary_names.add(boundary_name)
            else:
                face.bc("Transmission_Interface")

        overrides = {}
        local_mesh_overrides = domain_mesh_size_overrides(geometry)
        for selection in geometry.get("domain_selections", []):
            selected_material = selection.get("material")
            if not selected_material:
                continue
            resolve_material(selected_material)
            for region_id in selection.get("domain_ids", []):
                region_id = int(region_id)
                existing = overrides.get(region_id)
                if existing and existing.casefold() != selected_material.casefold():
                    raise ValueError(
                        f"Domain {region_id} in {geometry['name']!r} has conflicting material assignments."
                    )
                overrides[region_id] = selected_material
        if any(region_id >= len(solids) for region_id in overrides):
            raise ValueError(
                f"Saved domain ids for {geometry['name']!r} do not match its solids. "
                "Regenerate the mesh and update the domain selections."
            )

        default_mesh_size_mm = float(geometry.get("mesh_maxh_mm", 30.0))
        if not math.isfinite(default_mesh_size_mm) or default_mesh_size_mm <= 0:
            raise ValueError(f"{geometry['name']!r} needs a positive maximum element size.")
        for region_id, solid in enumerate(solids):
            material_name = overrides.get(region_id, default_material)
            material = resolve_material(material_name)
            solid.mat(material["name"])
            object_key = f"_mmfem_solid_{object_index}"
            object_index += 1
            objects[object_key] = solid
            domain_maxh[object_key] = (
                local_mesh_overrides.get(region_id, default_mesh_size_mm) * 0.001
            )
            used_materials[material["name"]] = material
            all_solids.append(solid)

        lower, upper = _shape_bounds(shape)
        bounds_min = [min(a, b) for a, b in zip(bounds_min, lower)]
        bounds_max = [max(a, b) for a, b in zip(bounds_max, upper)]

    span = max(upper - lower for lower, upper in zip(bounds_min, bounds_max))
    if not math.isfinite(span) or span <= 0:
        raise ValueError("The imported STEP solids have invalid overall dimensions.")
    padding = span * air_padding
    air_box = Box(
        Pnt(*(value - padding for value in bounds_min)),
        Pnt(*(value + padding for value in bounds_max)),
    ).bc("DirEM")
    inner_union = Glue(all_solids)
    air_shape = (air_box - inner_union).mat(air_record["name"])

    solid_maxh = max(domain_maxh.values())
    air_maxh = max(solid_maxh, span / 12.0)
    air_key = "_mmfem_air"
    objects[air_key] = air_shape
    domain_maxh[air_key] = air_maxh
    used_materials[air_record["name"]] = resolve_material(air_record["name"])
    material_names = list(used_materials)
    all_material_names = set(material_names)
    for trace_set in project.get("wire_trace_sets", []):
        for trace in trace_set.get("traces", []):
            trace_material = str(trace.get("material", trace_set.get("material", "Air"))).strip()
            trace_record = resolve_material(trace_material)
            used_materials.setdefault(trace_record["name"], trace_record)
            all_material_names.add(trace_record["name"])

    material_data = {name: used_materials[name] for name in used_materials}
    zero_vector = CoefficientFunction((0, 0, 0))
    problem = {
        "prob_dom": "3D",
        "geometry": OCCGeometry(Glue(list(objects.values()))),
        "objects": objects,
        "domain_maxh": domain_maxh,
        "mesh_curve_order": polynomial_order,
        "p": polynomial_order,
        "HpLayers": 0,
        "hp_doms": [],
        "bound_thick_list": [],
        "custom_boundary_layers": [],
        "interface_face_maxh": {},
        "sigmaG": 0.3,
        "layer_tot_max": 0.0,
        "max_target_frequency": frequency_hz,
        "temp_disc": "time_harmonic",
        "omega": [2 * math.pi * frequency_hz],
        "cACCoef": 1,
        "kACCoef": 1,
        "kACCoefDC": 0,
        "mMechCoef": 0,
        "kMechCoef": 0,
        "dampingalpha": 0,
        "dampingbeta": 0,
        "cCoupCoef": 0,
        "kCoupCoef": 0,
        "epsilonReg": 1e-8,
        "current_source": "wire_trace",
        "wire_trace_sets": build_wire_trace_sets(project, asset_paths),
        "srcdictAC": {name: 0 for name in all_material_names},
        "srcdictDC": {name: 0 for name in material_names},
        "srcAAC": zero_vector,
        "srcADC": zero_vector,
        "srcMech": zero_vector,
        "murdict": {name: item["mur"] for name, item in material_data.items()},
        "sigmadict": {name: item["sigma"] for name, item in material_data.items()},
        "Edict": {name: item["youngs"] for name, item in material_data.items()},
        "nudict": {name: item["poisson"] for name, item in material_data.items()},
        "rhodict": {name: item["density"] for name, item in material_data.items()},
        "inoroutdict": {name: int(name != air_record["name"]) for name in material_names},
        "mechSubdom": "|".join(name for name in material_names if name != air_record["name"]),
        "Neudict": {"Neu_0": 0},
        "Neukeys": ["Neu_0", "Transmission_Interface"],
        "dirconditionAAC": {
            name: zero_vector for name in ["DirEM", *dirichlet_boundary_names]
        },
        "dirkeysAAC": "|".join(["DirEM", *dirichlet_boundary_names]),
        "dirconditionADC": {},
        "dirkeysADC": "",
        "dirconditionMech": {},
        "dirkeysMech": "",
        "dirconditionDC": {},
        "dirkeysDC": "",
        "quarter_domain": False,
        "solver": "direct",
        "solver_EMAC": "direct",
        "statCondEM": True,
        "statCondMech": False,
        "statCond": False,
        "electromagnetic_only": True,
        "Aux": {
            "study_type": "harmonic_em",
            "frequency_hz": frequency_hz,
            "polynomial_order": polynomial_order,
            "material_properties": {
                name: {
                    "relative_permeability": item["mur"],
                    "electrical_conductivity": item["sigma"],
                }
                for name, item in material_data.items()
            },
        },
    }
    return {
        "problem": problem,
        "frequency_hz": frequency_hz,
        "polynomial_order": polynomial_order,
        "air_padding": air_padding,
        "h_global": max(domain_maxh.values()),
        "mesh_curve_order": max(1, min(polynomial_order, 3)),
        "domain_maxh": domain_maxh,
        "materials": material_data,
    }
