"""Axisymmetric fieldline rendering for a revolved meridian solution.

For a poloidal (r,z) field, physical fieldlines stay in meridian planes.
Trace the original finite-element field there, then distribute those paths
uniformly around the axis. This avoids re-integrating a sampled 3D field on the
preview tetrahedra, where interpolation and random volume seeds can make the
same axisymmetric field look tangled.
"""

from __future__ import annotations

import math
import re

import numpy as np
from ngsolve_webgpu.cf import FieldLines as _FieldLines
from webgpu.shapes import ShapeRenderer, generate_cylinder


def revolve_meridian_fieldline_arrays(data, plane_count):
    """Rotate NGSolve meridian line segments into evenly spaced 3D planes."""
    starts_2d = np.asarray(data["pstart"], dtype=np.float32).reshape(-1, 3)
    ends_2d = np.asarray(data["pend"], dtype=np.float32).reshape(-1, 3)
    values = np.asarray(data["value"], dtype=np.float32).reshape(-1)
    if starts_2d.shape != ends_2d.shape:
        raise ValueError("Fieldline start and end arrays must have matching shapes")
    if len(values) != len(starts_2d):
        if len(values) == 1:
            values = np.repeat(values, len(starts_2d))
        elif len(values) != 0 or len(starts_2d) != 0:
            raise ValueError("Fieldline values must have one value per segment")

    plane_count = max(1, int(plane_count))
    starts_3d = []
    directions_3d = []
    values_3d = []
    for plane in range(plane_count):
        angle = 2.0 * math.pi * plane / plane_count
        cosine, sine = math.cos(angle), math.sin(angle)
        start = np.column_stack(
            (
                starts_2d[:, 0] * cosine,
                starts_2d[:, 0] * sine,
                starts_2d[:, 1],
            )
        )
        end = np.column_stack(
            (
                ends_2d[:, 0] * cosine,
                ends_2d[:, 0] * sine,
                ends_2d[:, 1],
            )
        )
        starts_3d.append(start)
        directions_3d.append(end - start)
        values_3d.append(values)

    return (
        np.concatenate(starts_3d).astype(np.float32, copy=False).reshape(-1),
        np.concatenate(directions_3d).astype(np.float32, copy=False).reshape(-1),
        np.concatenate(values_3d).astype(np.float32, copy=False),
    )


class RevolvedAxisymmetricFieldLines(_FieldLines):
    """Draw original meridian streamlines in evenly spaced 3D radial planes."""

    def __init__(
        self,
        cf,
        start_region,
        *,
        source_cf,
        source_mesh,
        all_regions_name,
        **kwargs,
    ):
        super().__init__(cf, start_region, **kwargs)
        self.source_cf = source_cf
        self.source_mesh = source_mesh
        self.all_regions_name = all_regions_name

    def _source_seed_region(self):
        name = getattr(self, "_ngsolve_gui_start_region_name", None)
        materials = {str(material) for material in self.source_mesh.GetMaterials()}
        if not name or name == self.all_regions_name or name not in materials:
            return self.source_mesh.Materials(".*")
        return self.source_mesh.Materials(re.escape(str(name)))

    def update(self, options):
        if not self.needs_update:
            return
        if self.source_cf.dim != 2:
            return super().update(options)

        import ngsolve as ngs
        import ngsolve.webgui

        line_count = max(1, int(self.fieldline_options["num_lines"]))
        plane_count = min(
            line_count,
            max(1, min(12, int(round(math.sqrt(line_count))))),
        )
        meridian_line_count = max(1, math.ceil(line_count / plane_count))
        meridian_field = ngs.CF((self.source_cf[0], self.source_cf[1], 0))

        # NGSolve selects line seeds with numpy's global RNG. Keep the preview
        # repeatable without changing random sequences elsewhere in the app.
        rng_state = np.random.get_state()
        try:
            np.random.seed(0)
            data = ngsolve.webgui.FieldLines(
                meridian_field,
                self._source_seed_region(),
                num_lines=meridian_line_count,
                length=self.fieldline_options["length"],
                max_points_per_line=self.fieldline_options["max_points_per_line"],
                thickness=self.fieldline_options["thickness"],
                tolerance=self.fieldline_options["tolerance"],
                direction=self.fieldline_options["direction"],
            )
        finally:
            np.random.set_state(rng_state)

        self.positions, self.directions, self.values = (
            revolve_meridian_fieldline_arrays(data, plane_count)
        )
        if self.positions.size == 0:
            # Keep an empty source trace empty. Re-tracing on the display mesh
            # changes seed geometry and can create misleading 3D paths.
            ShapeRenderer.update(self, options)
            return

        bbox = self.mesh.ngmesh.bounding_box
        thickness = (
            (bbox[1] - bbox[0]).Norm() * self.fieldline_options["thickness"]
        )
        self.shape_data = generate_cylinder(
            8, thickness, 1.0, top_face=False, bottom_face=False
        )
        ShapeRenderer.update(self, options)
