from types import SimpleNamespace

from ngsolve_gui.plot import _InteractivePlotlyComponent
from ngapp.components.visualization import PlotlyComponent


def _bare_plot(monkeypatch):
    plot = _InteractivePlotlyComponent.__new__(_InteractivePlotlyComponent)
    plot._figure = None
    plot.data = {"previous": "copy"}
    plot._js_component = None
    plot._click_handler = None
    plot._click_proxy = None
    plot._click_bridge = None
    plot._click_listeners_bound = False
    plot._click_bind_error = None
    plot._click_element = None
    plot._render_lock = __import__("threading").Lock()
    plot._render_running = False
    plot._render_pending = False
    plot._click_unmounted = False
    plot._theme_observer = None
    plot._theme_mql = None
    plot._theme_proxy = None
    plot.call_js = lambda callback: callback(None)
    if monkeypatch is not None:
        monkeypatch.setattr(plot, "redraw", lambda: None)
    return plot


def test_interactive_plot_does_not_keep_a_second_full_figure_copy(monkeypatch):
    plot = _bare_plot(monkeypatch)

    class LargeFigure:
        def to_dict(self):
            raise AssertionError("interactive plot should not duplicate figure data")

    figure = LargeFigure()
    plot.draw(figure)

    assert plot._figure is figure
    assert plot.data is None


def test_interactive_plot_purges_webgl_graph_and_releases_python_data(monkeypatch):
    calls = []

    class Element:
        _mmfem_click_bound = True

        def _call_method_ignore_return(self, name, args):
            calls.append((name, args))

        def _call_method(self, name, args):
            calls.append((name, args))
            return self

    class Plotly:
        def _call_method_ignore_return(self, name, args):
            calls.append((name, args))

    element = Element()
    plot = _bare_plot(monkeypatch)
    plot._js_component = element
    plot._click_element = element
    plot._click_proxy = {"id": 1}
    plot._figure = object()
    plot.data = {"mesh": [object()]}
    monkeypatch.setattr(PlotlyComponent, "_plotly", Plotly())
    monkeypatch.setattr("webgpu.platform.destroy_proxy", lambda *_args: None)

    plot._on_unmount()

    assert ("purge", [element]) in calls
    assert plot._figure is None
    assert plot.data is None
    assert plot._click_handler is None
    assert plot._click_unmounted


def test_interactive_plot_unregisters_when_unmounted_before_plotly_loads(monkeypatch):
    plot = _bare_plot(monkeypatch)
    monkeypatch.setattr(PlotlyComponent, "_waiting", [plot])

    plot._on_unmount()

    assert plot not in PlotlyComponent._waiting


def test_plotly_click_bridge_preserves_triangle_number(monkeypatch):
    plot = _bare_plot(monkeypatch)
    selected = []
    plot._click_handler = lambda curve, point: selected.append((curve, point))
    plot._dispatch_click(SimpleNamespace(
        points=[SimpleNamespace(curveNumber=2, pointNumber=81)]
    ))

    assert selected == [(2, 81)]


def test_plotly_click_bridge_ignores_empty_events(monkeypatch):
    plot = _bare_plot(monkeypatch)
    selected = []
    plot._click_handler = lambda curve, point: selected.append((curve, point))
    plot._dispatch_click(SimpleNamespace(points=[]))
    plot._dispatch_click(SimpleNamespace(points=[SimpleNamespace(pointNumber=4)]))

    assert selected == []


