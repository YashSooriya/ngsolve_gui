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


def migrate_legacy_model(model):
    """Upgrade pre-SI geometry expressions while keeping their geometry fixed."""
    if not isinstance(model, dict) or model.get("schema_version") != LEGACY_SCHEMA_VERSION:
        return model
    geometry = model.get("geometry", {})
    if not isinstance(geometry, dict) or geometry.get("dimension_expression_unit") == "m":
        return model
    if geometry.get("dimension_expression_unit", "mm") != "mm":
        return model
    model = copy.deepcopy(model)
    geometry = model.get("geometry", {})
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
            "regions": [],
            "edges": [],
            "constraints": [],
        },
        "materials": [],
        "boundary_conditions": [],
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


def validate_model(model: dict) -> list[str]:
    """Return actionable schema/input problems without mutating the model."""
    errors = []
    if not isinstance(model, dict):
        return ["Model must be a JSON object."]
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
    if not isinstance(materials_list, list):
        errors.append("Materials must be a list.")
        materials_list = []
    materials_by_id = {}
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
            if key not in properties:
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
        if not isinstance(region.get("material_id"), str) or region.get("material_id") not in materials:
            errors.append(f"Region '{region.get('name', rid)}' needs a valid material.")
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
            if not _strictly_contains(first_points, second_points) and not _strictly_contains(second_points, first_points) and _polygons_overlap(first_points, second_points):
                errors.append(f"Regions '{first.get('name')}' and '{second.get('name')}' cross or touch; only strictly nested or disjoint regions are supported.")
    for region, points in valid_regions:
        child_area = abs(_signed_area(points))
        containers = [
            (abs(_signed_area(outer_points)), outer.get("id"))
            for outer, outer_points in valid_regions
            if outer.get("id") != region.get("id")
            and abs(_signed_area(outer_points)) > child_area
            and _strictly_contains(outer_points, points)
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

    conditions = model.get("boundary_conditions", [])
    if not isinstance(conditions, list):
        errors.append("Boundary conditions must be a list.")
        conditions = []
    condition_by_id = {}
    allowed_boundary_types = {
        "axis_of_symmetry", "magnetic_potential_zero", "natural",
        "mechanical_fixed", "mechanical_prescribed", "mechanical_traction", "mechanical_robin",
        "transmission_interface",
    }
    for condition in conditions:
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
                key = _coordinate_edge_key(endpoint_values)
                if key in edge_keys:
                    errors.append(f"Geometry edge '{edge.get('name', edge_id)}' duplicates another edge.")
                edge_keys[key] = edge_id
                if isinstance(edge_id, str):
                    edge_key_by_id[edge_id] = key
            except (TypeError, ValueError, OverflowError) as error:
                errors.append(f"Edge '{edge.get('name', edge_id)}': {error}.")
        assignments = edge.get("boundary_condition_ids")
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
        if sum(kind in {"axis_of_symmetry", "magnetic_potential_zero", "natural"} for kind in assigned_types) > 1:
            errors.append(f"Edge '{edge.get('name', edge_id)}' has multiple electromagnetic conditions.")
        if sum(str(kind).startswith("mechanical_") for kind in assigned_types) > 1:
            errors.append(f"Edge '{edge.get('name', edge_id)}' has multiple mechanical conditions.")
        if isinstance(edge_vertices, list) and len(edge_vertices) == 2:
            try:
                on_axis = all(abs(float(point[0])) <= 1e-12 for point in edge_vertices)
            except (TypeError, ValueError, IndexError, OverflowError):
                on_axis = False
            if on_axis and any(str(kind).startswith("mechanical_") for kind in assigned_types):
                errors.append(f"Edge '{edge.get('name', edge_id)}' cannot carry a mechanical support on r=0.")
        for condition_id in assignments:
            if condition_id not in condition_by_id:
                errors.append(f"Edge '{edge.get('name', edge_id)}' references a missing boundary condition.")

    expected_edge_keys = set()
    for region in valid_regions:
        region_data, points = region
        keys = {
            _coordinate_edge_key([points[index], points[(index + 1) % len(points)]])
            for index in range(len(points))
        }
        expected_edge_keys.update(keys)
        region_edge_ids = region_data.get("edge_ids")
        if not isinstance(region_edge_ids, list) or len(region_edge_ids) != len(keys):
            errors.append(f"Region '{region_data.get('name')}' does not have an id for each sketch edge.")
        elif any(edge_id not in edge_ids for edge_id in region_edge_ids):
            errors.append(f"Region '{region_data.get('name')}' references a missing sketch edge.")
        elif {edge_key_by_id[edge_id] for edge_id in region_edge_ids if edge_id in edge_key_by_id} != keys:
            errors.append(f"Region '{region_data.get('name')}' edge ids do not match its sketch geometry.")
    if set(edge_keys) != expected_edge_keys:
        errors.append("Geometry edge list does not match the region outlines.")

    for region in region_by_id.values():
        source = region.get("sources", {})
        if not isinstance(source, dict):
            errors.append(f"Region '{region.get('name', region.get('id'))}' sources must be an object.")
            continue
        for key in ("dc_current_density", "ac_current_density_real", "ac_current_density_imaginary"):
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
        if not isinstance(body, dict):
            errors.append(f"Region '{region.get('name', region.get('id'))}' body force must be an object.")
        else:
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

    physics = model.get("physics", {})
    if not isinstance(physics, dict):
        errors.append("Physics settings must be an object.")
        physics = {}
    expected_physics = ("dc_magnetic", "harmonic_electromagnetic", "mechanics", "coupling")
    for key in expected_physics:
        section = physics.get(key, {})
        if not isinstance(section, dict) or not isinstance(section.get("enabled", False), bool):
            errors.append(f"Physics setting '{key}' must contain an enabled boolean.")
    harmonic = physics.get("harmonic_electromagnetic", {})
    if isinstance(harmonic, dict):
        check_unit("Default frequency", harmonic.get("frequency_hz", "500"), "Hz")
        try:
            frequency = evaluate_expression(harmonic.get("frequency_hz", "500"), parameter_map)
            if frequency <= 0:
                raise ValueError("must be positive")
        except (TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
            errors.append(f"Default frequency: {error}.")

    mesh = model.get("mesh", {})
    if not isinstance(mesh, dict):
        errors.append("Mesh settings must be an object.")
        mesh = {}
    try:
        check_unit("Mesh element size", mesh.get("element_size", "0.01"), "m")
        if int(mesh.get("polynomial_order", 3)) not in range(1, 7):
            raise ValueError("polynomial order must be between 1 and 6")
        if float(evaluate_expression(mesh.get("element_size", "0.01"), parameter_map)) <= 0:
            raise ValueError("target element size must be positive")
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
            if float(evaluate_expression(expression, parameter_map)) <= 0:
                raise ValueError("target element size must be positive")
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
        errors.append("Solver settings must be an object.")
        solver = {}
    try:
        if int(solver.get("anderson_depth", 3)) < 0:
            raise ValueError("Anderson depth cannot be negative")
        if float(solver.get("anderson_beta", 1.0)) <= 0:
            raise ValueError("Anderson relaxation must be positive")
        if float(solver.get("relative_tolerance", 1e-6)) <= 0:
            raise ValueError("relative tolerance must be positive")
        if int(solver.get("maximum_iterations", 50)) < 1:
            raise ValueError("maximum iterations must be at least one")
        if solver.get("linear_solver", "direct") != "direct":
            raise ValueError("Only the direct linear solver is currently supported")
    except (TypeError, ValueError, OverflowError) as error:
        errors.append(f"Solver settings: {error}.")

    studies = model.get("studies", [])
    if studies is not None and not isinstance(studies, list):
        errors.append("Studies must be a list.")
    elif isinstance(studies, list):
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
                    if evaluate_expression(point, parameter_map) <= 0:
                        raise ValueError("frequency must be positive")
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


def validate_studies(studies: dict, parameters=None) -> list[str]:
    """Validate the initially supported DC/time-harmonic sweep definition."""
    errors = []
    if not isinstance(studies, dict) or studies.get("schema") != SCHEMA:
        return [f"Studies must use the '{SCHEMA}' schema."]
    if studies.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Unsupported studies schema version: {studies.get('schema_version')}.")
    entries = studies.get("studies")
    if not isinstance(entries, list) or len(entries) != 1:
        errors.append("Exactly one DC + harmonic study is supported; use its frequency list for a sweep.")
        return errors
    study = entries[0]
    if not isinstance(study, dict):
        return ["The study definition must be an object."]
    if not str(study.get("id", "")).strip() or not str(study.get("name", "")).strip():
        errors.append("The study needs an id and a name.")
    points = study.get("frequency_hz", [])
    if isinstance(points, str):
        points = [part.strip() for part in points.split(",") if part.strip()]
    if not isinstance(points, list) or not points:
        errors.append("Enter at least one positive frequency for the study.")
        return errors
    parameters = parameters or {}
    parameter_entries = list(parameters.values()) if isinstance(parameters, dict) else parameters
    try:
        parameter_dims = infer_parameter_dimensions(parameter_entries)
    except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        parameter_dims = {}
    for point in points:
        try:
            require_expression_unit(point, "Hz", parameter_dims)
            if evaluate_expression(point, parameters) <= 0:
                raise ValueError("frequency must be positive")
        except (TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError) as error:
            errors.append(f"Study frequency {point!r}: {error}.")
    return errors


def package_model(model: dict, studies: dict | None = None, layout: dict | None = None) -> bytes:
    model = migrate_legacy_model(model)
    errors = validate_model(model)
    if errors:
        raise ValueError("Cannot save model: " + " ".join(errors))
    studies = migrate_legacy_studies(studies or new_studies())
    parameter_map = {
        item.get("name"): item
        for item in model.get("parameters", [])
        if isinstance(item, dict) and item.get("name")
    }
    study_errors = validate_studies(studies, parameter_map)
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


def unpack_model(data: bytes) -> tuple[dict, dict, dict]:
    """Read a model archive with bounded sizes and strict known entry names."""
    if not isinstance(data, (bytes, bytearray)):
        data = bytes(data)
    if len(data) > 20_000_000:
        raise ValueError("Model archive exceeds the 20 MB size limit.")
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
        model = migrate_legacy_model(json.loads(archive.read(MODEL_FILENAME)))
        studies = json.loads(archive.read(STUDIES_FILENAME)) if STUDIES_FILENAME in names else new_studies()
        studies = migrate_legacy_studies(studies)
        layout = json.loads(archive.read(LAYOUT_FILENAME)) if LAYOUT_FILENAME in names else {}
    errors = validate_model(model)
    if errors:
        raise ValueError("Invalid model: " + " ".join(errors))
    parameter_map = {
        item.get("name"): item
        for item in model.get("parameters", [])
        if isinstance(item, dict) and item.get("name")
    }
    study_errors = validate_studies(studies, parameter_map)
    if study_errors:
        raise ValueError("Invalid studies.json: " + " ".join(study_errors))
    if not isinstance(layout, dict):
        raise ValueError("Invalid layout.json.")
    return model, studies, layout
