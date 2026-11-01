"""Helpers for the GUI tests that require the companion solver checkout."""

from __future__ import annotations

import os
from pathlib import Path


def solver_checkout_available() -> bool:
    """Return whether the companion numerical solver source is available."""
    candidates = []
    configured_root = os.environ.get("COUPLED_SOLVER_ROOT")
    if configured_root:
        candidates.append(Path(configured_root).expanduser())

    gui_root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            gui_root / "coupled_solver",
            gui_root.parent / "ngsolve-ui" / "coupled_solver",
            Path.cwd() / "coupled_solver",
        ]
    )
    return any(
        (candidate / "main.py").is_file()
        and (candidate / "scripts").is_dir()
        for candidate in candidates
    )


SOLVER_UNAVAILABLE_REASON = (
    "requires the companion ngsolve-ui/coupled_solver checkout; "
    "set COUPLED_SOLVER_ROOT or place the repositories side by side"
)
