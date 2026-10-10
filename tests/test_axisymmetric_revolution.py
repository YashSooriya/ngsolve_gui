"""The 3D result view preserves meridian values through a full revolution."""

import math

import ngsolve as ngs
import netgen.occ as occ
import numpy as np
import pytest
from playwright.sync_api import Page

from ngapp.e2e import app_test

from .helpers import _draw

from ngsolve_gui.axisymmetric_revolution import revolve_gridfunction


def _meridian_mesh():
    geometry = occ.OCCGeometry(occ.Rectangle(1, 1).Face(), dim=2)
    return ngs.Mesh(geometry.GenerateMesh(maxh=0.45))


def test_scalar_field_revolves_to_a_closed_3d_volume():
    meridian = _meridian_mesh()
    source = ngs.GridFunction(ngs.H1(meridian, order=2), name="scalar")
    source.Set(ngs.x + 2 * ngs.y)

    mesh3d, field3d = revolve_gridfunction(source, sectors=16)

    assert mesh3d.dim == 3
    assert mesh3d.ne > meridian.ne
    assert sum(1 for _ in mesh3d.Elements(ngs.BND)) > 0
    assert field3d(mesh3d(0.5, 0.0, 0.25)) == pytest.approx(1.0, abs=1e-9)
    assert field3d(mesh3d(0.0, 0.5, 0.25)) == pytest.approx(1.0, abs=1e-9)

    polygon_cylinder_volume = math.pi * math.sin(2 * math.pi / 16) / (2 * math.pi / 16)
    assert ngs.Integrate(1, mesh3d) == pytest.approx(polygon_cylinder_volume, rel=1e-9)


def test_radial_axial_vector_is_rotated_into_cartesian_components():
    meridian = _meridian_mesh()
    source = ngs.GridFunction(
        ngs.VectorH1(meridian, order=1, dim=2), name="radial_axial"
    )
    source.Set(ngs.CF((ngs.x, ngs.y)))

    mesh3d, field3d = revolve_gridfunction(source, sectors=16)
    assert field3d.dim == 3

    for vertex in mesh3d.vertices:
        x_coord, y_coord, axial = map(float, mesh3d[vertex].point)
        radius = math.hypot(x_coord, y_coord)
        angle = math.atan2(y_coord, x_coord)
        radial, axial_component = source(meridian(radius, axial))
        if radius < 1e-12:
            expected = np.asarray((0, 0, axial_component))
        else:
            expected = np.asarray(
                (radial * math.cos(angle), radial * math.sin(angle), axial_component)
            )
        actual = np.asarray(field3d(mesh3d(x_coord, y_coord, axial)))
        assert actual == pytest.approx(expected, abs=1e-9)


@app_test("ngsolve_gui.appconfig")
def test_axisymmetric_revolution_control_switches_viewport_dimension(
    page: Page, app
) -> None:
    # This local test environment currently has ngsolve_webgpu 0.4.0 installed,
    # while the app requires >=0.5.6 (which supplies RegionVisibility).
    import ngsolve_gui.function as function_module

    class _RegionVisibility:
        def set_alphas(self, **_alphas):
            pass

    missing_region_visibility = object()
    previous_region_visibility = getattr(
        function_module, "RegionVisibility", missing_region_visibility
    )
    function_module.RegionVisibility = _RegionVisibility
    try:
        meridian = _meridian_mesh()
        source = ngs.GridFunction(ngs.H1(meridian, order=1), name="Axisymmetric scalar")
        source.Set(ngs.x + ngs.y)
        _draw(app, source, name="Axisymmetric result", _ngsolve_gui_axisymmetric=True)
        app._set_workspace_mode("post_process")
        app._update()
        component = app.tab_panel.comp

        assert component.axisymmetric_revolution_available
        assert component.mesh.dim == 2
        assert any(
            section.__name__ == "AxisymmetricRevolutionSection"
            for section in component.property_sections
        )
        component.axisymmetric_revolved.value = True
        assert component.mesh.dim == 3
        assert component.cf.dim == 1
        assert component._clip_toolbar is not None

        component.axisymmetric_revolved.value = False
        assert component.mesh.dim == 2
        assert component._clip_toolbar is None
    finally:
        if previous_region_visibility is missing_region_visibility:
            del function_module.RegionVisibility
        else:
            function_module.RegionVisibility = previous_region_visibility
