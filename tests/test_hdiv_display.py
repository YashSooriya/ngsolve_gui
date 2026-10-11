"""Regression tests for one-sided H(div) result rendering."""

from __future__ import annotations

import numpy as np
import netgen.occ as occ
import ngsolve as ngs

from ngsolve_gui._visualization import visualization_cf


def test_hdiv_display_uses_full_volume_value_on_symmetry_face() -> None:
    """A tangential H(div) component must not vanish in surface coloring."""
    box = occ.Box(occ.Pnt(0, 0, 0), occ.Pnt(1, 1, 1))
    box.faces.Min(occ.X).name = "symmetry"
    mesh = ngs.Mesh(occ.OCCGeometry(box).GenerateMesh(maxh=0.5))
    gf = ngs.GridFunction(ngs.HDiv(mesh, order=1))
    gf.Set(ngs.CF((1, 2, 3)))

    region = mesh.Boundaries("symmetry")
    rule = ngs.IntegrationRule(ngs.ET.TRIG, 1)
    with ngs.TaskManager():
        points = mesh.MapToAllElements({ngs.ET.TRIG: rule}, region)
        boundary_trace = np.asarray(gf(points)).reshape(-1, 3)
        display_values = np.asarray(visualization_cf(gf)(points)).reshape(-1, 3)

    # The raw trace only contains the normal x component on this face. The
    # display coefficient must retain the adjacent volume's tangential z value.
    assert np.allclose(boundary_trace[:, 2], 0.0, atol=1e-12)
    assert np.allclose(display_values[:, 2], 3.0, atol=1e-12)


def test_non_hdiv_coefficients_are_unchanged_for_display() -> None:
    mesh = ngs.Mesh(
        occ.OCCGeometry(occ.Box(occ.Pnt(0, 0, 0), occ.Pnt(1, 1, 1))).GenerateMesh(
            maxh=0.5
        )
    )
    gf = ngs.GridFunction(ngs.H1(mesh, order=1))
    gf.Set(ngs.x + ngs.y + ngs.z)

    assert visualization_cf(gf) is gf
