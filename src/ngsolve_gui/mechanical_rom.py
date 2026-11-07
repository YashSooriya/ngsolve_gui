"""Shared project validation for the supported mechanical modal ROM."""

from __future__ import annotations

import math


MECHANICAL_SOLUTION_METHODS = {
    "full_order": "Full order",
    "modal_rom": "Mechanical modal ROM",
}

DEFAULT_MECHANICAL_ROM = {
    # Match the existing 3D solver's conservative mechanical ROM default.
    "modes": 40,
    "eigen_solver": "scipy",
    "pinvit_max_iterations": 80,
    "pinvit_tolerance": 1e-8,
    "pinvit_residual_tolerance": 1e-4,
    "static_enrichment": True,
}


def normalize_mechanical_rom_settings(settings):
    """Validate and normalize settings consumed by the existing ROM path."""
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("Mechanical ROM settings must be an object.")

    values = {**DEFAULT_MECHANICAL_ROM, **settings}

    def integer(key, label, minimum, maximum):
        raw = values[key]
        if isinstance(raw, bool):
            raise ValueError(f"{label} must be an integer from {minimum} to {maximum}.")
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                f"{label} must be an integer from {minimum} to {maximum}."
            ) from error
        if not math.isfinite(number) or not number.is_integer() or not minimum <= number <= maximum:
            raise ValueError(f"{label} must be an integer from {minimum} to {maximum}.")
        return int(number)

    def positive_tolerance(key, label, maximum):
        try:
            number = float(values[key])
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(f"{label} must be between 1e-14 and {maximum:g}.") from error
        if not math.isfinite(number) or not 1e-14 <= number <= maximum:
            raise ValueError(f"{label} must be between 1e-14 and {maximum:g}.")
        return number

    solver = str(values["eigen_solver"]).strip().lower()
    if solver not in {"scipy", "pinvit"}:
        raise ValueError("Choose SciPy sparse eigensolve or PINVIT for the mechanical ROM.")
    if not isinstance(values["static_enrichment"], bool):
        raise ValueError("Static load and boundary enrichment must be enabled or disabled.")

    return {
        "modes": integer("modes", "Number of mechanical modes", 1, 500),
        "eigen_solver": solver,
        "pinvit_max_iterations": integer(
            "pinvit_max_iterations", "PINVIT maximum iterations", 1, 1000
        ),
        "pinvit_tolerance": positive_tolerance(
            "pinvit_tolerance", "PINVIT eigenvalue tolerance", 0.1
        ),
        "pinvit_residual_tolerance": positive_tolerance(
            "pinvit_residual_tolerance", "PINVIT residual tolerance", 1.0
        ),
        "static_enrichment": values["static_enrichment"],
    }


def validate_mechanical_solution(method, settings, *, mechanics_active):
    """Return a normalized method/settings pair for an active mechanics solve."""
    method = str(method or "full_order").strip().lower()
    if method not in MECHANICAL_SOLUTION_METHODS:
        raise ValueError("Choose full-order mechanics or mechanical modal ROM.")
    if method == "full_order":
        try:
            normalized = normalize_mechanical_rom_settings(settings)
        except ValueError:
            # Inactive ROM options cannot invalidate a full-order solve.
            normalized = dict(DEFAULT_MECHANICAL_ROM)
    else:
        normalized = normalize_mechanical_rom_settings(settings)
    if method == "modal_rom" and not mechanics_active:
        raise ValueError("Mechanical modal ROM requires a time-harmonic mechanics study.")
    return method, normalized
