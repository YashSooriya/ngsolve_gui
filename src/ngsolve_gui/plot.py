from __future__ import annotations

import logging
import inspect
import threading

from ngapp.components import *
from ngapp.components.visualization import PlotlyComponent

from .property_panel import PropertyPanelMixin


_PICK_BRIDGE_SETUP_JS = r"""
(() => {
const graph = document.querySelector('.mmfem-3d-viewport .js-plotly-plot');
const dispatch = window[__CALLBACK_NAME__];
document.documentElement.setAttribute(
    'data-mmfem-pick-bridge-bootstrap',
    graph ? (typeof dispatch === 'function' ? 'ready' : 'no-dispatch') : 'no-graph'
);
if (!graph || typeof dispatch !== 'function') return;
if (graph._mmfemPickBridge) graph._mmfemPickBridge.dispose();
const bridge = {
    graph,
    dispatch,
    lastHover: null,
    lastPlotlyClick: -1,
    cancelled: false,
    bound: false,
    attempts: 0,
};
bridge.onHover = event => {
    const point = event && event.points && event.points[0];
    const mouse = event && event.event;
    bridge.lastHover = point ? {
        curveNumber: Number(point.curveNumber),
        pointNumber: Number(point.pointNumber),
        x: mouse && Number(mouse.clientX),
        y: mouse && Number(mouse.clientY),
        at: performance.now(),
    } : null;
};
bridge.onPlotlyClick = event => {
    bridge.lastPlotlyClick = performance.now();
    const points = event && event.points;
    if (!points) return;
    bridge.dispatch({points: points.map(point => ({
        curveNumber: Number(point.curveNumber),
        pointNumber: Number(point.pointNumber),
    }))});
};
bridge.onCanvasClick = event => {
    if (!event || !event.isTrusted || !event.target ||
        event.target.tagName !== 'CANVAS') return;
    const point = bridge.lastHover;
    const clickedAt = performance.now();
    if (!point || clickedAt - point.at > 750) return;
    if (Number.isFinite(point.x) && Math.abs(event.clientX - point.x) > 10) return;
    if (Number.isFinite(point.y) && Math.abs(event.clientY - point.y) > 10) return;
    // Let Plotly's native click handler run first. Mesh3d often emits no
    // plotly_click, while other traces may emit one and must not toggle twice.
    setTimeout(() => {
        if (bridge.cancelled || bridge.lastPlotlyClick >= clickedAt) return;
        bridge.lastPlotlyClick = performance.now();
        bridge.dispatch({points: [{
            curveNumber: point.curveNumber,
            pointNumber: point.pointNumber,
        }]});
    }, 0);
};
bridge.dispose = () => {
    if (bridge.cancelled) return;
    bridge.cancelled = true;
    if (bridge.bound) {
        try { graph.removeListener('plotly_hover', bridge.onHover); } catch (_) {}
        try { graph.removeListener('plotly_click', bridge.onPlotlyClick); } catch (_) {}
        try { graph.removeEventListener('click', bridge.onCanvasClick, true); } catch (_) {}
    }
    if (graph._mmfemPickBridge === bridge) graph._mmfemPickBridge = null;
    bridge.lastHover = null;
};
bridge.bind = () => {
    if (bridge.cancelled || bridge.bound) return;
    if (typeof graph.on === 'function' && graph._fullLayout) {
        graph.on('plotly_hover', bridge.onHover);
        graph.on('plotly_click', bridge.onPlotlyClick);
        graph.addEventListener('click', bridge.onCanvasClick, true);
        bridge.bound = true;
        document.documentElement.setAttribute('data-mmfem-pick-bridge-status', 'bound');
        return;
    }
    bridge.attempts += 1;
    if (bridge.attempts < 120) requestAnimationFrame(bridge.bind);
};
graph._mmfemPickBridge = bridge;
bridge.bind();
})();
"""


def _make_click_handler(callback, figure_index):
    try:
        parameters = inspect.signature(callback).parameters.values()
        accepts_point_number = any(
            parameter.kind == inspect.Parameter.VAR_POSITIONAL
            for parameter in parameters
        ) or sum(
            parameter.kind in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            }
            for parameter in parameters
        ) >= 3
    except (TypeError, ValueError):
        accepts_point_number = False

    return lambda curve, point_number=None: (
        callback(figure_index, curve, point_number)
        if accepts_point_number else callback(figure_index, curve)
    )


class _InteractivePlotlyComponent(PlotlyComponent):
    """Plotly canvas that can forward graph clicks to its owning component."""

    def __init__(self, *args, **kwargs):
        self._click_handler = None
        self._click_proxy = None
        self._click_bridge = None
        self._click_callback_name = f"__mmfemPickDispatch_{id(self):x}"
        self._click_listeners_bound = False
        self._click_bind_error = None
        self._render_lock = threading.Lock()
        self._render_running = False
        self._render_pending = False
        self._click_unmounted = False
        self._click_element = None
        super().__init__(*args, **kwargs)

    def set_click_handler(self, handler):
        self._click_handler = handler

    def draw(self, figure):
        """Keep the source figure once; the base class also builds a full copy."""
        self._figure = figure
        # PlotlyComponent.draw stores figure.to_dict() in ``data`` while also
        # retaining ``_figure``. Large tetrahedral surfaces can contain tens of
        # thousands of triangles, so retaining both Python representations
        # needlessly multiplies the renderer's peak memory.
        self.data = None
        self.redraw()

    def redraw(self):
        if self._click_handler is None:
            super().redraw()
            return
        if self._figure is None or self._js_component is None or self._click_unmounted:
            return
        from ngapp.components.visualization import PlotlyComponent

        if PlotlyComponent._state != "ready" or PlotlyComponent._plotly is None:
            return
        if self._click_proxy is None:
            import webgpu.platform as pl

            self._click_proxy = pl.create_proxy(
                self._dispatch_click, ignore_return_value=True
            )
        with self._render_lock:
            if self._render_running:
                self._render_pending = True
                return
            self._render_running = True
        thread = threading.Thread(
            target=self._render_interactive_queue,
            name="MMFEM-plotly-interactive-render",
            daemon=True,
        )
        thread.start()

    def _render_interactive_queue(self):
        from ngapp.components.visualization import PlotlyComponent

        while True:
            try:
                figure_data = self._themed_dict()
                PlotlyComponent._plotly._call_method(
                    "react",
                    [
                        self._js_component,
                        figure_data["data"],
                        figure_data["layout"],
                        {"responsive": True, "displaylogo": False},
                    ],
                )
                if not self._click_unmounted:
                    self.call_js(self._bind_click_handler_js)
            except Exception:
                logging.getLogger(__name__).exception(
                    "Interactive Plotly render failed"
                )
                with self._render_lock:
                    self._render_pending = False
                    self._render_running = False
                return
            with self._render_lock:
                if self._render_pending and not self._click_unmounted:
                    self._render_pending = False
                    continue
                self._render_pending = False
                self._render_running = False
                return

    def _bind_click_handler_js(self, js):
        if self._click_unmounted or self._click_listeners_bound:
            return
        graph = self._js_component
        if graph is None:
            return
        try:
            import webgpu.platform as pl

            if self._click_proxy is None:
                self._click_proxy = pl.create_proxy(
                    self._dispatch_click, ignore_return_value=True
                )
            # Plotly's gl3d Mesh3d trace emits plotly_hover with a picked face,
            # but its click event is missing in the packaged Plotly build. Keep
            # the frontend's hover pick in JavaScript and send only one small
            # click payload back to Python. This keeps pointer movement local
            # and avoids serialising Plotly's large event object through the
            # backend websocket on every hover.
            import json

            source = _PICK_BRIDGE_SETUP_JS.replace(
                "__CALLBACK_NAME__", json.dumps(self._click_callback_name)
            )
            js.window[self._click_callback_name] = self._click_proxy
            script = js.document.createElement("script")
            script.textContent = source
            js.document.head.appendChild(script)
            js.document.head.removeChild(script)
            self._click_bridge = graph._mmfemPickBridge
            if self._click_bridge is None:
                raise RuntimeError("inline 3D pick bridge did not attach to Plotly graph")
            self._click_element = graph
            self._click_listeners_bound = True
            self._click_bind_error = None
        except Exception as error:
            self._click_bind_error = repr(error)
            logging.getLogger(__name__).exception(
                "Could not bind 3D viewport interaction listeners"
            )

    def _dispatch_click(self, event):
        points = getattr(event, "points", None)
        if points is None and isinstance(event, dict):
            points = event.get("points")
        if not points:
            return
        point = points[0]
        curve_number = getattr(point, "curveNumber", None)
        if curve_number is None and isinstance(point, dict):
            curve_number = point.get("curveNumber")
        if curve_number is None or self._click_handler is None:
            return
        point_number = getattr(point, "pointNumber", None)
        if point_number is None and isinstance(point, dict):
            point_number = point.get("pointNumber")
        try:
            point_number = None if point_number is None else int(point_number)
        except (TypeError, ValueError, OverflowError):
            point_number = None
        self._click_handler(int(curve_number), point_number)

    def dispose(self):
        """Release click bridges, Python figure data and Plotly WebGL resources."""
        if self._click_unmounted:
            return
        self._click_unmounted = True
        proxy = self._click_proxy
        element = self._click_element or self._js_component
        with self._render_lock:
            self._render_pending = False

        try:
            from ngapp.components.visualization import PlotlyComponent

            while self in PlotlyComponent._waiting:
                PlotlyComponent._waiting.remove(self)
        except (AttributeError, ValueError):
            pass

        if proxy is not None or element is not None:
            def cleanup(js):
                if element is not None:
                    try:
                        if proxy is not None:
                            element._call_method_ignore_return(
                                "removeListener", ["plotly_click", proxy]
                            )
                        bridge = element._mmfemPickBridge
                        if bridge is not None:
                            bridge._call_method_ignore_return("dispose")
                    except Exception:
                        pass
                    try:
                        from ngapp.components.visualization import PlotlyComponent

                        plotly = PlotlyComponent._plotly
                        if plotly is not None:
                            plotly._call_method_ignore_return("purge", [element])
                    except Exception:
                        pass

            try:
                self.call_js(cleanup)
            except Exception:
                pass
        try:
            def remove_dispatch(js):
                js.window[self._click_callback_name] = None

            self.call_js(remove_dispatch)
        except Exception:
            pass
        try:
            import webgpu.platform as pl

            if proxy is not None:
                pl.destroy_proxy(proxy)
        except Exception:
            pass
        self._click_proxy = None
        self._click_bridge = None
        self._click_element = None
        self._click_listeners_bound = False
        self._click_handler = None
        self._figure = None
        self.data = None

    def _on_unmount(self):
        self.dispose()
        super()._on_unmount()


class PlotComponent(PropertyPanelMixin, Div):
    def __init__(self, name, data, app_data, *, on_plotly_click=None):
        self.name = name
        self.data = data
        self.app_data = app_data
        self._on_plotly_click = on_plotly_click
        self.icon = "mdi-chart-line"
        self._figures = []
        self._plots = []
        self.debug = bool(data.get("debug", False)) if isinstance(data, dict) else False
        self.container = Div(
            ui_class="fit scroll",
            ui_style="width:100%; height:100%; min-width:0; min-height:0;",
        )
        super().__init__(
            self.container,
            ui_class="fit",
        )
        self.on_mounted(self.draw)  # defer until mounted

    @property
    def title(self):
        return self.app_data.get_tab(self.name)["title"]

    def property_subtitle(self):
        return "Plot"

    def _normalize_figures(self, obj):
        if obj is None:
            return []
        if isinstance(obj, (list, tuple)):
            figures = []
            for item in obj:
                if isinstance(item, (list, tuple)):
                    figures.extend(item)
                else:
                    figures.append(item)
            return figures
        return [obj]

    def _to_plotly(self, fig):
        try:
            import plotly.graph_objects as go

            if isinstance(fig, go.Figure):
                return fig
            if isinstance(fig, dict):
                return go.Figure(fig)
            if hasattr(fig, "to_plotly_json"):
                return go.Figure(fig.to_plotly_json())
        except Exception:
            pass

        try:
            import matplotlib.figure
            import matplotlib.axes

            if isinstance(fig, matplotlib.axes.Axes):
                fig = fig.figure
            if isinstance(fig, matplotlib.figure.Figure):
                try:
                    from plotly.io import from_matplotlib
                except Exception:
                    from plotly.tools import mpl_to_plotly

                    return mpl_to_plotly(fig)

                return from_matplotlib(fig)
        except Exception:
            pass

        return None

    def draw(self):
        obj = self.data.get("obj") if isinstance(self.data, dict) else self.data
        figures = self._normalize_figures(obj)

        for previous_plot in self._plots:
            dispose = getattr(previous_plot, "dispose", None)
            if callable(dispose):
                dispose()
        self._figures = []
        self._plots = []
        plot_height = "100%" if len(figures) == 1 else "480px"
        if self.debug:
            print(f"[PlotComponent] figures={len(figures)}")
        for idx, fig in enumerate(figures):
            pfig = self._to_plotly(fig)
            if pfig is None:
                continue
            self._figures.append(pfig)
            plot = _InteractivePlotlyComponent(id=f"{self.name}_plot_{idx}")
            if self._on_plotly_click is not None:
                plot.set_click_handler(
                    _make_click_handler(self._on_plotly_click, idx)
                )
            plot.ui_class = "full-width"
            plot.ui_style = (
                f"width:100%; min-width:0; height:{plot_height};"
            )  # height is computed per figure
            # Keep the latest figure on the plot component before it mounts.
            # A project can change while this child is being attached (for
            # example, a STEP preview finishing before a wire-trace import).
            # Capturing ``pfig`` in the mount callback would then redraw the
            # stale first figure over the newer one.
            plot.draw(pfig)
            plot.on_mounted(lambda _=None, plot=plot: plot.redraw())
            self._plots.append(plot)

        if not self._plots:
            self.container.ui_children = [
                Div("No supported plot data.", ui_class="cb-muted")
            ]
        else:
            self.container.ui_children = self._plots

    def redraw(self):
        if not self._figures or not self._plots:
            self.draw()
            return
        for plot, fig in zip(self._plots, self._figures):
            plot.draw(fig)

    def update_figure(self, figure):
        """Update a single scene while keeping its WebGL canvas and camera alive."""
        self.data = {"obj": figure}
        if len(self._plots) == 1 and len(self._figures) == 1:
            self._figures = [figure]
            self._plots[0].draw(figure)
        else:
            self.draw()

    def clear(self):
        """Clear a scene and release its WebGL context and retained arrays."""
        for plot in self._plots:
            dispose = getattr(plot, "dispose", None)
            if callable(dispose):
                dispose()
        self._plots = []
        self._figures = []
        self.data = {"obj": []}
        self.container.ui_children = []

    def set_component(self, comp):
        self.ui_children = [comp]


# Register with the component registry
from .registry import register_component

register_component(
    "plot",
    icon="mdi-chart-line",
    component_class=PlotComponent,
)
