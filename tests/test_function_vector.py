"""Visual regression tests for vector CoefficientFunction rendering."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import ngsolve as ngs

from playwright.sync_api import Page
from ngapp.e2e import app_test

from ngapp.e2e_webgpu import assert_matches_baseline

from .helpers import (
    _draw,
    make_mesh_2d,
    make_mesh_3d,
    click_in_section,
    toggle_clipping,
)
from ngsolve_gui.function import FunctionComponent
from ngsolve_gui.slice_view import ALL_REGIONS
from ngsolve_gui.sections.vectors_flow import VectorsFlowSection


@app_test("ngsolve_gui.appconfig")
def test_function_vector_2d(page: Page, app) -> None:
    """2D vector CF: default → surface vectors."""
    mesh = make_mesh_2d()
    cf = ngs.CF((ngs.x, ngs.y))
    _draw(app, cf, mesh=mesh, name="Vec2D")
    comp = app.tab_panel.comp

    assert_matches_baseline(page, comp.wgpu, "func_vector_2d_default.png")

    click_in_section(page, "Vectors & Flow", "Surface")
    assert_matches_baseline(page, comp.wgpu, "func_vector_2d_surface_vectors.png")


@app_test("ngsolve_gui.appconfig")
def test_function_vector_3d(page: Page, app) -> None:
    """3D vector CF: enable clipping → clipping vectors → surface vectors."""
    mesh = make_mesh_3d()
    cf = ngs.CF((ngs.x, ngs.y, ngs.z))
    _draw(app, cf, mesh=mesh, name="Vec3D")
    comp = app.tab_panel.comp

    assert_matches_baseline(page, comp.wgpu, "func_vector_3d_default.png")

    # Enable clipping first so the clipping plane is visible
    toggle_clipping(page)
    assert_matches_baseline(page, comp.wgpu, "func_vector_3d_clipped.png")

    # Show clipping vectors (only meaningful with clipping enabled)
    click_in_section(page, "Vectors & Flow", "Clip")
    assert_matches_baseline(page, comp.wgpu, "func_vector_3d_clipping_vectors.png")

    # Switch to surface vectors
    click_in_section(page, "Vectors & Flow", "Clip")
    click_in_section(page, "Vectors & Flow", "Surface")
    assert_matches_baseline(page, comp.wgpu, "func_vector_3d_surface_vectors.png")


@app_test("ngsolve_gui.appconfig")
def test_function_streamline_defaults(page: Page, app) -> None:
    """Interactive streamlines use the requested light default settings."""
    mesh = make_mesh_3d()
    cf = ngs.CF((ngs.x, ngs.y, ngs.z))
    _draw(app, cf, mesh=mesh, name="StreamlineDefaults")
    comp = app.tab_panel.comp

    assert comp.fieldlines_num_lines.value == 20
    assert comp.fieldlines_thickness.value == 0.0001
    assert comp.fieldlines.fieldline_options["num_lines"] == 20
    assert comp.fieldlines.fieldline_options["thickness"] == 0.0001


@app_test("ngsolve_gui.appconfig")
def test_streamline_seed_material_is_temporarily_hidden(page: Page, app) -> None:
    """The selected seed region stays unchecked only while lines are active."""
    mesh = make_mesh_3d()
    seed_material = str(mesh.GetMaterials()[0])
    cf = ngs.CF((ngs.x, ngs.y, ngs.z))
    _draw(
        app,
        cf,
        mesh=mesh,
        name="StreamlineSeedVisibility",
        _ngsolve_gui_fast_fieldlines=True,
        _ngsolve_gui_fieldline_seed_material=seed_material,
    )

    comp = app.tab_panel.comp
    assert comp.region_state.material_visible(seed_material)

    comp.field_lines_visible.value = True
    assert not comp.region_state.material_visible(seed_material)
    assert seed_material not in comp.hidden_regions.value

    comp.field_lines_visible.value = False
    assert comp.region_state.material_visible(seed_material)

    comp.set_region_visible(seed_material, False)
    comp.field_lines_visible.value = True
    comp.field_lines_visible.value = False
    assert not comp.region_state.material_visible(seed_material)


@app_test("ngsolve_gui.appconfig")
def test_streamline_seed_region_selector_supports_all_regions(page: Page, app) -> None:
    """The seed selector scopes starts to a material or to the whole mesh."""
    import ngsolve_gui.function as function_module

    class _RegionVisibility:
        def set_alphas(self, **_alphas):
            pass

    mesh = make_mesh_3d()
    cf = ngs.CF((ngs.x, ngs.y, ngs.z))
    missing = object()
    previous = getattr(function_module, "RegionVisibility", missing)
    function_module.RegionVisibility = _RegionVisibility
    try:
        _draw(
            app,
            cf,
            mesh=mesh,
            name="StreamlineAllSeedRegions",
            _ngsolve_gui_fast_fieldlines=True,
        )
    finally:
        if previous is missing:
            del function_module.RegionVisibility
        else:
            function_module.RegionVisibility = previous
    comp = app.tab_panel.comp
    material_names = [str(material) for material in mesh.GetMaterials()]
    section = VectorsFlowSection(comp)

    assert comp.fieldline_seed_region_options == [ALL_REGIONS, *material_names]
    assert section.seed_region.ui_options == comp.fieldline_seed_region_options
    assert comp.fieldline_seed_region.value == ALL_REGIONS

    material = material_names[0]
    section._update_seed_region(SimpleNamespace(value=material))
    assert comp.fieldline_seed_region.value == material
    assert comp.fieldlines._ngsolve_gui_start_region_name == material
    assert list(comp.fieldlines.start_region.Mask()) == list(
        mesh.Materials(material).Mask()
    )

    comp.field_lines_visible.value = True
    assert comp.region_state.auto_hidden == {material}
    section._update_seed_region(SimpleNamespace(value=ALL_REGIONS))
    assert comp.fieldlines._ngsolve_gui_start_region_name == ALL_REGIONS
    assert list(comp.fieldlines.start_region.Mask()) == list(
        mesh.Materials(".*").Mask()
    )
    assert comp.region_state.auto_hidden == set(comp.region_state.unique_materials)


@app_test("ngsolve_gui.appconfig")
def test_imported_result_installs_fast_streamline_update(page: Page, app) -> None:
    """Imported 3D result fields opt into the cached coarse-seed tracer."""
    mesh = make_mesh_3d()
    cf = ngs.CF((ngs.x, ngs.y, ngs.z))
    _draw(
        app,
        cf,
        mesh=mesh,
        name="ImportedFastFieldlines",
        _ngsolve_gui_fast_fieldlines=True,
    )

    comp = app.tab_panel.comp
    assert comp.fieldlines.update.__func__.__module__ == "ngsolve_gui.fast_fieldlines"


@app_test("ngsolve_gui.appconfig")
def test_function_fieldlines_2d(page: Page, app) -> None:
    """2D vector CF with field lines."""
    mesh = make_mesh_2d()
    cf = ngs.CF((ngs.y, -ngs.x))
    _draw(app, cf, mesh=mesh, name="FieldLines2D")
    comp = app.tab_panel.comp

    np.random.seed(42)
    click_in_section(page, "Vectors & Flow", "Streamlines")
    assert_matches_baseline(page, comp.wgpu, "func_fieldlines_2d.png")

@app_test("ngsolve_gui.appconfig")
def test_complex_field_defaults_to_absolute_and_allows_component_selection(
    page: Page, app
) -> None:
    """Complex fields default to magnitude while retaining real/imaginary modes."""
    mesh = make_mesh_2d()
    field = ngs.GridFunction(ngs.H1(mesh, order=1, complex=True), name="Complex field")
    field.Set(ngs.CF(ngs.x + 1j * ngs.y))
    _draw(app, field, name="ComplexField")

    component = app.tab_panel.comp
    assert component.cf.is_complex
    assert component.complex_mode.value == "abs"

    component.complex_mode.value = "real"
    assert component.complex_mode.value == "real"
    component.complex_mode.value = "imag"
    assert component.complex_mode.value == "imag"


def test_complex_picked_values_follow_the_selected_display_mode() -> None:
    class _ComplexField:
        def __call__(self, _point):
            return np.asarray((3 + 4j, 1 - 1j))

    class _PickMesh:
        dim = 2

        def __call__(self, *coordinates):
            return coordinates

    for mode, expected in (
        ("real", (3.0, 1.0)),
        ("imag", (4.0, -1.0)),
        ("abs", (5.0, np.sqrt(2.0))),
        ("arg", (np.arctan2(4.0, 3.0), -np.pi / 4.0)),
    ):
        view = SimpleNamespace(
            mesh=_PickMesh(),
            cf=_ComplexField(),
            complex_mode=SimpleNamespace(value=mode),
        )
        actual = FunctionComponent._eval_cf(view, (0.0, 0.0))
        assert np.allclose(actual, expected)


def test_complex_vector_deformation_extracts_real_and_imaginary_components() -> None:
    mesh = make_mesh_3d()
    displacement = ngs.GridFunction(
        ngs.VectorH1(mesh, order=1, dim=3, complex=True), name="Complex displacement"
    )
    displacement.Set(ngs.CF((1 + 2j, 3 + 4j, 5 + 6j)))
    view = SimpleNamespace(mesh=mesh, _axisymmetric_solution=False)

    real = FunctionComponent._resolve_deformation(view, displacement, "real")
    imaginary = FunctionComponent._resolve_deformation(view, displacement, "imag")

    assert real.dim == imaginary.dim == 3
    assert not real.is_complex
    assert not imaginary.is_complex
    volume = ngs.Integrate(1, mesh, ngs.VOL)
    real_means = [ngs.Integrate(real[index], mesh, ngs.VOL) / volume for index in range(3)]
    imaginary_means = [
        ngs.Integrate(imaginary[index], mesh, ngs.VOL) / volume
        for index in range(3)
    ]
    expected_real = [
        ngs.Integrate(displacement[index].real, mesh, ngs.VOL) / volume
        for index in range(3)
    ]
    expected_imaginary = [
        ngs.Integrate(displacement[index].imag, mesh, ngs.VOL) / volume
        for index in range(3)
    ]
    assert np.allclose(real_means, expected_real)
    assert np.allclose(imaginary_means, expected_imaginary)


@app_test("ngsolve_gui.appconfig")
def test_3d_vector_result_can_deform_geometry(page: Page, app) -> None:
    """A 3D vector result exposes the deformation toggle and multiplier."""
    mesh = make_mesh_3d()
    displacement = ngs.GridFunction(
        ngs.VectorH1(mesh, order=1, dim=3), name="Displacement"
    )
    displacement.Set(ngs.CF((ngs.x, ngs.y, ngs.z)))
    _draw(app, displacement, name="3D displacement")

    component = app.tab_panel.comp
    assert component.deformation is not None
    assert component.deformation.dim == 3
    assert not component.deformation_enabled.value

    app._workspace_mode_toggle.select("post_process")
    app._update()
    section = page.locator(".cb-psec").filter(has_text="Deformation").first
    section.wait_for(state="attached", timeout=5000)
    assert section.count() == 1
    section.locator(".cb-psec-head-main").click()
    multiplier = section.locator('input[type="number"]').first
    multiplier.fill("250")
    multiplier.press("Enter")
    assert component.deformation_scale.value == 250.0

    component.deformation_enabled.value = True
    component.deformation_scale2.value = 0.4
    assert component.mdata.deformation_scale == 100.0
