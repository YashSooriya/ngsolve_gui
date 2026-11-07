"""Tests for the coarse-seed streamline update used by result pickles."""

import numpy as np
import ngsolve as ngs
from netgen.csg import unit_cube

import ngsolve_gui.fast_fieldlines as fast_fieldlines


class _BaseRenderer:
    def update(self, _options):
        self.needs_update = False


class _TestRenderer(_BaseRenderer):
    def __init__(self, mesh):
        self.mesh = mesh
        self.start_region = mesh.Materials(".*")
        self.cf = ngs.CF((ngs.x + 1, ngs.y, ngs.z))
        self.fieldline_options = {
            "thickness": 0.0001,
            "num_lines": 3,
            "length": 0.5,
            "max_points_per_line": 500,
            "tolerance": 0.0005,
            "direction": 0,
        }
        self.seed = 1
        self.needs_update = True


def test_fast_fieldline_update_uses_coarse_points_and_reuses_trace(monkeypatch):
    mesh = ngs.Mesh(unit_cube.GenerateMesh(maxh=0.8))
    renderer = _TestRenderer(mesh)
    trace_calls = []

    def fake_trace(_cf, _region, *, mesh, start_points, **options):
        trace_calls.append((mesh, np.array(start_points), options))
        return {
            "pstart": np.array([[0.0, 0.0, 0.0]]),
            "pend": np.array([[0.1, 0.0, 0.0]]),
            "value": np.array([1.0]),
        }

    monkeypatch.setattr(fast_fieldlines, "_trace_fieldlines", fake_trace)
    fast_fieldlines.install_fast_fieldline_update(renderer)

    assert renderer.fieldline_options["max_points_per_line"] == 150
    renderer.update({})
    assert len(trace_calls) == 1
    assert 0 < len(trace_calls[0][1]) <= 256
    assert trace_calls[0][2]["max_points_per_line"] == 150

    renderer.needs_update = True
    renderer.update({})
    assert len(trace_calls) == 1
