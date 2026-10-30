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
MAX_PROJECT_BYTES = 512_000_000
MAX_ASSET_BYTES = 256_000_000
MAX_PROJECT_ITEMS = 500
MAX_NAMED_SELECTIONS = 500
MAX_SELECTION_REGIONS = 100_000
MIN_MESH_SIZE_MM = 0.01
MAX_MESH_SIZE_MM = 100_000.0

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
    }


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


def add_step_geometry(project, assets, filename, data, *, units="m", material="Shield"):
    if units not in LENGTH_UNITS:
        raise ValueError(f"Unsupported STEP length unit: {units!r}.")
    payload = validate_step_file(filename, data)
    asset_id = _asset_id()
    suffix = ".step"
    asset = _asset_path("geometry", asset_id, filename, suffix)
    item = {
        "id": asset_id,
        "name": str(filename),
        "material": _safe_name(material, "Shield"),
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
        "epsilon": 0.001,
        "dsv": 0.4,
        "centre": [0.0, 0.0, 0.0],
        "samples": 41,
        "material": _safe_name(material, "Air"),
        "traces": [{
            "id": trace_id,
            "name": source_name,
            "material": _safe_name(material, "Air"),
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
        selection["name"] = name.strip()
        selection[region_key] = sorted(region_ids)
        used_ids.add(selection_id)
        used_names.add(normalized_name)


def validate_3d_project(project, assets):
    if not isinstance(project, dict) or project.get("schema") != PROJECT_SCHEMA:
        raise ValueError("This is not an MM-FEM 3D project.")
    if project.get("version") != PROJECT_VERSION:
        raise ValueError(f"Unsupported MM-FEM 3D project version: {project.get('version')}.")
    geometries = project.get("geometries")
    trace_sets = project.get("wire_trace_sets")
    if not isinstance(geometries, list) or not isinstance(trace_sets, list):
        raise ValueError("The project is missing its geometry or wire trace list.")
    if len(geometries) + len(trace_sets) > MAX_PROJECT_ITEMS:
        raise ValueError("A project can contain at most 500 geometry and trace items.")
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
        _validate_named_selections(
            geometry, "boundary_selections", "boundary_ids", "boundary"
        )
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
            epsilon = float(trace_set.get("epsilon", 0.001))
            if not math.isfinite(current) or not math.isfinite(epsilon) or epsilon <= 0:
                raise ValueError("Wire trace current must be finite and epsilon must be positive.")
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
        raise ValueError("The MM-FEM 3D project is empty or exceeds the 512 MB limit.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError(f"Could not read the MM-FEM 3D project archive: {error}") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_PROJECT_ITEMS * 3 + 1:
            raise ValueError("The MM-FEM 3D project contains too many files.")
        names = [item.filename for item in infos]
        if len(set(names)) != len(names):
            raise ValueError("The MM-FEM 3D project contains duplicate file entries.")
        if PROJECT_ENTRY not in names:
            raise ValueError(f"The archive does not contain {PROJECT_ENTRY}.")
        total_size = 0
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or "\\" in info.filename:
                raise ValueError(f"Unsafe path in MM-FEM 3D project: {info.filename!r}.")
            if info.flag_bits & 0x1:
                raise ValueError("Encrypted project files are not supported.")
            total_size += info.file_size
            if total_size > MAX_PROJECT_BYTES:
                raise ValueError("The uncompressed project exceeds the 512 MB limit.")
            if info.filename != PROJECT_ENTRY and info.file_size > MAX_ASSET_BYTES:
                raise ValueError("An asset in the MM-FEM 3D project exceeds 256 MB.")
        try:
            project = json.loads(archive.read(PROJECT_ENTRY).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid MM-FEM 3D project metadata: {error}") from error
        if not isinstance(project, dict):
            raise ValueError("MM-FEM 3D project metadata must be a JSON object.")
        geometries = project.get("geometries")
        trace_sets = project.get("wire_trace_sets")
        if not isinstance(geometries, list) or not isinstance(trace_sets, list):
            raise ValueError("The project is missing its geometry or wire trace list.")
        asset_paths = set()
        for geometry in geometries:
            if not isinstance(geometry, dict) or not isinstance(geometry.get("asset"), str):
                raise ValueError("Invalid geometry metadata in the MM-FEM 3D project.")
            asset_paths.add(geometry["asset"])
        for trace_set in trace_sets:
            if not isinstance(trace_set, dict) or not isinstance(trace_set.get("traces"), list):
                raise ValueError("Invalid wire trace metadata in the MM-FEM 3D project.")
            for trace in trace_set["traces"]:
                if not isinstance(trace, dict) or not isinstance(trace.get("filename"), str):
                    raise ValueError("Invalid wire trace metadata in the MM-FEM 3D project.")
                asset_paths.add(trace["filename"])
        unexpected = set(names) - {PROJECT_ENTRY} - asset_paths
        if unexpected:
            raise ValueError("The MM-FEM 3D project contains unsupported files: " + ", ".join(sorted(unexpected)))
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
