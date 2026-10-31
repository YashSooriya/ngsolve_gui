"""Visual regression tests for PlotComponent rendering.

Uses Playwright element screenshots compared against baselines via
assert_matches_baseline, targeting the rendered plotly container in the DOM.
"""

from __future__ import annotations

from types import SimpleNamespace

from playwright.sync_api import Page

from ngapp.e2e import app_test
from ngapp.e2e_webgpu import assert_matches_baseline

from .helpers import _draw


def _use_plot_baseline_viewport(page: Page) -> None:
    # Plotly's layout baselines were captured at this page height.
    page.set_viewport_size({"width": 1441, "height": 768})


def test_click_dispatch_keeps_two_argument_callbacks_and_passes_point_index() -> None:
    from ngsolve_gui.plot import _make_click_handler

    two_argument_events = []
    two_argument = _make_click_handler(
        lambda figure, curve: two_argument_events.append((figure, curve)), 0
    )
    two_argument(2, 7)

    three_argument_events = []
    three_argument = _make_click_handler(
        lambda figure, curve, point: three_argument_events.append(
            (figure, curve, point)
        ),
        0,
    )
    three_argument(2, 7)

    assert two_argument_events == [(0, 2)]
    assert three_argument_events == [(0, 2, 7)]


def test_plot_mount_redraw_uses_figure_updated_while_child_was_mounting(monkeypatch) -> None:
    import plotly.graph_objects as go
    from ngapp.components import Component

    from ngsolve_gui.plot import PlotComponent

    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    first = go.Figure(data=go.Scatter3d(x=[0], y=[0], z=[0]))
    latest = go.Figure(
        data=[
            go.Scatter3d(x=[0], y=[0], z=[0]),
            go.Scatter3d(x=[1], y=[1], z=[1]),
        ]
    )
    component = PlotComponent("MountRace", {"obj": first}, {})
    component.draw()
    plot = component._plots[0]

    # Model the graph child receiving a newer figure before its mounted event.
    component.update_figure(latest)
    assert plot._figure is latest

    rendered = []
    monkeypatch.setattr(plot, "redraw", lambda: rendered.append(plot._figure))
    mount_callback = plot._callbacks["mounted"][-1]
    mount_callback(SimpleNamespace())

    assert rendered == [latest]


@app_test("ngsolve_gui.appconfig")
def test_plot_single_figure(page: Page, app) -> None:
    """Single plotly scatter figure renders correctly."""
    import plotly.graph_objects as go
    from ngsolve_gui.plot import PlotComponent

    _use_plot_baseline_viewport(page)
    fig = go.Figure(data=go.Scatter(x=[1, 2, 3], y=[1, 4, 9]))
    _draw(app, fig, name="SinglePlot")

    comp = app.tab_panel.comp
    assert isinstance(comp, PlotComponent)

    page.wait_for_function("window.Plotly && window.Plotly.version === '2.35.2'")
    page.wait_for_timeout(100)
    locator = page.locator(".js-plotly-plot").first
    assert_matches_baseline(page, locator, "plot_single_scatter.png")


@app_test("ngsolve_gui.appconfig")
def test_plot_bar_chart(page: Page, app) -> None:
    """Single plotly bar chart renders correctly."""
    import plotly.graph_objects as go

    _use_plot_baseline_viewport(page)
    fig = go.Figure(data=go.Bar(x=["A", "B", "C"], y=[10, 20, 15]))
    _draw(app, fig, name="BarPlot")

    page.wait_for_timeout(100)
    locator = page.locator(".js-plotly-plot").first
    assert_matches_baseline(page, locator, "plot_bar_chart.png")


@app_test("ngsolve_gui.appconfig")
def test_plot_with_layout(page: Page, app) -> None:
    """Plotly figure with title and axis labels renders correctly."""
    import plotly.graph_objects as go

    _use_plot_baseline_viewport(page)
    fig = go.Figure(
        data=go.Scatter(x=[0, 1, 2], y=[0, 1, 4], mode="lines+markers"),
        layout=go.Layout(
            title="Quadratic",
            xaxis_title="X",
            yaxis_title="Y",
        ),
    )
    _draw(app, fig, name="LayoutPlot")

    page.wait_for_timeout(100)
    locator = page.locator(".js-plotly-plot").first
    assert_matches_baseline(page, locator, "plot_with_layout.png")


@app_test("ngsolve_gui.appconfig")
def test_plot_dict_figure(page: Page, app) -> None:
    """A plain dict is accepted and rendered as a plotly figure."""
    _use_plot_baseline_viewport(page)
    fig_dict = {"data": [{"type": "scatter", "x": [1, 2, 3], "y": [2, 4, 6]}]}
    _draw(app, fig_dict, name="DictPlot")

    page.wait_for_timeout(100)
    locator = page.locator(".js-plotly-plot").first
    assert_matches_baseline(page, locator, "plot_dict_figure.png")


@app_test("ngsolve_gui.appconfig")
def test_plot_multiple_figures(page: Page, app) -> None:
    """Two figures produce two plotly containers; screenshot the page area."""
    import plotly.graph_objects as go

    _use_plot_baseline_viewport(page)
    fig1 = go.Figure(data=go.Scatter(x=[1, 2, 3], y=[1, 4, 9]))
    fig2 = go.Figure(data=go.Bar(x=["A", "B", "C"], y=[10, 20, 15]))
    _draw(app, [fig1, fig2], name="MultiPlot")

    page.wait_for_selector(".js-plotly-plot >> nth=1", timeout=10000)
    page.wait_for_timeout(500)
    plots = page.locator(".js-plotly-plot")
    assert_matches_baseline(page, plots.first, "plot_multi_first.png")
    assert_matches_baseline(page, plots.nth(1), "plot_multi_second.png")
