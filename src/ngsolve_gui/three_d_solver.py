"""Build the supported 3D time-harmonic electromagnetic input for MagMechSim."""

from __future__ import annotations

import math
from pathlib import Path

from .axisymmetric_model import builtin_materials
from .three_d_project import (
    UNASSIGNED_MATERIAL,
    SUPPORTED_3D_EM_SOURCE_MODES,
    SUPPORTED_3D_STUDIES,
    _LEGACY_DOMAIN_SELECTION_ERROR,
    _LEGACY_STEP_FACE_TAG_PATTERN,
    build_3d_solver_inputs,
    build_wire_trace_sets,
    domain_material_overrides,
    domain_mesh_size_overrides,
    step_geometry_tags,
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


def material_properties_for_study(material, physics, *, for_solid=True):
    """Resolve only the properties required by the selected 3D formulation.

    The existing solver API still receives all five coefficient dictionaries.
    Values for the inactive formulation therefore receive neutral defaults;
    incomplete inactive properties never prevent a supported single-physics
    study from running.
    """
    if physics not in SUPPORTED_3D_STUDIES:
        raise ValueError("Choose a supported 3D harmonic study.")
    name = str(material.get("name", "Material"))
    is_em = physics == "harmonic_em"
    props = material.get("properties", {})
    defaults = {
        "relative_permeability": 1.0,
        "electrical_conductivity": 0.0,
        "youngs_modulus": 0.0,
        "poissons_ratio": 0.3,
        "density": 0.0,
    }
    required = (
        ("relative_permeability", "electrical_conductivity")
        if is_em else
        ("youngs_modulus", "poissons_ratio", "density")
    )
    values = {}
    for key in required:
        value = _finite_material_property(material, key)
        values[key] = value

    # Populate all coefficient dictionaries expected by the existing solver.
    # Invalid or absent inactive values are harmless to this single-physics
    # formulation and are deliberately replaced with neutral values.
    for key, default in defaults.items():
        if key in values:
            continue
        raw = props.get(key)
        try:
            value = float(raw) if raw not in (None, "") else default
            if not math.isfinite(value):
                value = default
        except (TypeError, ValueError, OverflowError):
            value = default
        values[key] = value

    mur = values["relative_permeability"]
    sigma = values["electrical_conductivity"]
    youngs = values["youngs_modulus"]
    poisson = values["poissons_ratio"]
    density = values["density"]
    if is_em and (mur <= 0 or sigma < 0):
        raise ValueError(
            f"Material {name!r} needs relative permeability > 0 and "
            "electrical conductivity ≥ 0 for time-harmonic EM."
        )
    if not is_em:
        if for_solid and name.strip().casefold() == "air":
            raise ValueError(
                "Air cannot be assigned to a mechanics solid. Choose a material "
                "with mechanical properties."
            )
        if youngs <= 0:
            raise ValueError(
                f"Material {name!r} needs positive Young's modulus for a mechanical study."
            )
        if density <= 0:
            raise ValueError(
                f"Material {name!r} has density outside the supported physical range; "
                "density must be positive for a mechanical study."
            )
        if not -0.99 < poisson < 0.5:
            raise ValueError(
                f"Material {name!r} needs Poisson's ratio between -0.99 and 0.5 "
                "for a mechanical study."
            )
    return {
        "mur": mur,
        "sigma": sigma,
        "youngs": youngs,
        "poisson": poisson,
        "density": density,
    }


def validate_material_definition(material):
    """Validate values the user has entered without requiring both physics sets."""
    props = material.get("properties", {})
    for key in (
        "relative_permeability", "electrical_conductivity", "youngs_modulus",
        "poissons_ratio", "density",
    ):
        raw = props.get(key)
        if raw in (None, ""):
            continue
        value = _finite_material_property(material, key)
        if key == "relative_permeability" and value <= 0:
            raise ValueError("Relative permeability must be greater than zero.")
        if key in {"electrical_conductivity", "youngs_modulus", "density"} and value < 0:
            raise ValueError(f"{key.replace('_', ' ').title()} cannot be negative.")
        if key == "poissons_ratio" and not -0.99 < value < 0.5:
            raise ValueError("Poisson's ratio must be between -0.99 and 0.5.")
    return True


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


def _phasor_vector(amplitudes, phases_deg):
    return tuple(
        float(amplitude) * complex(
            math.cos(math.radians(float(phase))),
            math.sin(math.radians(float(phase))),
        )
        for amplitude, phase in zip(amplitudes, phases_deg)
    )


def _domain_phasor_assignments(
    project, amplitude_key, phase_key, suppressed_key, description
):
    assignments = {}
    for geometry in project.get("geometries", []):
        geometry_id = geometry.get("id")
        for selection in geometry.get("domain_selections", []):
            if amplitude_key not in selection or selection.get(suppressed_key, False):
                continue
            tags = selection.get("domain_tags")
            if not tags:
                raise ValueError(
                    f"{description} selection {selection.get('name', '')!r} has no stable STEP solid mapping. "
                    "Reassign it from the current mesh."
                )
            amplitudes = selection[amplitude_key]
            phases = selection.get(phase_key, [0.0, 0.0, 0.0])
            if not isinstance(amplitudes, (list, tuple)) or len(amplitudes) != 3:
                raise ValueError(f"{description} must have X, Y and Z components.")
            if not isinstance(phases, (list, tuple)) or len(phases) != 3:
                raise ValueError(f"{description} phase must have X, Y and Z components.")
            try:
                vector = _phasor_vector(amplitudes, phases)
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(f"{description} needs finite amplitudes and phases.") from error
            if not all(
                math.isfinite(component.real) and math.isfinite(component.imag)
                for component in vector
            ):
                raise ValueError(f"{description} needs finite amplitudes and phases.")
            for tag in tags:
                key = (geometry_id, tag)
                existing = assignments.get(key)
                if existing is not None and any(
                    abs(previous - current)
                    > 1e-12 * max(abs(previous), abs(current), 1e-30)
                    for previous, current in zip(existing, vector)
                ):
                    raise ValueError(
                        f"Overlapping domain selections assign conflicting {description.lower()} values to {tag}."
                    )
                assignments[key] = vector
    return assignments


def _active_boundary_assignments(project, physics):
    assignments = {}
    supported = (
        {"magnetic_potential_zero"}
        if physics == "harmonic_em"
        else {
            "mechanical_fixed",
            "mechanical_prescribed_displacement",
            "mechanical_spring_support",
        }
    )
    for geometry in project.get("geometries", []):
        geometry_id = geometry.get("id")
        for selection in geometry.get("boundary_selections", []):
            condition = selection.get("boundary_condition")
            if (
                condition not in supported
                or selection.get("condition_suppressed", False)
            ):
                continue
            tags = selection.get("boundary_tags")
            if not tags:
                raise ValueError(
                    f"Boundary condition {selection.get('name', '')!r} has no stable STEP face mapping. "
                    "Reassign it from the current mesh."
                )
            if any(
                isinstance(tag, str) and _LEGACY_STEP_FACE_TAG_PATTERN.fullmatch(tag)
                for tag in tags
            ):
                raise ValueError(
                    "Active boundary selections use order-based STEP references. "
                    "Reassign them from the current mesh before solving."
                )
            payload = {"type": condition}
            if condition == "mechanical_prescribed_displacement":
                try:
                    amplitude = selection.get(
                        "prescribed_displacement_amplitude_m", [0.0, 0.0, 0.0]
                    )
                    phase = selection.get(
                        "prescribed_displacement_phase_deg", [0.0, 0.0, 0.0]
                    )
                    if len(amplitude) != 3 or len(phase) != 3:
                        raise ValueError
                    payload["value"] = _phasor_vector(amplitude, phase)
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValueError(
                        f"Prescribed displacement on {selection.get('name', '')!r} needs finite X, Y and Z amplitudes and phases."
                    ) from error
                if not all(
                    math.isfinite(component.real) and math.isfinite(component.imag)
                    for component in payload["value"]
                ):
                    raise ValueError(
                        f"Prescribed displacement on {selection.get('name', '')!r} needs finite X, Y and Z amplitudes and phases."
                    )
            elif condition == "mechanical_spring_support":
                try:
                    normal = float(selection.get("spring_normal_stiffness_n_per_m3", 0.0))
                    tangential = float(selection.get("spring_tangential_stiffness_n_per_m3", 0.0))
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValueError("Spring support stiffness must be finite and non-negative.") from error
                if (
                    not math.isfinite(normal)
                    or not math.isfinite(tangential)
                    or normal < 0
                    or tangential < 0
                    or normal > 1e18
                    or tangential > 1e18
                ):
                    raise ValueError("Spring support stiffness must be finite and between 0 and 1e18 N/m³.")
                if normal == 0 and tangential == 0:
                    raise ValueError(
                        f"Spring support {selection.get('name', '')!r} needs a non-zero normal or tangential stiffness."
                    )
                payload.update({"normal": normal, "tangential": tangential})
            for tag in tags:
                key = (geometry_id, tag)
                existing = assignments.get(key)
                if existing is not None and existing != payload:
                    raise ValueError(
                        f"Overlapping boundary selections assign incompatible conditions to STEP face {tag}."
                    )
                assignments[key] = payload
    return assignments


def validate_time_harmonic_em_setup(project):
    """Validate supported 3D harmonic EM or mechanics inputs without NGSolve."""
    study = project.get("study", {})
    try:
        physics = study.get("physics", "harmonic_em")
        em_source_mode = study.get("em_source_mode", "wire_trace")
        frequency_hz = float(study.get("frequency_hz", 500.0))
        polynomial_order = int(study.get("polynomial_order", 1))
        air_padding = float(study.get("air_padding", 2.0))
        body_force = [float(value) for value in study.get(
            "mechanical_body_force_n_m3", [0.0, 0.0, 0.0]
        )]
        body_force_phase = [float(value) for value in study.get(
            "mechanical_body_force_phase_deg", [0.0, 0.0, 0.0]
        )]
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"Invalid 3D study settings: {error}") from error
    if not math.isfinite(frequency_hz) or frequency_hz <= 0:
        raise ValueError("The study frequency must be positive and finite.")
    if physics not in SUPPORTED_3D_STUDIES:
        raise ValueError("Choose a supported 3D harmonic study.")
    if em_source_mode not in SUPPORTED_3D_EM_SOURCE_MODES:
        raise ValueError("Choose a supported 3D electromagnetic excitation source.")
    electromagnetic = physics == "harmonic_em"
    mechanical = physics == "harmonic_mechanical"
    if polynomial_order not in {1, 2, 3, 4}:
        raise ValueError("The finite-element order must be between 1 and 4.")
    if not math.isfinite(air_padding) or not 0.25 <= air_padding <= 20:
        raise ValueError("Air-domain padding must be between 0.25 and 20 model spans.")
    if not project.get("geometries"):
        raise ValueError("Import at least one closed STEP solid before running a study.")
    if (
        len(body_force) != 3
        or len(body_force_phase) != 3
        or not all(math.isfinite(value) and abs(value) <= 1e15 for value in body_force)
        or not all(math.isfinite(value) and abs(value) <= 360_000 for value in body_force_phase)
    ):
        raise ValueError("Enter finite X, Y and Z mechanical body-force amplitudes and phases.")
    body_force_phasor = _phasor_vector(body_force, body_force_phase)
    boundary_assignments = _active_boundary_assignments(project, physics)
    mechanical_body_force_by_domain = _domain_phasor_assignments(
        project,
        "mechanical_body_force_n_m3",
        "mechanical_body_force_phase_deg",
        "mechanical_body_force_suppressed",
        "Mechanical body force",
    ) if mechanical else {}
    current_density_by_domain = {}
    if electromagnetic:
        if em_source_mode == "wire_trace":
            trace_sets = project.get("wire_trace_sets", [])
            if not trace_sets:
                raise ValueError("Import at least one wire trace or select domain current density.")
            active_currents = []
            for trace_set in trace_sets:
                source_value = trace_set.get("target_gradient")
                if source_value is None:
                    source_value = trace_set.get("current", 1.0)
                try:
                    source_value = float(source_value)
                    phase = float(trace_set.get("current_phase_deg", 0.0))
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValueError("Wire-trace current and phase must be finite numbers.") from error
                if not math.isfinite(source_value) or not math.isfinite(phase):
                    raise ValueError("Wire-trace current and phase must be finite numbers.")
                active_currents.append(source_value)
            if not any(value != 0 for value in active_currents):
                raise ValueError("Set a non-zero wire-trace current or target gradient before solving.")
        else:
            current_density_by_domain = _domain_phasor_assignments(
                project,
                "current_density_amplitude_a_per_m2",
                "current_density_phase_deg",
                "current_density_suppressed",
                "Electromagnetic current density",
            )
            if not any(
                abs(component) > 0
                for vector in current_density_by_domain.values()
                for component in vector
            ):
                raise ValueError(
                    "Assign a non-zero current-density vector to at least one domain selection."
                )
    has_mechanical_body_force = any(abs(value) > 0 for value in body_force_phasor) or any(
        abs(component) > 0
        for vector in mechanical_body_force_by_domain.values()
        for component in vector
    )
    has_prescribed_displacement = any(
        condition["type"] == "mechanical_prescribed_displacement"
        and any(abs(component) > 0 for component in condition["value"])
        for condition in boundary_assignments.values()
    )
    if mechanical and not (has_mechanical_body_force or has_prescribed_displacement):
        raise ValueError(
            "Set a non-zero body force or prescribed displacement before solving a mechanical study."
        )

    records = {item["name"].casefold(): item for item in builtin_materials()}
    records.update({item["name"].casefold(): item for item in project.get("materials", [])})
    if electromagnetic and "air" not in records:
        raise ValueError("The Air material is unavailable; restore the built-in material library.")

    material_names = set()
    solid_material_names = set()
    for geometry in project.get("geometries", []):
        default_material = str(geometry.get("material", "")).strip()
        if not default_material:
            raise ValueError(f"Assign a material to {geometry.get('name', 'Geometry')!r} before running.")
        if default_material.casefold() == UNASSIGNED_MATERIAL.casefold():
            raise ValueError(
                f"Assign a physical material to {geometry.get('name', 'Geometry')!r} before running."
            )
        names = [default_material]
        names.extend(domain_material_overrides(geometry, require_stable_tags=True).values())
        mesh_size = float(geometry.get("mesh_maxh_mm", 30.0))
        if not math.isfinite(mesh_size) or mesh_size <= 0:
            raise ValueError(f"{geometry.get('name', 'Geometry')!r} needs a positive mesh size.")
        domain_mesh_size_overrides(geometry, require_stable_tags=True)
        for selection in geometry.get("domain_selections", []):
            if selection.get("material") and not selection.get("domain_tags"):
                raise ValueError(
                    f"{_LEGACY_DOMAIN_SELECTION_ERROR} Selection {selection.get('name', '')!r}."
                )
        material_names.update(str(name).strip() for name in names if str(name).strip())
        solid_material_names.update(str(name).strip() for name in names if str(name).strip())
    if electromagnetic and em_source_mode == "wire_trace":
        for trace_set in project.get("wire_trace_sets", []):
            for trace in trace_set.get("traces", []):
                material_names.add(str(trace.get("material", trace_set.get("material", "Air"))).strip())

    resolved = {}
    required_material_names = material_names | ({"Air"} if electromagnetic else set())
    solid_material_keys = {item.casefold() for item in solid_material_names}
    for name in required_material_names:
        record = records.get(name.casefold())
        if record is None:
            if name.casefold() == UNASSIGNED_MATERIAL.casefold():
                raise ValueError(
                    "Assign a physical material from the library to every solid and region before running."
                )
            raise ValueError(
                f"Material {name!r} is not embedded in this project. Choose it from the material library."
            )
        properties = material_properties_for_study(
            record,
            physics,
            for_solid=name.casefold() in solid_material_keys,
        )
        resolved[record["name"]] = {"name": record["name"], **properties}
    return {
        "physics": physics,
        "em_source_mode": em_source_mode,
        "frequency_hz": frequency_hz,
        "polynomial_order": polynomial_order,
        "air_padding": air_padding,
        "mechanical_body_force_n_m3": body_force,
        "mechanical_body_force_phase_deg": body_force_phase,
        "mechanical_body_force_phasor": body_force_phasor,
        "mechanical_body_force_by_domain": mechanical_body_force_by_domain,
        "current_density_by_domain": current_density_by_domain,
        "boundary_assignments": boundary_assignments,
        "materials": resolved,
    }


def build_time_harmonic_em_problem(project, asset_paths):
    """Translate the supported 3D harmonic study into the existing solver API."""
    validated = validate_time_harmonic_em_setup(project)
    from ngsolve import CoefficientFunction
    from netgen.occ import Box, Glue, OCCGeometry, Pnt

    physics = validated["physics"]
    electromagnetic = physics == "harmonic_em"
    mechanical = physics == "harmonic_mechanical"
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
    air_record = material_records.get("air") if electromagnetic else None

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
    solid_domain_keys = []
    material_display_names = {}
    bounds_min = [math.inf, math.inf, math.inf]
    bounds_max = [-math.inf, -math.inf, -math.inf]
    used_materials = {}
    dirichlet_boundary_names = []
    mechanical_boundary_names = []
    dircondition_values = {}
    robin_boundary_names = []
    robin_normal_stiffness = {}
    robin_tangential_stiffness = {}
    mechanical_source_by_domain = {}
    current_density_by_domain = {}
    effective_mechanical_force = False

    for geometry_index, (geometry_input, geometry) in enumerate(
        zip(geometry_inputs, project["geometries"])
    ):
        default_material = str(geometry.get("material", "")).strip()
        if not default_material:
            raise ValueError(f"Assign a material to {geometry['name']!r} before running.")
        resolve_material(default_material)
        source = OCCGeometry(geometry_input["filename"])
        source_shape = source.shape
        source_solids = list(source_shape.solids)
        if not source_solids:
            raise ValueError(f"{geometry['name']!r} does not contain a closed STEP solid.")
        # Project selections are tagged from the source STEP coordinates. Resolve
        # those tags before converting the OCC shape to metres: scaling a
        # translated solid can move a few OCC edge coordinates across the
        # descriptor's rounding boundary and invalidate otherwise identical
        # face tags. Boundary names assigned below are carried through OCC's
        # scale transform.
        solid_tags, face_refs = step_geometry_tags(source_solids)

        selected_boundary_conditions = {
            tag: condition
            for (selection_geometry_id, tag), condition
            in validated["boundary_assignments"].items()
            if selection_geometry_id == geometry["id"]
        }

        faces_by_tag = {
            tag: (solid_tag, face)
            for tag, solid_tag, face in face_refs
            if tag is not None
        }
        stale_tags = sorted(set(selected_boundary_conditions) - set(faces_by_tag))
        if stale_tags:
            raise ValueError(
                f"Boundary selections for {geometry['name']!r} no longer match unique STEP faces. "
                "Inspect the changed or ambiguous geometry and recreate affected selections."
            )

        for tag, solid_tag, face in face_refs:
            assignment = selected_boundary_conditions.get(tag, {}) if tag else {}
            condition = assignment.get("type")
            if electromagnetic and condition == "magnetic_potential_zero":
                boundary_name = f"DirEM_G{geometry_index}_{tag}"
                face.bc(boundary_name)
                dirichlet_boundary_names.append(boundary_name)
            elif mechanical and condition in {
                "mechanical_fixed", "mechanical_prescribed_displacement"
            }:
                boundary_name = f"DirMech_G{geometry_index}_{tag}"
                face.bc(boundary_name)
                mechanical_boundary_names.append(boundary_name)
                value = (
                    (0.0, 0.0, 0.0)
                    if condition == "mechanical_fixed"
                    else assignment["value"]
                )
                dircondition_values[boundary_name] = CoefficientFunction(value)
            elif mechanical and condition == "mechanical_spring_support":
                boundary_name = f"RobinMech_G{geometry_index}_{tag}"
                face.bc(boundary_name)
                robin_boundary_names.append(boundary_name)
                robin_normal_stiffness[boundary_name] = assignment["normal"]
                robin_tangential_stiffness[boundary_name] = assignment["tangential"]
            else:
                face.bc("Transmission_Interface")

        scaled_shape = source_shape.Scale(
            Pnt(0, 0, 0), geometry_input["unit_scale"]
        )
        shape = scaled_shape if scaled_shape is not None else source_shape
        solids = list(shape.solids)
        if len(solids) != len(solid_tags):
            raise ValueError(
                f"Could not preserve the solid mapping for {geometry['name']!r} while converting units."
            )

        overrides = {}
        local_mesh_overrides = domain_mesh_size_overrides(
            geometry, require_stable_tags=True
        )
        for selection in geometry.get("domain_selections", []):
            selected_material = selection.get("material")
            if not selected_material:
                continue
            domain_tags = selection.get("domain_tags")
            if not domain_tags:
                raise ValueError(
                    f"{_LEGACY_DOMAIN_SELECTION_ERROR} Selection {selection.get('name', '')!r}."
                )
            resolve_material(selected_material)
            stale_domains = set(domain_tags) - set(solid_tags)
            if stale_domains:
                raise ValueError(
                    f"Domain selection {selection.get('name', '')!r} no longer matches "
                    f"the solids in {geometry['name']!r}. Recreate the selection."
                )
            for domain_tag in domain_tags:
                existing = overrides.get(domain_tag)
                if existing and existing.casefold() != selected_material.casefold():
                    raise ValueError(
                        f"Solid {domain_tag} in {geometry['name']!r} has conflicting material assignments."
                    )
                overrides[domain_tag] = selected_material
        if set(local_mesh_overrides) - set(solid_tags):
            raise ValueError(
                f"A local mesh control no longer matches the solids in {geometry['name']!r}. "
                "Recreate the affected selection."
            )

        default_mesh_size_mm = float(geometry.get("mesh_maxh_mm", 30.0))
        if not math.isfinite(default_mesh_size_mm) or default_mesh_size_mm <= 0:
            raise ValueError(f"{geometry['name']!r} needs a positive maximum element size.")
        for solid_tag, solid in zip(solid_tags, solids):
            material_name = overrides.get(solid_tag, default_material)
            material = resolve_material(material_name)
            object_key = f"_mmfem_g{geometry_index}_{solid_tag}"
            solid.mat(object_key)
            objects[object_key] = solid
            domain_maxh[object_key] = (
                local_mesh_overrides.get(solid_tag, default_mesh_size_mm) * 0.001
            )
            used_materials[object_key] = material
            solid_domain_keys.append(object_key)
            material_display_names[object_key] = material["name"]
            all_solids.append(solid)
            domain_ref = (geometry["id"], solid_tag)
            if mechanical:
                body_force = validated["mechanical_body_force_by_domain"].get(
                    domain_ref, validated["mechanical_body_force_phasor"]
                )
                effective_mechanical_force = effective_mechanical_force or any(
                    abs(component) > 0 for component in body_force
                )
                mechanical_source_by_domain[object_key] = CoefficientFunction(body_force)
            if electromagnetic:
                current_density = validated["current_density_by_domain"].get(
                    domain_ref, (0.0, 0.0, 0.0)
                )
                current_density_by_domain[object_key] = CoefficientFunction(current_density)

        lower, upper = _shape_bounds(shape)
        bounds_min = [min(a, b) for a, b in zip(bounds_min, lower)]
        bounds_max = [max(a, b) for a, b in zip(bounds_max, upper)]

    prescribed_displacement = any(
        assignment.get("type") == "mechanical_prescribed_displacement"
        and any(abs(component) > 0 for component in assignment.get("value", ()))
        for assignment in validated["boundary_assignments"].values()
    )
    if mechanical and not (effective_mechanical_force or prescribed_displacement):
        raise ValueError(
            "No non-zero mechanical body-force or prescribed-displacement input remains after domain overrides."
        )

    span = max(upper - lower for lower, upper in zip(bounds_min, bounds_max))
    if not math.isfinite(span) or span <= 0:
        raise ValueError("The imported STEP solids have invalid overall dimensions.")
    if electromagnetic:
        padding = span * air_padding
        air_box = Box(
            Pnt(*(value - padding for value in bounds_min)),
            Pnt(*(value + padding for value in bounds_max)),
        ).bc("DirEM")
        inner_union = Glue(all_solids)
        air_shape = (air_box - inner_union).mat("_mmfem_air")

        solid_maxh = max(domain_maxh.values())
        air_maxh = max(solid_maxh, span / 12.0)
        air_key = "_mmfem_air"
        objects[air_key] = air_shape
        domain_maxh[air_key] = air_maxh
        used_materials[air_key] = resolve_material(air_record["name"])
        material_display_names[air_key] = air_record["name"]

    material_names = list(used_materials)
    if electromagnetic and validated["em_source_mode"] == "wire_trace":
        for trace_set in project.get("wire_trace_sets", []):
            for trace in trace_set.get("traces", []):
                trace_material = str(trace.get("material", trace_set.get("material", "Air"))).strip()
                trace_record = resolve_material(trace_material)
                used_materials.setdefault(trace_record["name"], trace_record)

    material_data = {name: used_materials[name] for name in used_materials}
    zero_vector = CoefficientFunction((0, 0, 0))
    study_type = "harmonic_em" if electromagnetic else "harmonic_mechanical"
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
        "cACCoef": int(electromagnetic),
        "kACCoef": int(electromagnetic),
        "kACCoefDC": 0,
        "mMechCoef": int(mechanical),
        "kMechCoef": int(mechanical),
        "dampingalpha": 0,
        "dampingbeta": 0,
        "cCoupCoef": 0,
        "kCoupCoef": 0,
        "epsilonReg": 1e-8,
        "current_source": (
            validated["em_source_mode"] if electromagnetic else "none"
        ),
        "wire_trace_sets": (
            build_wire_trace_sets(project, asset_paths)
            if electromagnetic and validated["em_source_mode"] == "wire_trace"
            else []
        ),
        "srcdictAC": {
            name: current_density_by_domain.get(name, zero_vector)
            if validated["em_source_mode"] == "domain_current_density"
            else 0
            for name in material_names
        },
        "srcdictDC": {name: 0 for name in material_names},
        "srcAAC": zero_vector,
        "srcADC": zero_vector,
        "srcMech": mechanical_source_by_domain if mechanical else zero_vector,
        "murdict": {name: item["mur"] for name, item in material_data.items()},
        "sigmadict": {name: item["sigma"] for name, item in material_data.items()},
        "Edict": {name: item["youngs"] for name, item in material_data.items()},
        "nudict": {name: item["poisson"] for name, item in material_data.items()},
        "rhodict": {name: item["density"] for name, item in material_data.items()},
        # The current 3D harmonic formulation distinguishes conducting
        # domains from nonconducting media through this flag.  Derive it from
        # the assigned conductivity so a zero-conductivity solid receives the
        # same magnetic-only regularized form as the surrounding air.
        "inoroutdict": {
            name: int(material_data[name]["sigma"] > 0.0)
            for name in material_names
        },
        "mechSubdom": "|".join(solid_domain_keys),
        "Neudict": {"Neu_0": 0},
        "Neukeys": ["Neu_0", "Transmission_Interface"],
        "dirconditionAAC": (
            {name: zero_vector for name in ["DirEM", *dirichlet_boundary_names]}
            if electromagnetic else {}
        ),
        "dirkeysAAC": (
            "|".join(["DirEM", *dirichlet_boundary_names])
            if electromagnetic else ""
        ),
        "dirconditionADC": {},
        "dirkeysADC": "",
        "dirconditionMech": dircondition_values if mechanical else {},
        "dirkeysMech": "|".join(mechanical_boundary_names),
        "robin_bcs": robin_boundary_names if mechanical else [],
        "robin_k_normal": robin_normal_stiffness if mechanical else {},
        "robin_k_tangential": robin_tangential_stiffness if mechanical else {},
        "dirconditionDC": {},
        "dirkeysDC": "",
        "quarter_domain": False,
        "solver": "direct",
        "solver_EMAC": "direct",
        "statCondEM": True,
        "statCondMech": False,
        "statCond": False,
        "electromagnetic_only": not mechanical,
        "Aux": {
            "study_type": study_type,
            "frequency_hz": frequency_hz,
            "polynomial_order": polynomial_order,
            "mechanical_body_force_n_m3": validated["mechanical_body_force_n_m3"],
            "mechanical_body_force_phase_deg": validated["mechanical_body_force_phase_deg"],
            "em_source_mode": validated["em_source_mode"],
            "wire_trace_sources": [
                {
                    "name": trace_set.get("name", "Wire trace"),
                    "current_a": float(trace_set.get("current", 1.0)),
                    "current_phase_deg": float(trace_set.get("current_phase_deg", 0.0)),
                    "target_gradient_t_per_m": (
                        float(trace_set["target_gradient"])
                        if trace_set.get("target_gradient") is not None
                        else None
                    ),
                }
                for trace_set in (
                    project.get("wire_trace_sets", [])
                    if electromagnetic and validated["em_source_mode"] == "wire_trace"
                    else []
                )
            ],
            "current_density_by_domain": {
                f"{geometry_id}:{domain_tag}": [
                    [component.real, component.imag] for component in vector
                ]
                for (geometry_id, domain_tag), vector
                in validated["current_density_by_domain"].items()
            },
            "mechanical_body_force_by_domain": {
                f"{geometry_id}:{domain_tag}": [
                    [component.real, component.imag] for component in vector
                ]
                for (geometry_id, domain_tag), vector
                in validated["mechanical_body_force_by_domain"].items()
            },
            "boundary_assignments": {
                f"{geometry_id}:{boundary_tag}": {
                    key: (
                        [[component.real, component.imag] for component in value]
                        if key == "value"
                        else value
                    )
                    for key, value in assignment.items()
                }
                for (geometry_id, boundary_tag), assignment
                in validated["boundary_assignments"].items()
            },
            "material_properties": {
                name: {
                    "relative_permeability": item["mur"],
                    "electrical_conductivity": item["sigma"],
                    "youngs_modulus": item["youngs"],
                    "poissons_ratio": item["poisson"],
                    "density": item["density"],
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
        "physics": physics,
        "mechanical_body_force_n_m3": validated["mechanical_body_force_n_m3"],
        "em_source_mode": validated["em_source_mode"],
        "material_display_names": material_display_names,
    }
