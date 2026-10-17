"""Runtime compatibility for rendering NGSolve fields on Windows.

The solver's own TaskManager remains unchanged. Only TaskManager scopes entered
while ngsolve_webgpu samples a FunctionData for display are disabled; on the
supported Windows laptop, entering a parallel TaskManager while evaluating
these mapped GridFunctions terminates the viewer process.
"""
from __future__ import annotations

from contextlib import nullcontext
from functools import wraps
import sys
import threading

_render_state = threading.local()
_original_task_manager = None
_installed = False


def _render_task_manager(*args, **kwargs):
    if getattr(_render_state, "depth", 0) > 0:
        return nullcontext()
    return _original_task_manager(*args, **kwargs)


def install_renderer_taskmanager_guard() -> bool:
    """Keep ngsolve_webgpu's field sampling serial on Windows only.

    Rendering still evaluates the full coefficient function at the requested
    interpolation order. NGSolve solver calls outside FunctionData.update
    continue to use the original parallel TaskManager.
    """
    global _original_task_manager, _installed
    if sys.platform != "win32":
        return False
    if _installed:
        return True

    import ngsolve as ngs
    from ngsolve_webgpu.cf import FunctionData

    if getattr(FunctionData, "_mmfem_render_taskmanager_guard", False):
        _installed = True
        return True

    _original_task_manager = ngs.TaskManager
    _render_task_manager._mmfem_render_guard = True
    ngs.TaskManager = _render_task_manager
    original_update = FunctionData.update

    @wraps(original_update)
    def update_for_render(self, *args, **kwargs):
        depth = getattr(_render_state, "depth", 0)
        _render_state.depth = depth + 1
        try:
            return original_update(self, *args, **kwargs)
        finally:
            _render_state.depth = depth

    FunctionData.update = update_for_render
    FunctionData._mmfem_render_taskmanager_guard = True
    _installed = True
    return True


def automatic_render_subdivision(
    element_count: int, effective_order: int, max_render_triangles: int = 2_000_000
) -> int | None:
    """Choose a display-only tessellation override for dense meshes.

    ``None`` preserves ngsolve_webgpu's normal order-based choice. On larger
    meshes, cap the estimated number of rendered surface triangles while
    keeping at least one triangle per mesh element. Solver meshes and field
    interpolation data are not changed.
    """
    element_count = max(0, int(element_count))
    effective_order = max(1, int(effective_order))
    max_render_triangles = max(1, int(max_render_triangles))
    if element_count == 0:
        return None

    if effective_order > 3:
        base_subdivision = (effective_order + 2) // 3 + 1
    elif effective_order > 1:
        base_subdivision = 3
    else:
        base_subdivision = 1

    if element_count * base_subdivision * base_subdivision <= max_render_triangles:
        return None

    subdivision = int((max_render_triangles / element_count) ** 0.5)
    return max(1, min(base_subdivision, subdivision))


def automatic_render_order(
    element_count: int, requested_order: int, has_quads: bool = False,
    max_sample_points: int = 3_000_000,
) -> int:
    """Lower only viewer interpolation order when dense meshes over-sample.

    ngsolve_webgpu doubles its requested order when the mesh contains quads.
    The solver field, saved GridFunction, and mesh remain untouched.
    """
    element_count = max(0, int(element_count))
    order = max(1, int(requested_order))
    max_sample_points = max(1, int(max_sample_points))
    if element_count == 0:
        return order

    while order > 1:
        sample_order = 2 * order if has_quads else order
        points_per_element = (sample_order + 1) * (sample_order + 2) // 2
        if element_count * points_per_element <= max_sample_points:
            break
        order -= 1
    return order
