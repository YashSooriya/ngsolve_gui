"""Import data-only legacy axisymmetric problems into MM-FEM documents.

The selected Python file is executed to capture its SplineGeometry definitions.
Lines and rational quadratic ``spline3`` curves, domain-side labels, constant
material/source data, and mesh controls are preserved. Unsupported symbolic
physics data is rejected rather than silently approximated.
"""

from __future__ import annotations

import inspect
import math
import numbers
import runpy
import sys
import uuid
from pathlib import Path

from .axisymmetric_model import new_model, new_studies, validate_model, validate_studies


class LegacySplineGeometryCapture:
    """Capture the simple SplineGeometry calls used by legacy problem files."""

    def __init__(self):
        self.points = {}
        self.segments = []
        self.materials = {}
        self.domain_maxh = {}
        self._next_point = 0

    def AppendPoint(self, x, y, **kwargs):
        if set(kwargs) - {"hpref"}:
            raise ValueError("Only the legacy point hpref option is supported.")
        point_id = self._next_point
        self._next_point += 1
        self.points[point_id] = {
            "coordinates": (float(x), float(y)),
            "hpref": bool(kwargs.get("hpref", False)),
        }
        return point_id

    def Append(self, curve, **kwargs):
        if not isinstance(curve, (list, tuple)) or not curve:
            raise ValueError("A legacy boundary must define a line or spline3 curve.")
        kind = str(curve[0]).lower()
        if kind == "line" and len(curve) == 3:
            point_ids = [int(curve[1]), int(curve[2])]
        elif kind == "spline3" and len(curve) == 4:
            point_ids = [int(curve[1]), int(curve[2]), int(curve[3])]
        else:
            raise ValueError(f"Legacy curve type {kind!r} is not supported; use line or spline3.")
        if any(point_id not in self.points for point_id in point_ids):
            raise ValueError("A legacy boundary refers to a point that was not defined.")
        allowed = {"bc", "leftdomain", "rightdomain", "maxh", "hpref"}
        if set(kwargs) - allowed:
            raise ValueError("The legacy boundary uses unsupported options: " + ", ".join(sorted(set(kwargs) - allowed)))
        maxh = kwargs.get("maxh")
        if maxh is not None:
            maxh = float(maxh)
            if not math.isfinite(maxh) or maxh <= 0:
                raise ValueError("Legacy boundary maxh must be finite and positive.")
        self.segments.append({
            "type": kind,
            "start": point_ids[0],
            "end": point_ids[-1],
            "control": point_ids[1] if kind == "spline3" else None,
            "bc": kwargs.get("bc"),
            "leftdomain": int(kwargs.get("leftdomain", 1)),
            "rightdomain": int(kwargs.get("rightdomain", 0)),
            "maxh": maxh,
            "hpref": bool(kwargs.get("hpref", False)),
        })

    def SetMaterial(self, domain, name):
        self.materials[int(domain)] = str(name)

    def SetDomainMaxH(self, domain, maxh):
        value = float(maxh)
        if not math.isfinite(value) or value <= 0:
            raise ValueError("Legacy domain maxh must be finite and positive.")
        self.domain_maxh[int(domain)] = value


def _number(value, label):
    if isinstance(value, bool) or not isinstance(value, numbers.Number):
        raise ValueError(f"{label} must be a constant numeric value; symbolic expressions are not supported yet.")
    if isinstance(value, numbers.Complex) and not isinstance(value, numbers.Real):
        if abs(value.imag) > 1e-14:
            raise ValueError(f"{label} must be real-valued.")
        value = value.real
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite.")
    return result


def _expression(value):
    return format(float(value), ".15g")


def _coordinate_key(point):
    return tuple(round(float(value), 12) for value in point)


def _edge_key(start, end):
    return tuple(sorted((_coordinate_key(start), _coordinate_key(end))))


def _signed_area(vertices):
    return 0.5 * sum(
        vertices[index][0] * vertices[(index + 1) % len(vertices)][1]
        - vertices[(index + 1) % len(vertices)][0] * vertices[index][1]
        for index in range(len(vertices))
    )


def _point_in_polygon(point, polygon):
    x, y = point
    inside = False
    previous = len(polygon) - 1
    for index, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[previous]
        if (yi > y) != (yj > y):
            crossing = (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi
            if x < crossing:
                inside = not inside
        previous = index
    return inside


def _segment_samples(geometry, segment, subdivisions=32):
    """Sample a legacy boundary for viewport polygons; solver geometry stays exact."""
    points = [geometry.points[segment["start"]]["coordinates"]]
    if segment["type"] == "line":
        points.append(geometry.points[segment["end"]]["coordinates"])
        return [list(point) for point in points]

    control = geometry.points[segment["control"]]["coordinates"]
    end = geometry.points[segment["end"]]["coordinates"]
    start = points[0]
    chord = math.dist(start, end)
    control_weight = math.sqrt(0.5 * (
        math.dist(start, control) ** 2 + math.dist(control, end) ** 2
    ))
    if chord <= 1e-14 or control_weight <= 1e-14:
        raise ValueError("A legacy spline3 boundary has degenerate control points.")
    weight = chord / control_weight
    sampled = []
    for step in range(subdivisions + 1):
        t = step / subdivisions
        b0 = (1.0 - t) ** 2
        b1 = weight * t * (1.0 - t)
        b2 = t * t
        denominator = b0 + b1 + b2
        sampled.append([
            (b0 * start[axis] + b1 * control[axis] + b2 * end[axis]) / denominator
            for axis in range(2)
        ])
    return sampled


def _domain_boundary_loops(geometry, domain):
    """Return consistently oriented loops with the requested domain on the left."""
    directed = []
    for segment_index, segment in enumerate(geometry.segments):
        if segment["leftdomain"] == domain:
            directed.append((segment_index, segment["start"], segment["end"], True))
        elif segment["rightdomain"] == domain:
            directed.append((segment_index, segment["end"], segment["start"], False))
    if len(directed) < 3:
        raise ValueError(f"Domain {domain} has no supported closed boundary contour.")

    outgoing = {}
    incoming = {}
    for item_index, (_segment_index, start_id, end_id, _forward) in enumerate(directed):
        outgoing.setdefault(start_id, []).append(item_index)
        incoming.setdefault(end_id, []).append(item_index)
    vertices = set(outgoing) | set(incoming)
    if any(len(outgoing.get(point_id, [])) != 1 or len(incoming.get(point_id, [])) != 1 for point_id in vertices):
        raise ValueError(f"Domain {domain} has a non-manifold or open boundary; every contour vertex must have one incoming and one outgoing curve.")

    unused = set(range(len(directed)))
    loops = []
    while unused:
        first_index = min(unused)
        start_id = directed[first_index][1]
        current_id = start_id
        items = []
        ordered_points = []
        while True:
            candidates = [item_index for item_index in outgoing[current_id] if item_index in unused]
            if len(candidates) != 1:
                raise ValueError(f"Domain {domain} has a disconnected or intersecting boundary contour.")
            item_index = candidates[0]
            segment_index, segment_start, segment_end, forward = directed[item_index]
            unused.remove(item_index)
            items.append({"segment_index": segment_index, "forward": forward})
            samples = _segment_samples(geometry, geometry.segments[segment_index])
            if not forward:
                samples.reverse()
            ordered_points.extend(samples if not ordered_points else samples[1:])
            current_id = segment_end
            if current_id == start_id:
                break
            if len(items) > len(directed):
                raise ValueError(f"Domain {domain} boundary traversal did not close.")
        if len(ordered_points) > 1 and math.dist(ordered_points[0], ordered_points[-1]) <= 1e-12:
            ordered_points.pop()
        area = _signed_area(ordered_points)
        if not math.isfinite(area) or abs(area) <= 1e-18:
            raise ValueError(f"Domain {domain} has a zero-area boundary loop.")
        loops.append({
            "items": items,
            "vertices": ordered_points,
            "signed_area": area,
        })
    return loops


def _loop_domain_probe(loop):
    """Return a point just inside the oriented domain side of a boundary loop."""
    points = loop["vertices"]
    extent = max(
        max(point[0] for point in points) - min(point[0] for point in points),
        max(point[1] for point in points) - min(point[1] for point in points),
        1e-12,
    )
    for start, end in zip(points, points[1:] + points[:1]):
        dx, dy = end[0] - start[0], end[1] - start[1]
        length = math.hypot(dx, dy)
        if length <= 1e-14:
            continue
        offset = min(length * 1e-4, extent * 1e-7)
        offset = max(offset, min(length * 1e-8, 1e-12))
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        return (midpoint[0] - dy / length * offset, midpoint[1] + dx / length * offset)
    raise ValueError("A legacy boundary loop has no nonzero-length segment.")


def _loop_edge_refs(loop, edge_id_by_segment, *, reverse=False):
    items = list(reversed(loop["items"])) if reverse else loop["items"]
    return [
        {
            "edge_id": edge_id_by_segment[item["segment_index"]],
            "forward": bool(item["forward"]) != bool(reverse),
        }
        for item in items
    ]


def _legacy_boundary_names(value, label):
    if value is None:
        return set()
    if isinstance(value, str):
        values = value.split("|")
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        raise ValueError(f"Legacy {label} must be a pipe-separated string or a collection.")
    return {str(item).strip() for item in values if str(item).strip()}


def _make_boundary_condition(tag, by_type, conditions, *, active_em_tags, active_mech_tags):
    if tag in {"default", "Default", ""}:
        return None
    em_dirichlet_tags = {"DirEM", "DirAAC", "DirEMDC", "Dir"}
    mech_dirichlet_tags = {"DirMechFix", "DirMech"}
    if tag in em_dirichlet_tags and tag not in active_em_tags:
        return None
    if tag in mech_dirichlet_tags and tag not in active_mech_tags:
        return None
    mapping = {
        "DirEM": ("magnetic_potential_zero", "Magnetic potential = 0"),
        "DirAAC": ("magnetic_potential_zero", "Magnetic potential = 0"),
        "DirEMDC": ("magnetic_potential_zero", "Magnetic potential = 0"),
        "Dir": ("magnetic_potential_zero", "Magnetic potential = 0"),
        "Neu_0": ("axis_of_symmetry", "Axis of symmetry"),
        "Axis": ("axis_of_symmetry", "Axis of symmetry"),
        "Neu": ("natural", "Natural magnetic boundary"),
        "DirMechFix": ("mechanical_fixed", "Fixed displacement"),
        "DirMech": ("mechanical_fixed", "Fixed displacement"),
        "Transmission_Interface": ("transmission_interface", "Transmission interface"),
    }
    if tag not in mapping:
        raise ValueError(f"Legacy boundary condition {tag!r} is not supported by this importer yet.")
    kind, name = mapping[tag]
    condition_id = by_type.get(kind)
    if condition_id is None:
        condition_id = "legacy-boundary-" + kind
        condition = {"id": condition_id, "name": name, "type": kind}
        if kind == "axis_of_symmetry":
            condition["automatic_for_axis"] = True
        conditions.append(condition)
        by_type[kind] = condition_id
    return condition_id


def legacy_problem_to_model(problem, *, name="Imported legacy problem", element_size=0.01, polynomial_order=3):
    """Convert captured legacy curves, domains, and constant data to an MM-FEM model."""
    if not isinstance(problem, dict):
        raise ValueError("The selected Python file did not return a legacy problem dictionary.")
    geometry = problem.get("geometry")
    if not isinstance(geometry, LegacySplineGeometryCapture):
        raise ValueError("The problem geometry could not be captured. Import a get_problem() file that builds line/spline3 SplineGeometry boundaries.")
    if not geometry.materials:
        raise ValueError("The legacy geometry does not define any material domains.")

    adc_dirichlet = _legacy_boundary_names(problem.get("dirkeysADC"), "dirkeysADC")
    aac_dirichlet = _legacy_boundary_names(problem.get("dirkeysAAC"), "dirkeysAAC")
    dc_enabled = _number(problem.get("kACCoefDC", 0), "DC magnetic enable coefficient") != 0
    harmonic_enabled = (
        _number(problem.get("kACCoef", 0), "Harmonic magnetic enable coefficient") != 0
        or _number(problem.get("cACCoef", 0), "Harmonic magnetic enable coefficient") != 0
    )
    if dc_enabled and harmonic_enabled and adc_dirichlet != aac_dirichlet:
        raise ValueError(
            "This importer cannot preserve different magnetic Dirichlet boundary "
            "selections when both DC and time-harmonic EM are enabled."
        )
    active_em_tags = adc_dirichlet | aac_dirichlet
    if "dirkeysMech" in problem:
        mech_dirichlet = _legacy_boundary_names(problem.get("dirkeysMech"), "dirkeysMech")
    else:
        mech_dirichlet = {
            str(segment.get("bc")) for segment in geometry.segments
            if segment.get("bc") in {"DirMechFix", "DirMech"}
        }
    domain_ids = sorted(geometry.materials)
    loops_by_domain = {domain: _domain_boundary_loops(geometry, domain) for domain in domain_ids}
    components = []
    component_by_id = {}
    loop_owner = {}
    region_id_by_loop = {}
    edge_id_by_segment = {index: f"legacy-edge-{index + 1}" for index in range(len(geometry.segments))}
    for domain in domain_ids:
        loops = loops_by_domain[domain]
        outer_loops = [loop for loop in loops if loop["signed_area"] > 0]
        hole_loops = [loop for loop in loops if loop["signed_area"] < 0]
        if not outer_loops:
            raise ValueError(f"Domain {domain} has no counter-clockwise exterior boundary loop.")
        outer_loops.sort(key=lambda loop: abs(loop["signed_area"]), reverse=True)
        domain_components = []
        for part, loop in enumerate(outer_loops, start=1):
            region_id = f"legacy-region-{domain}" if len(outer_loops) == 1 else f"legacy-region-{domain}-{part}"
            component = {"domain": domain, "region_id": region_id, "outer": loop, "holes": []}
            components.append(component)
            component_by_id[region_id] = component
            domain_components.append(component)
            region_id_by_loop[(domain, id(loop))] = region_id
            for item in loop["items"]:
                loop_owner[(domain, item["segment_index"])] = region_id
        for loop in hole_loops:
            probe = _loop_domain_probe(loop)
            candidates = [
                component for component in domain_components
                if abs(component["outer"]["signed_area"]) > abs(loop["signed_area"])
                and _point_in_polygon(probe, component["outer"]["vertices"])
            ]
            if not candidates:
                raise ValueError(f"Domain {domain} has an inner boundary loop that cannot be assigned to an enclosing component.")
            owner = min(candidates, key=lambda component: abs(component["outer"]["signed_area"]))
            owner["holes"].append(loop)
            region_id_by_loop[(domain, id(loop))] = owner["region_id"]
            for item in loop["items"]:
                loop_owner[(domain, item["segment_index"])] = owner["region_id"]

    model = new_model(name)
    model["geometry"]["regions"] = []
    model["geometry"]["edges"] = []
    model["geometry"]["sketch_entities"] = []
    point_id_by_key = {}
    topology_points = []
    for original_id in sorted(geometry.points):
        point = geometry.points[original_id]
        key = _coordinate_key(point["coordinates"])
        if key not in point_id_by_key:
            point_id = f"legacy-point-{len(topology_points) + 1}"
            point_id_by_key[key] = point_id
            topology_points.append({"id": point_id, "coordinates": list(key)})
    model["geometry"]["points"] = topology_points
    model["materials"] = []
    model["boundary_conditions"] = []
    mesh = model["mesh"]
    mesh["element_size"] = _expression(_number(element_size, "Target element size"))
    mesh["polynomial_order"] = int(polynomial_order)
    mesh["curve_order"] = int(polynomial_order)
    mesh["hp_layers"] = int(problem.get("HpLayers", 0) or 0)
    mesh["hp_grading_factor"] = _number(problem.get("sigmaG", 0.3), "HP grading factor")
    mesh["region_element_sizes"] = {}
    mesh["hp_region_ids"] = []
    mesh["hp_edge_ids"] = []

    material_names = {domain: geometry.materials[domain] for domain in domain_ids}
    material_id_by_name = {}
    for domain in domain_ids:
        material_name = material_names[domain]
        if material_name not in material_id_by_name:
            material_id = "legacy-material-" + str(uuid.uuid5(uuid.NAMESPACE_URL, material_name))
            material_id_by_name[material_name] = material_id
            properties = {}
            source_dictionaries = (
                ("relative_permeability", "murdict", 1.0),
                ("electrical_conductivity", "sigmadict", 0.0),
                ("youngs_modulus", "Edict", 0.0),
                ("poissons_ratio", "nudict", 0.3),
                ("density", "rhodict", 0.0),
            )
            for property_name, dictionary_name, default in source_dictionaries:
                value = problem.get(dictionary_name, {}).get(material_name, default)
                properties[property_name] = _expression(_number(value, f"{material_name} {property_name}"))
            model["materials"].append({"id": material_id, "name": material_name, "properties": properties})

    mechanical_names = {item.strip() for item in str(problem.get("mechSubdom", "")).split("|") if item.strip()}
    regions_by_id = {}
    regions_by_domain = {}
    for component in components:
        domain = component["domain"]
        region_id = component["region_id"]
        vertices = [[float(point[0]), float(point[1])] for point in component["outer"]["vertices"]]
        if any(point[0] < -1e-12 or not all(math.isfinite(value) for value in point) for point in vertices):
            raise ValueError(f"Domain {domain} must have finite axisymmetric coordinates with r >= 0.")
        vertices = [[0.0 if abs(point[0]) <= 1e-12 else point[0], point[1]] for point in vertices]
        material_name = material_names[domain]
        part_count = sum(1 for item in components if item["domain"] == domain)
        region_name = f"{material_name} (domain {domain})" if part_count == 1 else f"{material_name} (domain {domain}, part {1 + regions_by_domain.get(domain, 0)})"
        regions_by_domain[domain] = regions_by_domain.get(domain, 0) + 1
        outer_refs = _loop_edge_refs(component["outer"], edge_id_by_segment)
        region = {
            "id": region_id,
            "name": region_name,
            "shape": {"type": "imported_contour", "domain": domain},
            "vertices": vertices,
            "material_id": material_id_by_name[material_name],
            "parent_id": None,
            "mechanical": material_name in mechanical_names,
            "sources": {
                "dc_current_density": "0",
                "ac_current_density_real": "0",
                "ac_current_density_imaginary": "0",
                "mechanical_body_force": {"r": "0", "z": "0"},
            },
            "edge_ids": list(dict.fromkeys(item["edge_id"] for item in outer_refs)),
            "boundary_loops": [{"role": "outer", "edges": outer_refs}],
            "constraints": [],
            "legacy_domain": domain,
        }
        domain_maxh = geometry.domain_maxh.get(domain)
        if domain_maxh is not None:
            mesh["region_element_sizes"][region_id] = _expression(
                _number(domain_maxh, f"Domain {domain} maximum element size")
            )
        regions_by_id[region_id] = region
        model["geometry"]["regions"].append(region)

    # Build each exact input curve once. The side map preserves Netgen's left/right
    # material labels even when a legacy domain has several disconnected loops.
    condition_ids_by_type = {}
    for segment_index, segment in enumerate(geometry.segments):
        start = geometry.points[segment["start"]]["coordinates"]
        end = geometry.points[segment["end"]]["coordinates"]
        samples = _segment_samples(geometry, segment)
        curve = {"type": segment["type"], "sampled_points": samples}
        if segment["type"] == "spline3":
            control = geometry.points[segment["control"]]["coordinates"]
            curve["control_point"] = list(control)
            curve["control_point_id"] = point_id_by_key[_coordinate_key(control)]
        tag = str(segment.get("bc") or "")
        condition_ids = []
        if tag and tag not in {"default", "Default"}:
            condition_id = _make_boundary_condition(
                tag,
                condition_ids_by_type,
                model["boundary_conditions"],
                active_em_tags=active_em_tags,
                active_mech_tags=mech_dirichlet,
            )
            if condition_id:
                condition_ids.append(condition_id)
        lies_on_axis = abs(float(start[0])) <= 1e-12 and abs(float(end[0])) <= 1e-12
        if lies_on_axis and (dc_enabled or harmonic_enabled):
            axis_id = _make_boundary_condition(
                "Neu_0", condition_ids_by_type, model["boundary_conditions"],
                active_em_tags=active_em_tags, active_mech_tags=mech_dirichlet,
            )
            condition_ids = [
                item for item in condition_ids
                if next((bc.get("type") for bc in model["boundary_conditions"] if bc.get("id") == item), None) != "magnetic_potential_zero"
            ]
            if axis_id not in condition_ids:
                condition_ids.append(axis_id)
        left_domain = segment["leftdomain"]
        right_domain = segment["rightdomain"]
        left_region_id = loop_owner.get((left_domain, segment_index)) if left_domain else None
        right_region_id = loop_owner.get((right_domain, segment_index)) if right_domain else None
        if left_domain and left_region_id is None:
            raise ValueError(f"Legacy boundary {segment_index + 1} is not part of the left-domain {left_domain} contour.")
        if right_domain and right_region_id is None:
            raise ValueError(f"Legacy boundary {segment_index + 1} is not part of the right-domain {right_domain} contour.")
        edge_id = edge_id_by_segment[segment_index]
        edge = {
            "id": edge_id,
            "name": f"Boundary {segment_index + 1}",
            "vertices": [list(start), list(end)],
            "point_ids": [point_id_by_key[_coordinate_key(start)], point_id_by_key[_coordinate_key(end)]],
            "curve": curve,
            "boundary_condition_ids": condition_ids,
            "legacy_boundary_tag": tag,
            "legacy_domain_sides": {"left_region_id": left_region_id, "right_region_id": right_region_id},
        }
        if segment.get("maxh") is not None:
            edge["legacy_maxh"] = segment["maxh"]
        model["geometry"]["edges"].append(edge)
        if segment.get("hpref") or geometry.points[segment["start"]]["hpref"] or geometry.points[segment["end"]]["hpref"] or (segment.get("control") is not None and geometry.points[segment["control"]]["hpref"]):
            mesh["hp_edge_ids"].append(edge_id)

    for component in components:
        region = regions_by_id[component["region_id"]]
        for hole in component["holes"]:
            neighbor_ids = set()
            for item in hole["items"]:
                segment = geometry.segments[item["segment_index"]]
                domain = component["domain"]
                neighbor_domain = segment["rightdomain"] if segment["leftdomain"] == domain else segment["leftdomain"]
                if neighbor_domain:
                    neighbor_id = loop_owner.get((neighbor_domain, item["segment_index"]))
                    if neighbor_id:
                        neighbor_ids.add(neighbor_id)
            hole_region_id = next(iter(neighbor_ids)) if len(neighbor_ids) == 1 else None
            hole_refs = _loop_edge_refs(hole, edge_id_by_segment, reverse=True)
            boundary_loop = {"role": "hole", "edges": hole_refs}
            if hole_region_id:
                boundary_loop["region_id"] = hole_region_id
            region["boundary_loops"].append(boundary_loop)
            for child_id in neighbor_ids:
                child = regions_by_id[child_id]
                if child["parent_id"] not in (None, region["id"]):
                    raise ValueError(f"Imported region {child['name']!r} is enclosed by more than one material region.")
                child["parent_id"] = region["id"]

    for component in components:
        domain = component["domain"]
        region = regions_by_id[component["region_id"]]
        material_name = material_names[domain]
        region["sources"]["dc_current_density"] = _expression(_number(
            problem.get("srcdictDC", {}).get(material_name, 0), f"{material_name} DC current density"
        ))
        ac_value = problem.get("srcdictAC", {}).get(material_name, 0)
        if isinstance(ac_value, numbers.Complex) and not isinstance(ac_value, numbers.Real):
            ac_real, ac_imag = float(ac_value.real), float(ac_value.imag)
        else:
            ac_real, ac_imag = _number(ac_value, f"{material_name} AC current density"), 0.0
        if not math.isfinite(ac_real) or not math.isfinite(ac_imag):
            raise ValueError(f"{material_name} AC current density must be finite.")
        region["sources"]["ac_current_density_real"] = _expression(ac_real)
        region["sources"]["ac_current_density_imaginary"] = _expression(ac_imag)
        body_force = problem.get("srcMechDict", {}).get(material_name)
        if body_force is not None:
            if not isinstance(body_force, (list, tuple)) or len(body_force) != 2:
                raise ValueError(f"{material_name} mechanical body force must be a constant (r, z) pair.")
            region["sources"]["mechanical_body_force"] = {
                "r": _expression(_number(body_force[0], f"{material_name} radial body force")),
                "z": _expression(_number(body_force[1], f"{material_name} axial body force")),
            }
        local_maxh = geometry.domain_maxh.get(domain)
        if local_maxh is not None:
            mesh["region_element_sizes"][region["id"]] = _expression(_number(local_maxh, f"Domain {domain} target size"))

    physics = model["physics"]
    mechanics_enabled = (
        _number(problem.get("mMechCoef", 0), "Mechanical mass coefficient") != 0
        or _number(problem.get("kMechCoef", 0), "Mechanical stiffness coefficient") != 0
    )
    coupling_enabled = (
        _number(problem.get("kCoupCoef", 0), "Magneto-mechanical coupling coefficient") != 0
        or _number(problem.get("cCoupCoef", 0), "Magneto-mechanical coupling coefficient") != 0
    )
    physics["dc_magnetic"]["enabled"] = dc_enabled
    physics["harmonic_electromagnetic"]["enabled"] = harmonic_enabled
    physics["mechanics"]["enabled"] = mechanics_enabled
    physics["coupling"]["enabled"] = coupling_enabled
    physics["mechanics"]["rayleigh_alpha"] = _expression(_number(problem.get("dampingalpha", 0), "Rayleigh alpha"))
    physics["mechanics"]["rayleigh_beta"] = _expression(_number(problem.get("dampingbeta", 0), "Rayleigh beta"))

    omega = problem.get("omega", []) or []
    if isinstance(omega, numbers.Number):
        omega = [omega]
    frequencies = [_number(value, "Angular frequency") / (2 * math.pi) for value in omega]
    if harmonic_enabled and not frequencies:
        raise ValueError("The legacy problem does not define any time-harmonic frequencies.")
    if not frequencies:
        frequencies = [500.0]
    frequency_expressions = [_expression(value) for value in frequencies]
    physics["harmonic_electromagnetic"]["frequency_hz"] = frequency_expressions[0]
    studies = new_studies()
    studies["studies"][0]["name"] = "Imported legacy study"
    studies["studies"][0]["physics"] = [
        key for key, enabled in (
            ("dc_magnetic", dc_enabled),
            ("harmonic_electromagnetic", harmonic_enabled),
            ("mechanics", mechanics_enabled),
            ("coupling", coupling_enabled),
        ) if enabled
    ]
    studies["studies"][0]["frequency_hz"] = frequency_expressions

    solver_name = str(problem.get("solver", "direct")).strip().lower()
    if solver_name not in {"direct", "iterative", "gmres", "cg"}:
        raise ValueError(f"Legacy linear solver {solver_name!r} is not supported by the GUI yet.")
    model["solver"]["linear_solver"] = "direct" if solver_name == "direct" else "iterative"
    # Match settings_axis() defaults so the imported run begins with the same
    # fixed-point budget as the established problem-file route.
    model["solver"]["maximum_iterations"] = max(1, int(problem.get("Nfixpoint", 10)))
    model["legacy_import"] = {
        "source_name": str(name),
        "topology_mode": "direct_curve_edges",
        "boundary_keys": {
            "dc_magnetic": sorted(adc_dirichlet),
            "harmonic_electromagnetic": sorted(aac_dirichlet),
            "mechanics": sorted(mech_dirichlet),
        },
        "solver_parameters": {
            "transmission_exterior_subdomain": str(problem.get("transmissionExteriorSubdom", "") or ""),
            "ramp_time": _number(problem.get("ramp_time", 0), "Ramp time"),
            "ramp_type": str(problem.get("ramp_type", "linear") or "linear"),
            "source_type": str(problem.get("source_type", "sine") or "sine"),
            "coupled_approach": str(problem.get("coupled_approach", "linearised") or "linearised"),
        },
    }
    model["geometry"]["dimension_expression_unit"] = "m"

    errors = validate_model(model)
    if errors:
        raise ValueError("Imported problem is not a valid MM-FEM model: " + " ".join(errors))
    study_errors = validate_studies(studies)
    if study_errors:
        raise ValueError("Imported study is not valid: " + " ".join(study_errors))
    return model, studies, {"schema_version": 1, "active_section": "geometry", "camera": "fit"}


def import_legacy_problem_file(path, *, element_size=1.0, polynomial_order=3, legacy_h_local=0.1):
    """Execute a trusted legacy ``.py`` problem and convert its data to MM-FEM."""
    source = Path(path).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != ".py":
        raise ValueError("Select an existing legacy Python problem file (.py).")
    namespace = runpy.run_path(str(source))
    factory = namespace.get("get_problem")
    if not callable(factory):
        raise ValueError("The selected Python file must define get_problem(...).")

    namespace["SplineGeometry"] = LegacySplineGeometryCapture
    factory.__globals__["SplineGeometry"] = LegacySplineGeometryCapture
    signature = inspect.signature(factory)
    # Legacy problem files commonly set per-domain maxh from this parameter.
    # Keep it aligned with main()'s historical default so the import reproduces
    # the original mesh density even when the GUI's global target is different.
    supplied = {"RHS": "midpoint", "dt": 1e-3, "h_local": float(legacy_h_local)}
    positional = []
    keyword = {}
    for parameter in signature.parameters.values():
        if parameter.kind in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}:
            continue
        if parameter.name not in supplied:
            if parameter.default is inspect.Parameter.empty:
                raise ValueError(f"Legacy get_problem() requires unsupported argument {parameter.name!r}.")
            continue
        value = supplied[parameter.name]
        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
            positional.append(value)
        else:
            keyword[parameter.name] = value
    problem = factory(*positional, **keyword)
    model, studies, layout = legacy_problem_to_model(
        problem,
        name=source.stem.replace("_", " ").strip().title() or "Imported legacy problem",
        element_size=element_size,
        polynomial_order=polynomial_order,
    )
    model["legacy_import"]["source_name"] = source.name
    return model, studies, layout
