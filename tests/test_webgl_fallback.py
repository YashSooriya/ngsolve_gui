from __future__ import annotations

import sys
import os
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
from playwright.sync_api import Page

from ngapp.e2e import app_test

from ngsolve_gui import webgl_fallback as fallback
from .helpers import _draw, make_geometry


@pytest.fixture(autouse=True)
def _use_cached_frontend_cache_headers(monkeypatch):
    """Let the packaged ngapp frontend start without its optional header helper."""
    try:
        available = importlib.util.find_spec("frontend_cache") is not None
    except (ImportError, ValueError):
        available = False
    if available:
        return
    shim_dir = Path(__file__).parent / "fixtures"
    monkeypatch.syspath_prepend(str(shim_dir))
    inherited = os.environ.get("PYTHONPATH", "")
    paths = [str(shim_dir)] + ([inherited] if inherited else [])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    # This VM already carries the version-matched ngapp frontend archive; avoid
    # a network lookup while the local UI runner starts its static server.
    monkeypatch.setenv("NGAPP_FRONTEND_CACHE_ONLY", "1")


def _assert_has_3d_surface(figure):
    assert figure.data
    mesh = next(trace for trace in figure.data if trace.type == "mesh3d")
    assert len(mesh.i) == len(mesh.j) == len(mesh.k)
    assert len(mesh.x) == len(mesh.y) == len(mesh.z)
    assert np.isfinite(np.asarray(mesh.x, dtype=float)).all()
    assert np.isfinite(np.asarray(mesh.y, dtype=float)).all()
    assert np.isfinite(np.asarray(mesh.z, dtype=float)).all()


def test_geometry_fallback_creates_interactive_3d_surface():
    from netgen.occ import Box, OCCGeometry, Pnt

    geometry = OCCGeometry(Box(Pnt(0, 0, 0), Pnt(1, 2, 3)))
    figure = fallback.geometry_figure(geometry.shape)

    _assert_has_3d_surface(figure)
    assert figure.layout.scene.aspectmode == "data"


def test_mesh_fallback_displays_3d_boundary():
    import ngsolve as ngs

    mesh = ngs.Mesh(ngs.unit_cube.GenerateMesh(maxh=0.8))
    figure = fallback.mesh_figure(mesh)

    _assert_has_3d_surface(figure)
    assert any(trace.type == "scatter3d" for trace in figure.data)


def test_function_fallback_colors_surface_from_real_field_values():
    import ngsolve as ngs

    mesh = ngs.Mesh(ngs.unit_cube.GenerateMesh(maxh=0.8))
    cf = ngs.x + 2 * ngs.y + 3 * ngs.z
    comp = SimpleNamespace(
        mesh=mesh,
        cf=cf,
        visualization_cf=cf,
        axisymmetric_revolution_available=False,
        colormap_name=SimpleNamespace(value="viridis"),
        complex_mode=SimpleNamespace(value="real"),
        name="Manufactured field",
    )

    figure = fallback.function_figure(comp)

    _assert_has_3d_surface(figure)
    surface = next(trace for trace in figure.data if trace.type == "mesh3d")
    assert surface.intensity is not None
    assert min(surface.intensity) >= -1e-12
    assert max(surface.intensity) > 1
    assert figure.layout.meta["renderer"] == "webgl"


def test_function_fallback_uses_vector_magnitude():
    import ngsolve as ngs

    mesh = ngs.Mesh(ngs.unit_cube.GenerateMesh(maxh=0.8))
    vector_field = ngs.CF((ngs.x, 2 * ngs.y, 3 * ngs.z))
    comp = SimpleNamespace(
        mesh=mesh,
        cf=vector_field,
        visualization_cf=vector_field,
        axisymmetric_revolution_available=False,
        colormap_name=SimpleNamespace(value="viridis"),
        complex_mode=SimpleNamespace(value="real"),
        name="Vector field",
    )

    figure = fallback.function_figure(comp)

    surface = next(trace for trace in figure.data if trace.type == "mesh3d")
    assert max(surface.intensity) > 3.0


def test_axisymmetric_function_fallback_revolves_to_3d():
    import ngsolve as ngs

    meridian = ngs.Mesh(ngs.unit_square.GenerateMesh(maxh=0.8))
    space = ngs.H1(meridian, order=1)
    field = ngs.GridFunction(space, name="axisymmetric scalar")
    field.Set(ngs.x + ngs.y)
    comp = SimpleNamespace(
        mesh=meridian,
        cf=field,
        visualization_cf=field,
        axisymmetric_revolution_available=True,
        _axisymmetric_original_state={"cf": field},
        colormap_name=SimpleNamespace(value="viridis"),
        complex_mode=SimpleNamespace(value="real"),
        name="Axisymmetric field",
    )

    figure = fallback.function_figure(comp)

    _assert_has_3d_surface(figure)
    assert figure.layout.meta["axisymmetric_revolved"] is True
    surface = next(trace for trace in figure.data if trace.type == "mesh3d")
    assert max(surface.z) - min(surface.z) > 0


def test_browser_probe_hook_uses_bridge_compatible_name():
    appended = []

    class Head:
        def appendChild(self, script):
            appended.append(script)

    document = SimpleNamespace(
        createElement=lambda _tag: SimpleNamespace(textContent=""), head=Head()
    )
    js = SimpleNamespace(document=document, mmfemProbeWebGPU=None)

    fallback._install_verified_probe(js)

    assert len(appended) == 1
    assert "globalThis.mmfemProbeWebGPU" in appended[0].textContent
    assert "__mmfemProbeWebGPU" not in appended[0].textContent
    assert ".catch(error => ({" in appended[0].textContent
    assert "5000" in appended[0].textContent


def _fake_webgpu(monkeypatch, *, gpu=True, selected=None, selector_error=None):
    package = ModuleType("webgpu")
    utils = SimpleNamespace(_device=None, ensure_engine_js=lambda: True)
    platform = SimpleNamespace()
    js = SimpleNamespace(
        navigator=SimpleNamespace(gpu=object() if gpu else None),
        webgpuSelectDevice=lambda *_args: None,
    )
    if selected is not None:
        js.mmfemProbeWebGPU = lambda canvas: selected
    elif selector_error is not None:
        def fail(_canvas):
            raise selector_error
        js.mmfemProbeWebGPU = fail
    platform.js = js
    package.platform = platform
    package.utils = utils
    api = ModuleType("webgpu.webgpu_api")

    class Device:
        def __init__(self, handle):
            self.handle = handle

    api.Device = Device
    monkeypatch.setitem(sys.modules, "webgpu", package)
    monkeypatch.setitem(sys.modules, "webgpu.webgpu_api", api)
    monkeypatch.setattr(fallback, "_install_verified_probe", lambda _js: None)
    fallback.reset_webgpu_probe_state()
    return utils, js, Device


def test_webgpu_probe_failure_becomes_normal_unavailable_result(monkeypatch):
    utils, js, _device = _fake_webgpu(monkeypatch, gpu=False)

    with pytest.raises(fallback.WebGPUUnavailable, match="no WebGPU interface"):
        fallback.ensure_webgpu_device(object())

    assert utils._device is None
    assert fallback.webgpu_is_unavailable()


def test_webgpu_probe_requires_successful_canvas_render(monkeypatch):
    device_handle = object()
    selected = SimpleNamespace(
        verdict="probed", device=device_handle, messages=["low-power adapter verified"]
    )
    utils, js, device_type = _fake_webgpu(monkeypatch, selected=selected)
    canvas = object()

    device = fallback.ensure_webgpu_device(canvas)

    assert isinstance(device, device_type)
    assert device.handle is device_handle
    assert utils._device is device
    assert fallback.webgpu_is_unavailable() is False


def test_preexisting_unverified_device_is_probed_before_reuse(monkeypatch):
    selected = SimpleNamespace(
        verdict="probed", device=object(), messages=["triangle probe passed"]
    )
    utils, _js, device_type = _fake_webgpu(monkeypatch, selected=selected)
    stale_device = object()
    utils._device = stale_device

    device = fallback.ensure_webgpu_device(object())

    assert isinstance(device, device_type)
    assert device is utils._device
    assert device is not stale_device


def test_unverified_or_failed_probe_selects_fallback(monkeypatch):
    selected = SimpleNamespace(
        verdict="unverified", device=object(), messages=["probe timed out"]
    )
    _utils, _js, _device = _fake_webgpu(monkeypatch, selected=selected)

    with pytest.raises(fallback.WebGPUUnavailable, match="probe timed out"):
        fallback.ensure_webgpu_device(object())

    assert fallback.webgpu_is_unavailable()


def test_adaptive_component_switches_owner_without_propagating_probe_error(monkeypatch):
    calls = []
    component = fallback.AdaptiveWebgpuComponent.__new__(
        fallback.AdaptiveWebgpuComponent
    )
    component._fallback_owner = SimpleNamespace(
        _activate_webgl_fallback=lambda reason: calls.append(reason)
    )
    component._js_component = object()
    component.canvas = None
    component.scene = None
    monkeypatch.setattr(
        fallback,
        "ensure_webgpu_device",
        lambda _canvas: (_ for _ in ()).throw(
            fallback.WebGPUUnavailable("no adapter")
        ),
    )

    component.connect_webgpu()

    assert calls == ["no adapter"]
    assert fallback.webgpu_is_unavailable()


@app_test("ngsolve_gui.appconfig")
def test_present_webgpu_interface_without_adapter_uses_fallback(
    page: Page, app
) -> None:
    """A present API that cannot supply any adapter must fall back cleanly."""
    from webgpu import utils

    fallback.reset_webgpu_probe_state()
    utils._device = None
    page.evaluate(
        """Object.defineProperty(navigator, 'gpu', {
          value: {requestAdapter: async () => null}, configurable: true
        })"""
    )
    assert page.evaluate("navigator.gpu !== undefined")

    _draw(app, make_geometry(), page=page, name="NoAvailableAdapter")
    component = app.tab_panel.comp
    page.wait_for_function(
        "document.body.innerText.includes('No working WebGPU adapter')",
        timeout=10000,
    )
    assert component._webgl_fallback_active
    assert "adapter" in component._webgl_fallback_reason.casefold()
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('.js-plotly-plot'))
          .some(plot => plot.data?.some(trace => trace.type === 'mesh3d'))""",
        timeout=15000,
    )


@app_test("ngsolve_gui.appconfig")
def test_real_geometry_view_uses_webgl_when_browser_has_no_webgpu(
    page: Page, app
) -> None:
    """Exercise real geometry, mesh and field views without a WebGPU adapter."""
    page.evaluate(
        "Object.defineProperty(navigator, 'gpu', {value: undefined, configurable: true})"
    )
    assert page.evaluate("navigator.gpu === undefined")

    _draw(app, make_geometry(), page=page, name="NoWebGPUGeometry")
    component = app.tab_panel.comp
    page.wait_for_function(
        "document.body.innerText.includes('3D WebGL fallback')", timeout=10000
    )

    assert component._webgl_fallback_active
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('.js-plotly-plot canvas')).some(canvas => {
            const rect = canvas.getBoundingClientRect();
            return canvas.width > 0 && canvas.height > 0 && rect.width > 0 && rect.height > 0;
        })""",
        timeout=15000,
    )
    assert page.get_by_text("3D WebGL fallback", exact=False).count() > 0
    page.screenshot(
        path=str(Path(__file__).parent / "output" / "webgl-fallback-geometry.png"),
        full_page=True,
    )

    import ngsolve as ngs

    from .helpers import make_mesh_3d

    mesh = make_mesh_3d()
    _draw(app, mesh, page=page, name="NoWebGPUMesh")
    mesh_component = app.tab_panel.comp
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('.js-plotly-plot'))
          .some(plot => plot.getBoundingClientRect().width > 0
            && plot.data?.some(trace => trace.type === 'mesh3d'))""",
        timeout=15000,
    )
    assert mesh_component._webgl_fallback_active

    space = ngs.H1(mesh, order=1)
    field = ngs.GridFunction(space, name="No WebGPU field")
    field.Set(ngs.x + 2 * ngs.y + 3 * ngs.z)
    _draw(app, field, page=page, name="NoWebGPUField")
    field_component = app.tab_panel.comp
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('.js-plotly-plot'))
          .some(plot => plot.getBoundingClientRect().width > 0
            && plot.data?.some(trace => trace.type === 'mesh3d'
              && trace.intensity?.length > 0))""",
        timeout=15000,
    )
    assert field_component._webgl_fallback_active
    page.screenshot(
        path=str(Path(__file__).parent / "output" / "webgl-fallback-field.png"),
        full_page=True,
    )



@app_test("ngsolve_gui.appconfig")
def test_hung_webgpu_adapter_probe_uses_fallback_within_bound(page: Page, app) -> None:
    """A present-but-stalled WebGPU API must not leave the viewport waiting."""
    page.evaluate(
        """Object.defineProperty(navigator, 'gpu', {
          value: {requestAdapter: () => new Promise(() => {})},
          configurable: true
        })"""
    )
    _draw(app, make_geometry(), page=page, name="StalledWebGPUProbe")
    component = app.tab_panel.comp
    page.wait_for_function(
        "document.body.innerText.includes('adapter/render probe timed out')",
        timeout=10000,
    )
    assert component._webgl_fallback_active
    assert "timed out" in component._webgl_fallback_reason
