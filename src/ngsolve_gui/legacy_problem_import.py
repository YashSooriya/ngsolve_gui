"""Import the supported, data-only subset of legacy axisymmetric problem files.

The selected Python file is executed to extract its problem definitions. This
importer deliberately accepts straight, rectangular domains and constant
material/source data; it refuses constructs that it cannot preserve exactly.
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
        if not isinstance(curve, (list, tuple)) or len(curve) != 3 or curve[0] != "line":
            raise ValueError("Only straight line boundaries are supported by this importer.")
        start, end = int(curve[1]), int(curve[2])
        if start not in self.points or end not in self.points:
            raise ValueError("A legacy boundary refers to a point that was not defined.")
        allowed = {"bc", "leftdomain", "rightdomain", "maxh", "hpref"}
        if set(kwargs) - allowed:
            raise ValueError("The legacy boundary uses unsupported options: " + ", ".join(sorted(set(kwargs) - allowed)))
        self.segments.append({
            "start": start,
            "end": end,
            "bc": kwargs.get("bc"),
            "leftdomain": int(kwargs.get("leftdomain", 0)),
            "rightdomain": int(kwargs.get("rightdomain", 0)),
            "maxh": kwargs.get("maxh"),
            "hpref": bool(kwargs.get("hpref", False)),
        })

    def SetMaterial(self, domain, name):
        self.materials[int(domain)] = str(name)

    def SetDomainMaxH(self, domain, maxh):
        self.domain_maxh[int(domain)] = float(maxh)


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


def _domain_polygon(geometry, domain):
    # The supported legacy files define every material contour with the
    # material on its left. Child contours are separate regions in MM-FEM, so
    # the parent's polygon is its exterior loop only.
    segments = [segment for segment in geometry.segments if segment["leftdomain"] == domain]
    if len(segments) < 3:
        raise ValueError(f"Domain {domain} has no supported closed exterior contour.")
    adjacency = {}
    for index, segment in enumerate(segments):
        adjacency.setdefault(segment["start"], []).append(index)
        adjacency.setdefault(segment["end"], []).append(index)
    if any(len(items) != 2 for items in adjacency.values()):
        raise ValueError(f"Domain {domain} must have one simple, closed boundary loop.")

    first = min(range(len(segments)), key=lambda item: min(segments[item]["start"], segments[item]["end"]))
    first_segment = segments[first]
    start_point = min(first_segment["start"], first_segment["end"])
    current = start_point
    order = []
    used = set()
    while True:
        candidates = [index for index in adjacency[current] if index not in used]
        if not candidates:
            break
        index = candidates[0]
        used.add(index)
        segment = segments[index]
        next_point = segment["end"] if segment["start"] == current else segment["start"]
        order.append((current, next_point, segment))
        current = next_point
        if current == start_point:
            break
    if current != start_point or len(used) != len(segments):
        raise ValueError(f"Domain {domain} contains multiple loops or disconnected boundary segments.")

    vertices = [geometry.points[start]["coordinates"] for start, _end, _segment in order]
    if len(vertices) != 4:
        raise ValueError(f"Domain {domain} must be a single four-sided rectangle for this initial importer.")
    radial = sorted({round(point[0], 12) for point in vertices})
    axial = sorted({round(point[1], 12) for point in vertices})
    expected = {(r, z) for r in radial for z in axial}
    if len(radial) != 2 or len(axial) != 2 or {_coordinate_key(point) for point in vertices} != expected:
        raise ValueError(f"Domain {domain} must be an axis-aligned rectangle for this initial importer.")
    if _signed_area(vertices) < 0:
        vertices.reverse()
    if abs(_signed_area(vertices)) <= 1e-14:
        raise ValueError(f"Domain {domain} has zero area.")
    return vertices


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


def _make_boundary_condition(tag, by_type, conditions, *, dir_em_is_active):
    # Legacy files may label the outer mesh contour DirEM while leaving both
    # Dirichlet key lists empty. In that case this is only a mesh marker.
    if tag == "DirEM" and not dir_em_is_active:
        return None
    mapping = {
        "DirEM": ("magnetic_potential_zero", "Magnetic potential = 0"),
        "Neu_0": ("axis_of_symmetry", "Axis of symmetry"),
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
    """Convert a captured legacy problem dictionary to a validated GUI model."""
    if not isinstance(problem, dict):
        raise ValueError("The selected Python file did not return a legacy problem dictionary.")
    geometry = problem.get("geometry")
    if not isinstance(geometry, LegacySplineGeometryCapture):
        raise ValueError("The problem geometry could not be captured. This importer currently supports get_problem() files that build straight rectangular domains.")
    if not geometry.materials:
        raise ValueError("The legacy geometry does not define any material domains.")

    adc_dirichlet = _legacy_boundary_names(problem.get("dirkeysADC"), "dirkeysADC")
    aac_dirichlet = _legacy_boundary_names(problem.get("dirkeysAAC"), "dirkeysAAC")
    if adc_dirichlet != aac_dirichlet:
        raise ValueError(
            "This importer cannot yet preserve different magnetic Dirichlet "
            "boundary selections between DC and time-harmonic problems."
        )
    dir_em_is_active = "DirEM" in adc_dirichlet
    legacy_zero_on_all_exterior_edges = not adc_dirichlet

    domain_ids = sorted(geometry.materials)
    polygons = {domain: _domain_polygon(geometry, domain) for domain in domain_ids}
    model = new_model(name)
    model["geometry"]["regions"] = []
    model["geometry"]["edges"] = []
    model["materials"] = []
    model["boundary_conditions"] = []
    mesh = model["mesh"]
    mesh["element_size"] = _expression(_number(element_size, "Target element size"))
    mesh["polynomial_order"] = int(polynomial_order)
    mesh["hp_layers"] = int(problem.get("HpLayers", 0) or 0)
    mesh["hp_grading_factor"] = _number(problem.get("sigmaG", 0.3), "HP grading factor")
    mesh["region_element_sizes"] = {}
    mesh["hp_region_ids"] = []
    mesh["hp_edge_ids"] = []

    material_names = {domain: geometry.materials[domain] for domain in domain_ids}
    region_ids = {domain: f"legacy-region-{domain}" for domain in domain_ids}
    region_material_ids = {}
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
        region_material_ids[domain] = material_id_by_name[material_name]

    mechanical_names = {
        name.strip()
        for name in str(problem.get("mechSubdom", "")).split("|")
        if name.strip()
    }
    region_by_domain = {}
    for domain in domain_ids:
        vertices = [[float(r), float(z)] for r, z in polygons[domain]]
        r_min = min(point[0] for point in vertices)
        r_max = max(point[0] for point in vertices)
        z_min = min(point[1] for point in vertices)
        z_max = max(point[1] for point in vertices)
        region_id = region_ids[domain]
        material_name = material_names[domain]
        shape = {
            "type": "rectangle",
            "r_min": r_min,
            "z_min": z_min,
            "width": r_max - r_min,
            "height": z_max - z_min,
            "dimension_expressions": {
                "r_min": _expression(r_min),
                "z_min": _expression(z_min),
                "width": _expression(r_max - r_min),
                "height": _expression(z_max - z_min),
            },
        }
        region = {
            "id": region_id,
            "name": f"{material_name} (domain {domain})",
            "shape": shape,
            "vertices": vertices,
            "material_id": region_material_ids[domain],
            "parent_id": None,
            "mechanical": material_name in mechanical_names,
            "sources": {
                "dc_current_density": "0",
                "ac_current_density_real": "0",
                "ac_current_density_imaginary": "0",
                "mechanical_body_force": {"r": "0", "z": "0"},
            },
            "edge_ids": [],
            "constraints": [
                {"type": "horizontal", "target": "edge-0", "driving": False},
                {"type": "vertical", "target": "edge-1", "driving": False},
                {"type": "horizontal", "target": "edge-2", "driving": False},
                {"type": "vertical", "target": "edge-3", "driving": False},
                {"type": "radial_position", "target": "shape", "driving": True},
                {"type": "axial_position", "target": "shape", "driving": True},
                {"type": "width", "target": "shape", "driving": True},
                {"type": "height", "target": "shape", "driving": True},
            ],
        }
        region_by_domain[domain] = region
        model["geometry"]["regions"].append(region)

    # Parent links represent the nearest enclosing region. The solver adapter
    # reconstructs each child contour as a material interface in its parent.
    for domain in domain_ids:
        child = polygons[domain]
        child_area = abs(_signed_area(child))
        parents = []
        for candidate in domain_ids:
            if candidate == domain:
                continue
            outer = polygons[candidate]
            if abs(_signed_area(outer)) <= child_area:
                continue
            if all(_point_in_polygon(point, outer) for point in child):
                parents.append((abs(_signed_area(outer)), candidate))
        if parents:
            region_by_domain[domain]["parent_id"] = region_ids[min(parents)[1]]

    condition_ids_by_type = {}
    segment_by_key = {}
    for segment in geometry.segments:
        start = geometry.points[segment["start"]]["coordinates"]
        end = geometry.points[segment["end"]]["coordinates"]
        key = _edge_key(start, end)
        if key in segment_by_key:
            raise ValueError("The legacy geometry contains duplicate boundary segments.")
        segment_by_key[key] = segment
    used_segment_keys = set()
    region_edge_ids = {}
    for domain in domain_ids:
        region = region_by_domain[domain]
        vertices = region["vertices"]
        edge_ids = []
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            key = _edge_key(start, end)
            segment = segment_by_key.get(key)
            if segment is None or segment["leftdomain"] != domain:
                raise ValueError(f"Domain {domain} has a boundary that cannot be matched to a straight legacy line segment.")
            used_segment_keys.add(key)
            edge_id = f"legacy-edge-{len(model['geometry']['edges']) + 1}"
            condition_ids = []
            tag = segment.get("bc")
            if tag and not (legacy_zero_on_all_exterior_edges and str(tag) == "Neu_0"):
                condition_id = _make_boundary_condition(
                    str(tag),
                    condition_ids_by_type,
                    model["boundary_conditions"],
                    dir_em_is_active=dir_em_is_active,
                )
                if condition_id is not None:
                    condition_ids.append(condition_id)
            # In the legacy solver, empty dirkeysADC/dirkeysAAC do not mean
            # "no magnetic Dirichlet condition": both solvers fall back to
            # BND, constraining every exterior mesh edge to zero. Store that
            # behavior explicitly in JSON, including the axis edge; interior
            # material interfaces are excluded by the domain IDs.
            if (
                legacy_zero_on_all_exterior_edges
                and ((segment["leftdomain"] == 0) != (segment["rightdomain"] == 0))
            ):
                condition_id = _make_boundary_condition(
                    "DirEM",
                    condition_ids_by_type,
                    model["boundary_conditions"],
                    dir_em_is_active=True,
                )
                if condition_id not in condition_ids:
                    condition_ids.append(condition_id)
            edge = {
                "id": edge_id,
                "name": f"Edge {len(model['geometry']['edges']) + 1}",
                "vertices": [list(start), list(end)],
                "boundary_condition_ids": condition_ids,
            }
            if tag in {"DirMechFix", "DirMech", "Transmission_Interface"}:
                edge["legacy_mechanical_boundary_override"] = True
            model["geometry"]["edges"].append(edge)
            edge_ids.append(edge_id)
            if segment.get("hpref") or geometry.points[segment["start"]]["hpref"] or geometry.points[segment["end"]]["hpref"]:
                mesh["hp_edge_ids"].append(edge_id)
        region["edge_ids"] = edge_ids
        region_edge_ids[domain] = edge_ids

        maxh_values = [
            _number(segment["maxh"], f"Domain {domain} edge target size")
            for segment in geometry.segments
            if segment["leftdomain"] == domain and segment.get("maxh") is not None
        ]
        if domain in geometry.domain_maxh:
            maxh_values.append(_number(geometry.domain_maxh[domain], f"Domain {domain} target size"))
        if maxh_values:
            if max(maxh_values) - min(maxh_values) > max(1e-12, abs(max(maxh_values)) * 1e-9):
                raise ValueError(f"Domain {domain} uses nonuniform local mesh sizes, which this importer cannot preserve yet.")
            local_size = maxh_values[0]
            global_size = _number(element_size, "Target element size")
            if not math.isclose(local_size, global_size, rel_tol=1e-9, abs_tol=1e-12):
                mesh["region_element_sizes"][region["id"]] = _expression(local_size)

    if used_segment_keys != set(segment_by_key):
        raise ValueError("The legacy geometry has boundary segments outside the supported rectangular region contours.")

    for domain in domain_ids:
        region = region_by_domain[domain]
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

    physics = model["physics"]
    dc_enabled = _number(problem.get("kACCoefDC", 0), "DC magnetic enable coefficient") != 0
    harmonic_enabled = (
        _number(problem.get("kACCoef", 0), "Harmonic magnetic enable coefficient") != 0
        or _number(problem.get("cACCoef", 0), "Harmonic magnetic enable coefficient") != 0
    )
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
    model["legacy_import"] = {"source_name": str(name)}
    if legacy_zero_on_all_exterior_edges:
        model["legacy_import"]["magnetic_dirichlet_fallback"] = "all_exterior"
    model["geometry"]["dimension_expression_unit"] = "m"

    errors = validate_model(model)
    if errors:
        raise ValueError("Imported problem is not a valid MM-FEM model: " + " ".join(errors))
    study_errors = validate_studies(studies)
    if study_errors:
        raise ValueError("Imported study is not valid: " + " ".join(study_errors))
    return model, studies, {"schema_version": 1, "active_section": "geometry", "camera": "fit"}


def import_legacy_problem_file(path, *, element_size=0.01, polynomial_order=3):
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
    supplied = {"RHS": "midpoint", "dt": 1e-3, "h_local": float(element_size)}
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
