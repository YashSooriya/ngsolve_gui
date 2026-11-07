"""Regression coverage for Windows-only viewer sampling compatibility."""
from __future__ import annotations

import contextlib
import sys
from types import SimpleNamespace

import ngsolve as ngs
import pytest
from ngsolve_webgpu.cf import FunctionData

from ngsolve_gui import _render_runtime


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rendering regression")
def test_taskmanager_is_bypassed_only_during_function_data_updates(monkeypatch):
    calls = []

    @contextlib.contextmanager
    def recording_taskmanager(*_args, **_kwargs):
        calls.append("entered")
        yield

    monkeypatch.setattr(_render_runtime, "_original_task_manager", recording_taskmanager)

    class MeshDataStub:
        needs_update = False

        def update(self, _options):
            calls.append("mesh")

    data = FunctionData.__new__(FunctionData)
    data._timestamp = -1
    data.mesh_data = MeshDataStub()

    def sample_field():
        with ngs.TaskManager():
            calls.append("sampled")

    data._create_data = sample_field
    FunctionData.update(data, SimpleNamespace(timestamp=1))

    assert calls == ["mesh", "sampled"]

    with ngs.TaskManager():
        calls.append("solver")

    assert calls[-2:] == ["entered", "solver"]


def test_automatic_render_subdivision_caps_only_dense_meshes():
    choose = _render_runtime.automatic_render_subdivision

    # The original 2.9k-element p=3 study keeps the renderer's smooth default.
    assert choose(2_897, 3) is None
    # The 410k-element rerun lowers only display tessellation to stay near the
    # two-million-triangle viewport budget.
    assert choose(410_230, 3) == 2
    # A stricter device/display budget still preserves one triangle per cell.
    assert choose(410_230, 3, max_render_triangles=1_000_000) == 1
    assert choose(0, 3) is None


def test_automatic_render_order_preserves_small_and_caps_dense_meshes():
    choose = _render_runtime.automatic_render_order

    # The original 2.9k-element p=3 mesh keeps full viewer sampling.
    assert choose(2_897, 3, has_quads=True) == 3
    # The 410k-element rerun caps order only for rendering, accounting for
    # ngsolve_webgpu's doubled order on quads.
    assert choose(410_230, 3, has_quads=True) == 1
    assert choose(410_230, 3, has_quads=False) == 2
    assert choose(0, 3, has_quads=True) == 3


def test_optional_renderer_rebuild_is_deferred_until_visible():
    from ngsolve_gui.function import FunctionComponent

    calls = []
    component = SimpleNamespace(fieldlines=None, draw=lambda: calls.append("draw"))

    assert FunctionComponent._refresh_optional_renderer(
        component, "fieldlines", False
    ) is False
    assert calls == []

    assert FunctionComponent._refresh_optional_renderer(
        component, "fieldlines", True
    ) is True
    assert calls == ["draw"]

    component.fieldlines = object()
    assert FunctionComponent._refresh_optional_renderer(
        component, "fieldlines", False
    ) is True
    assert calls == ["draw", "draw"]
