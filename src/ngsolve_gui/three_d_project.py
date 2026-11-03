"""Portable project files for imported 3D geometry and wire traces."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import math
import re
from pathlib import Path
import zipfile
from pathlib import PurePosixPath
from uuid import uuid4


PROJECT_SUFFIX = ".mmfem3d"
PROJECT_SCHEMA = "mm-fem.3d-project"
PROJECT_VERSION = 1
PROJECT_ENTRY = "project.json"
UNASSIGNED_MATERIAL = "Unassigned"
MAX_PROJECT_BYTES = 512_000_000
MAX_ASSET_BYTES = 256_000_000
MAX_PROJECT_ITEMS = 500
MAX_NAMED_SELECTIONS = 500
MAX_SELECTION_REGIONS = 100_000
MIN_MESH_SIZE_MM = 0.01
MAX_MESH_SIZE_MM = 100_000.0
SUPPORTED_3D_BOUNDARY_CONDITIONS = {
    "magnetic_potential_zero": "Vector potential A = 0",
    "mechanical_fixed": "Fixed support (u = 0)",
    "mechanical_prescribed_displacement": "Prescribed displacement",
    "mechanical_spring_support": "Spring support",
}
SUPPORTED_3D_STUDIES = {
    "harmonic_em": "Time-harmonic electromagnetics",
    "harmonic_mechanical": "Time-harmonic mechanics",
    "magnetostatic_dc": "Static/DC magnetics",
    "coupled_magneto_mechanical": "Fully coupled magneto-mechanics",
}
SUPPORTED_3D_EM_SOURCE_MODES = {
    "wire_trace": "Wire traces",
    "domain_current_density": "Domain current density",
}
_STEP_FACE_TAG_PATTERN = re.compile(r"MMFEM_FACE_[0-9a-f]{24}\Z")
_STEP_SOLID_TAG_PATTERN = re.compile(r"MMFEM_SOLID_[0-9a-f]{24}\Z")
_LEGACY_STEP_FACE_TAG_PATTERN = re.compile(r"MMFEM_FACE_\d+_\d+\Z")
_LEGACY_DOMAIN_SELECTION_ERROR = (
    "This saved domain assignment uses order-based geometry ids. Recreate the named "
    "selection from the current mesh before applying a material or local mesh size."
)

LENGTH_UNITS = {
    "m": 1.0,
    "cm": 0.01,
    "mm": 0.001,
    "um": 1e-6,
    "nm": 1e-9,
    "in": 0.0254,
    "ft": 0.3048,
}
_STEP_UNIT_PATTERNS = (
    (re.compile(rb"SI_UNIT\s*\(\s*\.MILLI\.\s*,\s*\.METRE\.\s*\)", re.I), "mm"),
    (re.compile(rb"SI_UNIT\s*\(\s*\.CENTI\.\s*,\s*\.METRE\.\s*\)", re.I), "cm"),
    (re.compile(rb"SI_UNIT\s*\(\s*\.MICRO\.\s*,\s*\.METRE\.\s*\)", re.I), "um"),
    (re.compile(rb"SI_UNIT\s*\(\s*\.NANO\.\s*,\s*\.METRE\.\s*\)", re.I), "nm"),
    (re.compile(rb"SI_UNIT\s*\(\s*\$\s*,\s*\.METRE\.\s*\)", re.I), "m"),
    (re.compile(rb"CONVERSION_BASED_UNIT\s*\(\s*'INCH'", re.I), "in"),
    (re.compile(rb"CONVERSION_BASED_UNIT\s*\(\s*'FOOT'", re.I), "ft"),
    (re.compile(rb"CONVERSION_BASED_UNIT\s*\(\s*'MILLIMET(?:RE|ER)'", re.I), "mm"),
    (re.compile(rb"CONVERSION_BASED_UNIT\s*\(\s*'CENTIMET(?:RE|ER)'", re.I), "cm"),
    (re.compile(rb"CONVERSION_BASED_UNIT\s*\(\s*'MET(?:RE|ER)'", re.I), "m"),
)
STEP_SUFFIXES = {".step", ".stp"}
TRACE_SUFFIXES = {".txt", ".csv", ".dat", ".xyz"}


def new_3d_project(name="Untitled 3D project"):
    return {
        "schema": PROJECT_SCHEMA,
        "version": PROJECT_VERSION,
        "name": str(name),
        "geometries": [],
        "wire_trace_sets": [],
        "materials": [],
        "study": {
            "physics": "harmonic_em",
            "em_source_mode": "wire_trace",
            "frequency_hz": 500.0,
            "polynomial_order": 1,
            "air_padding": 2.0,
            "dc_background_field_t": [0.0, 0.0, 0.0],
            "mechanical_body_force_n_m3": [0.0, 0.0, 0.0],
            "mechanical_body_force_phase_deg": [0.0, 0.0, 0.0],
            "coupling_max_iterations": 15,
            "coupling_tolerance": 1e-4,
        },
    }


def _finite_vector(value, label, *, limit):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must contain finite X, Y and Z components.")
    try:
        vector = [float(component) for component in value]
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} must contain finite X, Y and Z components.") from error
    if not all(math.isfinite(component) and abs(component) <= limit for component in vector):
        raise ValueError(f"{label} components must be finite and no larger than {limit:g}.")
    return vector


def _validate_optional_phasor(selection, amplitude_key, phase_key, label, *, limit):
    if amplitude_key not in selection:
        if phase_key in selection:
            raise ValueError(f"{label} phase requires an assigned {label.lower()} vector.")
        return
    selection[amplitude_key] = _finite_vector(
        selection[amplitude_key], label, limit=limit
    )
    phases = _finite_vector(
        selection.get(phase_key, [0.0, 0.0, 0.0]),
        f"{label} phase (degrees)",
        limit=360_000.0,
    )
    selection[phase_key] = phases


def snapshot_material(project, material):
    """Embed or refresh a material definition used by this project.

    Project files carry snapshots so a future change to a user's library does
    not silently alter an existing model. Re-selecting a material explicitly
    refreshes its snapshot; editing a library material updates its snapshot
    through :func:`update_material_references`.
    """
    if not isinstance(material, dict):
        return False
    name = str(material.get("name", "")).strip()
    material_id = str(material.get("id", "")).strip()
    properties = material.get("properties")
    if not name or not material_id or not isinstance(properties, dict):
        return False
    materials = project.setdefault("materials", [])
    snapshot = copy.deepcopy({
        "id": material_id,
        "name": name,
        "properties": properties,
    })
    existing = next(
        (
            item for item in materials
            if isinstance(item, dict) and item.get("id") == material_id
        ),
        None,
    )
    if existing is None:
        existing = next(
            (
                item for item in materials
                if isinstance(item, dict)
                and str(item.get("name", "")).strip().casefold() == name.casefold()
            ),
            None,
        )
    if existing is None:
        materials.append(snapshot)
        return True
    if existing == snapshot:
        return False
    existing.clear()
    existing.update(snapshot)
    return True


def update_material_references(project, material, *, previous_name=None):
    """Update a definition snapshot and all name-based assignments in a project."""
    if not isinstance(material, dict):
        return False
    name = str(material.get("name", "")).strip()
    if not name:
        return False
    old_name = str(previous_name if previous_name is not None else name).strip()
    changed = False
    assigned = False
    old_key = old_name.casefold()

    for geometry in project.get("geometries", []):
        if str(geometry.get("material", "")).strip().casefold() == old_key:
            assigned = True
            geometry["material"] = name
            changed = True
        for selection in geometry.get("domain_selections", []):
            if str(selection.get("material", "")).strip().casefold() == old_key:
                assigned = True
                selection["material"] = name
                changed = True
    for trace_set in project.get("wire_trace_sets", []):
        if str(trace_set.get("material", "")).strip().casefold() == old_key:
            assigned = True
            trace_set["material"] = name
            changed = True
        for trace in trace_set.get("traces", []):
            if str(trace.get("material", "")).strip().casefold() == old_key:
                assigned = True
                trace["material"] = name
                changed = True

    materials = project.setdefault("materials", [])
    material_id = str(material.get("id", "")).strip()
    snapshot = copy.deepcopy({
        "id": material_id,
        "name": name,
        "properties": material.get("properties", {}),
    })
    existing = next(
        (
            item for item in materials
            if isinstance(item, dict)
            and (item.get("id") == material_id or
                 str(item.get("name", "")).strip().casefold() == old_key)
        ),
        None,
    )
    if existing is not None and existing != snapshot:
        existing.clear()
        existing.update(snapshot)
        changed = True
    elif existing is None and assigned:
        materials.append(snapshot)
        changed = True
    return changed


def _safe_name(name, fallback):
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "").strip())
    value = value.strip("._-")
    return (value or fallback)[:100]


def _asset_id():
    return uuid4().hex


def _asset_path(kind, asset_id, filename, suffix):
    return f"assets/{kind}/{asset_id}_{_safe_name(filename, 'trace')}{suffix}"


def validate_step_file(filename, data):
    """Validate a STEP file envelope before passing it to the CAD importer."""
    if not str(filename).lower().endswith(tuple(STEP_SUFFIXES)):
        raise ValueError("Choose a STEP geometry file (.step or .stp).")
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ValueError("The STEP file is empty.")
    if len(data) > MAX_ASSET_BYTES:
        raise ValueError("STEP files must be smaller than 256 MB.")
    header = bytes(data[:4096]).lstrip(b"\xef\xbb\xbf\x00\t\r\n ").upper()
    if b"ISO-10303-21" not in header:
        raise ValueError("This file does not have a valid STEP header.")
    return bytes(data)


def detect_step_unit(data):
    """Read the common length-unit declarations used in STEP files."""
    payload = bytes(data)
    for pattern, unit in _STEP_UNIT_PATTERNS:
        if pattern.search(payload):
            return unit
    return None


def step_unit_scale(data, *, units):
    """Convert Netgen/OCC STEP coordinates to metres.

    OCC's STEP reader normalizes coordinates to millimetres. When the file
    declares a unit, ``units`` can override that declaration; for untagged STEP
    files it is the assumed unit of the numeric coordinates.
    """
    if units not in LENGTH_UNITS:
        raise ValueError(f"Unsupported STEP length unit: {units!r}.")
    declared = detect_step_unit(data)
    if declared is None:
        return LENGTH_UNITS[units]
    return LENGTH_UNITS[units] / (1000.0 * LENGTH_UNITS[declared])


def parse_wire_trace(data, units="m"):
    """Parse an XYZ trace, convert to metres, and keep NaN rows as path breaks."""
    if units not in LENGTH_UNITS:
        raise ValueError(f"Unsupported wire trace unit: {units!r}.")
    if isinstance(data, str):
        text = data
    else:
        try:
            text = bytes(data).decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise ValueError("Wire traces must be UTF-8 text.") from error
    points = []
    valid_points = 0
    has_segment = False
    previous = None
    scale = LENGTH_UNITS[units]
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        row = next(csv.reader([stripped])) if "," in stripped else stripped.split()
        if len(row) != 3:
            raise ValueError(f"Line {line_number} must contain exactly three XYZ values.")
        if valid_points == 0 and tuple(value.strip().lower() for value in row) == ("x", "y", "z"):
            continue
        try:
            raw_point = tuple(float(value.strip()) for value in row)
        except (ValueError, OverflowError) as error:
            raise ValueError(f"Line {line_number} contains a non-numeric coordinate.") from error
        if all(math.isnan(value) for value in raw_point):
            if points and points[-1] is not None:
                points.append(None)
            previous = None
            continue
        if not all(math.isfinite(value) for value in raw_point):
            raise ValueError(f"Line {line_number} contains a non-finite coordinate.")
        point = tuple(value * scale for value in raw_point)
        if previous is not None and point != previous:
            has_segment = True
        points.append(point)
        valid_points += 1
        previous = point
        if valid_points > 2_000_000:
            raise ValueError("Wire traces are limited to 2 million points per file.")
    if valid_points < 2:
        raise ValueError("A wire trace must contain at least two XYZ points.")
    if not has_segment:
        raise ValueError("A wire trace must contain at least one non-zero length segment.")
    return points


def wire_trace_text(points):
    return "\n".join(
        "nan nan nan" if point is None else " ".join(format(value, ".17g") for value in point)
        for point in points
    ) + "\n"


def add_step_geometry(
    project, assets, filename, data, *, units="m", material=UNASSIGNED_MATERIAL
):
    if units not in LENGTH_UNITS:
        raise ValueError(f"Unsupported STEP length unit: {units!r}.")
    payload = validate_step_file(filename, data)
    asset_id = _asset_id()
    suffix = ".step"
    asset = _asset_path("geometry", asset_id, filename, suffix)
    item = {
        "id": asset_id,
        "name": str(filename),
        "material": str(material or "").strip() or UNASSIGNED_MATERIAL,
        "units": units,
        "asset": asset,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "domain_selections": [],
        "boundary_selections": [],
    }
    project["geometries"].append(item)
    assets[asset] = payload
    validate_3d_project(project, assets)
    return item


def add_wire_trace(project, assets, filename, data, *, units="m", material="Air"):
    if not str(filename).lower().endswith(tuple(TRACE_SUFFIXES)):
        raise ValueError("Choose a text wire trace (.txt, .csv, .dat, or .xyz).")
    points = parse_wire_trace(data, units)
    payload = wire_trace_text(points).encode("utf-8")
    trace_id = _asset_id()
    source_name = str(filename)
    asset = _asset_path("traces", trace_id, source_name, ".txt")
    name = _safe_name(PurePosixPath(source_name.replace("\\", "/")).stem, "wire_trace")
    trace_set = {
        "id": trace_id,
        "name": name,
        "direction": "x",
        "current": 1.0,
        "current_phase_deg": 0.0,
        "epsilon": 0.001,
        "dsv": 0.4,
        "centre": [0.0, 0.0, 0.0],
        "samples": 41,
        "material": str(material or "").strip() or "Air",
        "traces": [{
            "id": trace_id,
            "name": source_name,
            "material": str(material or "").strip() or "Air",
            "filename": asset,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "current_scale": 1.0,
            "points": sum(point is not None for point in points),
        }],
    }
    project["wire_trace_sets"].append(trace_set)
    assets[asset] = payload
    validate_3d_project(project, assets)
    return trace_set


def _validate_asset_reference(item, assets, *, kind, extension):
    asset = item.get("asset") if kind == "geometry" else item.get("filename")
    if not isinstance(asset, str) or not asset.startswith(f"assets/{kind}/"):
        raise ValueError(f"Invalid {kind} asset reference.")
    path = PurePosixPath(asset)
    if path.is_absolute() or ".." in path.parts or "\\" in asset:
        raise ValueError(f"Unsafe {kind} asset path: {asset!r}.")
    if path.suffix.lower() != extension:
        raise ValueError(f"Invalid {kind} asset extension: {path.suffix!r}.")
    payload = assets.get(asset)
    if not isinstance(payload, (bytes, bytearray)):
        raise ValueError(f"Missing {kind} asset: {asset}.")
    if len(payload) > MAX_ASSET_BYTES:
        raise ValueError(f"{kind.title()} assets must be smaller than 256 MB.")
    if hashlib.sha256(payload).hexdigest() != item.get("sha256"):
        raise ValueError(f"Asset checksum mismatch: {asset}.")
    return bytes(payload)


def _validate_named_selections(geometry, key, region_key, label):
    selections = geometry.setdefault(key, [])
    if not isinstance(selections, list) or len(selections) > MAX_NAMED_SELECTIONS:
        raise ValueError(
            f"Each geometry can contain at most {MAX_NAMED_SELECTIONS} named {label} selections."
        )
    used_ids = set()
    used_names = set()
    for selection in selections:
        if not isinstance(selection, dict):
            raise ValueError(f"Named {label} selections must be objects.")
        selection_id = selection.get("id")
        name = selection.get("name")
        region_ids = selection.get(region_key)
        if not isinstance(selection_id, str) or not selection_id.strip() or len(selection_id) > 128:
            raise ValueError(f"Each named {label} selection must have an id.")
        if selection_id in used_ids:
            raise ValueError(f"Named {label} selection ids must be unique.")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError(f"Each named {label} selection needs a name of at most 100 characters.")
        normalized_name = name.strip().casefold()
        if normalized_name in used_names:
            raise ValueError(f"Named {label} selection names must be unique within a geometry.")
        if (
            not isinstance(region_ids, list)
            or not region_ids
            or len(region_ids) > MAX_SELECTION_REGIONS
        ):
            raise ValueError(
                f"Each named {label} selection must include between 1 and "
                f"{MAX_SELECTION_REGIONS:,} regions."
            )
        if any(type(region_id) is not int or region_id < 0 for region_id in region_ids):
            raise ValueError(f"Named {label} selection region ids must be non-negative integers.")
        if len(set(region_ids)) != len(region_ids):
            raise ValueError(f"Named {label} selection region ids must be unique.")
        tag_key = "domain_tags" if label == "domain" else "boundary_tags"
        tags = selection.get(tag_key)
        if tags is not None:
            pattern = _STEP_SOLID_TAG_PATTERN if label == "domain" else _STEP_FACE_TAG_PATTERN
            accepted = (pattern,)
            if label == "boundary":
                accepted += (_LEGACY_STEP_FACE_TAG_PATTERN,)
            if (
                not isinstance(tags, list)
                or not tags
                or len(tags) > MAX_SELECTION_REGIONS
                or any(
                    not isinstance(tag, str)
                    or not any(tag_pattern.fullmatch(tag) for tag_pattern in accepted)
                    for tag in tags
                )
            ):
                if label == "boundary":
                    raise ValueError(
                        "Named boundary selection face tags must be valid STEP face identifiers."
                    )
                raise ValueError("Named domain selection solid tags are invalid.")
            if len(set(tags)) != len(tags):
                raise ValueError(f"Named {label} selection tags must be unique.")
            if len(tags) != len(region_ids):
                raise ValueError(
                    f"Named {label} selection tags must map one-to-one to its region ids."
                )
            pairs = sorted(zip(region_ids, tags), key=lambda pair: pair[0])
            region_ids[:] = [region_id for region_id, _tag in pairs]
            tags[:] = [tag for _region_id, tag in pairs]
        if label == "boundary":
            condition = selection.get("boundary_condition")
            if condition is not None and (
                not isinstance(condition, str)
                or condition not in SUPPORTED_3D_BOUNDARY_CONDITIONS
            ):
                raise ValueError(
                    f"Unsupported 3D boundary condition {condition!r}. "
                    "Choose a condition supported by the 3D time-harmonic solver."
                )
            if (
                condition is not None
                and not selection.get("condition_suppressed", False)
                and not tags
            ):
                raise ValueError(
                    "A 3D boundary condition needs saved STEP face identifiers. "
                    "Regenerate the mesh and save the boundary selection again."
                )
        else:
            if "dc_current_density_a_per_m2" in selection:
                selection["dc_current_density_a_per_m2"] = _finite_vector(
                    selection["dc_current_density_a_per_m2"],
                    "DC current density (A/m²)",
                    limit=1e15,
                )
                if not isinstance(selection.get("dc_current_density_suppressed", False), bool):
                    raise ValueError("DC current-density suppression must be a boolean.")
            if (
                "dc_current_density_a_per_m2" in selection
                and not selection.get("dc_current_density_suppressed", False)
                and any(selection["dc_current_density_a_per_m2"])
                and not tags
            ):
                raise ValueError(
                    "A DC current-density source needs saved STEP solid identifiers. "
                    "Regenerate the mesh and save the domain selection again."
                )
        selection["name"] = name.strip()
        selection[region_key] = sorted(region_ids)
        used_ids.add(selection_id)
        used_names.add(normalized_name)


def _normalized_shape_descriptor(shape, scale, dimension):
    """Describe a STEP subshape using geometric properties, not OCC enumeration."""
    def number(value):
        # OCC bounding-box tolerances scale imperfectly after unit conversion;
        # quantize relative to the solid size so mm and m imports stay identical.
        return round(float(value) / scale, 4)

    def point(value):
        return tuple(number(coordinate) for coordinate in value)

    lower, upper = shape.bounding_box
    measure, centre = shape.Properties()
    edges = []
    for edge in shape.edges:
        edge_measure, edge_centre = edge.Properties()
        edge_lower, edge_upper = edge.bounding_box
        edge_vertices = sorted(point(vertex.center) for vertex in edge.vertices)
        edges.append((
            number(edge_measure),
            point(edge_centre),
            point(edge_lower),
            point(edge_upper),
            edge_vertices,
        ))
    edges.sort()
    vertices = sorted(point(vertex.center) for vertex in shape.vertices)
    return (
        number(measure ** (1.0 / dimension)),
        point(centre),
        point(lower),
        point(upper),
        tuple(edges),
        tuple(vertices),
    )


def step_geometry_tags(solids):
    """Return order-independent tags for distinguishable STEP solids and faces.

    Tags are based on normalized OCC geometric properties. A solid signature
    includes every face signature, so an edit to its topology invalidates its
    domain identity and all face identities. Duplicate face signatures are
    deliberately omitted: a saved assignment to one of those faces would be
    ambiguous and must be recreated after the geometry is inspected.
    """
    solids = list(solids)
    if not solids:
        return [], []

    descriptions = []
    for solid in solids:
        lower, upper = solid.bounding_box
        scale = max(float(upper[axis] - lower[axis]) for axis in range(3))
        if not math.isfinite(scale) or scale <= 0:
            raise ValueError("STEP solids have invalid dimensions and cannot be assigned safely.")
        face_descriptors = [
            _normalized_shape_descriptor(face, scale, 2) for face in solid.faces
        ]
        solid_descriptor = (
            _normalized_shape_descriptor(solid, scale, 3),
            tuple(sorted(face_descriptors)),
        )
        solid_signature = hashlib.sha256(
            json.dumps(solid_descriptor, separators=(",", ":")).encode("ascii")
        ).hexdigest()[:24]
        descriptions.append((solid_signature, face_descriptors))

    solid_signatures = [signature for signature, _faces in descriptions]
    if len(set(solid_signatures)) != len(solid_signatures):
        raise ValueError(
            "This STEP file contains indistinguishable overlapping solids. "
            "Separate or repair them before creating material or mesh assignments."
        )
    solid_tags = [f"MMFEM_SOLID_{signature}" for signature in solid_signatures]

    face_signatures = []
    for solid_tag, (_solid_signature, face_descriptors) in zip(solid_tags, descriptions):
        face_signatures.extend(
            (
                solid_tag,
                hashlib.sha256(
                    json.dumps(descriptor, separators=(",", ":")).encode("ascii")
                ).hexdigest()[:24],
            )
            for descriptor in face_descriptors
        )
    counts = {}
    for signature in face_signatures:
        counts[signature] = counts.get(signature, 0) + 1

    face_refs = []
    for solid_tag, solid, (_solid_signature, face_descriptors) in zip(
        solid_tags, solids, descriptions
    ):
        for face, descriptor in zip(solid.faces, face_descriptors):
            face_signature = hashlib.sha256(
                json.dumps(descriptor, separators=(",", ":")).encode("ascii")
            ).hexdigest()[:24]
            signature = (solid_tag, face_signature)
            tag = (
                f"MMFEM_FACE_{hashlib.sha256('|'.join(signature).encode('ascii')).hexdigest()[:24]}"
                if counts[signature] == 1 else None
            )
            face_refs.append((tag, solid_tag, face))
    return solid_tags, face_refs


def step_solid_tags(solids):
    """Return stable solid tags in the current import order."""
    return step_geometry_tags(solids)[0]


def step_face_tags(solids):
    """Yield only unambiguous, order-independent STEP face tags."""
    for tag, _solid_tag, face in step_geometry_tags(solids)[1]:
        if tag is not None:
            yield tag, face


def domain_mesh_size_overrides(geometry, *, require_stable_tags=False):
    """Return validated per-domain mesh sizes, rejecting ambiguous overlaps."""
    overrides = {}
    for selection in geometry.get("domain_selections", []):
        value = selection.get("mesh_maxh_mm")
        if value is None or value == "":
            continue
        try:
            maxh = float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                f"Invalid local mesh size for {selection.get('name', 'domain selection')!r}."
            ) from error
        if not math.isfinite(maxh) or not MIN_MESH_SIZE_MM <= maxh <= MAX_MESH_SIZE_MM:
            raise ValueError(
                f"Local maximum element size must be between {MIN_MESH_SIZE_MM:g} "
                f"and {MAX_MESH_SIZE_MM:g} mm."
            )
        domain_tags = selection.get("domain_tags")
        if require_stable_tags and not domain_tags:
            raise ValueError(
                f"{_LEGACY_DOMAIN_SELECTION_ERROR} Selection {selection.get('name', '')!r}."
            )
        refs = domain_tags if domain_tags else selection.get("domain_ids", [])
        for region_ref in refs:
            region_ref = str(region_ref) if domain_tags else int(region_ref)
            existing = overrides.get(region_ref)
            if existing is not None and not math.isclose(
                existing, maxh, rel_tol=1e-12, abs_tol=1e-12
            ):
                raise ValueError(
                    f"Domain {region_ref} has conflicting local mesh sizes. "
                    "Use one size for every overlapping selection."
                )
            overrides[region_ref] = maxh
        selection["mesh_maxh_mm"] = maxh
    return overrides


def domain_material_overrides(geometry, *, require_stable_tags=False):
    """Return non-conflicting material overrides keyed by solid tag or region id."""
    overrides = {}
    for selection in geometry.get("domain_selections", []):
        material = selection.get("material")
        if not material:
            continue
        domain_tags = selection.get("domain_tags")
        if require_stable_tags and not domain_tags:
            raise ValueError(
                f"{_LEGACY_DOMAIN_SELECTION_ERROR} Selection {selection.get('name', '')!r}."
            )
        refs = domain_tags if domain_tags else selection.get("domain_ids", [])
        for region_ref in refs:
            key = str(region_ref) if domain_tags else int(region_ref)
            existing = overrides.get(key)
            if existing is not None and existing.casefold() != str(material).strip().casefold():
                raise ValueError(
                    f"Domain {key} has conflicting material assignments. "
                    "Edit the overlapping named selections so the solid has one material."
                )
            overrides[key] = str(material).strip()
    return overrides


def validate_3d_project(project, assets):
    if not isinstance(project, dict) or project.get("schema") != PROJECT_SCHEMA:
        raise ValueError("This is not a MagMechSim 3D project.")
    if project.get("version") != PROJECT_VERSION:
        raise ValueError(f"Unsupported MagMechSim 3D project version: {project.get('version')}.")
    geometries = project.get("geometries")
    trace_sets = project.get("wire_trace_sets")
    if not isinstance(geometries, list) or not isinstance(trace_sets, list):
        raise ValueError("The project is missing its geometry or wire trace list.")
    if len(geometries) + len(trace_sets) > MAX_PROJECT_ITEMS:
        raise ValueError("A project can contain at most 500 geometry and trace items.")

    study = project.setdefault("study", {})
    if not isinstance(study, dict):
        raise ValueError("The 3D study settings must be an object.")
    physics = study.get("physics", "harmonic_em")
    if physics not in SUPPORTED_3D_STUDIES:
        raise ValueError("Choose a supported 3D study.")
    em_source_mode = study.get("em_source_mode", "wire_trace")
    coupled = physics == "coupled_magneto_mechanical"
    harmonic = physics in {"harmonic_em", "harmonic_mechanical"} or coupled
    electromagnetic = physics in {"harmonic_em", "magnetostatic_dc"} or coupled
    mechanical = physics in {"harmonic_mechanical"} or coupled
    uses_dc = physics in {"magnetostatic_dc"} or coupled
    if physics in {"harmonic_em", "coupled_magneto_mechanical"} and em_source_mode not in SUPPORTED_3D_EM_SOURCE_MODES:
        raise ValueError("Choose a supported 3D electromagnetic excitation source.")
    try:
        polynomial_order = int(study.get("polynomial_order", 1))
        if polynomial_order not in {1, 2, 3, 4}:
            raise ValueError("Polynomial order must be between 1 and 4.")
        if harmonic:
            frequency_hz = float(study.get("frequency_hz", 500.0))
            if not math.isfinite(frequency_hz) or not 0 < frequency_hz <= 1e9:
                raise ValueError("Frequency must be greater than 0 and no more than 1e9 Hz.")
        if electromagnetic:
            air_padding = float(study.get("air_padding", 2.0))
            if not math.isfinite(air_padding) or not 0.25 <= air_padding <= 20:
                raise ValueError("Air-domain padding must be between 0.25 and 20 model spans.")
        if uses_dc:
            dc_background_field = _finite_vector(
                study.get("dc_background_field_t", [0.0, 0.0, 0.0]),
                "Applied DC magnetic flux density (T)",
                limit=1e6,
            )
        if mechanical:
            body_force = _finite_vector(
                study.get("mechanical_body_force_n_m3", [0.0, 0.0, 0.0]),
                "Mechanical body force (N/m³)", limit=1e15,
            )
            body_force_phase = _finite_vector(
                study.get("mechanical_body_force_phase_deg", [0.0, 0.0, 0.0]),
                "Mechanical body-force phase (degrees)", limit=360_000.0,
            )
        if coupled:
            raw_max_iterations = study.get("coupling_max_iterations", 15)
            if isinstance(raw_max_iterations, bool):
                raise ValueError("Maximum coupling iterations must be an integer from 1 to 100.")
            numeric_max_iterations = float(raw_max_iterations)
            if (
                not math.isfinite(numeric_max_iterations)
                or not numeric_max_iterations.is_integer()
                or not 1 <= numeric_max_iterations <= 100
            ):
                raise ValueError("Maximum coupling iterations must be an integer from 1 to 100.")
            coupling_max_iterations = int(numeric_max_iterations)
            coupling_tolerance = float(study.get("coupling_tolerance", 1e-4))
            if not math.isfinite(coupling_tolerance) or not 1e-12 <= coupling_tolerance <= 0.1:
                raise ValueError("Coupling tolerance must be between 1e-12 and 0.1.")
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"Invalid 3D study settings: {error}") from error
    study["physics"] = physics
    study["polynomial_order"] = polynomial_order
    if physics in {"harmonic_em", "coupled_magneto_mechanical"}:
        study["em_source_mode"] = em_source_mode
    if electromagnetic:
        study["air_padding"] = air_padding
    if uses_dc:
        study["dc_background_field_t"] = dc_background_field
    if harmonic:
        study["frequency_hz"] = frequency_hz
    if mechanical:
        study["mechanical_body_force_n_m3"] = body_force
        study["mechanical_body_force_phase_deg"] = body_force_phase
    if coupled:
        study["coupling_max_iterations"] = coupling_max_iterations
        study["coupling_tolerance"] = coupling_tolerance

    materials = project.setdefault("materials", [])
    if not isinstance(materials, list) or len(materials) > MAX_PROJECT_ITEMS:
        raise ValueError("The project material list is invalid or too large.")
    material_ids = set()
    material_names = set()
    for material in materials:
        if not isinstance(material, dict):
            raise ValueError("Each project material must be an object.")
        material_id = material.get("id")
        material_name = material.get("name")
        properties = material.get("properties")
        if (
            not isinstance(material_id, str) or not material_id.strip()
            or not isinstance(material_name, str) or not material_name.strip()
            or not isinstance(properties, dict)
        ):
            raise ValueError("Each project material needs an id, name and properties object.")
        if material_id in material_ids or material_name.strip().casefold() in material_names:
            raise ValueError("Project material ids and names must be unique.")
        material_ids.add(material_id)
        material_names.add(material_name.strip().casefold())
    referenced = set()
    for geometry in geometries:
        if not isinstance(geometry, dict) or geometry.get("units") not in LENGTH_UNITS:
            raise ValueError("Each geometry must define a supported length unit.")
        if not isinstance(geometry.get("name"), str) or not geometry["name"].strip():
            raise ValueError("Each geometry must have a file name.")
        if not isinstance(geometry.get("material"), str) or not geometry["material"].strip():
            raise ValueError("Each geometry must have a material name.")
        if "mesh_maxh_mm" in geometry:
            try:
                maxh = float(geometry["mesh_maxh_mm"])
                if not math.isfinite(maxh) or not MIN_MESH_SIZE_MM <= maxh <= MAX_MESH_SIZE_MM:
                    raise ValueError(
                        f"Maximum mesh size must be between {MIN_MESH_SIZE_MM:g} "
                        f"and {MAX_MESH_SIZE_MM:g} mm."
                    )
                geometry["mesh_maxh_mm"] = maxh
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError(
                    f"Invalid maximum mesh size for {geometry['name']!r}: {error}"
                ) from error
        _validate_named_selections(
            geometry, "domain_selections", "domain_ids", "domain"
        )
        solid_names = geometry.setdefault("solid_names", {})
        if not isinstance(solid_names, dict) or len(solid_names) > MAX_NAMED_SELECTIONS:
            raise ValueError("Saved solid names must be a mapping of stable STEP solid ids to names.")
        normalized_solid_names = set()
        for solid_tag, solid_name in solid_names.items():
            if (
                not isinstance(solid_tag, str)
                or not _STEP_SOLID_TAG_PATTERN.fullmatch(solid_tag)
                or not isinstance(solid_name, str)
                or not solid_name.strip()
                or len(solid_name.strip()) > 120
            ):
                raise ValueError("Each saved solid name needs a stable STEP identity and a name of at most 120 characters.")
            key = solid_name.strip().casefold()
            if key in normalized_solid_names:
                raise ValueError("Solid names must be unique within a geometry model.")
            normalized_solid_names.add(key)
        for selection in geometry.get("domain_selections", []):
            assigned_material = selection.get("material")
            if assigned_material is not None and (
                not isinstance(assigned_material, str) or not assigned_material.strip()
            ):
                raise ValueError("A domain material assignment must be a material name.")
            _validate_optional_phasor(
                selection,
                "current_density_amplitude_a_per_m2",
                "current_density_phase_deg",
                "Current density (A/m²)",
                limit=1e15,
            )
            _validate_optional_phasor(
                selection,
                "mechanical_body_force_n_m3",
                "mechanical_body_force_phase_deg",
                "Mechanical body force (N/m³)",
                limit=1e15,
            )
            for suppression_key in (
                "current_density_suppressed",
                "mechanical_body_force_suppressed",
            ):
                if suppression_key in selection and not isinstance(
                    selection[suppression_key], bool
                ):
                    raise ValueError(f"{suppression_key.replace('_', ' ').capitalize()} must be true or false.")
            if any(
                key in selection
                for key in (
                    "current_density_amplitude_a_per_m2",
                    "mechanical_body_force_n_m3",
                )
            ) and not selection.get("domain_tags"):
                raise ValueError(
                    "A domain source or load needs stable STEP solid identifiers. "
                    "Regenerate the mesh and save the named selection again."
                )
        domain_mesh_size_overrides(geometry)
        _validate_named_selections(
            geometry, "boundary_selections", "boundary_ids", "boundary"
        )
        for selection in geometry.get("boundary_selections", []):
            if "condition_suppressed" in selection and not isinstance(
                selection["condition_suppressed"], bool
            ):
                raise ValueError("Boundary-condition suppression must be true or false.")
            condition = selection.get("boundary_condition")
            if condition == "mechanical_prescribed_displacement":
                selection["prescribed_displacement_amplitude_m"] = _finite_vector(
                    selection.get("prescribed_displacement_amplitude_m", [0, 0, 0]),
                    "Prescribed displacement amplitude (m)",
                    limit=1e3,
                )
                selection["prescribed_displacement_phase_deg"] = _finite_vector(
                    selection.get("prescribed_displacement_phase_deg", [0, 0, 0]),
                    "Prescribed displacement phase (degrees)",
                    limit=360_000.0,
                )
            if condition == "mechanical_spring_support":
                try:
                    normal = float(selection.get("spring_normal_stiffness_n_per_m3", 0.0))
                    tangential = float(selection.get("spring_tangential_stiffness_n_per_m3", 0.0))
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValueError("Spring stiffness values must be finite and non-negative.") from error
                if (
                    not math.isfinite(normal)
                    or not math.isfinite(tangential)
                    or not 0 <= normal <= 1e18
                    or not 0 <= tangential <= 1e18
                ):
                    raise ValueError("Spring stiffness values must be finite and between 0 and 1e18 N/m³.")
                selection["spring_normal_stiffness_n_per_m3"] = normal
                selection["spring_tangential_stiffness_n_per_m3"] = tangential
        payload = _validate_asset_reference(geometry, assets, kind="geometry", extension=".step")
        validate_step_file(geometry.get("name", "geometry.step"), payload)
        referenced.add(geometry["asset"])
    for trace_set in trace_sets:
        if not isinstance(trace_set, dict):
            raise ValueError("Wire trace sets must be objects.")
        if not isinstance(trace_set.get("name"), str) or not trace_set["name"].strip():
            raise ValueError("Each wire trace set must have a name.")
        traces = trace_set.get("traces")
        if not isinstance(traces, list) or not traces:
            raise ValueError("Each wire trace set must contain at least one trace.")
        try:
            direction = str(trace_set.get("direction", "x")).lower()
            if direction not in {"x", "y", "z"}:
                raise ValueError("Wire trace direction must be x, y, or z.")
            current = float(trace_set.get("current", 1.0))
            current_phase = float(trace_set.get("current_phase_deg", 0.0))
            epsilon = float(trace_set.get("epsilon", 0.001))
            if (
                not math.isfinite(current)
                or not math.isfinite(current_phase)
                or abs(current_phase) > 360_000
                or not math.isfinite(epsilon)
                or epsilon <= 0
            ):
                raise ValueError("Wire trace current and phase must be finite, and epsilon must be positive.")
            dsv = float(trace_set.get("dsv", 0.4))
            samples = int(trace_set.get("samples", 41))
            if not math.isfinite(dsv) or dsv <= 0 or samples < 2:
                raise ValueError("DSV must be positive and use at least two samples.")
            if "target_gradient" in trace_set:
                target_gradient = float(trace_set["target_gradient"])
                if not math.isfinite(target_gradient):
                    raise ValueError("Target gradient must be finite.")
                centre = trace_set.get("centre", [0.0, 0.0, 0.0])
                if not isinstance(centre, (list, tuple)) or len(centre) != 3:
                    raise ValueError("Gradient centre must contain three coordinates.")
                centre = [float(value) for value in centre]
                if not all(math.isfinite(value) for value in centre):
                    raise ValueError("Gradient centre must contain finite coordinates.")
                trace_set["target_gradient"] = target_gradient
                trace_set["centre"] = centre
            trace_set["dsv"] = dsv
            trace_set["samples"] = samples
            trace_set["direction"] = direction
            trace_set["current"] = current
            trace_set["current_phase_deg"] = current_phase
            trace_set["epsilon"] = epsilon
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"Invalid wire trace set {trace_set.get('name', '')!r}: {error}") from error
        for trace in traces:
            if not isinstance(trace, dict):
                raise ValueError("Wire traces must be objects.")
            payload = _validate_asset_reference(trace, assets, kind="traces", extension=".txt")
            parse_wire_trace(payload, "m")
            referenced.add(trace["filename"])
    if set(assets) != referenced:
        raise ValueError("The project contains unreferenced or missing asset files.")
    return project


def package_3d_project(project, assets):
    project = copy.deepcopy(project)
    validate_3d_project(project, assets)
    encoded = (json.dumps(project, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    total_size = len(encoded) + sum(len(payload) for payload in assets.values())
    if total_size > MAX_PROJECT_BYTES:
        raise ValueError("The 3D project is larger than the 512 MB project limit.")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(PROJECT_ENTRY, encoded)
        for path in sorted(assets):
            archive.writestr(path, assets[path])
    return output.getvalue()


def unpack_3d_project(data):
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_PROJECT_BYTES:
        raise ValueError("The MagMechSim 3D project is empty or exceeds the 512 MB limit.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"Could not read the MagMechSim 3D project archive: {error}") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_PROJECT_ITEMS * 3 + 1:
            raise ValueError("The MagMechSim 3D project contains too many files.")
        names = [item.filename for item in infos]
        if len(set(names)) != len(names):
            raise ValueError("The MagMechSim 3D project contains duplicate file entries.")
        if PROJECT_ENTRY not in names:
            raise ValueError(f"The archive does not contain {PROJECT_ENTRY}.")
        total_size = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                raise ValueError(f"Unsafe path in MagMechSim 3D project: {info.filename!r}.")
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted project files are not supported.")
            total_size += info.file_size
            if total_size > MAX_PROJECT_BYTES:
                raise ValueError("The uncompressed project exceeds the 512 MB limit.")
            if info.filename != PROJECT_ENTRY and info.file_size > MAX_ASSET_BYTES:
                raise ValueError("An asset in the MagMechSim 3D project exceeds 256 MB.")
        try:
            project = json.loads(archive.read(PROJECT_ENTRY).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid MagMechSim 3D project metadata: {error}") from error
        if not isinstance(project, dict):
            raise ValueError("MagMechSim 3D project metadata must be a JSON object.")
        geometries = project.get("geometries")
        trace_sets = project.get("wire_trace_sets")
        if not isinstance(geometries, list) or not isinstance(trace_sets, list):
            raise ValueError("The project is missing its geometry or wire trace list.")
        asset_paths = set()
        for geometry in geometries:
            if not isinstance(geometry, dict) or not isinstance(geometry.get("asset"), str):
                raise ValueError("Invalid geometry metadata in the MagMechSim 3D project.")
            asset_paths.add(geometry["asset"])
        for trace_set in trace_sets:
            if not isinstance(trace_set, dict) or not isinstance(trace_set.get("traces"), list):
                raise ValueError("Invalid wire trace metadata in the MagMechSim 3D project.")
            for trace in trace_set["traces"]:
                if not isinstance(trace, dict) or not isinstance(trace.get("filename"), str):
                    raise ValueError("Invalid wire trace metadata in the MagMechSim 3D project.")
                asset_paths.add(trace["filename"])
        unexpected = set(names) - {PROJECT_ENTRY} - asset_paths
        if unexpected:
            raise ValueError("The MagMechSim 3D project contains unsupported files: " + ", ".join(sorted(unexpected)))
        assets = {path: archive.read(path) for path in asset_paths if path in names}
    validate_3d_project(project, assets)
    return project, assets


def materialize_3d_assets(project, assets, directory):
    """Write bundled assets into a solver run folder and return their paths."""
    validate_3d_project(project, assets)
    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    paths = {}
    for asset in sorted(assets):
        destination = (root / Path(*PurePosixPath(asset).parts)).resolve()
        try:
            destination.relative_to(root)
        except ValueError as error:
            raise ValueError(f"Unsafe asset destination: {asset!r}.") from error
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(assets[asset])
        temporary.replace(destination)
        paths[asset] = destination
    return paths


def build_wire_trace_sets(project, asset_paths):
    """Build the filename-based source structure used by the 3D solver."""
    result = []
    for trace_set in project.get("wire_trace_sets", []):
        converted = {
            "name": trace_set["name"],
            "direction": trace_set.get("direction", "x"),
            "current": float(trace_set.get("current", 1.0)),
            "current_phase_deg": float(trace_set.get("current_phase_deg", 0.0)),
            "epsilon": float(trace_set.get("epsilon", 0.001)),
            "traces": [],
        }
        if trace_set.get("target_gradient") is not None:
            converted.update({
                "target_gradient": float(trace_set["target_gradient"]),
                "dsv": float(trace_set.get("dsv", 0.4)),
                "centre": tuple(trace_set.get("centre", (0.0, 0.0, 0.0))),
                "samples": int(trace_set.get("samples", 41)),
            })
        for trace in trace_set["traces"]:
            filename = asset_paths.get(trace["filename"])
            if not filename:
                raise ValueError(f"No extracted path for wire trace: {trace['filename']}.")
            converted["traces"].append({
                "material": trace.get("material", trace_set.get("material", "Air")),
                "filename": str(filename),
                "current_scale": float(trace.get("current_scale", 1.0)),
                "epsilon": float(trace_set.get("epsilon", 0.001)),
            })
        result.append(converted)
    return result


def build_3d_solver_inputs(project, asset_paths):
    """Map imported assets to geometry records and the solver's trace schema.

    STEP paths keep their source declaration; a 3D geometry loader must scale
    OCC's millimetre coordinates by ``unit_scale`` before using SI trace data.
    """
    geometries = []
    for item in project.get("geometries", []):
        filename = asset_paths.get(item["asset"])
        if not filename:
            raise ValueError(f"No extracted path for STEP geometry: {item['asset']}.")
        geometries.append({
            "name": item["name"],
            "material": item["material"],
            "filename": str(filename),
            "units": item["units"],
            "unit_scale": step_unit_scale(
                Path(filename).read_bytes(), units=item["units"]
            ),
        })
    return {
        "geometries": geometries,
        "wire_trace_sets": build_wire_trace_sets(project, asset_paths),
    }
