"""Dimension checks for scalar expressions entered in the Solve workspace.

All stored values are SI. Bare numeric expressions in a typed field inherit
that field's unit, while named parameters keep the dimension selected for the
parameter. This lets a user enter ``5`` as a current density without silently
allowing a length parameter to be used as a current density.
"""

from __future__ import annotations

import ast
from fractions import Fraction
import math


# Base dimensions are (mass, length, time, electric current).
_ZERO = (Fraction(0),) * 4
_LENGTH = (Fraction(0), Fraction(1), Fraction(0), Fraction(0))
_UNIT_DIMENSIONS = {
    "dimensionless": _ZERO,
    "1": _ZERO,
    "m": _LENGTH,
    "kg/m^3": (Fraction(1), Fraction(-3), Fraction(0), Fraction(0)),
    "A/m^2": (Fraction(0), Fraction(-2), Fraction(0), Fraction(1)),
    "N/m^3": (Fraction(1), Fraction(-2), Fraction(-2), Fraction(0)),
    "N/m^2": (Fraction(1), Fraction(-1), Fraction(-2), Fraction(0)),
    "Pa": (Fraction(1), Fraction(-1), Fraction(-2), Fraction(0)),
    "S/m": (Fraction(-1), Fraction(-3), Fraction(3), Fraction(2)),
    "Hz": (Fraction(0), Fraction(0), Fraction(-1), Fraction(0)),
}

UNIT_OPTIONS = (
    {"label": "Dimensionless", "value": "dimensionless"},
    {"label": "Length (m)", "value": "m"},
    {"label": "Density (kg/m³)", "value": "kg/m^3"},
    {"label": "Current density (A/m²)", "value": "A/m^2"},
    {"label": "Body force (N/m³)", "value": "N/m^3"},
    {"label": "Pressure / modulus (Pa)", "value": "Pa"},
    {"label": "Conductivity (S/m)", "value": "S/m"},
    {"label": "Frequency (Hz)", "value": "Hz"},
)

_SUFFIXES = tuple(sorted((unit for unit in _UNIT_DIMENSIONS if unit not in {"dimensionless", "1"}), key=len, reverse=True))


def dimension_for_unit(unit: str | None):
    """Return the SI dimension vector for a supported unit label."""
    key = str(unit or "dimensionless").strip()
    try:
        return _UNIT_DIMENSIONS[key]
    except KeyError as error:
        raise ValueError(f"unsupported unit {key!r}; choose a listed SI unit") from error


def unit_for_dimension(dimension):
    """Return a readable unit label for a known dimension vector."""
    for unit, candidate in _UNIT_DIMENSIONS.items():
        if unit not in {"dimensionless", "1", "N/m^2"} and candidate == dimension:
            return unit
    if dimension == _ZERO:
        return "dimensionless"
    return " × ".join(
        f"{base}^{power}" if power != 1 else base
        for base, power in zip(("kg", "m", "s", "A"), dimension)
        if power
    ) or "dimensionless"


def _split_suffix(expression):
    text = str(expression).strip()
    for unit in _SUFFIXES:
        if text.endswith(unit):
            prefix = text[:-len(unit)]
            if prefix and (prefix[-1].isspace() or prefix[-1].isdigit() or prefix[-1] == ")"):
                return prefix.strip(), unit
    if text.endswith("mm"):
        prefix = text[:-2]
        if prefix and (prefix[-1].isspace() or prefix[-1].isdigit() or prefix[-1] == ")"):
            raise ValueError("use SI metres (m), not millimetres (mm)")
    return text, None


def _combine(left, right, operation):
    return tuple(a + b if operation == "add" else a - b for a, b in zip(left, right))


def _infer(node, parameter_dimensions, allow_coordinates, parameter_values):
    """Return (dimension, only_literal_constants) for one safe AST expression."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return _ZERO, True
    if isinstance(node, ast.Name):
        if node.id in {"pi", "e"}:
            return _ZERO, True
        if allow_coordinates and node.id in {"r", "z"}:
            return _LENGTH, False
        if node.id in parameter_dimensions:
            return parameter_dimensions[node.id], False
        raise ValueError(f"unknown parameter {node.id!r}")
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        return _infer(node.operand, parameter_dimensions, allow_coordinates, parameter_values)
    if isinstance(node, ast.BinOp):
        left, left_literal = _infer(node.left, parameter_dimensions, allow_coordinates, parameter_values)
        right, right_literal = _infer(node.right, parameter_dimensions, allow_coordinates, parameter_values)
        if isinstance(node.op, (ast.Add, ast.Sub, ast.Mod)):
            if left_literal and not right_literal:
                left = right
            elif right_literal and not left_literal:
                right = left
            if left != right:
                raise ValueError(f"cannot add or subtract {unit_for_dimension(left)} and {unit_for_dimension(right)}")
            return left, left_literal and right_literal
        if isinstance(node.op, ast.Mult):
            return _combine(left, right, "add"), left_literal and right_literal
        if isinstance(node.op, ast.Div):
            return _combine(left, right, "subtract"), left_literal and right_literal
        if isinstance(node.op, ast.Pow):
            if right != _ZERO:
                raise ValueError("an exponent must be dimensionless")
            if left == _ZERO:
                return _ZERO, left_literal and right_literal
            if not right_literal:
                raise ValueError("a dimensioned value must use a constant exponent")
            exponent = float(node.right.value) if isinstance(node.right, ast.Constant) else _constant_value(node.right)
            power = Fraction(str(exponent)).limit_denominator(1024)
            return tuple(value * power for value in left), left_literal and right_literal
        raise ValueError("unsupported arithmetic operator")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        name = node.func.id
        args = [_infer(arg, parameter_dimensions, allow_coordinates, parameter_values) for arg in node.args]
        if not args:
            raise ValueError(f"{name} requires at least one argument")
        if name in {"sin", "cos", "tan", "exp", "log"}:
            if any(dimension != _ZERO for dimension, _ in args):
                raise ValueError(f"{name} requires dimensionless arguments")
            if len(args) != 1 and name not in {"min", "max"}:
                raise ValueError(f"{name} requires exactly one argument")
            return _ZERO, all(literal for _, literal in args)
        if name == "sqrt":
            if len(args) != 1:
                raise ValueError("sqrt requires exactly one argument")
            return tuple(value / 2 for value in args[0][0]), args[0][1]
        if name == "abs":
            if len(args) != 1:
                raise ValueError("abs requires exactly one argument")
            return args[0]
        if name in {"min", "max"}:
            common = next((dimension for dimension, literal in args if not literal), _ZERO)
            for dimension, literal in args:
                if literal:
                    continue
                if dimension != common:
                    raise ValueError(f"{name} arguments must have matching dimensions")
            return common, all(literal for _, literal in args)
        raise ValueError(f"unsupported function {name!r}")
    raise ValueError("unsupported expression syntax")


def _constant_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name) and node.id == "pi":
        return math.pi
    if isinstance(node, ast.Name) and node.id == "e":
        return math.e
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.UAdd):
        return _constant_value(node.operand)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_constant_value(node.operand)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
        left, right = _constant_value(node.left), _constant_value(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        return left ** right
    raise ValueError("a dimensioned value must use a constant exponent")


def expression_dimension(expression, parameter_dimensions=None, *, allow_coordinates=False):
    """Infer the dimensions of an expression, including a trailing SI unit."""
    text, suffix = _split_suffix(expression)
    if not text or len(text) > 256:
        raise ValueError("expressions must contain 1 to 256 characters")
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as error:
        raise ValueError(f"invalid expression: {error.msg}") from error
    if sum(1 for _ in ast.walk(tree)) > 128:
        raise ValueError("expression is too complex")
    dimension, literal_only = _infer(tree.body, parameter_dimensions or {}, allow_coordinates, {})
    if suffix:
        suffix_dimension = dimension_for_unit(suffix)
        if not literal_only and dimension != suffix_dimension:
            raise ValueError(f"expression has unit {unit_for_dimension(dimension)} but is labelled {suffix}")
        dimension = suffix_dimension
        literal_only = False
    return dimension, literal_only


def require_expression_unit(expression, expected_unit, parameter_dimensions=None, *, allow_coordinates=False):
    """Check an expression against the unit required by its destination field."""
    expected = dimension_for_unit(expected_unit)
    actual, literal_only = expression_dimension(
        expression,
        parameter_dimensions,
        allow_coordinates=allow_coordinates,
    )
    if literal_only:
        return expected
    if actual != expected:
        raise ValueError(f"has unit {unit_for_dimension(actual)}; expected {expected_unit}")
    return actual


def parameter_dimensions(parameters):
    """Resolve parameter dimensions and enforce each parameter's unit choice."""
    by_name = {}
    for parameter in parameters if isinstance(parameters, list) else []:
        if isinstance(parameter, dict) and parameter.get("name"):
            by_name[str(parameter["name"])] = parameter
    resolved = {}
    resolving = set()

    def resolve(name):
        if name in resolved:
            return resolved[name]
        if name in resolving:
            raise ValueError("Circular parameter reference: " + " -> ".join((*resolving, name)))
        parameter = by_name[name]
        declared_unit = parameter.get("unit", "dimensionless")
        try:
            declared = dimension_for_unit(declared_unit)
        except ValueError as error:
            raise ValueError(f"Parameter '{name}': {error}") from error
        resolving.add(name)
        try:
            try:
                actual, literal_only = expression_dimension(parameter.get("expression", ""), {
                    other_name: resolve(other_name)
                    for other_name in _expression_names(parameter.get("expression", ""))
                    if other_name in by_name and other_name != name
                })
            except ValueError as error:
                if str(error).startswith("Circular parameter reference"):
                    raise
                raise ValueError(f"Parameter '{name}': {error}") from error
            if literal_only:
                actual = declared
            if actual != declared:
                raise ValueError(f"Parameter '{name}': expression has unit {unit_for_dimension(actual)} but parameter is declared as {declared_unit}")
            resolved[name] = declared
            return declared
        finally:
            resolving.remove(name)

    for name in by_name:
        resolve(name)
    return resolved


def _expression_names(expression):
    text, _ = _split_suffix(expression)
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, TypeError):
        return set()
    return {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and node.id not in {"pi", "e", "abs", "sqrt", "sin", "cos", "tan", "exp", "log", "min", "max"}
    }
