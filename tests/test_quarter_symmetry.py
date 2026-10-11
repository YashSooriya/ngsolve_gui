"""Tests for visualization of quarter-domain 3D solution fields."""

import numpy as np
import ngsolve as ngs
from ngsolve import x, y
from netgen.occ import Box, Pnt, X, Y
import pytest
from playwright.sync_api import Page
from ngapp.e2e import app_test

from ngsolve_gui.quarter_symmetry import (
    find_quarter_symmetry_planes,
    mirror_quarter_gridfunction,
)
from .helpers import _draw


def _quarter_box(maxh=0.5):
    geometry = Box(Pnt(0, 0, 0), Pnt(1, 1, 1))
    geometry.faces.Min(X).name = "SymmX"
    geometry.faces.Min(Y).name = "SymmY"
    return geometry.GenerateMesh(maxh=maxh)


def test_quarter_symmetry_planes_require_two_tagged_coordinate_faces():
    mesh = _quarter_box()

    planes = find_quarter_symmetry_planes(mesh)

    assert [(plane["name"], plane["coordinate"], plane["source_side"]) for plane in planes] == [
        ("x", 0.0, 1),
        ("y", 0.0, 1),
    ]
    ordinary_box = Box(Pnt(0, 0, 0), Pnt(1, 1, 1)).GenerateMesh(maxh=0.5)
    assert find_quarter_symmetry_planes(ordinary_box) == ()


def test_mirrored_polar_vector_covers_all_four_quadrants_without_changing_source():
    source_mesh = _quarter_box()
    source_element_count = len(list(source_mesh.Elements(ngs.VOL)))
    source = ngs.GridFunction(ngs.VectorH1(source_mesh, order=1))
    source.Set(ngs.CF((x, y, 1)))

    full_mesh, full_field = mirror_quarter_gridfunction(
        source,
        find_quarter_symmetry_planes(source_mesh),
    )

    assert len(list(full_mesh.Elements(ngs.VOL))) == 4 * source_element_count
    assert len(list(source_mesh.Elements(ngs.VOL))) == source_element_count
    points = (
        (0.4, 0.4, 0.3),
        (-0.4, 0.4, 0.3),
        (0.4, -0.4, 0.3),
        (-0.4, -0.4, 0.3),
    )
    for point in points:
        values = tuple(full_field(full_mesh(*point)))
        assert values == pytest.approx((point[0], point[1], 1.0), abs=1e-8)


def test_mirrored_magnetic_flux_density_uses_axial_vector_parity():
    source_mesh = _quarter_box()
    source = ngs.GridFunction(ngs.VectorH1(source_mesh, order=1))
    # These components satisfy the two symmetry-plane constraints for an
    # axial vector and remain exactly representable by the generated mesh.
    source.Set(ngs.CF((y, x, x * y)))

    full_mesh, full_field = mirror_quarter_gridfunction(
        source,
        find_quarter_symmetry_planes(source_mesh),
        vector_kind="axial",
    )

    points = (
        (0.4, 0.4, 0.3),
        (-0.4, 0.4, 0.3),
        (0.4, -0.4, 0.3),
        (-0.4, -0.4, 0.3),
    )
    for point in points:
        values = tuple(full_field(full_mesh(*point)))
        source_value = np.asarray(
            source(source_mesh(abs(point[0]), abs(point[1]), point[2]))
        )
        signs = (-1 if point[0] < 0 else 1, -1 if point[1] < 0 else 1, 1)
        determinant = signs[0] * signs[1]
        factors = tuple(determinant * sign for sign in signs)
        expected = tuple(value * factor for value, factor in zip(source_value, factors))
        assert values == pytest.approx(expected, abs=1e-8)


def test_scalar_result_is_repeated_unchanged_across_both_planes():
    source_mesh = _quarter_box()
    source = ngs.GridFunction(ngs.H1(source_mesh, order=1))
    source.Set(1 + x + y)

    full_mesh, full_field = mirror_quarter_gridfunction(
        source,
        find_quarter_symmetry_planes(source_mesh),
    )

    points = (
        (0.4, 0.4, 0.3),
        (-0.4, 0.4, 0.3),
        (0.4, -0.4, 0.3),
        (-0.4, -0.4, 0.3),
    )
    values = [full_field(full_mesh(*point)) for point in points]
    assert values == pytest.approx([1.8] * 4, abs=1e-8)


@app_test("ngsolve_gui.appconfig")
def test_full_model_symmetry_switch_is_available_for_tagged_quarter_results(
    page: Page, app
) -> None:
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
        mesh = _quarter_box()
        source = ngs.GridFunction(ngs.H1(mesh, order=1), name="quarter_scalar")
        source.Set(1 + x + y)
        _draw(app, source, name="Quarter result")
        app._set_workspace_mode("post_process")
        app._update()
        component = app.tab_panel.comp

        assert component.quarter_symmetry_expansion_available
        assert any(
            section.__name__ == "QuarterSymmetrySection"
            for section in component.property_sections
        )
        quarter_element_count = len(list(mesh.Elements(ngs.VOL)))
        component.symmetry_expanded.value = True
        assert component.mesh.dim == 3
        assert len(list(component.mesh.Elements(ngs.VOL))) == 4 * quarter_element_count
        assert component.cf.dim == 1

        component.symmetry_expanded.value = False
        assert component.mesh is mesh
    finally:
        if previous_region_visibility is missing_region_visibility:
            del function_module.RegionVisibility
        else:
            function_module.RegionVisibility = previous_region_visibility
