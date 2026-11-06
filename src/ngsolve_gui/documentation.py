"""Locate the locally installed Sphinx documentation for Lorentz."""

from __future__ import annotations

import os
from pathlib import Path


DOCUMENTATION_DIRECTORY_ENV = "MMFEM_DOCUMENTATION_DIR"


def find_documentation_index(solver_root: str | os.PathLike[str] | None = None) -> Path | None:
    """Return the first local Sphinx ``index.html`` found for this install.

    Packaged builds set ``MMFEM_DOCUMENTATION_DIR`` to the bundled site. In a
    source checkout the solver repository's normal Sphinx build directory is
    used, with the unbundled package directory as a fallback.
    """
    candidates: list[Path] = []
    configured = os.environ.get(DOCUMENTATION_DIRECTORY_ENV)
    if configured:
        candidates.append(Path(configured).expanduser() / "index.html")

    if solver_root is None:
        configured_solver_root = os.environ.get("COUPLED_SOLVER_ROOT")
        if configured_solver_root:
            solver_root = configured_solver_root

    if solver_root is not None:
        docs = Path(solver_root).expanduser() / "docs"
        candidates.extend((docs / "build" / "html" / "index.html", docs / "_build" / "html" / "index.html"))

    package_docs = Path(__file__).resolve().parent / "docs" / "index.html"
    candidates.append(package_docs)

    seen: set[Path] = set()
    for index in candidates:
        try:
            resolved = index.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None
