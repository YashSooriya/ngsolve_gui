"""Geometry helpers for the 3-D result slice view."""

from __future__ import annotations

import itertools

import numpy as np


ALL_REGIONS = "All regions"
_AXIS_NORMALS = {
    "x": (1.0, 0.0, 0.0),
    "y": (0.0, 1.0, 0.0),
    "z": (0.0, 0.0, 1.0),
}


def plane_normal(axis: str, custom_normal=(0.0, 0.0, 1.0)) -> np.ndarray:
    """Return a unit plane normal for an axis preset or custom direction."""
    axis = str(axis).lower()
    if axis in _AXIS_NORMALS:
        return np.asarray(_AXIS_NORMALS[axis], dtype=float)
    if axis != "custom":
        raise ValueError(f"Unknown slice plane: {axis!r}")

    normal = np.asarray(custom_normal, dtype=float).reshape(-1)
    if normal.size != 3 or not np.all(np.isfinite(normal)):
        raise ValueError("A custom slice normal needs three finite values")
    length = float(np.linalg.norm(normal))
    if length <= 1e-14:
        raise ValueError("A custom slice normal cannot be zero")
    return normal / length


def projection_bounds(bounds, normal) -> tuple[float, float]:
    """Project an axis-aligned 3-D bounding box onto a plane normal."""
    low, high = (np.asarray(side, dtype=float).reshape(3) for side in bounds)
    n = np.asarray(normal, dtype=float).reshape(3)
    corners = np.asarray(
        [
            (x, y, z)
            for x, y, z in itertools.product(
                (low[0], high[0]),
                (low[1], high[1]),
                (low[2], high[2]),
            )
        ],
        dtype=float,
    )
    distances = corners @ n
    return float(distances.min()), float(distances.max())


def position_on_plane(bounds, normal, fraction: float) -> tuple[np.ndarray, float, float]:
    """Map a 0..1 scrollbar value to a plane centre and signed offset."""
    low, high = (np.asarray(side, dtype=float).reshape(3) for side in bounds)
    n = np.asarray(normal, dtype=float).reshape(3)
    start, end = projection_bounds(bounds, n)
    t = min(1.0, max(0.0, float(fraction)))
    distance = start + t * (end - start)
    center = 0.5 * (low + high)
    offset = distance - float(np.dot(center, n))
    return center, offset, distance


def material_element_mask(mesh, material: str | None):
    """Return a volume-element mask for one material, or None for all regions."""
    if material in (None, "", ALL_REGIONS):
        return None
    materials = [str(name) for name in mesh.GetMaterials()]
    try:
        material_index = materials.index(str(material)) + 1
    except ValueError as exc:
        raise ValueError(f"Unknown mesh material: {material!r}") from exc

    elements = mesh.ngmesh.Elements3D().NumPy()
    return np.asarray(elements["index"] == material_index, dtype=bool)
