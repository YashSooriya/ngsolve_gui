"""The 3D result view preserves meridian values through a full revolution."""

import math

import ngsolve as ngs
from netgen.geom2d import SplineGeometry
import netgen.occ as occ
import numpy as np
import pytest
from playwright.sync_api import Page

from ngapp.e2e import app_test

from .helpers import _draw

from ngsolve_gui.axisymmetric_revolution import revolve_gridfunction
from ngsolve_gui.region_state import RegionState
from ngsolve_gui.sections.axisymmetric_revolution import AxisymmetricRevolutionSection


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


def test_revolved_interface_stays_visible_when_outer_region_is_hidden():
    geometry = SplineGeometry()
    geometry.AddRectangle(
        p1=(0, 0), p2=(1, 1), bc="outer", leftdomain=1, rightdomain=0
    )
    geometry.AddRectangle(
        p1=(0.3, 0.3), p2=(0.7, 0.7),
        bc="interface", leftdomain=2, rightdomain=1,
    )
    geometry.SetMaterial(1, "square_1")
    geometry.SetMaterial(2, "square_2")
    meridian = ngs.Mesh(geometry.GenerateMesh(maxh=0.25))
    source = ngs.GridFunction(ngs.H1(meridian, order=1), name="nested")
    source.Set(ngs.x + ngs.y)

    mesh3d, _ = revolve_gridfunction(source, sectors=8)
    assert list(mesh3d.GetMaterials()) == ["square_1", "square_2"]

    descriptors = list(mesh3d.ngmesh.FaceDescriptors())
    state = RegionState(mesh3d)
    state.hidden = {"square_1"}
    surface_alphas = state.surf_alphas()

    outer_surfaces = [
        index for index, descriptor in enumerate(descriptors)
        if descriptor.domin == 1 and descriptor.domout == 0
    ]
    shared_interfaces = [
        index for index, descriptor in enumerate(descriptors)
        if {descriptor.domin, descriptor.domout} == {1, 2}
    ]
    assert outer_surfaces
    assert shared_interfaces
    assert all(surface_alphas[index] == 0.0 for index in outer_surfaces)
    assert all(surface_alphas[index] == 1.0 for index in shared_interfaces)


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
        assert not component.slice_available
        assert component.slice_feature_available
        assert any(
            section.__name__ == "AxisymmetricRevolutionSection"
            for section in component.property_sections
        )
        assert any(
            section.__name__ == "SliceViewSection"
            for section in component.property_sections
        )
        section_names = [section.__name__ for section in component.property_sections]
        assert section_names.index("RegionsSection") < section_names.index(
            "AxisymmetricRevolutionSection"
        ) < section_names.index("SliceViewSection")
        section_ui = AxisymmetricRevolutionSection(component)
        assert section_ui._caret in section_ui._head_main.ui_children
        assert section_ui._head_main.ui_children[2].ui_children == [
            "Revolved 3D View"
        ]

        component.axisymmetric_revolved.value = True
        assert component.mesh.dim == 3
        assert component.cf.dim == 1
        assert component.slice_available
        assert component.slice_region_options[0] == "All regions"
        assert component.slice_renderer is not None
        assert component._clip_toolbar is not None
        component.slice_enabled.value = True
        assert component.slice_renderer.active

        component.axisymmetric_revolved.value = False
        assert component.mesh.dim == 2
        assert not component.slice_available
        assert not component.slice_enabled.value
        assert component._clip_toolbar is None
    finally:
        if previous_region_visibility is missing_region_visibility:
            del function_module.RegionVisibility
        else:
            function_module.RegionVisibility = previous_region_visibility

@app_test("ngsolve_gui.appconfig")
def test_vector_displacement_deforms_axisymmetric_revolution(page: Page, app) -> None:
    """A meridional (radial, axial) displacement becomes Cartesian in 3D."""
    import ngsolve_gui.function as function_module

    class _RegionVisibility:
        def set_alphas(self, **_alphas):
            pass

    missing = object()
    previous = getattr(function_module, "RegionVisibility", missing)
    function_module.RegionVisibility = _RegionVisibility
    try:
        meridian = _meridian_mesh()
        displacement = ngs.GridFunction(
            ngs.VectorH1(meridian, order=1, dim=2), name="Displacement"
        )
        displacement.Set(ngs.CF((ngs.x, ngs.y)))
        _draw(
            app,
            displacement,
            name="Axisymmetric displacement",
            _ngsolve_gui_axisymmetric=True,
        )
        component = app.tab_panel.comp

        assert component.deformation is not None
        assert component.deformation.dim == 3
        assert not component.deformation_enabled.value

        component.axisymmetric_revolved.value = True
        assert component.mesh.dim == 3
        assert component.deformation is not None
        assert component.deformation.dim == 3
        for vertex in component.mesh.vertices:
            x_coord, y_coord, axial = map(float, component.mesh[vertex].point)
            radius = math.hypot(x_coord, y_coord)
            angle = math.atan2(y_coord, x_coord)
            radial, axial_component = displacement(
                meridian(radius, axial)
            )
            if radius < 1e-12:
                expected = (0.0, 0.0, axial_component)
            else:
                expected = (
                    radial * math.cos(angle),
                    radial * math.sin(angle),
                    axial_component,
                )
            actual = component.deformation(
                component.mesh(x_coord, y_coord, axial)
            )
            assert tuple(map(float, actual)) == pytest.approx(
                expected, abs=1e-8
            )

        component.deformation_enabled.value = True
        component.deformation_scale.value = 1000.0
        component.deformation_scale2.value = 0.5
        assert component.mdata.deformation_scale == pytest.approx(500.0)
    finally:
        if previous is missing:
            del function_module.RegionVisibility
        else:
            function_module.RegionVisibility = previous
