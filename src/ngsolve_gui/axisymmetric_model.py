"""Portable, declarative model files for the axisymmetric Solve workspace.

The project format intentionally contains data only. In particular, parameter
expressions are parsed and evaluated by a small arithmetic interpreter rather
than ``eval`` so opening a model file cannot execute Python code.
"""

from __future__ import annotations

import ast
import copy
import io
import json
import math
import operator
import re
import uuid
import zipfile

from .units import parameter_dimensions as infer_parameter_dimensions, require_expression_unit


SCHEMA = "ngsolve-gui.axisymmetric"
SCHEMA_VERSION = 1
LEGACY_SCHEMA_VERSION = 1
MODEL_FILENAME = "problem.json"
STUDIES_FILENAME = "studies.json"
LAYOUT_FILENAME = "layout.json"
PROJECT_JSON_FORMAT = "mm-fem.axisymmetric-project"
PROJECT_JSON_VERSION = 1

_BINARY_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {
    "abs": abs,
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "exp": math.exp,
    "log": math.log,
    "min": min,
    "max": max,
}
_RESERVED_NAMES = set(_FUNCTIONS) | {"r", "z", "pi", "e"}
_EXPRESSION_UNITS = ("kg/m^3", "A/m^2", "N/m^3", "N/m^2", "S/m", "Pa", "Hz", "m")
_LEGACY_EXPRESSION_UNITS = (*_EXPRESSION_UNITS[:-1], "mm", "m")



def find_closed_sketch_loops(entities, *, tolerance=1e-12):
    """Return bounded faces after noding all intersections in sampled curves.

    Each sampled curve is treated as a polyline. Proper crossings, T-junctions,
    and coincident endpoints are inserted as graph vertices before the
    half-edge walk, so a line that bisects an existing outline creates two
    selectable faces instead of remaining a visual overlay.
    """
    if not isinstance(entities, list):
        return []
    try:
        tolerance = float(tolerance)
    except (TypeError, ValueError, OverflowError):
        return []
    if not math.isfinite(tolerance) or tolerance <= 0:
        return []

    paths = {}
    raw_segments = []
    nodes = {}
    adjacency = {}

    def point_key(point):
        return (round(point[0] / tolerance), round(point[1] / tolerance))

    for entity in entities:
        if not isinstance(entity, dict) or not isinstance(entity.get("id"), str):
            continue
        raw = entity.get("sampled_points") or entity.get("points")
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            continue
        path = []
        for point in raw:
            try:
                value = (float(point[0]), float(point[1]))
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
            if not all(math.isfinite(component) for component in value):
                continue
            if not path or math.dist(path[-1], value) > tolerance:
                path.append(value)
        if len(path) < 2:
            continue
        paths[entity["id"]] = path
        segment_count = len(path) - 1
        for segment_index, (first, second) in enumerate(zip(path, path[1:])):
            if math.dist(first, second) <= tolerance:
                continue
            raw_segments.append({
                "a": first,
                "b": second,
                "entity_id": entity["id"],
                "segment_index": segment_index,
                "segment_count": segment_count,
            })

    if len(raw_segments) < 3:
        return []

    cuts = [[(0.0, segment["a"]), (1.0, segment["b"])] for segment in raw_segments]

    def cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    def add_cut(index, fraction, point):
        fraction = min(1.0, max(0.0, float(fraction)))
        cuts[index].append((fraction, (float(point[0]), float(point[1]))))

    def point_parameter(point, first, second):
        dx, dy = second[0] - first[0], second[1] - first[1]
        length_squared = dx * dx + dy * dy
        if length_squared <= tolerance * tolerance:
            return None
        fraction = ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / length_squared
        parameter_tolerance = max(1e-10, tolerance / math.sqrt(length_squared))
        if fraction < -parameter_tolerance or fraction > 1.0 + parameter_tolerance:
            return None
        nearest = (first[0] + fraction * dx, first[1] + fraction * dy)
        if math.dist(point, nearest) > max(tolerance * 10.0, 1e-12):
            return None
        return min(1.0, max(0.0, fraction))

    for first_index, first in enumerate(raw_segments):
        a, b = first["a"], first["b"]
        rx, ry = b[0] - a[0], b[1] - a[1]
        first_length = math.hypot(rx, ry)
        for second_index in range(first_index + 1, len(raw_segments)):
            second = raw_segments[second_index]
            c, d = second["a"], second["b"]
            if (
                max(a[0], b[0]) < min(c[0], d[0]) - tolerance
                or max(c[0], d[0]) < min(a[0], b[0]) - tolerance
                or max(a[1], b[1]) < min(c[1], d[1]) - tolerance
                or max(c[1], d[1]) < min(a[1], b[1]) - tolerance
            ):
                continue
            if first["entity_id"] == second["entity_id"]:
                gap = abs(first["segment_index"] - second["segment_index"])
                closed_path = math.dist(paths[first["entity_id"]][0], paths[first["entity_id"]][-1]) <= tolerance
                if gap <= 1 or (closed_path and gap == first["segment_count"] - 1):
                    continue
            sx, sy = d[0] - c[0], d[1] - c[1]
            qx, qy = c[0] - a[0], c[1] - a[1]
            denominator = cross((rx, ry), (sx, sy))
            second_length = math.hypot(sx, sy)
            parameter_tolerance = max(
                1e-10,
                tolerance / max(first_length, tolerance),
                tolerance / max(second_length, tolerance),
            )
            area_tolerance = max(tolerance * max(first_length, second_length), 1e-24)
            if abs(denominator) > area_tolerance:
                t = cross((qx, qy), (sx, sy)) / denominator
                u = cross((qx, qy), (rx, ry)) / denominator
                if (
                    -parameter_tolerance <= t <= 1.0 + parameter_tolerance
                    and -parameter_tolerance <= u <= 1.0 + parameter_tolerance
                ):
                    t = min(1.0, max(0.0, t))
                    u = min(1.0, max(0.0, u))
                    point_a = (a[0] + t * rx, a[1] + t * ry)
                    point_b = (c[0] + u * sx, c[1] + u * sy)
                    point = ((point_a[0] + point_b[0]) / 2.0, (point_a[1] + point_b[1]) / 2.0)
                    add_cut(first_index, t, point)
                    add_cut(second_index, u, point)
                continue

            if abs(cross((qx, qy), (rx, ry))) > area_tolerance:
                continue
            for point in (c, d):
                t = point_parameter(point, a, b)
                if t is not None:
                    u = point_parameter(point, c, d)
                    add_cut(first_index, t, point)
                    if u is not None:
                        add_cut(second_index, u, point)
            for point in (a, b):
                u = point_parameter(point, c, d)
                if u is not None:
                    t = point_parameter(point, a, b)
                    add_cut(second_index, u, point)
                    if t is not None:
                        add_cut(first_index, t, point)

    segments = []
    seen_edges = set()
    for index, segment in enumerate(raw_segments):
        ordered = sorted(cuts[index], key=lambda item: item[0])
        clean = []
        for fraction, point in ordered:
            key = point_key(point)
            if clean and key == clean[-1][2]:
                continue
            clean.append((fraction, point, key))
            nodes.setdefault(key, point)
        for first_cut, second_cut in zip(clean, clean[1:]):
            first_t, _first_point, first_key = first_cut
            second_t, _second_point, second_key = second_cut
            if first_key == second_key:
                continue
            edge_key = tuple(sorted((first_key, second_key)))
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            path_t0 = (segment["segment_index"] + first_t) / segment["segment_count"]
            path_t1 = (segment["segment_index"] + second_t) / segment["segment_count"]
            edge_index = len(segments)
            segments.append((first_key, second_key, segment["entity_id"], path_t0, path_t1))
            adjacency.setdefault(first_key, []).append((second_key, edge_index))
            adjacency.setdefault(second_key, []).append((first_key, edge_index))

    if len(segments) < 3:
        return []

    ordered_neighbors = {
        origin: sorted(
            neighbors,
            key=lambda item: math.atan2(
                nodes[item[0]][1] - nodes[origin][1],
                nodes[item[0]][0] - nodes[origin][0],
            ),
        )
        for origin, neighbors in adjacency.items()
    }

    visited = set()
    loops = []
    seen_loops = set()
    for edge_index, (first, second, _entity_id, _path_t0, _path_t1) in enumerate(segments):
        for origin, _destination in ((first, second), (second, first)):
            initial = (origin, edge_index)
            if initial in visited:
                continue
            current = initial
            cycle_nodes = []
            cycle_refs = []
            closed = False
            for _ in range(2 * len(segments) + 1):
                start, current_edge = current
                if current in visited:
                    closed = current == initial
                    break
                visited.add(current)
                cycle_nodes.append(start)
                edge_start, edge_end, entity_id, path_t0, path_t1 = segments[current_edge]
                forward = start == edge_start
                from_t, to_t = (path_t0, path_t1) if forward else (path_t1, path_t0)
                cycle_refs.append({
                    "entity_id": entity_id,
                    "forward": forward,
                    "start_t": from_t,
                    "end_t": to_t,
                })
                end = edge_end if forward else edge_start
                outgoing = ordered_neighbors.get(end, [])
                reverse_position = next(
                    (slot for slot, item in enumerate(outgoing) if item[0] == start and item[1] == current_edge),
                    None,
                )
                if reverse_position is None or not outgoing:
                    break
                _next_neighbor, next_edge_index = outgoing[(reverse_position - 1) % len(outgoing)]
                current = (end, next_edge_index)
                if current == initial:
                    closed = True
                    break
            if not closed or len(cycle_nodes) < 3:
                continue
            canonical_start = min(range(len(cycle_nodes)), key=lambda index: cycle_nodes[index])
            cycle_nodes = cycle_nodes[canonical_start:] + cycle_nodes[:canonical_start]
            cycle_refs = cycle_refs[canonical_start:] + cycle_refs[:canonical_start]
            polygon = [nodes[key] for key in cycle_nodes]
            area = 0.5 * sum(
                polygon[i][0] * polygon[(i + 1) % len(polygon)][1]
                - polygon[(i + 1) % len(polygon)][0] * polygon[i][1]
                for i in range(len(polygon))
            )
            if area <= tolerance * tolerance:
                continue
            canonical = tuple(cycle_nodes)
            rotations = [canonical[i:] + canonical[:i] for i in range(len(canonical))]
            signature = min(rotations)
            if signature in seen_loops:
                continue
            seen_loops.add(signature)

            boundary = []
            for reference_index, reference in enumerate(cycle_refs):
                join_node = cycle_nodes[reference_index]
                is_topology_junction = len(adjacency.get(join_node, ())) > 2
                if boundary and not is_topology_junction:
                    previous = boundary[-1]
                    contiguous = abs(previous["end_t"] - reference["start_t"]) <= 1e-9
                    same_direction = (previous["end_t"] - previous["start_t"]) * (
                        reference["end_t"] - reference["start_t"]
                    ) > 0
                    if previous["entity_id"] == reference["entity_id"] and contiguous and same_direction:
                        previous["end_t"] = reference["end_t"]
                        previous["forward"] = previous["end_t"] > previous["start_t"]
                        continue
                boundary.append(reference)
            loops.append({
                "vertices": [[float(point[0]), float(point[1])] for point in polygon],
                "boundary": boundary,
                "entity_ids": list(dict.fromkeys(item["entity_id"] for item in boundary)),
            })
    return loops

def builtin_materials() -> list[dict]:
    """Return the material presets represented in the current problem files.

    Presets are offered in region selectors, but are copied into a model only
    when a user assigns one.  Values for the magnet-specific entries match
    the material dictionaries used by the axisymmetric magnet examples.
    """
    return copy.deepcopy([
        {
            "id": "material-air",
            "name": "Air",
            "properties": {
                "relative_permeability": "1",
                "electrical_conductivity": "0",
                "youngs_modulus": "0",
                "poissons_ratio": "0.3",
                "density": "0",
            },
        },
        {
            "id": "material-copper",
            "name": "Copper",
            "properties": {
                "relative_permeability": "1",
                "electrical_conductivity": "5.8e7",
                "youngs_modulus": "110e9",
                "poissons_ratio": "0.34",
                "density": "8960",
            },
        },
        {
            "id": "material-main-coil-composite",
            "name": "Main coil (effective composite)",
            "properties": {
                "relative_permeability": "1",
                "electrical_conductivity": "0",
                "youngs_modulus": "84e9",
                "poissons_ratio": "0.33",
                "density": "5700",
            },
        },
        {
            "id": "material-gradient-coil-copper",
            "name": "Copper (gradient coil)",
            "properties": {
                "relative_permeability": "1",
                "electrical_conductivity": "59e6",
                "youngs_modulus": "130e9",
                "poissons_ratio": "0.34",
                "density": "8960",
            },
        },
        {
            "id": "material-cryogenic-steel",
            "name": "Stainless steel (4 K / OVC)",
            "properties": {
                "relative_permeability": "1",
                "electrical_conductivity": "1.4e6",
                "youngs_modulus": "210e9",
                "poissons_ratio": "0.283",
                "density": "7900",
            },
        },
        {
            "id": "material-aluminium-77k",
            "name": "Aluminium (77 K shield)",
            "properties": {
                "relative_permeability": "1",
                "electrical_conductivity": "33e6",
                "youngs_modulus": "81e9",
                "poissons_ratio": "0.337",
                "density": "2698",
            },
        },
        {
            "id": "material-epoxy",
            "name": "Epoxy",
            "properties": {
                "relative_permeability": "1",
                "electrical_conductivity": "0",
                "youngs_modulus": "2.75e9",
                "poissons_ratio": "0.4",
                "density": "1160",
            },
        },
        {
            "id": "material-shim",
            "name": "Shim",
            "properties": {
                "relative_permeability": "1",
                "electrical_conductivity": "0",
                "youngs_modulus": "13e9",
                "poissons_ratio": "0.13",
                "density": "1850",
            },
        },
        {
            "id": "material-analytical-test",
            "name": "Analytical test material",
            "properties": {
                "relative_permeability": "1",
                "electrical_conductivity": "1",
                "youngs_modulus": "1e7",
                "poissons_ratio": "0.33",
                "density": "500",
            },
        },
    ])


def _normalise_expression(expression, units=_EXPRESSION_UNITS):
    text = str(expression).strip()
    for unit in units:
        if not text.endswith(unit):
            continue
        prefix = text[: -len(unit)]
        if prefix and (prefix[-1].isspace() or prefix[-1].isdigit() or prefix[-1] == ")"):
            return prefix.strip()
    return text


def _strip_legacy_mm_suffix(expression):
    if isinstance(expression, str):
        return _normalise_expression(expression, ("mm",))
    return expression


def _regional_load_value(load, key):
    return str(load.get(key, "0") or "0").strip() or "0"


def _regional_load_body_force(load):
    force = load.get("mechanical_body_force", {})
    if not isinstance(force, dict):
        force = {}
    return {
        "r": str(force.get("r", "0") or "0").strip() or "0",
        "z": str(force.get("z", "0") or "0").strip() or "0",
    }


def _expression_is_zero(expression):
    value = str(expression or "0").strip().lower().replace(" ", "")
    return value in {"", "0", "0.0", "0.00", "0.000", "0e0", "0.0e0", "0e+0", "0.0e+0"}


def _sum_source_expressions(expressions):
    active = [str(value or "0").strip() or "0" for value in expressions]
    active = [value for value in active if not _expression_is_zero(value)]
    if not active:
        return "0"
    if len(active) == 1:
        return active[0]
    return " + ".join(f"({value})" for value in active)


def migrate_regional_loads(model):
    """Add named regional loads and materialize their assignments for solver compatibility."""
    if not isinstance(model, dict):
        return model
    migrated = copy.deepcopy(model)
    geometry = migrated.get("geometry", {})
    if not isinstance(geometry, dict):
        return migrated
    regions = geometry.get("regions", [])
    if not isinstance(regions, list):
        return migrated

    loads = migrated.get("regional_loads", [])
    if not isinstance(loads, list):
        loads = []
    for load in loads:
        if not isinstance(load, dict):
            continue
        load.setdefault("region_ids", [])
        for key in ("dc_current_density", "ac_current_density_real", "ac_current_density_imaginary"):
            load.setdefault(key, "0")
        load.setdefault("mechanical_body_force", {"r": "0", "z": "0"})
        load["mechanical_body_force"] = _regional_load_body_force(load)

    assigned_region_ids = {
        region_id
        for load in loads if isinstance(load, dict)
        for region_id in (load.get("region_ids", []) if isinstance(load.get("region_ids", []), list) else [])
    }
    for region in regions:
        if not isinstance(region, dict):
            continue
        region_id = region.get("id")
        source = region.get("sources", {})
        if region_id in assigned_region_ids or not isinstance(source, dict):
            continue
        body = source.get("mechanical_body_force", {})
        if not isinstance(body, dict):
            body = {}
        values = {
            "dc_current_density": str(source.get("dc_current_density", "0") or "0").strip() or "0",
            "ac_current_density_real": str(source.get("ac_current_density_real", "0") or "0").strip() or "0",
            "ac_current_density_imaginary": str(source.get("ac_current_density_imaginary", "0") or "0").strip() or "0",
            "mechanical_body_force": {
                "r": str(body.get("r", "0") or "0").strip() or "0",
                "z": str(body.get("z", "0") or "0").strip() or "0",
            },
        }
        if all(_expression_is_zero(value) for key, value in values.items() if key != "mechanical_body_force") and all(
            _expression_is_zero(value) for value in values["mechanical_body_force"].values()
        ):
            continue
        base_name = f"{region.get('name', 'Region')} load"
        used_names = {str(item.get("name", "")).casefold() for item in loads if isinstance(item, dict)}
        name = base_name
        suffix = 2
        while name.casefold() in used_names:
            name = f"{base_name} {suffix}"
            suffix += 1
        loads.append({
            "id": new_id("load"),
            "name": name,
            "region_ids": [region_id] if region_id else [],
            **{key: values[key] for key in ("dc_current_density", "ac_current_density_real", "ac_current_density_imaginary")},
            "mechanical_body_force": values["mechanical_body_force"],
        })
    migrated["regional_loads"] = loads

    for region in regions:
        if not isinstance(region, dict) or not region.get("id"):
            continue
        region_loads = [
            load for load in loads
            if isinstance(load, dict)
            and isinstance(load.get("region_ids", []), list)
            and region["id"] in load.get("region_ids", [])
        ]
        if not region_loads:
            continue
        region["sources"] = {
            key: _sum_source_expressions(_regional_load_value(load, key) for load in region_loads)
            for key in ("dc_current_density", "ac_current_density_real", "ac_current_density_imaginary")
        }
        region["sources"]["mechanical_body_force"] = {
            component: _sum_source_expressions(
                _regional_load_body_force(load)[component] for load in region_loads
            )
            for component in ("r", "z")
        }
    return migrated


def _topology_coordinate_key(point):
    return tuple(round(float(value), 12) for value in point)


def _topology_point_id(point):
    key = _topology_coordinate_key(point)
    token = f"{key[0]:.12g},{key[1]:.12g}"
    return "point-" + uuid.uuid5(uuid.NAMESPACE_URL, "mm-fem-sketch-point:" + token).hex[:12]


def _edge_forward_for_region(edge, vertices):
    """Determine whether stored endpoint order follows the region contour."""
    endpoints = edge.get("vertices", [])
    if len(endpoints) != 2 or len(vertices) < 3:
        return True
    scale = max(1.0, *(abs(float(v)) for point in vertices for v in point))
    tolerance = scale * 1e-9

    def position(point):
        px, py = float(point[0]), float(point[1])
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            dx, dy = end[0] - start[0], end[1] - start[1]
            length2 = dx * dx + dy * dy
            if length2 <= 1e-30:
                continue
            fraction = ((px - start[0]) * dx + (py - start[1]) * dy) / length2
            if fraction < -1e-8 or fraction > 1.0 + 1e-8:
                continue
            distance = abs(dx * (py - start[1]) - dy * (px - start[0])) / math.sqrt(length2)
            if distance <= tolerance:
                return index + min(1.0, max(0.0, fraction))
        return None

    first, second = position(endpoints[0]), position(endpoints[1])
    if first is None or second is None:
        return True
    count = float(len(vertices))
    return (second - first) % count <= (first - second) % count


def _upgrade_sketch_topology(model):
    """Add persistent point references, curve types and oriented region loops."""
    geometry = model.get("geometry")
    if not isinstance(geometry, dict):
        return
    existing_points = geometry.get("points", [])
    if not isinstance(existing_points, list):
        existing_points = []
    point_ids = {}
    points = []
    for point in existing_points:
        if not isinstance(point, dict):
            continue
        coords = point.get("coordinates")
        if not isinstance(coords, (list, tuple)) or len(coords) != 2:
            continue
        try:
            key = _topology_coordinate_key(coords)
            if not all(math.isfinite(v) for v in key):
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        point_id = point.get("id")
        if not isinstance(point_id, str) or not point_id:
            point_id = _topology_point_id(key)
        if key not in point_ids:
            point_ids[key] = point_id
            points.append({"id": point_id, "coordinates": list(key)})

    regions = geometry.get("regions", [])
    edges = geometry.get("edges", [])
    if not isinstance(regions, list):
        regions = []
    if not isinstance(edges, list):
        edges = []
    edge_by_id = {
        edge.get("id"): edge
        for edge in edges
        if isinstance(edge, dict) and isinstance(edge.get("id"), str)
    }
    regions_by_edge = {}
    for region in regions:
        if not isinstance(region, dict):
            continue
        edge_ids = region.get("edge_ids", [])
        for edge_id in edge_ids if isinstance(edge_ids, list) else []:
            regions_by_edge.setdefault(edge_id, []).append(region)

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        endpoints = edge.get("vertices")
        if not isinstance(endpoints, list) or len(endpoints) != 2:
            continue
        try:
            coords = [_topology_coordinate_key(point) for point in endpoints]
        except (TypeError, ValueError, OverflowError):
            continue
        if not all(math.isfinite(v) for point in coords for v in point):
            continue
        references = []
        for key in coords:
            point_id = point_ids.get(key)
            if point_id is None:
                point_id = _topology_point_id(key)
                point_ids[key] = point_id
                points.append({"id": point_id, "coordinates": list(key)})
            references.append(point_id)
        edge["point_ids"] = references
        curve = edge.get("curve")
        if isinstance(curve, dict) and curve.get("type") in {"line", "arc", "circle", "spline3"}:
            continue
        circle = next((
            region for region in regions_by_edge.get(edge.get("id"), [])
            if isinstance(region.get("shape"), dict) and region["shape"].get("type") == "circle"
        ), None)
        if circle:
            shape = circle["shape"]
            center = [float(shape["r_center"]), float(shape["z_center"])]
            start, end = endpoints
            cross = (
                (float(start[0]) - center[0]) * (float(end[1]) - center[1])
                - (float(start[1]) - center[1]) * (float(end[0]) - center[0])
            )
            edge["curve"] = {
                "type": "arc",
                "center": center,
                "radius": float(shape["radius"]),
                "clockwise": cross < 0.0,
            }
        else:
            edge["curve"] = {"type": "line"}

    for region in regions:
        if not isinstance(region, dict):
            continue
        loops = region.get("boundary_loops")
        if isinstance(loops, list) and loops:
            continue
        try:
            vertices = [(float(point[0]), float(point[1])) for point in region.get("vertices", [])]
        except (TypeError, ValueError, IndexError, OverflowError):
            vertices = []
        refs = []
        edge_ids = region.get("edge_ids", [])
        for edge_id in edge_ids if isinstance(edge_ids, list) else []:
            edge = edge_by_id.get(edge_id)
            if edge:
                refs.append({"edge_id": edge_id, "forward": _edge_forward_for_region(edge, vertices)})
        region["boundary_loops"] = [{"role": "outer", "edges": refs}] if refs else []

    regions_by_id = {
        region.get("id"): region for region in regions
        if isinstance(region, dict) and isinstance(region.get("id"), str)
    }
    for child in regions:
        if not isinstance(child, dict):
            continue
        parent = regions_by_id.get(child.get("parent_id"))
        if not isinstance(parent, dict):
            continue
        outer = next((loop for loop in child.get("boundary_loops", []) if loop.get("role") == "outer"), None)
        if not isinstance(outer, dict):
            continue
        parent_loops = parent.setdefault("boundary_loops", [])
        if any(
            isinstance(loop, dict) and loop.get("role") == "hole" and loop.get("region_id") == child.get("id")
            for loop in parent_loops
        ):
            continue
        parent_loops.append({
            "role": "hole",
            "region_id": child.get("id"),
            "edges": [
                {"edge_id": item["edge_id"], "forward": not bool(item.get("forward", True))}
                for item in reversed(outer.get("edges", []))
                if isinstance(item, dict) and item.get("edge_id")
            ],
        })
    geometry["points"] = points
    geometry["topology_version"] = 1
    geometry.setdefault("sketch_entities", [])
    geometry.setdefault("voids", [])


def migrate_legacy_model(model):
    """Upgrade pre-SI geometry expressions while keeping their geometry fixed."""
    if not isinstance(model, dict) or model.get("schema_version") != LEGACY_SCHEMA_VERSION:
        return model
    model = copy.deepcopy(model)
    geometry = model.get("geometry", {})
    if not isinstance(geometry, dict):
        return model
    dimension_unit = geometry.get("dimension_expression_unit", "mm")
    if dimension_unit == "m":
        _upgrade_sketch_topology(model)
        return model
    if dimension_unit != "mm":
        return model
    for parameter in model.get("parameters", []) if isinstance(model.get("parameters", []), list) else []:
        if isinstance(parameter, dict) and "expression" in parameter:
            parameter["expression"] = _strip_legacy_mm_suffix(parameter["expression"])
    for material in model.get("materials", []) if isinstance(model.get("materials", []), list) else []:
        properties = material.get("properties", {}) if isinstance(material, dict) else {}
        if isinstance(properties, dict):
            for key, expression in properties.items():
                properties[key] = _strip_legacy_mm_suffix(expression)
    for condition in model.get("boundary_conditions", []) if isinstance(model.get("boundary_conditions", []), list) else []:
        if isinstance(condition, dict):
            for key in ("displacement_r", "displacement_z", "traction_r", "traction_z", "stiffness_normal", "stiffness_tangential"):
                if key in condition:
                    condition[key] = _strip_legacy_mm_suffix(condition[key])
    for section_name in ("dc_magnetic", "harmonic_electromagnetic"):
        physics = model.get("physics", {})
        section = physics.get(section_name, {}) if isinstance(physics, dict) else {}
        if isinstance(section, dict):
            for key in ("frequency_hz", "source_current_density", "source_current_density_real", "source_current_density_imaginary"):
                if key in section:
                    section[key] = _strip_legacy_mm_suffix(section[key])
    mesh = model.get("mesh", {})
    if isinstance(mesh, dict) and "element_size" in mesh:
        mesh["element_size"] = _strip_legacy_mm_suffix(mesh["element_size"])
    regions = geometry.get("regions", []) if isinstance(geometry, dict) else []
    if isinstance(regions, list):
        for region in regions:
            if not isinstance(region, dict):
                continue
            shape = region.get("shape", {})
            if not isinstance(shape, dict):
                continue
            expressions = shape.get("dimension_expressions", {})
            if isinstance(expressions, dict):
                for key, expression in expressions.items():
                    legacy_expression = _normalise_expression(expression, _LEGACY_EXPRESSION_UNITS)
                    expressions[key] = f"({legacy_expression}) / 1000"
            sources = region.get("sources", {})
            if isinstance(sources, dict):
                for key in ("dc_current_density", "ac_current_density_real", "ac_current_density_imaginary"):
                    if key in sources:
                        sources[key] = _strip_legacy_mm_suffix(sources[key])
                body = sources.get("mechanical_body_force", {})
                if isinstance(body, dict):
                    for key, expression in body.items():
                        body[key] = _strip_legacy_mm_suffix(expression)
    geometry["dimension_expression_unit"] = "m"
    _upgrade_sketch_topology(model)
    return model


def migrate_legacy_studies(studies):
    if not isinstance(studies, dict):
        return studies
    studies = copy.deepcopy(studies)
    entries = studies.get("studies", [])
    if isinstance(entries, list):
        for study in entries:
            if not isinstance(study, dict):
                continue
            points = study.get("frequency_hz")
            if isinstance(points, list):
                study["frequency_hz"] = [_strip_legacy_mm_suffix(point) for point in points]
            elif isinstance(points, str):
                study["frequency_hz"] = _strip_legacy_mm_suffix(points)
    return studies


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def new_model(name: str = "Untitled axisymmetric model") -> dict:
    """Return a blank r-z model with no material or boundary assignments."""
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "name": name,
        "units": "SI",
        "parameters": [],
        "geometry": {
            "coordinate_system": "r-z",
            "dimension_expression_unit": "m",
            "topology_version": 1,
            "points": [],
            "sketch_entities": [],
            "regions": [],
            "edges": [],
            "voids": [],
            "constraints": [],
        },
        "materials": [],
        "boundary_conditions": [],
        "regional_loads": [],
        "physics": {
            "dc_magnetic": {"enabled": True},
            "harmonic_electromagnetic": {
                "enabled": True,
                "frequency_hz": "500",
            },
            "mechanics": {"enabled": False},
            "coupling": {"enabled": False},
        },
        "mesh": {
            "element_size": "0.01",
            "polynomial_order": 3,
            "region_element_sizes": {},
            "hp_layers": 0,
            "hp_grading_factor": 0.3,
            "hp_region_ids": [],
            "hp_edge_ids": [],
        },
        "solver": {
            "linear_solver": "direct",
            "anderson": True,
            "anderson_depth": 3,
            "anderson_beta": 1.0,
            "relative_tolerance": 1e-6,
            "maximum_iterations": 50,
        },
    }


def new_studies() -> dict:
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "studies": [
            {
                "id": "study-static",
                "name": "DC + harmonic",
                "physics": ["dc_magnetic", "harmonic_electromagnetic"],
                "frequency_hz": ["500"],
            }
        ],
    }


def _evaluate(node: ast.AST, names: dict[str, float]) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id == "pi":
            return math.pi
        if node.id == "e":
            return math.e
        if node.id in names:
            return float(names[node.id])
        raise ValueError(f"Unknown parameter '{node.id}'")
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPS:
        left = _evaluate(node.left, names)
        right = _evaluate(node.right, names)
        if isinstance(node.op, ast.Pow) and abs(right) > 50:
            raise ValueError("Exponent magnitude must not exceed 50")
        result = _BINARY_OPS[type(node.op)](left, right)
        if not math.isfinite(result):
            raise ValueError("Expression result must be finite")
        return float(result)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return float(_UNARY_OPS[type(node.op)](_evaluate(node.operand, names)))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        function = _FUNCTIONS.get(node.func.id)
        if function is None or node.keywords:
            raise ValueError("Only supported arithmetic functions may be used")
        return float(function(*[_evaluate(arg, names) for arg in node.args]))
    raise ValueError("Use numbers, parameter names, arithmetic, and supported math functions")


def evaluate_expression(expression, parameters=None) -> float:
    """Evaluate a safe scalar expression, optionally with named parameters."""
    if isinstance(expression, (int, float)):
        value = float(expression)
        if math.isfinite(value):
            return value
        raise ValueError("Expression result must be finite")
    text = _normalise_expression(expression)
    # Units are retained as metadata in the model. Numeric evaluation uses the
    # leading expression; SI scale conversion is handled by the solver adapter.
    if not text:
        raise ValueError("Expression is empty")
    if len(text) > 256:
        raise ValueError("Expressions must contain no more than 256 characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise ValueError(f"Invalid expression: {error.msg}") from error
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ValueError("Expression is too complex")
    parameters = parameters or {}
    _validate_expression(text, set(parameters))

    def resolve_parameter(name, stack):
        if name in stack:
            raise ValueError("Circular parameter reference: " + " -> ".join((*stack, name)))
        definition = parameters[name]
        if isinstance(definition, dict):
            definition = definition.get("expression", "")
        definition_text = _normalise_expression(definition)
        if len(definition_text) > 256:
            raise ValueError("Expressions must contain no more than 256 characters")
        try:
            expression_tree = ast.parse(definition_text, mode="eval").body
        except SyntaxError as error:
            raise ValueError(f"Invalid parameter expression for '{name}': {error.msg}") from error
        _validate_expression(definition_text, set(parameters))
        symbols = {
            child.id for child in ast.walk(expression_tree)
            if isinstance(child, ast.Name) and child.id not in _FUNCTIONS
        }
        resolved = {}
        for symbol in symbols:
            if symbol in parameters:
                resolved[symbol] = resolve_parameter(symbol, (*stack, name))
        result = float(_evaluate(expression_tree, resolved))
        if not math.isfinite(result):
            raise ValueError(f"Parameter '{name}' must evaluate to a finite value")
        return result

    symbols = {
        child.id for child in ast.walk(tree.body)
        if isinstance(child, ast.Name) and child.id not in _FUNCTIONS
    }
    names = {name: resolve_parameter(name, ()) for name in symbols if name in parameters}
    result = float(_evaluate(tree.body, names))
    if not math.isfinite(result):
        raise ValueError("Expression result must be finite")
    return result


def _validate_draft_structure(model: dict) -> list[str]:
    """Check the document envelope so incomplete projects remain saveable."""
    errors = []
    if not isinstance(model, dict):
        return ["Model must be a JSON object."]
    if model.get("schema") != SCHEMA:
        errors.append(f"Expected schema '{SCHEMA}'.")
    if model.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Unsupported model schema version: {model.get('schema_version')}.")
    for key in ("parameters", "materials", "boundary_conditions", "regional_loads"):
        if key in model and not isinstance(model[key], list):
            errors.append(f"{key.replace('_', ' ').capitalize()} must be a list.")
    for key in ("physics", "mesh", "solver"):
        if key in model and not isinstance(model[key], dict):
            errors.append(f"{key.capitalize()} settings must be an object.")
    geometry = model.get("geometry")
    if not isinstance(geometry, dict):
        errors.append("Geometry section is missing.")
    else:
        for key in ("regions", "edges", "voids"):
            if key in geometry and not isinstance(geometry[key], list):
                errors.append(f"Geometry {key} must be a list.")
    studies = model.get("studies")
    if studies is not None and not isinstance(studies, list):
        errors.append("Studies must be a list.")
    return errors


def validate_model(model: dict, *, scope="solve") -> list[str]:
    """Validate for draft saving, meshing, or solving.

    Draft checks only the document envelope; mesh checks geometry and mesh
    controls; solve also checks active physics inputs.
    """
    if scope not in {"draft", "mesh", "solve"}:
        return [f"Unknown model validation scope: {scope!r}."]
    if not isinstance(model, dict):
        return ["Model must be a JSON object."]
    if scope == "draft":
        return _validate_draft_structure(model)
    errors = []
    semantic = scope == "solve"
    if model.get("schema") != SCHEMA:
        errors.append(f"Expected schema '{SCHEMA}'.")
    if model.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Unsupported model schema version: {model.get('schema_version')}.")
    parameters = model.get("parameters", [])
    if not isinstance(parameters, list):
        errors.append("Parameters must be a list.")
        parameters = []
    parameter_map = {}
    for parameter in parameters:
        if not isinstance(parameter, dict):
            errors.append("Each parameter must be an object.")
            continue
        name = str(parameter.get("name", "")).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            errors.append(f"Invalid parameter name: {name!r}.")
        elif name in _RESERVED_NAMES:
            errors.append(f"Parameter name '{name}' is reserved.")
        elif name in parameter_map:
            errors.append(f"Parameter name '{name}' is duplicated.")
        else:
            parameter_map[name] = parameter
    for name in parameter_map:
        try:
            evaluate_expression(name, parameter_map)
        except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
            errors.append(f"Parameter '{name}': {error}")
    physics = model.get("physics", {})
    if not isinstance(physics, dict):
        physics = {}
    dc_enabled = physics.get("dc_magnetic", {}).get("enabled") is True if isinstance(physics.get("dc_magnetic", {}), dict) else False
    ac_enabled = physics.get("harmonic_electromagnetic", {}).get("enabled") is True if isinstance(physics.get("harmonic_electromagnetic", {}), dict) else False
    mechanics_enabled = physics.get("mechanics", {}).get("enabled") is True if isinstance(physics.get("mechanics", {}), dict) else False
    try:
        parameter_dims = infer_parameter_dimensions(parameters)
    except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
        errors.append(f"Parameter units: {error}.")
        parameter_dims = {}

    def check_unit(label, expression, expected_unit, *, allow_coordinates=False):
        try:
            require_expression_unit(
                expression,
                expected_unit,
                parameter_dims,
                allow_coordinates=allow_coordinates,
            )
        except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
            errors.append(f"{label}: {error}.")

    materials_list = model.get("materials", [])
    if scope != "solve":
        # Meshing uses region names and coordinates, not physical material
        # assignments. Let users generate a mesh while materials are unfinished.
        materials_list = []
    elif not isinstance(materials_list, list):
        errors.append("Materials must be a list.")
        materials_list = []
    materials_by_id = {}
    geometry_preview = model.get("geometry", {})
    preview_regions = geometry_preview.get("regions", []) if isinstance(geometry_preview, dict) else []
    active_material_fields = {}
    for region in preview_regions if isinstance(preview_regions, list) else []:
        if not isinstance(region, dict) or not isinstance(region.get("material_id"), str):
            continue
        fields = active_material_fields.setdefault(region["material_id"], set())
        if dc_enabled or ac_enabled:
            fields.add("relative_permeability")
        if ac_enabled:
            fields.add("electrical_conductivity")
        if mechanics_enabled and region.get("mechanical") is True:
            fields.update(("youngs_modulus", "poissons_ratio", "density"))
    for material in materials_list:
        if not isinstance(material, dict):
            errors.append("Each material must be an object.")
            continue
        material_id = material.get("id")
        if not isinstance(material_id, str) or not material_id or material_id in materials_by_id:
            errors.append("Every material must have a unique id.")
            continue
        materials_by_id[material_id] = material
        if not str(material.get("name", "")).strip():
            errors.append(f"Material {material_id!r} needs a name.")
        properties = material.get("properties", {})
        if not isinstance(properties, dict):
            errors.append(f"Material '{material.get('name', material_id)}' properties must be an object.")
            continue
        for key in ("relative_permeability", "electrical_conductivity", "youngs_modulus", "poissons_ratio", "density"):
            if scope != "solve" or key not in active_material_fields.get(material_id, set()) or key not in properties:
                continue
            expected_units = {
                "relative_permeability": "dimensionless",
                "electrical_conductivity": "S/m",
                "youngs_modulus": "Pa",
                "poissons_ratio": "dimensionless",
                "density": "kg/m^3",
            }
            check_unit(
                f"Material '{material.get('name', material_id)}' {key}",
                properties[key],
                expected_units[key],
            )
            try:
                value = evaluate_expression(properties[key], parameter_map)
                if not math.isfinite(float(value)):
                    raise ValueError("must be finite")
                if key == "relative_permeability" and value <= 0:
                    raise ValueError("must be positive")
                if key in {"electrical_conductivity", "youngs_modulus", "density"} and value < 0:
                    raise ValueError("must be non-negative")
                if key == "poissons_ratio" and not -1.0 < value < 0.5:
                    raise ValueError("must be between -1 and 0.5")
            except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
                errors.append(f"Material '{material.get('name', material_id)}' {key}: {error}.")

    geometry = model.get("geometry")
    if not isinstance(geometry, dict):
        errors.append("Geometry section is missing.")
        return errors
    dimension_unit = geometry.get("dimension_expression_unit", "mm")
    if dimension_unit not in {"m", "mm"}:
        errors.append("Geometry dimension expressions must use metres (m).")
        dimension_unit = "m"
    regions = geometry.get("regions", [])
    if not isinstance(regions, list):
        errors.append("Geometry regions must be a list.")
        return errors
    voids = geometry.get("voids", [])
    if not isinstance(voids, list):
        errors.append("Geometry voids must be a list.")
        voids = []
    ids = set()
    materials = set(materials_by_id)
    region_by_id = {}
    for region in regions:
        if not isinstance(region, dict):
            errors.append("Each region must be an object.")
            continue
        rid = region.get("id")
        if not isinstance(rid, str) or not rid or rid in ids:
            errors.append("Every region must have a unique id.")
        else:
            ids.add(rid)
            region_by_id[rid] = region
        if not region.get("name"):
            errors.append(f"Region {rid or '(unnamed)'} needs a name.")
        if scope == "solve" and (
            not isinstance(region.get("material_id"), str)
            or region.get("material_id") not in materials
        ):
            errors.append(f"Region '{region.get('name', rid)}' needs a valid material.")
        if scope == "solve" and mechanics_enabled and region.get("mechanical") is True:
            material = materials_by_id.get(region.get("material_id"), {})
            properties = material.get("properties", {}) if isinstance(material, dict) else {}
            try:
                youngs_modulus = evaluate_expression(properties.get("youngs_modulus", "0"), parameter_map)
                if not math.isfinite(float(youngs_modulus)) or youngs_modulus <= 0:
                    raise ValueError("must be finite and positive")
            except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
                errors.append(
                    f"Material '{material.get('name', region.get('material_id'))}' needs positive "
                    f"Young's modulus for mechanical region '{region.get('name', rid)}': {error}."
                )
        vertices = region.get("vertices", [])
        if not isinstance(vertices, list) or len(vertices) < 3:
            errors.append(f"Region '{region.get('name', rid)}' needs at least three vertices.")
            continue
        try:
            if any(not isinstance(v, (list, tuple)) or len(v) != 2 or any(isinstance(c, bool) for c in v) for v in vertices):
                raise ValueError("coordinates must be numeric pairs")
            points = [(float(v[0]), float(v[1])) for v in vertices]
        except (TypeError, ValueError, IndexError, OverflowError):
            errors.append(f"Region '{region.get('name', rid)}' has invalid r-z coordinates.")
            continue
        if any(not math.isfinite(r) or not math.isfinite(z) for r, z in points):
            errors.append(f"Region '{region.get('name', rid)}' has non-finite r-z coordinates.")
        if any(r < 0 for r, _ in points):
            errors.append(f"Region '{region.get('name', rid)}' crosses the r=0 axis.")
        if abs(_signed_area(points)) <= 1e-14:
            errors.append(f"Region '{region.get('name', rid)}' has zero area.")
        elif not _simple_polygon(points):
            errors.append(f"Region '{region.get('name', rid)}' has self-intersecting edges.")
        parent = region.get("parent_id")
        if parent is not None and not isinstance(parent, str):
            errors.append(f"Region '{region.get('name', rid)}' parent id must be text or null.")
        elif parent is not None and parent not in ids and parent != rid:
            # The parent may occur later in the file; verify after the pass.
            pass
    for region in regions:
        if not isinstance(region, dict):
            continue
        parent = region.get("parent_id")
        if parent is not None and (not isinstance(parent, str) or parent not in ids):
            errors.append(f"Region '{region.get('name', region.get('id'))}' has a missing parent.")

    # Region parent references form a strict containment tree.
    for region_id in region_by_id:
        seen = set()
        current_id = region_id
        while isinstance(current_id, str) and current_id in region_by_id:
            if current_id in seen:
                errors.append(f"Region '{region_by_id[region_id].get('name', region_id)}' has a parent cycle.")
                break
            seen.add(current_id)
            current_id = region_by_id[current_id].get("parent_id")

    valid_regions = []
    for region in regions:
        if not isinstance(region, dict):
            continue
        try:
            points = [(float(point[0]), float(point[1])) for point in region.get("vertices", [])]
        except (TypeError, ValueError, IndexError, OverflowError):
            continue
        if len(points) >= 3 and all(math.isfinite(value) for point in points for value in point):
            valid_regions.append((region, points))
    for index, (first, first_points) in enumerate(valid_regions):
        for second, second_points in valid_regions[index + 1:]:
            first_area = abs(_signed_area(first_points))
            second_area = abs(_signed_area(second_points))
            tolerance = _area_tolerance(first_points, second_points)
            nested = (
                first_area > second_area + tolerance
                and _contains_or_touches(first_points, second_points)
            ) or (
                second_area > first_area + tolerance
                and _contains_or_touches(second_points, first_points)
            )
            if not nested and _polygons_overlap_interior(first_points, second_points):
                errors.append(
                    f"Regions '{first.get('name')}' and '{second.get('name')}' overlap; "
                    "boundaries may touch, but region interiors must not overlap."
                )
    for region, points in valid_regions:
        child_area = abs(_signed_area(points))
        containers = [
            (abs(_signed_area(outer_points)), outer.get("id"))
            for outer, outer_points in valid_regions
            if outer.get("id") != region.get("id")
            and abs(_signed_area(outer_points))
                > child_area + _area_tolerance(outer_points, points)
            and _contains_or_touches(outer_points, points)
        ]
        expected_parent = min(containers)[1] if containers else None
        if region.get("parent_id") != expected_parent:
            errors.append(f"Region '{region.get('name')}' must name its nearest containing region as its parent.")
        shape = region.get("shape", {})
        if not isinstance(shape, dict):
            continue
        dimensions = shape.get("dimension_expressions", {})
        if not isinstance(dimensions, dict):
            errors.append(f"Region '{region.get('name')}' dimension expressions must be an object.")
            continue
        for key, expression in dimensions.items():
            if key not in shape:
                errors.append(f"Region '{region.get('name')}' has an expression for unknown dimension '{key}'.")
                continue
            try:
                check_unit(f"Region '{region.get('name')}' dimension {key}", expression, "m")
                dimension_value = evaluate_expression(expression, parameter_map)
                dimension_m = dimension_value / 1000 if dimension_unit == "mm" else dimension_value
                if not math.isclose(float(shape[key]), dimension_m, rel_tol=1e-9, abs_tol=1e-12):
                    raise ValueError("expression and saved dimension disagree")
            except (TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
                errors.append(f"Region '{region.get('name')}' dimension {key}: {error}.")

    valid_voids = []
    void_ids = set()
    valid_region_points = {region.get("id"): points for region, points in valid_regions}
    for void in voids:
        if not isinstance(void, dict):
            errors.append("Each geometry void must be an object.")
            continue
        void_id = void.get("id")
        if not isinstance(void_id, str) or not void_id or void_id in void_ids:
            errors.append("Every geometry void must have a unique id.")
            continue
        void_ids.add(void_id)
        parent_id = void.get("parent_id")
        parent_points = valid_region_points.get(parent_id)
        if not isinstance(parent_id, str) or parent_points is None:
            errors.append(f"Void '{void.get('name', void_id)}' must name a containing region as its parent.")
        try:
            raw_points = void.get("vertices", [])
            if not isinstance(raw_points, list) or len(raw_points) < 3:
                raise ValueError("needs at least three vertices")
            if any(not isinstance(point, (list, tuple)) or len(point) != 2 or any(isinstance(value, bool) for value in point) for point in raw_points):
                raise ValueError("coordinates must be numeric pairs")
            points = [(float(point[0]), float(point[1])) for point in raw_points]
            if not all(math.isfinite(value) for point in points for value in point):
                raise ValueError("coordinates must be finite")
            if any(radius < 0 for radius, _ in points):
                raise ValueError("must not cross the r=0 axis")
            if abs(_signed_area(points)) <= 1e-14 or not _simple_polygon(points):
                raise ValueError("must have a nonzero, non-self-intersecting outline")
        except (TypeError, ValueError, IndexError, OverflowError) as error:
            errors.append(f"Void '{void.get('name', void_id)}': {error}.")
            continue
        if parent_points is not None:
            if not _contains_or_touches(parent_points, points):
                errors.append(f"Void '{void.get('name', void_id)}' must lie inside its parent region.")
            touches_parent = any(
                _point_on_segment(
                    point,
                    parent_points[index],
                    parent_points[(index + 1) % len(parent_points)],
                    1e-10,
                )
                for point in points
                for index in range(len(parent_points))
            )
            if touches_parent:
                errors.append(f"Void '{void.get('name', void_id)}' must not touch its parent boundary.")
        void_edge_ids = void.get("edge_ids")
        if not isinstance(void_edge_ids, list) or not void_edge_ids:
            errors.append(f"Void '{void.get('name', void_id)}' must reference its boundary edges.")
            continue
        if any(not isinstance(edge_id, str) or not edge_id for edge_id in void_edge_ids):
            errors.append(f"Void '{void.get('name', void_id)}' boundary references must be edge ids.")
            continue
        if len(void_edge_ids) != len(set(void_edge_ids)):
            errors.append(f"Void '{void.get('name', void_id)}' repeats a boundary edge id.")
            continue
        if any(edge_id not in {edge.get("id") for edge in geometry.get("edges", []) if isinstance(edge, dict)} for edge_id in void_edge_ids):
            errors.append(f"Void '{void.get('name', void_id)}' references a missing boundary edge.")
            continue
        valid_voids.append((void, points))

    conditions = model.get("boundary_conditions", [])
    if not isinstance(conditions, list):
        if scope == "solve":
            errors.append("Boundary conditions must be a list.")
        conditions = []
    condition_by_id = {}
    allowed_boundary_types = {
        "axis_of_symmetry", "magnetic_potential_zero", "natural",
        "mechanical_fixed", "mechanical_prescribed", "mechanical_traction", "mechanical_robin",
        "transmission_interface",
    }
    for condition in (conditions if scope == "solve" else []):
        if not isinstance(condition, dict):
            errors.append("Each boundary condition must be an object.")
            continue
        condition_id = condition.get("id")
        if not isinstance(condition_id, str) or not condition_id or condition_id in condition_by_id:
            errors.append("Every boundary condition must have a unique id.")
            continue
        condition_by_id[condition_id] = condition
        if not str(condition.get("name", "")).strip():
            errors.append(f"Boundary condition {condition_id!r} needs a name.")
        kind = condition.get("type")
        if not isinstance(kind, str) or kind not in allowed_boundary_types:
            errors.append(f"Boundary condition '{condition.get('name', condition_id)}' has unsupported type {kind!r}.")
            continue
        expression_keys = {
            "mechanical_prescribed": ("displacement_r", "displacement_z"),
            "mechanical_traction": ("traction_r", "traction_z"),
            "mechanical_robin": ("stiffness_normal", "stiffness_tangential"),
        }.get(kind, ())
        if kind.startswith("mechanical_") and not mechanics_enabled:
            continue
        if kind in {"axis_of_symmetry", "magnetic_potential_zero", "natural"} and not (dc_enabled or ac_enabled):
            continue
        for key in expression_keys:
            required_unit = {
                "displacement_r": "m",
                "displacement_z": "m",
                "traction_r": "N/m^2",
                "traction_z": "N/m^2",
                "stiffness_normal": "N/m^3",
                "stiffness_tangential": "N/m^3",
            }[key]
            check_unit(
                f"Boundary '{condition.get('name', condition_id)}' {key}",
                condition.get(key, "0"),
                required_unit,
                allow_coordinates=True,
            )
            try:
                _validate_expression(condition.get(key, "0"), set(parameter_map), allow_coordinates=True)
            except (TypeError, ValueError, SyntaxError) as error:
                errors.append(f"Boundary '{condition.get('name', condition_id)}' {key}: {error}.")

    edge_list = geometry.get("edges", [])
    if not isinstance(edge_list, list):
        errors.append("Geometry edges must be a list.")
        edge_list = []
    edge_ids = set()
    edge_keys = {}
    edge_key_by_id = {}
    for edge in edge_list:
        if not isinstance(edge, dict):
            errors.append("Each geometry edge must be an object.")
            continue
        edge_id = edge.get("id")
        if not isinstance(edge_id, str) or not edge_id or edge_id in edge_ids:
            errors.append("Every geometry edge must have a unique id.")
        else:
            edge_ids.add(edge_id)
        edge_vertices = edge.get("vertices")
        if not isinstance(edge_vertices, list) or len(edge_vertices) != 2:
            errors.append(f"Edge '{edge.get('name', edge_id)}' must contain two endpoint coordinate pairs.")
        else:
            try:
                if any(not isinstance(point, (list, tuple)) or len(point) != 2 or any(isinstance(coordinate, bool) for coordinate in point) for point in edge_vertices):
                    raise ValueError("endpoints must be numeric coordinate pairs")
                endpoint_values = [[float(value) for value in point] for point in edge_vertices]
                if not all(math.isfinite(value) for point in endpoint_values for value in point):
                    raise ValueError("endpoints must be finite")
                curve = edge.get("curve")
                if isinstance(curve, dict) and curve.get("type") == "circle":
                    try:
                        center = tuple(round(float(value), 12) for value in curve.get("center", []))
                        radius = round(float(curve.get("radius")), 12)
                        if len(center) != 2 or radius <= 0:
                            raise ValueError("a circle needs a finite centre and positive radius")
                        if tuple(round(value, 12) for value in endpoint_values[0]) != tuple(
                            round(value, 12) for value in endpoint_values[1]
                        ):
                            raise ValueError("a full-circle edge must have coincident endpoints")
                        key = ("circle", *center, radius)
                    except (TypeError, ValueError, OverflowError) as error:
                        errors.append(f"Edge '{edge.get('name', edge_id)}': {error}.")
                        continue
                else:
                    if tuple(round(value, 12) for value in endpoint_values[0]) == tuple(
                        round(value, 12) for value in endpoint_values[1]
                    ):
                        errors.append(f"Edge '{edge.get('name', edge_id)}' must have distinct endpoints.")
                        continue
                    key = _coordinate_edge_key(endpoint_values)
                if key in edge_keys:
                    errors.append(f"Geometry edge '{edge.get('name', edge_id)}' duplicates another edge.")
                edge_keys[key] = edge_id
                if isinstance(edge_id, str):
                    edge_key_by_id[edge_id] = key
            except (TypeError, ValueError, OverflowError) as error:
                errors.append(f"Edge '{edge.get('name', edge_id)}': {error}.")
        assignments = edge.get("boundary_condition_ids") if scope == "solve" else []
        if assignments is None:
            legacy_id = edge.get("boundary_condition_id")
            assignments = [legacy_id] if legacy_id else []
        if not isinstance(assignments, list):
            errors.append(f"Edge '{edge.get('name', edge_id)}' boundary assignments must be a list.")
            continue
        if any(not isinstance(item, str) for item in assignments):
            errors.append(f"Edge '{edge.get('name', edge_id)}' assignments must be condition ids.")
            continue
        if len(assignments) != len(set(assignments)):
            errors.append(f"Edge '{edge.get('name', edge_id)}' repeats a boundary assignment.")
        assigned_types = [
            kind if isinstance((kind := condition_by_id.get(item, {}).get("type", "")), str) else ""
            for item in assignments
        ]
        electromagnetic_types = {"axis_of_symmetry", "magnetic_potential_zero", "natural"}
        mechanical_types = [kind for kind in assigned_types if str(kind).startswith("mechanical_") or kind == "transmission_interface"]
        if sum(kind in electromagnetic_types for kind in assigned_types) > 1:
            errors.append(f"Edge '{edge.get('name', edge_id)}' has multiple electromagnetic conditions.")
        if len(mechanical_types) > 1:
            errors.append(f"Edge '{edge.get('name', edge_id)}' has multiple mechanical conditions.")
        if scope == "solve" and isinstance(edge_vertices, list) and len(edge_vertices) == 2:
            try:
                edge_curve = edge.get("curve")
                sampled_path = (
                    edge_curve.get("sampled_points")
                    if isinstance(edge_curve, dict) else None
                )
                axis_points = sampled_path if isinstance(sampled_path, list) and len(sampled_path) >= 2 else edge_vertices
                edge_scale = max(
                    (
                        math.hypot(float(second[0]) - float(first[0]), float(second[1]) - float(first[1]))
                        for first, second in zip(axis_points, axis_points[1:])
                    ),
                    default=1e-12,
                )
                axis_tol = max(1e-12, edge_scale * 1e-12)
                on_axis = all(abs(float(point[0])) <= axis_tol for point in axis_points)
            except (TypeError, ValueError, IndexError, OverflowError):
                on_axis = False
            if "axis_of_symmetry" in assigned_types and not on_axis:
                errors.append(f"Edge '{edge.get('name', edge_id)}' has an axis-of-symmetry condition away from r=0.")
            if on_axis and (dc_enabled or ac_enabled):
                em_assigned = [kind for kind in assigned_types if kind in electromagnetic_types]
                if em_assigned != ["axis_of_symmetry"]:
                    errors.append(
                        f"Edge '{edge.get('name', edge_id)}' on r=0 must have the axis-of-symmetry "
                        "electromagnetic condition; one mechanical condition may also be assigned."
                    )
        for condition_id in assignments:
            if condition_id not in condition_by_id:
                errors.append(f"Edge '{edge.get('name', edge_id)}' references a missing boundary condition.")

    expected_edge_keys = set()
    outline_tolerance = 1e-9
    outline_records = [
        (region, points, f"Region '{region.get('name')}'")
        for region, points in valid_regions
    ] + [
        (void, points, f"Void '{void.get('name', void.get('id'))}'")
        for void, points in valid_voids
    ]
    for outline, points, label in outline_records:
        expected_outline_ids = set()
        for index, start_point in enumerate(points):
            end_point = points[(index + 1) % len(points)]
            dx = end_point[0] - start_point[0]
            dy = end_point[1] - start_point[1]
            length_squared = dx * dx + dy * dy
            if length_squared <= 1e-30:
                continue
            candidates = []
            for edge in edge_list:
                edge_id = edge.get("id")
                edge_points = edge.get("vertices")
                if not isinstance(edge_points, list) or len(edge_points) != 2:
                    continue
                curve = edge.get("curve")
                sampled = curve.get("sampled_points") if isinstance(curve, dict) else None
                path_points = sampled if isinstance(sampled, list) and len(sampled) >= 2 else edge_points
                for path_start, path_end in zip(path_points, path_points[1:]):
                    try:
                        first_point = (float(path_start[0]), float(path_start[1]))
                        second_point = (float(path_end[0]), float(path_end[1]))
                    except (TypeError, ValueError, IndexError, OverflowError):
                        continue
                    if not (
                        _point_on_segment(first_point, start_point, end_point, outline_tolerance)
                        and _point_on_segment(second_point, start_point, end_point, outline_tolerance)
                    ):
                        continue
                    first_t = (
                        (first_point[0] - start_point[0]) * dx
                        + (first_point[1] - start_point[1]) * dy
                    ) / length_squared
                    second_t = (
                        (second_point[0] - start_point[0]) * dx
                        + (second_point[1] - start_point[1]) * dy
                    ) / length_squared
                    lower, upper = sorted((first_t, second_t))
                    if upper - lower > outline_tolerance:
                        candidates.append((lower, upper, edge_id, edge_key_by_id.get(edge_id)))
            candidates.sort(key=lambda item: (item[0], item[1]))
            cursor = 0.0
            for lower, upper, edge_id, key in candidates:
                if lower > cursor + outline_tolerance:
                    break
                if upper > cursor + outline_tolerance:
                    cursor = upper
                    if isinstance(edge_id, str):
                        expected_outline_ids.add(edge_id)
                    if key is not None:
                        expected_edge_keys.add(key)
            if cursor < 1.0 - outline_tolerance:
                errors.append(f"{label} has a sketch edge without complete boundary coverage.")
        outline_edge_ids = outline.get("edge_ids")
        if not isinstance(outline_edge_ids, list):
            errors.append(f"{label} must reference its sketch edges.")
        elif len(outline_edge_ids) != len(set(outline_edge_ids)):
            errors.append(f"{label} repeats a sketch edge id.")
        elif any(edge_id not in edge_ids for edge_id in outline_edge_ids):
            errors.append(f"{label} references a missing sketch edge.")
        elif set(outline_edge_ids) != expected_outline_ids:
            errors.append(f"{label} edge ids do not match its sketch geometry.")

    # Open sketch curves remain editable and need not bound a material region.
    sketch_entity_ids = {
        entity.get("id") for entity in geometry.get("sketch_entities", [])
        if isinstance(entity, dict) and isinstance(entity.get("id"), str)
    }
    for edge in edge_list:
        if edge.get("sketch_entity_id") in sketch_entity_ids:
            key = edge_key_by_id.get(edge.get("id"))
            if key is not None:
                expected_edge_keys.add(key)

    if set(edge_keys) != expected_edge_keys:
        errors.append("Geometry edge list does not match the region outlines.")

    regional_loads = model.get("regional_loads", [])
    if not isinstance(regional_loads, list):
        if scope == "solve":
            errors.append("Regional loads must be a list.")
        regional_loads = []
    if scope != "solve":
        regional_loads = []
    regional_load_ids = set()
    regional_load_names = set()
    allowed_load_fields = {
        "dc_current_density": "A/m^2",
        "ac_current_density_real": "A/m^2",
        "ac_current_density_imaginary": "A/m^2",
    }
    for load in regional_loads:
        if not isinstance(load, dict):
            errors.append("Each regional load must be an object.")
            continue
        load_id = load.get("id")
        if not isinstance(load_id, str) or not load_id or load_id in regional_load_ids:
            errors.append("Every regional load must have a unique id.")
        else:
            regional_load_ids.add(load_id)
        load_name = str(load.get("name", "")).strip()
        if not load_name:
            errors.append(f"Regional load {load_id!r} needs a name.")
        elif load_name.casefold() in regional_load_names:
            errors.append(f"Regional load name '{load_name}' is duplicated.")
        else:
            regional_load_names.add(load_name.casefold())
        region_ids = load.get("region_ids", [])
        if not isinstance(region_ids, list):
            errors.append(f"Regional load '{load_name or load_id}' region_ids must be a list.")
            region_ids = []
        elif any(not isinstance(region_id, str) or not region_id for region_id in region_ids):
            errors.append(f"Regional load '{load_name or load_id}' has an invalid region assignment.")
        elif len(region_ids) != len(dict.fromkeys(region_ids)):
            errors.append(f"Regional load '{load_name or load_id}' repeats a region assignment.")
        for region_id in region_ids:
            if region_id not in region_by_id:
                errors.append(f"Regional load '{load_name or load_id}' references a missing region.")
        for key, unit in allowed_load_fields.items():
            if key == "dc_current_density" and not dc_enabled:
                continue
            if key.startswith("ac_current_density") and not ac_enabled:
                continue
            expression = load.get(key, "0")
            check_unit(f"Regional load '{load_name or load_id}' {key}", expression, unit, allow_coordinates=True)
            try:
                _validate_expression(expression, set(parameter_map), allow_coordinates=True)
            except (TypeError, ValueError, SyntaxError) as error:
                errors.append(f"Regional load '{load_name or load_id}' {key}: {error}.")
        body = load.get("mechanical_body_force", {})
        if mechanics_enabled and not isinstance(body, dict):
            errors.append(f"Regional load '{load_name or load_id}' body force must be an object.")
        elif mechanics_enabled:
            for component in ("r", "z"):
                expression = body.get(component, "0")
                check_unit(f"Regional load '{load_name or load_id}' body force {component}", expression, "N/m^3", allow_coordinates=True)
                try:
                    _validate_expression(expression, set(parameter_map), allow_coordinates=True)
                except (TypeError, ValueError, SyntaxError) as error:
                    errors.append(f"Regional load '{load_name or load_id}' body force {component}: {error}.")

    for region in (region_by_id.values() if scope == "solve" else []):
        source = region.get("sources", {})
        if not isinstance(source, dict):
            errors.append(f"Region '{region.get('name', region.get('id'))}' sources must be an object.")
            continue
        for key in ("dc_current_density", "ac_current_density_real", "ac_current_density_imaginary"):
            if key == "dc_current_density" and not dc_enabled:
                continue
            if key.startswith("ac_current_density") and not ac_enabled:
                continue
            check_unit(
                f"Region '{region.get('name', region.get('id'))}' {key}",
                source.get(key, "0"),
                "A/m^2",
                allow_coordinates=True,
            )
            try:
                _validate_expression(source.get(key, "0"), set(parameter_map), allow_coordinates=True)
            except (TypeError, ValueError, SyntaxError) as error:
                errors.append(f"Region '{region.get('name', region.get('id'))}' {key}: {error}.")
        body = source.get("mechanical_body_force", {})
        if mechanics_enabled and not isinstance(body, dict):
            errors.append(f"Region '{region.get('name', region.get('id'))}' body force must be an object.")
        elif mechanics_enabled:
            for key in ("r", "z"):
                check_unit(
                    f"Region '{region.get('name', region.get('id'))}' body force {key}",
                    body.get(key, "0"),
                    "N/m^3",
                    allow_coordinates=True,
                )
                try:
                    _validate_expression(body.get(key, "0"), set(parameter_map), allow_coordinates=True)
                except (TypeError, ValueError, SyntaxError) as error:
                    errors.append(f"Region '{region.get('name', region.get('id'))}' body force {key}: {error}.")

    if scope == "solve":
        physics = model.get("physics", {})
        if not isinstance(physics, dict):
            errors.append("Physics settings must be an object.")
            physics = {}
        expected_physics = ("dc_magnetic", "harmonic_electromagnetic", "mechanics", "coupling")
        for key in expected_physics:
            section = physics.get(key, {})
            if not isinstance(section, dict) or not isinstance(section.get("enabled", False), bool):
                errors.append(f"Physics setting '{key}' must contain an enabled boolean.")

    mesh = model.get("mesh", {})
    if not isinstance(mesh, dict):
        errors.append("Mesh settings must be an object.")
        mesh = {}
    try:
        check_unit("Mesh element size", mesh.get("element_size", "0.01"), "m")
        raw_order = mesh.get("polynomial_order", 3)
        order_value = float(raw_order)
        if isinstance(raw_order, bool) or not math.isfinite(order_value) or not order_value.is_integer():
            raise ValueError("polynomial order must be an integer between 1 and 6")
        if int(order_value) not in range(1, 7):
            raise ValueError("polynomial order must be between 1 and 6")
        element_size = float(evaluate_expression(mesh.get("element_size", "0.01"), parameter_map))
        if not math.isfinite(element_size) or element_size <= 0:
            raise ValueError("target element size must be finite and positive")
    except (TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
        errors.append(f"Mesh settings: {error}.")

    region_sizes = mesh.get("region_element_sizes", {})
    if not isinstance(region_sizes, dict):
        errors.append("Regional mesh sizes must be an object keyed by region id.")
        region_sizes = {}
    for region_id, expression in region_sizes.items():
        if region_id not in ids:
            errors.append(f"Regional mesh size refers to missing region {region_id!r}.")
            continue
        label = region_by_id.get(region_id, {}).get("name", region_id)
        try:
            check_unit(f"Region '{label}' mesh size", expression, "m")
            size_value = float(evaluate_expression(expression, parameter_map))
            if not math.isfinite(size_value) or size_value <= 0:
                raise ValueError("target element size must be finite and positive")
        except (TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
            errors.append(f"Region '{label}' mesh size: {error}.")

    try:
        hp_layers = mesh.get("hp_layers", 0)
        if isinstance(hp_layers, bool) or int(hp_layers) != hp_layers or int(hp_layers) not in range(0, 9):
            raise ValueError("number of hp layers must be an integer between 0 and 8")
        hp_factor = float(mesh.get("hp_grading_factor", 0.3))
        if not math.isfinite(hp_factor) or not 0 < hp_factor < 1:
            raise ValueError("hp grading factor must be between 0 and 1")
    except (TypeError, ValueError, OverflowError) as error:
        errors.append(f"HP layer settings: {error}.")

    hp_region_ids = mesh.get("hp_region_ids", [])
    hp_edge_ids = mesh.get("hp_edge_ids", [])
    if not isinstance(hp_region_ids, list):
        errors.append("HP-refined regions must be a list of region ids.")
        hp_region_ids = []
    if not isinstance(hp_edge_ids, list):
        errors.append("HP-refined boundaries must be a list of edge ids.")
        hp_edge_ids = []
    if any(not isinstance(item, str) for item in hp_region_ids):
        errors.append("HP-refined regions must contain region ids as text.")
    elif len(hp_region_ids) != len(set(hp_region_ids)):
        errors.append("HP-refined regions must contain unique region ids.")
    if any(not isinstance(item, str) for item in hp_edge_ids):
        errors.append("HP-refined boundaries must contain edge ids as text.")
    elif len(hp_edge_ids) != len(set(hp_edge_ids)):
        errors.append("HP-refined boundaries must contain unique edge ids.")
    for region_id in hp_region_ids:
        if not isinstance(region_id, str) or region_id not in ids:
            errors.append(f"HP refinement refers to missing region {region_id!r}.")
    for edge_id in hp_edge_ids:
        if not isinstance(edge_id, str) or edge_id not in edge_ids:
            errors.append(f"HP refinement refers to missing boundary {edge_id!r}.")
    try:
        if int(mesh.get("hp_layers", 0)) > 0 and not (hp_region_ids or hp_edge_ids):
            errors.append("Select at least one region or boundary for hp layers, or set the layer count to zero.")
    except (TypeError, ValueError, OverflowError):
        pass

    solver = model.get("solver", {})
    if not isinstance(solver, dict):
        if scope == "solve":
            errors.append("Solver settings must be an object.")
        solver = {}
    coupling_enabled = bool(
        isinstance(physics.get("coupling"), dict)
        and physics["coupling"].get("enabled") is True
    ) if isinstance(physics, dict) else False
    try:
        if scope == "solve" and coupling_enabled:
            raw_depth = solver.get("anderson_depth", 3)
            depth_value = float(raw_depth)
            if isinstance(raw_depth, bool) or not math.isfinite(depth_value) or not depth_value.is_integer() or depth_value < 0:
                raise ValueError("Anderson depth must be a finite non-negative integer")
            beta = float(solver.get("anderson_beta", 1.0))
            tolerance = float(solver.get("relative_tolerance", 1e-6))
            raw_iterations = solver.get("maximum_iterations", 50)
            iteration_value = float(raw_iterations)
            if not math.isfinite(beta) or beta <= 0:
                raise ValueError("Anderson relaxation must be finite and positive")
            if not math.isfinite(tolerance) or tolerance <= 0:
                raise ValueError("relative tolerance must be finite and positive")
            if isinstance(raw_iterations, bool) or not math.isfinite(iteration_value) or not iteration_value.is_integer() or iteration_value < 1:
                raise ValueError("maximum iterations must be a finite positive integer")
        if scope == "solve" and solver.get("linear_solver", "direct") != "direct":
            raise ValueError("Only the direct linear solver is currently supported")
    except (TypeError, ValueError, OverflowError) as error:
        errors.append(f"Solver settings: {error}.")

    studies = model.get("studies", [])
    harmonic_enabled = bool(
        physics.get("harmonic_electromagnetic", {}).get("enabled", False)
        or physics.get("mechanics", {}).get("enabled", False)
    ) if isinstance(physics, dict) else False
    if scope == "solve" and studies is not None and not isinstance(studies, list):
        errors.append("Studies must be a list.")
    elif scope == "solve" and isinstance(studies, list) and harmonic_enabled:
        for study in studies:
            if not isinstance(study, dict):
                errors.append("Each study must be an object.")
                continue
            points = study.get("frequency_hz", [])
            if isinstance(points, str):
                points = [part.strip() for part in points.split(",") if part.strip()]
            if not isinstance(points, list):
                errors.append(f"Study '{study.get('name', '')}' frequencies must be a list or comma-separated text.")
                continue
            for point in points:
                try:
                    check_unit(f"Study '{study.get('name', '')}' frequency", point, "Hz")
                    frequency = float(evaluate_expression(point, parameter_map))
                    if not math.isfinite(frequency) or frequency <= 0:
                        raise ValueError("frequency must be finite and positive")
                except (TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
                    errors.append(f"Study '{study.get('name', '')}' frequency: {error}.")
    return errors


def _validate_expression(expression, parameters, *, allow_coordinates=False):
    text = _normalise_expression(expression)
    if not text or len(text) > 256:
        raise ValueError("expressions must contain 1 to 256 characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise ValueError(f"invalid expression: {error.msg}") from error
    nodes = list(ast.walk(tree))
    if len(nodes) > 128:
        raise ValueError("expression is too complex")
    coordinate_names = {"r", "z"} if allow_coordinates else set()
    allowed_names = set(parameters) | coordinate_names | {"pi", "e"}
    for node in nodes:
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
                raise ValueError("only numeric constants are allowed")
        elif isinstance(node, ast.Name):
            if isinstance(getattr(node, "ctx", None), ast.Load) and node.id in _FUNCTIONS:
                continue
            if node.id not in allowed_names:
                raise ValueError(f"unknown name {node.id!r}")
        elif isinstance(node, ast.BinOp):
            if type(node.op) not in _BINARY_OPS:
                raise ValueError("unsupported arithmetic operator")
        elif isinstance(node, ast.UnaryOp):
            if type(node.op) not in _UNARY_OPS:
                raise ValueError("unsupported unary operator")
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS or node.keywords:
                raise ValueError("unsupported function call")
        elif isinstance(node, (ast.Expression, ast.Load, ast.operator, ast.unaryop)):
            continue
        else:
            raise ValueError("unsupported expression syntax")


def _signed_area(points):
    return 0.5 * sum(
        points[i][0] * points[(i + 1) % len(points)][1]
        - points[(i + 1) % len(points)][0] * points[i][1]
        for i in range(len(points))
    )


def _coordinate_edge_key(points):
    coordinates = [tuple(round(float(value), 12) for value in point) for point in points]
    return tuple(sorted(coordinates))


def _point_in_polygon(point, polygon):
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            crossing_x = (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi
            if x < crossing_x:
                inside = not inside
        j = i
    return inside


def _point_on_segment(point, start, end, tolerance=1e-12):
    px, py = point
    ax, ay = start
    bx, by = end
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    scale = max(abs(bx - ax), abs(by - ay), 1.0)
    return (
        abs(cross) <= tolerance * scale
        and min(ax, bx) - tolerance <= px <= max(ax, bx) + tolerance
        and min(ay, by) - tolerance <= py <= max(ay, by) + tolerance
    )


def _segments_intersect(a, b, c, d, tolerance=1e-12):
    def orientation(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2, o3, o4 = orientation(a, b, c), orientation(a, b, d), orientation(c, d, a), orientation(c, d, b)
    if ((o1 > tolerance and o2 < -tolerance) or (o1 < -tolerance and o2 > tolerance)) and ((o3 > tolerance and o4 < -tolerance) or (o3 < -tolerance and o4 > tolerance)):
        return True
    return (
        (abs(o1) <= tolerance and _point_on_segment(c, a, b, tolerance))
        or (abs(o2) <= tolerance and _point_on_segment(d, a, b, tolerance))
        or (abs(o3) <= tolerance and _point_on_segment(a, c, d, tolerance))
        or (abs(o4) <= tolerance and _point_on_segment(b, c, d, tolerance))
    )


def _strictly_contains(outer, inner):
    if len(outer) < 3 or len(inner) < 3 or not all(_point_in_polygon(point, outer) for point in inner):
        return False
    return not any(
        _segments_intersect(outer[i], outer[(i + 1) % len(outer)], inner[j], inner[(j + 1) % len(inner)])
        for i in range(len(outer))
        for j in range(len(inner))
    )



def _area_tolerance(first, second):
    first_width = max(p[0] for p in first) - min(p[0] for p in first)
    first_height = max(p[1] for p in first) - min(p[1] for p in first)
    second_width = max(p[0] for p in second) - min(p[0] for p in second)
    second_height = max(p[1] for p in second) - min(p[1] for p in second)
    scale = max(first_width, first_height, second_width, second_height, 1e-12)
    return max(abs(_signed_area(first)), abs(_signed_area(second)), 1e-30) * 1e-12


def _contains_or_touches(outer, inner):
    if len(outer) < 3 or len(inner) < 3:
        return False
    return all(
        _point_in_polygon(point, outer)
        or any(
            _point_on_segment(
                point,
                outer[index],
                outer[(index + 1) % len(outer)],
            )
            for index in range(len(outer))
        )
        for point in inner
    )


def _polygons_overlap_interior(first, second):
    if len(first) < 3 or len(second) < 3:
        return False
    first_bounds = (
        min(p[0] for p in first), max(p[0] for p in first),
        min(p[1] for p in first), max(p[1] for p in first),
    )
    second_bounds = (
        min(p[0] for p in second), max(p[0] for p in second),
        min(p[1] for p in second), max(p[1] for p in second),
    )
    if (
        first_bounds[1] <= second_bounds[0]
        or second_bounds[1] <= first_bounds[0]
        or first_bounds[3] <= second_bounds[2]
        or second_bounds[3] <= first_bounds[2]
    ):
        return False
    def strictly_inside(point, polygon):
        if any(
            _point_on_segment(point, polygon[index], polygon[(index + 1) % len(polygon)])
            for index in range(len(polygon))
        ):
            return False
        return _point_in_polygon(point, polygon)

    if any(strictly_inside(point, second) for point in first):
        return True
    if any(strictly_inside(point, first) for point in second):
        return True

    scale = max(
        first_bounds[1] - first_bounds[0],
        first_bounds[3] - first_bounds[2],
        second_bounds[1] - second_bounds[0],
        second_bounds[3] - second_bounds[2],
        1e-12,
    )
    tolerance = scale * scale * 1e-12

    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    for i, p in enumerate(first):
        q = first[(i + 1) % len(first)]
        for j, r in enumerate(second):
            t = second[(j + 1) % len(second)]
            o1, o2 = orient(p, q, r), orient(p, q, t)
            o3, o4 = orient(r, t, p), orient(r, t, q)
            if (
                ((o1 > tolerance and o2 < -tolerance) or (o1 < -tolerance and o2 > tolerance))
                and ((o3 > tolerance and o4 < -tolerance) or (o3 < -tolerance and o4 > tolerance))
            ):
                return True

    first_center = (
        sum(p[0] for p in first) / len(first),
        sum(p[1] for p in first) / len(first),
    )
    second_center = (
        sum(p[0] for p in second) / len(second),
        sum(p[1] for p in second) / len(second),
    )
    return _point_in_polygon(first_center, second) or _point_in_polygon(second_center, first)

def _polygons_overlap(first, second):
    first_bounds = (min(p[0] for p in first), max(p[0] for p in first), min(p[1] for p in first), max(p[1] for p in first))
    second_bounds = (min(p[0] for p in second), max(p[0] for p in second), min(p[1] for p in second), max(p[1] for p in second))
    if first_bounds[1] < second_bounds[0] or second_bounds[1] < first_bounds[0] or first_bounds[3] < second_bounds[2] or second_bounds[3] < first_bounds[2]:
        return False
    if any(_point_in_polygon(point, second) or any(_point_on_segment(point, second[i], second[(i + 1) % len(second)]) for i in range(len(second))) for point in first):
        return True
    if any(_point_in_polygon(point, first) or any(_point_on_segment(point, first[i], first[(i + 1) % len(first)]) for i in range(len(first))) for point in second):
        return True
    return any(
        _segments_intersect(first[i], first[(i + 1) % len(first)], second[j], second[(j + 1) % len(second)])
        for i in range(len(first))
        for j in range(len(second))
    )


def _simple_polygon(points):
    count = len(points)
    for i in range(count):
        a, b = points[i], points[(i + 1) % count]
        for j in range(i + 1, count):
            if j == i or j == (i + 1) % count or i == (j + 1) % count:
                continue
            c, d = points[j], points[(j + 1) % count]
            if _segments_intersect(a, b, c, d):
                return False
    return True


def validate_studies(studies: dict, parameters=None, *, physics=None, for_solve=False) -> list[str]:
    """Validate saved study data and current solver capability when requested."""
    errors = []
    if not isinstance(studies, dict) or studies.get("schema") != SCHEMA:
        return [f"Studies must use the '{SCHEMA}' schema."]
    if studies.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Unsupported studies schema version: {studies.get('schema_version')}.")
    entries = studies.get("studies")
    if not isinstance(entries, list):
        return ["Studies must contain a list of study definitions."]
    if for_solve and len(entries) != 1:
        errors.append("This solver currently runs one study at a time; use its frequency list for a sweep.")
    if physics is None:
        harmonic_enabled = True
    elif isinstance(physics, dict):
        def enabled(name):
            section = physics.get(name)
            return isinstance(section, dict) and section.get("enabled") is True

        harmonic_enabled = enabled("harmonic_electromagnetic") or enabled("mechanics")
    else:
        harmonic_enabled = False
    parameters = parameters or {}
    parameter_entries = list(parameters.values()) if isinstance(parameters, dict) else parameters
    try:
        parameter_dims = infer_parameter_dimensions(parameter_entries)
    except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        parameter_dims = {}
    for study in entries:
        if not isinstance(study, dict):
            errors.append("The study definition must be an object.")
            continue
        if not str(study.get("id", "")).strip() or not str(study.get("name", "")).strip():
            errors.append("The study needs an id and a name.")
        if not harmonic_enabled:
            continue
        points = study.get("frequency_hz", [])
        if isinstance(points, str):
            points = [part.strip() for part in points.split(",") if part.strip()]
        if not isinstance(points, list) or not points:
            if for_solve:
                errors.append(f"Study '{study.get('name', '')}': enter at least one positive frequency.")
            continue
        for point in points:
            try:
                require_expression_unit(point, "Hz", parameter_dims)
                frequency = float(evaluate_expression(point, parameters))
                if not math.isfinite(frequency) or frequency <= 0:
                    raise ValueError("frequency must be finite and positive")
            except (TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
                errors.append(f"Study frequency {point!r}: {error}.")
    return errors


def package_model(model: dict, studies: dict | None = None, layout: dict | None = None) -> bytes:
    model = migrate_legacy_model(model)
    errors = validate_model(model, scope="draft")
    if errors:
        raise ValueError("Cannot save model document: " + " ".join(errors))
    studies = migrate_legacy_studies(studies or new_studies())
    parameter_map = {
        item.get("name"): item
        for item in model.get("parameters", [])
        if isinstance(item, dict) and item.get("name")
    }
    study_errors = validate_studies(studies, parameter_map, physics=model.get("physics"), for_solve=False)
    if study_errors:
        raise ValueError("Cannot save studies: " + " ".join(study_errors))
    layout = layout or {"schema_version": 1, "active_section": "geometry", "camera": "fit"}
    if not isinstance(layout, dict):
        raise ValueError("Layout data must be an object.")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MODEL_FILENAME, json.dumps(model, indent=2, sort_keys=True) + "\n")
        archive.writestr(STUDIES_FILENAME, json.dumps(studies, indent=2, sort_keys=True) + "\n")
        archive.writestr(LAYOUT_FILENAME, json.dumps(layout, indent=2, sort_keys=True) + "\n")
    return out.getvalue()


def serialize_model_json(model: dict, studies: dict | None = None, layout: dict | None = None) -> bytes:
    """Serialize a standalone JSON problem file containing model and study data."""
    model = migrate_legacy_model(model)
    errors = validate_model(model, scope="draft")
    if errors:
        raise ValueError("Cannot save model document: " + " ".join(errors))
    studies = migrate_legacy_studies(studies or new_studies())
    parameter_map = {
        item.get("name"): item
        for item in model.get("parameters", [])
        if isinstance(item, dict) and item.get("name")
    }
    study_errors = validate_studies(studies, parameter_map, physics=model.get("physics"), for_solve=False)
    if study_errors:
        raise ValueError("Cannot save studies: " + " ".join(study_errors))
    layout = layout or {"schema_version": 1, "active_section": "geometry", "camera": "fit"}
    if not isinstance(layout, dict):
        raise ValueError("Layout data must be an object.")
    project = {
        "format": PROJECT_JSON_FORMAT,
        "format_version": PROJECT_JSON_VERSION,
        "model": model,
        "studies": studies,
        "layout": layout,
    }
    try:
        payload = json.dumps(project, indent=2, sort_keys=True, allow_nan=False) + "\n"
    except (TypeError, ValueError) as error:
        raise ValueError(f"Project data cannot be represented as JSON: {error}") from error
    encoded = payload.encode("utf-8")
    if len(encoded) > 20_000_000:
        raise ValueError("Problem JSON exceeds the 20 MB size limit.")
    return encoded


def _validate_unpacked_model(model, studies, layout):
    model = migrate_legacy_model(model)
    studies = migrate_legacy_studies(studies)
    errors = validate_model(model, scope="draft")
    if errors:
        raise ValueError("Invalid model document: " + " ".join(errors))
    parameter_map = {
        item.get("name"): item
        for item in model.get("parameters", [])
        if isinstance(item, dict) and item.get("name")
    }
    study_errors = validate_studies(studies, parameter_map, physics=model.get("physics"), for_solve=False)
    if study_errors:
        raise ValueError("Invalid studies: " + " ".join(study_errors))
    if not isinstance(layout, dict):
        raise ValueError("Invalid layout data.")
    return model, studies, layout


def unpack_model(data: bytes) -> tuple[dict, dict, dict]:
    """Read a standalone JSON problem file or a legacy .ngsmodel ZIP archive."""
    if not isinstance(data, (bytes, bytearray)):
        data = bytes(data)
    if len(data) > 20_000_000:
        raise ValueError("Problem file exceeds the 20 MB size limit.")
    data = bytes(data)
    if not zipfile.is_zipfile(io.BytesIO(data)):
        def reject_constant(value):
            raise ValueError(f"Invalid JSON number: {value}")
        try:
            project = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"Problem file is neither a supported JSON project nor a valid .ngsmodel archive: {error}") from error
        if not isinstance(project, dict):
            raise ValueError("Problem JSON must contain an object.")
        if project.get("format") == PROJECT_JSON_FORMAT:
            if project.get("format_version") != PROJECT_JSON_VERSION:
                raise ValueError(f"Unsupported JSON project format version: {project.get('format_version')}.")
            model = project.get("model")
            studies = project.get("studies", new_studies())
            layout = project.get("layout", {})
        elif project.get("schema") == SCHEMA:
            # Accept a bare data-only model JSON as well as the full project envelope.
            project = copy.deepcopy(project)
            metadata = project.pop("project", {})
            model = project
            studies = metadata.get("studies", new_studies()) if isinstance(metadata, dict) else new_studies()
            layout = metadata.get("layout", {}) if isinstance(metadata, dict) else {}
        else:
            raise ValueError("JSON file is not an MM-FEM axisymmetric problem project.")
        return _validate_unpacked_model(model, studies, layout)

    with zipfile.ZipFile(io.BytesIO(data), "r") as archive:
        names = set(archive.namelist())
        if len(names) != len(archive.infolist()):
            raise ValueError("Model archive contains duplicate file entries.")
        if MODEL_FILENAME not in names:
            raise ValueError(f"Archive does not contain {MODEL_FILENAME}.")
        allowed = {MODEL_FILENAME, STUDIES_FILENAME, LAYOUT_FILENAME}
        if names - allowed:
            raise ValueError("Archive contains unsupported files: " + ", ".join(sorted(names - allowed)))
        for name in names:
            if name.startswith(("/", "\\")) or ".." in name.split("/"):
                raise ValueError(f"Model archive contains an unsafe path: {name}")
            if archive.getinfo(name).file_size > 2_000_000:
                raise ValueError(f"Model entry is too large: {name}")
        model = json.loads(archive.read(MODEL_FILENAME))
        studies = json.loads(archive.read(STUDIES_FILENAME)) if STUDIES_FILENAME in names else new_studies()
        layout = json.loads(archive.read(LAYOUT_FILENAME)) if LAYOUT_FILENAME in names else {}
    return _validate_unpacked_model(model, studies, layout)
