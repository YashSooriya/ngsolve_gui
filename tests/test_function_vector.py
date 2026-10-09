"""Visual regression tests for vector CoefficientFunction rendering."""

from __future__ import annotations

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
