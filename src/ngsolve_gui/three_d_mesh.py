"""Netgen volume meshing helpers for imported STEP solids."""

from __future__ import annotations

import math
import tempfile
from pathlib import Path

from .three_d_project import MAX_MESH_SIZE_MM, MIN_MESH_SIZE_MM

MAX_ESTIMATED_VOLUME_ELEMENTS = 3_000_000
MAX_VOLUME_ELEMENTS = 2_000_000
MAX_BOUNDARY_FACETS = 300_000
MAX_WIREFRAME_EDGES = 250_000


def generate_step_mesh(data, *, maxh_mm=30.0):
    """Generate a 3D volume mesh and return it with a displayable boundary.

    Netgen's OCC STEP reader uses millimetres for model coordinates. The
    returned coordinates therefore remain in millimetres; project unit scaling
    is applied by the 3D preview when the boundary is displayed.
    """
    try:
        maxh = float(maxh_mm)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("Maximum element size must be a finite number in millimetres.") from error
    if not math.isfinite(maxh) or not MIN_MESH_SIZE_MM <= maxh <= MAX_MESH_SIZE_MM:
        raise ValueError(
            f"Maximum element size must be between {MIN_MESH_SIZE_MM:g} and "
            f"{MAX_MESH_SIZE_MM:g} mm."
        )

    try:
        import numpy as np
        import ngsolve as ngs
        from netgen.occ import OCCGeometry
    except ImportError as error:
        raise RuntimeError("3D mesh generation requires the Netgen/NGSolve runtime.") from error

    with tempfile.TemporaryDirectory(prefix="mmfem_step_mesh_") as directory:
        source = Path(directory) / "geometry.step"
        source.write_bytes(bytes(data))
        try:
            geometry = OCCGeometry(str(source))
            solids = list(geometry.solids)
            if not solids:
                raise ValueError(
                    "This STEP file does not contain a closed solid. "
                    "A volume mesh needs a closed 3D body."
                )
            volume = sum(float(solid.mass) for solid in solids)
            estimated_elements = math.ceil(12.0 * volume / maxh**3)
            if estimated_elements > MAX_ESTIMATED_VOLUME_ELEMENTS:
                suggested = (12.0 * volume / MAX_ESTIMATED_VOLUME_ELEMENTS) ** (1 / 3)
                raise ValueError(
                    "The requested size is estimated to create an impractically "
                    f"large mesh. Increase it to about {suggested:.3g} mm or larger."
                )
            netgen_mesh = geometry.GenerateMesh(maxh=maxh)
        except ValueError:
            raise
        except Exception as error:
            detail = str(error).strip() or type(error).__name__
            raise RuntimeError(f"Netgen could not mesh this STEP solid: {detail}") from error

    try:
        mesh = ngs.Mesh(netgen_mesh)
        if mesh.dim != 3:
            raise ValueError("Netgen did not produce a 3D volume mesh.")
        volume_elements = list(mesh.Elements(ngs.VOL))
        if not volume_elements:
            raise ValueError("Netgen produced no 3D volume elements for this STEP solid.")
        if len(volume_elements) > MAX_VOLUME_ELEMENTS:
            raise ValueError(
                "The generated mesh exceeds the 2 million volume-element limit. "
                "Increase the maximum element size and try again."
            )

        vertices_mm = np.asarray(
            [tuple(mesh[vertex].point)[:3] for vertex in mesh.vertices],
            dtype=np.float64,
        )
        surface_triangles = []
        for element in mesh.Elements(ngs.BND):
            indices = [int(vertex.nr) for vertex in element.vertices]
            if len(indices) < 3:
                continue
            for index in range(1, len(indices) - 1):
                surface_triangles.append((indices[0], indices[index], indices[index + 1]))
        if not surface_triangles:
            raise ValueError("The generated volume mesh has no displayable boundary facets.")
        if len(surface_triangles) > MAX_BOUNDARY_FACETS:
            raise ValueError(
                "The generated boundary has more than 300,000 facets, which is "
                "too large to display interactively. Increase the maximum element size."
            )
        triangles = np.asarray(surface_triangles, dtype=np.int32)

        edge_pairs = np.concatenate(
            (triangles[:, [0, 1]], triangles[:, [1, 2]], triangles[:, [2, 0]]),
            axis=0,
        )
        edge_pairs.sort(axis=1)
        edges = np.unique(edge_pairs, axis=0).astype(np.int32, copy=False)
        wireframe_available = len(edges) <= MAX_WIREFRAME_EDGES
        if not wireframe_available:
            edges = np.empty((0, 2), dtype=np.int32)

        return {
            "mesh": mesh,
            "vertices_mm": vertices_mm.astype(np.float32),
            "triangles": triangles,
            "edges": edges,
            "volume_elements": len(volume_elements),
            "surface_faces": len(triangles),
            "wireframe_available": wireframe_available,
            "estimated_volume_elements": estimated_elements,
            "maxh_mm": maxh,
        }
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(f"Could not extract the generated mesh boundary: {error}") from error
