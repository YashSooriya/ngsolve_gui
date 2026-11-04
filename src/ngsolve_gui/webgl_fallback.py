"""Automatic WebGPU detection and display-only WebGL 3D fallbacks.

The numerical model and solver remain unchanged.  When a mounted WebGPU view
cannot obtain a verified adapter, the corresponding view is represented with
Plotly's WebGL renderer instead.  Field fallbacks show the field on the outer
surface; axisymmetric result fields are revolved so they remain genuinely 3D.
"""

from __future__ import annotations

import threading
from ngapp.components.visualization import WebgpuComponent


class WebGPUUnavailable(RuntimeError):
    """The browser cannot provide a verified WebGPU render device."""


_probe_lock = threading.RLock()
_probe_state: str | None = None
_probe_error = ""
_verified_device = None
_WEBGPU_PROBE_TIMEOUT_MS = 5_000
_MAX_FALLBACK_TRIANGLES = 100_000


def reset_webgpu_probe_state() -> None:
    """Reset the process cache; used by isolated GUI tests."""
    global _probe_state, _probe_error, _verified_device
    with _probe_lock:
        _probe_state = None
        _probe_error = ""
        _verified_device = None


def _record_unavailable(error) -> None:
    global _probe_state, _probe_error, _verified_device
    _probe_state = "unavailable"
    _probe_error = _short_reason(error)
    _verified_device = None


def _short_reason(error, limit=180) -> str:
    text = str(error).strip().replace("\n", " ") or type(error).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _install_verified_probe(js) -> None:
    """Install a small wrapper around webgpuSelectDevice.

    Upstream remembers a previously selected adapter in localStorage and then
    skips its render probe on later launches.  Temporarily removing that hint
    makes each new application process verify the current machine/canvas; the
    prior preference is restored if verification fails.
    """
    if getattr(js, "mmfemProbeWebGPU", None) is not None:
        return
    script = js.document.createElement("script")
    script.textContent = r"""
      globalThis.mmfemProbeWebGPU = async function(canvas) {
        const key = 'webgpuPowerPreference';
        let previous = null;
        let hadPrevious = false;
        try {
          previous = localStorage.getItem(key);
          hadPrevious = previous !== null;
          localStorage.removeItem(key);
        } catch (_) {}
        let verified = false;
        let timedOut = false;
        let timer = null;
        const restorePreference = () => {
          if (hadPrevious) {
            try { localStorage.setItem(key, previous); } catch (_) {}
          }
        };
        const probe = Promise.resolve().then(() => webgpuSelectDevice(canvas, null));
        try {
          const timeout = new Promise(resolve => {
            timer = setTimeout(() => {
              timedOut = true;
              resolve({
                verdict: 'unverified',
                device: null,
                messages: ['WebGPU adapter/render probe timed out after 5 seconds'],
              });
            }, __MMFEM_PROBE_TIMEOUT_MS__);
          });
          const selected = await Promise.race([probe, timeout]);
          verified = selected.verdict === 'probed';
          if (timedOut) {
            // requestAdapter/requestDevice may never settle on a broken driver.
            // If they eventually do, dispose the unused device; the page is
            // already using the WebGL fallback by then.
            probe.then(result => {
              try { result.device?.destroy(); } catch (_) {}
            }, () => {});
          }
          return selected;
        } finally {
          if (timer !== null) clearTimeout(timer);
          if (!verified) restorePreference();
        }
      };
    """.replace("__MMFEM_PROBE_TIMEOUT_MS__", str(_WEBGPU_PROBE_TIMEOUT_MS))
    js.document.head.appendChild(script)


def ensure_webgpu_device(html_canvas):
    """Return a device only after an actual canvas render probe succeeds.

    This deliberately avoids ``utils.init_device_sync``'s alert/``sys.exit``
    failure path.  The same upstream adapter chooser and triangle probe are
    used, but an unavailable or inconclusive adapter is reported as a normal
    application condition so the caller can switch renderers.
    """
    global _probe_state, _verified_device
    from webgpu import platform, utils

    with _probe_lock:
        if _probe_state == "unavailable":
            raise WebGPUUnavailable(_probe_error or "WebGPU is unavailable")
        if (
            _probe_state == "available"
            and utils._device is not None
            and utils._device is _verified_device
        ):
            # The process-local device has already passed the real canvas
            # triangle probe.  A device populated by another initializer is
            # deliberately not trusted here: upstream can select one without
            # a canvas and return it as "unprobed".
            return utils._device

        try:
            js = platform.js
            if js is None or not bool(js.navigator.gpu):
                raise WebGPUUnavailable("This browser has no WebGPU interface")
            if not utils.ensure_engine_js() or not getattr(
                js, "webgpuSelectDevice", None
            ):
                raise WebGPUUnavailable("The WebGPU renderer could not be loaded")
            _install_verified_probe(js)
            selected = js.mmfemProbeWebGPU(html_canvas)
            verdict = str(selected.verdict)
            messages = [str(message) for message in selected.messages]
            if verdict != "probed":
                detail = "; ".join(messages)
                try:
                    selected.device.destroy()
                except Exception:
                    pass
                raise WebGPUUnavailable(
                    detail or f"WebGPU render probe was {verdict}"
                )

            from webgpu.webgpu_api import Device

            device = Device(selected.device)
            # Match the upstream cache so Canvas construction reuses the
            # verified device.  If a previous initializer populated an
            # unverified device, replace it before any view is attached.
            utils._device = device
            _verified_device = device
            _probe_state = "available"
            return device
        except (Exception, SystemExit) as error:
            _record_unavailable(error)
            if isinstance(error, WebGPUUnavailable):
                raise
            raise WebGPUUnavailable(_probe_error) from error


def webgpu_is_unavailable() -> bool:
    return _probe_state == "unavailable"


def webgpu_failure_reason() -> str:
    return _probe_error


class AdaptiveWebgpuComponent(WebgpuComponent):
    """A WebGPU canvas that switches its owner to WebGL when probing fails."""

    def __init__(self, owner, **kwargs):
        self._fallback_owner = owner
        super().__init__(**kwargs)

    def connect_webgpu(self):
        try:
            if not getattr(self, "canvas", None):
                ensure_webgpu_device(self._js_component)
            WebgpuComponent.connect_webgpu(self)
        except (Exception, SystemExit) as error:
            _record_unavailable(error)
            self._release_partial_webgpu()
            self._fallback_owner._activate_webgl_fallback(_probe_error)

    def _release_partial_webgpu(self):
        scene = getattr(self, "scene", None)
        canvas = getattr(self, "canvas", None)
        if scene is not None:
            try:
                scene.cleanup()
            except Exception:
                pass
        if canvas is not None:
            try:
                canvas.update_html_canvas(None)
            except Exception:
                pass
        self.canvas = None
        observer = getattr(self, "_theme_observer", None)
        self._theme_observer = None
        if observer is not None:
            try:
                observer.disconnect()
            except Exception:
                pass
        proxy = getattr(self, "_theme_proxy", None)
        self._theme_proxy = None
        if proxy is not None:
            try:
                proxy.destroy()
            except Exception:
                pass


def _figure_layout(fig, revision):
    fig.update_layout(
        autosize=True,
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        showlegend=False,
        uirevision=revision,
        scene={
            "aspectmode": "data",
            "dragmode": "orbit",
            "xaxis_title": "X",
            "yaxis_title": "Y",
            "zaxis_title": "Z",
            "bgcolor": "rgba(0,0,0,0)",
        },
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
    )
    return fig
def _mesh_vertices(mesh):
    import numpy as np

    points = []
    for vertex in mesh.vertices:
        point = tuple(float(value) for value in mesh[vertex].point)
        if len(point) == 2:
            point += (0.0,)
        points.append(point[:3])
    if not points:
        raise ValueError("The mesh contains no vertices to display")
    vertices = np.asarray(points, dtype=np.float64)
    if not np.isfinite(vertices).all():
        raise ValueError("The mesh has non-finite display coordinates")
    return vertices


def _surface_triangles(mesh):
    import ngsolve as ngs
    import numpy as np

    kind = ngs.BND if int(mesh.dim) == 3 else ngs.VOL
    rows = []
    if int(mesh.dim) == 1:
        segments = []
        for element in mesh.Elements(kind):
            ids = [int(vertex.nr) for vertex in element.vertices]
            if len(ids) == 2:
                segments.append(ids)
        if not segments:
            raise ValueError("The mesh contains no line elements to display")
        return np.empty((0, 3), dtype=np.int32), np.asarray(segments, dtype=np.int32)

    for element in mesh.Elements(kind):
        ids = [int(vertex.nr) for vertex in element.vertices]
        if len(ids) < 3:
            continue
        for index in range(1, len(ids) - 1):
            rows.append((ids[0], ids[index], ids[index + 1]))
    if not rows:
        raise ValueError("The mesh contains no displayable surface elements")
    triangles = np.asarray(rows, dtype=np.int32)
    if triangles.min() < 0 or triangles.max() >= len(mesh.vertices):
        raise ValueError("The mesh contains invalid surface connectivity")
    return triangles, np.empty((0, 2), dtype=np.int32)


def _reduce_surface(vertices, triangles, limit=_MAX_FALLBACK_TRIANGLES):
    """Bound Plotly payload size with topology-preserving vertex clustering."""
    import numpy as np

    if len(triangles) <= limit:
        return vertices, triangles
    points = vertices[triangles]
    cross = np.cross(points[:, 1] - points[:, 0], points[:, 2] - points[:, 0])
    area = float(np.sum(np.linalg.norm(cross, axis=1)) * 0.5)
    spacing = max((area / limit) ** 0.5, float(np.ptp(vertices, axis=0).max()) * 1e-7)
    origin = vertices.min(axis=0)
    for _ in range(8):
        cell = np.floor((vertices - origin) / spacing).astype(np.int64)
        _cells, inverse = np.unique(cell, axis=0, return_inverse=True)
        reduced_vertices = np.zeros((int(inverse.max()) + 1, 3), dtype=np.float64)
        counts = np.bincount(inverse, minlength=len(reduced_vertices))
        np.add.at(reduced_vertices, inverse, vertices)
        reduced_vertices /= counts[:, None]
        reduced_triangles = inverse[triangles]
        keep = (
            (reduced_triangles[:, 0] != reduced_triangles[:, 1])
            & (reduced_triangles[:, 1] != reduced_triangles[:, 2])
            & (reduced_triangles[:, 2] != reduced_triangles[:, 0])
        )
        reduced_triangles = reduced_triangles[keep]
        if len(reduced_triangles) <= limit:
            return reduced_vertices, reduced_triangles
        spacing *= max(1.15, (len(reduced_triangles) / limit) ** 0.5 * 1.02)
    # Last-resort bounded subset after clustering. This path is only reached by
    # exceptionally dense, highly nonuniform surface meshes.
    stride = max(1, (len(reduced_triangles) + limit - 1) // limit)
    return reduced_vertices, reduced_triangles[::stride]


def _add_surface_edges(fig, vertices, triangles, *, max_edges=120_000):
    import numpy as np
    import plotly.graph_objects as go

    if not len(triangles):
        return
    edges = np.concatenate(
        (triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]),
        axis=0,
    )
    edges.sort(axis=1)
    edges = np.unique(edges, axis=0)
    if len(edges) > max_edges:
        edges = edges[:: (len(edges) + max_edges - 1) // max_edges]
    coords = vertices[edges].reshape((-1, 2, 3))
    separated = np.empty((len(coords), 3, 3), dtype=object)
    separated[:, :2, :] = coords
    separated[:, 2, :] = None
    flat = separated.reshape((-1, 3))
    fig.add_trace(go.Scatter3d(
        x=flat[:, 0].tolist(), y=flat[:, 1].tolist(), z=flat[:, 2].tolist(),
        mode="lines", line={"color": "#25384d", "width": 1},
        hoverinfo="skip", showlegend=False,
    ))


def geometry_figure(shape):
    """Return a real 3D WebGL surface for an OCC shape."""
    import numpy as np
    import plotly.graph_objects as go

    from .three_d_preview import tessellate_occ_feature_edges, tessellate_occ_shape

    payload = tessellate_occ_shape(shape, max_triangles=400_000)
    vertices, triangles = _reduce_surface(payload["vertices"], payload["triangles"])
    fig = go.Figure()
    fig.add_trace(go.Mesh3d(
        x=vertices[:, 0].tolist(), y=vertices[:, 1].tolist(), z=vertices[:, 2].tolist(),
        i=triangles[:, 0].tolist(), j=triangles[:, 1].tolist(), k=triangles[:, 2].tolist(),
        name="Geometry", color="#7895b5", flatshading=False,
        lighting={"ambient": 0.7, "diffuse": 0.85, "specular": 0.08, "roughness": 0.8},
        hoverinfo="name", showscale=False,
    ))
    try:
        edges = tessellate_occ_feature_edges(shape, max_segments=50_000)
        if len(edges):
            separated = np.empty((len(edges), 3, 3), dtype=object)
            separated[:, :2, :] = edges
            separated[:, 2, :] = None
            flat = separated.reshape((-1, 3))
            fig.add_trace(go.Scatter3d(
                x=flat[:, 0].tolist(), y=flat[:, 1].tolist(), z=flat[:, 2].tolist(),
                mode="lines", line={"color": "#25384d", "width": 1.5},
                hoverinfo="skip", showlegend=False,
            ))
    except Exception:
        # Faces remain a complete and useful preview when OCC edge tessellation
        # is unavailable or exceeds its display bound.
        pass
    return _figure_layout(fig, "mmfem-webgl-geometry")


def mesh_figure(mesh):
    """Display mesh surfaces/facets through WebGL with a bounded payload."""
    import numpy as np
    import plotly.graph_objects as go

    vertices = _mesh_vertices(mesh)
    triangles, segments = _surface_triangles(mesh)
    fig = go.Figure()
    if len(triangles):
        vertices, triangles = _reduce_surface(vertices, triangles)
        fig.add_trace(go.Mesh3d(
            x=vertices[:, 0].tolist(), y=vertices[:, 1].tolist(), z=vertices[:, 2].tolist(),
            i=triangles[:, 0].tolist(), j=triangles[:, 1].tolist(), k=triangles[:, 2].tolist(),
            name="Mesh surface", color="#91a9c1", flatshading=True,
            lighting={"ambient": 0.8, "diffuse": 0.75, "specular": 0.0, "roughness": 1.0},
            hoverinfo="name", showscale=False,
        ))
        _add_surface_edges(fig, vertices, triangles)
    elif len(segments):
        separated = np.empty((len(segments), 3, 3), dtype=object)
        separated[:, :2, :] = vertices[segments]
        separated[:, 2, :] = None
        flat = separated.reshape((-1, 3))
        fig.add_trace(go.Scatter3d(
            x=flat[:, 0].tolist(), y=flat[:, 1].tolist(), z=flat[:, 2].tolist(),
            mode="lines", line={"color": "#6886ac", "width": 3},
            hoverinfo="skip", showlegend=False,
        ))
    if not fig.data:
        raise ValueError("The mesh has no geometry that can be shown in 3D")
    return _figure_layout(fig, "mmfem-webgl-mesh")


def _field_intensity(comp, mesh, vertices, cf):
    import numpy as np

    coordinates = [vertices[:, axis] for axis in range(int(mesh.dim))]
    points = mesh(*coordinates)
    values = np.asarray(cf(points))
    if values.ndim == 1:
        values = values.reshape((-1, 1))
    else:
        values = values.reshape((len(vertices), -1))
    if len(values) != len(vertices):
        raise ValueError("The field samples do not match the display mesh")

    mode = getattr(getattr(comp, "complex_mode", None), "value", "real")
    if np.iscomplexobj(values):
        if mode == "imag":
            values = values.imag
        elif mode == "abs":
            values = np.abs(values)
        elif mode == "arg":
            values = np.angle(values)
        else:
            values = values.real
    else:
        values = values.astype(np.float64, copy=False)

    if values.shape[1] > 1:
        intensity = np.linalg.norm(values, axis=1)
        label = "field magnitude"
    else:
        intensity = values[:, 0]
        label = {"real": "real part", "imag": "imaginary part", "abs": "magnitude", "arg": "phase"}.get(mode, "field value")
    if not np.isfinite(intensity).all():
        raise ValueError("The field contains non-finite display values")
    return intensity.astype(np.float64), label


def function_figure(comp):
    """Show field magnitude/selected complex component on a 3D WebGL surface."""
    import numpy as np
    import plotly.graph_objects as go

    mesh = comp.mesh
    cf = comp.visualization_cf
    revolved = False
    if int(mesh.dim) == 2 and getattr(comp, "axisymmetric_revolution_available", False):
        original = getattr(comp, "_axisymmetric_original_state", None) or {}
        source = original.get("cf", comp.cf)
        import ngsolve as ngs
        if not isinstance(source, ngs.GridFunction):
            raise ValueError("This axisymmetric result cannot be revolved for 3D display")
        from .axisymmetric_revolution import revolve_gridfunction
        mesh, revolved_field = revolve_gridfunction(source, sectors=16)
        from ._visualization import visualization_cf
        cf = visualization_cf(revolved_field)
        revolved = True

    vertices = _mesh_vertices(mesh)
    triangles, _segments = _surface_triangles(mesh)
    vertices, triangles = _reduce_surface(vertices, triangles)
    intensity, label = _field_intensity(comp, mesh, vertices, cf)

    colorscale = {
        "rainbow": "Rainbow",
        "turbo": "Turbo",
        "viridis": "Viridis",
        "plasma": "Plasma",
        "cet_l20": "Viridis",
        "matlab:jet": "Jet",
        "matplotlib:coolwarm": "RdBu",
    }.get(str(getattr(comp.colormap_name, "value", "viridis")).lower(), "Viridis")
    lo = float(np.min(intensity))
    hi = float(np.max(intensity))
    if hi <= lo:
        hi = lo + max(abs(lo), 1.0) * 1e-12
    fig = go.Figure(go.Mesh3d(
        x=vertices[:, 0].tolist(), y=vertices[:, 1].tolist(), z=vertices[:, 2].tolist(),
        i=triangles[:, 0].tolist(), j=triangles[:, 1].tolist(), k=triangles[:, 2].tolist(),
        intensity=intensity.tolist(), intensitymode="vertex",
        colorscale=colorscale, cmin=lo, cmax=hi,
        colorbar={"title": {"text": label}},
        name=getattr(comp, "name", "Field"), flatshading=False,
        lighting={"ambient": 0.75, "diffuse": 0.8, "specular": 0.05, "roughness": 0.8},
        hovertemplate=f"{label}: %{{intensity:.5g}}<extra></extra>",
    ))
    _add_surface_edges(fig, vertices, triangles, max_edges=60_000)
    fig.update_layout(coloraxis_showscale=True)
    fig = _figure_layout(fig, "mmfem-webgl-field")
    # Clarify the representation in metadata for diagnostics and automated UI checks.
    fig.update_layout(meta={"renderer": "webgl", "surface_field": True, "axisymmetric_revolved": revolved})
    return fig
