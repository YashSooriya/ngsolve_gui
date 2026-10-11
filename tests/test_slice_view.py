"""Tests for the interactive 3-D result slice view."""

import numpy as np
import ngsolve as ngs
import pytest
from netgen.occ import Box, Pnt, X, Y
from ngapp.e2e import app_test
from ngsolve_webgpu import FunctionData, MeshData
from ngsolve_webgpu.mesh import ElType
from playwright.sync_api import Page

from ngsolve_gui.slice_view import (
    ALL_REGIONS,
    material_element_mask,
    material_region,
    plane_normal,
    position_on_plane,
    projection_bounds,
)
from .helpers import _draw


def _quarter_box(maxh=0.5):
    geometry = Box(Pnt(0, 0, 0), Pnt(1, 1, 1))
    geometry.faces.Min(X).name = "SymmX"
    geometry.faces.Min(Y).name = "SymmY"
    return geometry.GenerateMesh(maxh=maxh)


def test_axis_and_custom_slice_normals_are_unit_vectors():
    assert plane_normal("x").tolist() == [1.0, 0.0, 0.0]
    assert plane_normal("Y").tolist() == [0.0, 1.0, 0.0]
    assert plane_normal("custom", (2, -2, 1)) == pytest.approx(
        np.array((2, -2, 1)) / 3
    )
    with pytest.raises(ValueError, match="cannot be zero"):
        plane_normal("custom", (0, 0, 0))


def test_scroll_fraction_maps_to_projected_plane_position():
    bounds = ((-1, -2, -3), (1, 2, 3))
    normal = plane_normal("custom", (1, 1, 0))

    start, end = projection_bounds(bounds, normal)
    center, offset, distance = position_on_plane(bounds, normal, 0.75)

    assert (start, end) == pytest.approx((-3 / np.sqrt(2), 3 / np.sqrt(2)))
    assert distance == pytest.approx(start + 0.75 * (end - start))
    assert np.dot(center, normal) + offset == pytest.approx(distance)


def test_material_selection_builds_a_volume_element_mask():
    mesh = _quarter_box()
    material = str(next(iter(mesh.GetMaterials())))
    mask = material_element_mask(mesh, material)

    assert mask.dtype == np.bool_
    assert mask.shape == (len(list(mesh.Elements(ngs.VOL))),)
    assert mask.all()
    assert material_element_mask(mesh, ALL_REGIONS) is None


def test_all_region_selection_resolves_to_every_mesh_material():
    mesh = _quarter_box()
    all_materials = material_region(mesh, ALL_REGIONS)
    regex_all_materials = mesh.Materials(".*")

    assert list(all_materials.Mask()) == list(regex_all_materials.Mask())
    material = str(next(iter(mesh.GetMaterials())))
    assert list(material_region(mesh, material).Mask()) == list(
        regex_all_materials.Mask()
    )


def test_region_scoped_field_data_matches_slice_mesh_elements():
    from ngsolve import x, y, z

    mesh = _quarter_box()
    material = str(next(iter(mesh.GetMaterials())))
    mask = material_element_mask(mesh, material)
    mesh_data = MeshData(mesh.Materials(material), el3d_bitarray=mask)
    source = ngs.GridFunction(ngs.H1(mesh, order=1))
    source.Set(x + y + z)
    field_data = FunctionData(mesh_data, source, order=1)
    field_data.need_3d = True
    field_data._create_data()
    mesh_data._create_data()

    tetrahedra = mesh_data.num_elements[ElType.TET]
    assert tetrahedra == int(mask.sum())
    assert field_data.data_3d.shape == (3 + 4 * tetrahedra,)
    assert field_data.data_3d[:3] == pytest.approx((1, 1, 0))


@app_test("ngsolve_gui.appconfig")
def test_slice_control_adds_viewport_slider_and_hides_only_selected_region(
    page: Page, app
) -> None:
    import ngsolve_gui.function as function_module
    from ngsolve import x, y, z

    class _RegionVisibility:
        def set_alphas(self, **_alphas):
            pass

    missing = object()
    previous = getattr(function_module, "RegionVisibility", missing)
    function_module.RegionVisibility = _RegionVisibility
    try:
        mesh = _quarter_box()
        source = ngs.GridFunction(ngs.H1(mesh, order=1), name="quarter_scalar")
        source.Set(x + y + z)
        _draw(app, source, name="Quarter result")
        app._set_workspace_mode("post_process")
        app._update()
        component = app.tab_panel.comp

        assert component.slice_available
        assert ALL_REGIONS in component.slice_region_options
        assert any(
            section.__name__ == "SliceViewSection"
            for section in component.property_sections
        )
        assert component._slice_position_slider.ui_vertical
        assert component._slice_overlay.ui_hidden

        selected_material = component.slice_region.value
        component.slice_enabled.value = True
        assert component.slice_renderer.active
        assert not component._slice_overlay.ui_hidden
        assert component.region_state.auto_hidden == {selected_material}
        assert component.slice_renderer.data.mesh_data.on_region

        component.slice_axis.value = "x"
        assert component._slice_clipping.normal == pytest.approx([1, 0, 0])
        component.slice_position.value = 0.75
        assert component._slice_position_slider.ui_model_value == pytest.approx(0.75)

        component.slice_axis.value = "custom"
        component.slice_custom_normal[0].value = 1.0
        component.slice_custom_normal[1].value = 1.0
        component.slice_custom_normal[2].value = 0.0
        assert component._slice_clipping.normal == pytest.approx(
            [1 / np.sqrt(2), 1 / np.sqrt(2), 0]
        )

        component.slice_region.value = ALL_REGIONS
        assert component.region_state.auto_hidden == set(
            component.region_state.unique_materials
        )
        component.slice_enabled.value = False
        assert not component.slice_renderer.active
        assert component._slice_overlay.ui_hidden
    finally:
        if previous is missing:
            del function_module.RegionVisibility
        else:
            function_module.RegionVisibility = previous
