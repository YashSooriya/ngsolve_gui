"""Coarse seeding for responsive 3-D streamlines in imported result fields."""

from __future__ import annotations

import time
import types

import numpy as np
import ngsolve as ngs
from ngsolve.webgui import FieldLines as _trace_fieldlines
from ngsolve_webgpu.cf import generate_cylinder


def install_fast_fieldline_update(renderer) -> None:
    """Use a cached coarse seed set and bounded line length for one renderer.

    NGSolve's default tracer scans every element with a fifth-order integration
    rule each time it is asked to draw field lines. For imported 3-D result
    files, a first-order seed sample is sufficient to select well-distributed
    starts; line integration itself still uses NGSolve's normal tracer.
    """
    renderer.fieldline_options["max_points_per_line"] = min(
        150,
        int(renderer.fieldline_options.get("max_points_per_line", 150)),
    )
    base_update = renderer.__class__.__mro__[1].update
    seed_points_cache: dict[tuple[str, int], np.ndarray] = {}
    cached_key = None
    cached_data = None

    def update(self, options):
        if not self.needs_update:
            return

        nonlocal cached_key, cached_data
        material_name = getattr(self, "_ngsolve_gui_start_region_name", None)
        region = self.start_region
        requested_lines = max(1, int(self.fieldline_options.get("num_lines", 20)))
        candidate_count = min(4096, max(256, 16 * requested_lines))
        seed_cache_key = (material_name or "<current>", candidate_count)
        seed_points = seed_points_cache.get(seed_cache_key)
        if seed_points is None:
            started = time.perf_counter()
            rules = {
                element_type: ngs.IntegrationRule(element_type, 1)
                for element_type in (
                    ngs.ET.TRIG,
                    ngs.ET.QUAD,
                    ngs.ET.TET,
                    ngs.ET.HEX,
                    ngs.ET.PRISM,
                    ngs.ET.PYRAMID,
                    ngs.ET.SEGM,
                )
            }
            mapped_points = self.mesh.MapToAllElements(rules, region)
            seed_points = np.asarray(
                ngs.CF((ngs.x, ngs.y, ngs.z))(mapped_points), dtype=float
            ).reshape(-1, 3)
            seed_points = seed_points[np.all(np.isfinite(seed_points), axis=1)]
            mapped_count = len(seed_points)
            if mapped_count > candidate_count:
                indices = np.linspace(
                    0, mapped_count - 1, candidate_count, dtype=np.int64
                )
                seed_points = seed_points[indices]
            seed_points_cache[seed_cache_key] = seed_points
            print(
                f"Prepared {len(seed_points)} coarse streamline seed candidates "
                f"in {time.perf_counter() - started:.2f}s.",
                flush=True,
            )

        trace_options = dict(self.fieldline_options)
        cache_key = (
            material_name or "<current>",
            tuple(sorted(trace_options.items())),
        )
        data = cached_data if cache_key == cached_key else None
        if data is None:
            if self.seed is not None:
                np.random.seed(self.seed)
            started = time.perf_counter()
            print(
                f"Tracing streamlines from {len(seed_points)} candidates...",
                flush=True,
            )
            data = _trace_fieldlines(
                self.cf,
                self.start_region,
                mesh=self.mesh,
                start_points=seed_points,
                **trace_options,
            )
            cached_key = cache_key
            cached_data = data
            print(
                f"Created {len(data.get('pstart', ()))} streamline segments in "
                f"{time.perf_counter() - started:.2f}s.",
                flush=True,
            )

        starts = np.asarray(data["pstart"])
        ends = np.asarray(data["pend"])
        self.values = np.asarray(data["value"])
        bbox = self.mesh.ngmesh.bounding_box
        thickness = (
            (bbox[1] - bbox[0]).Norm()
            * self.fieldline_options["thickness"]
        )
        self.shape_data = generate_cylinder(
            8, thickness, 1.0, top_face=False, bottom_face=False
        )
        self.positions = starts
        self.directions = ends - starts
        return base_update(self, options)

    renderer.update = types.MethodType(update, renderer)
