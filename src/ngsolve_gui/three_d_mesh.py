"""Netgen volume meshing helpers for imported STEP solids."""

from __future__ import annotations

import math
import tempfile
from collections import Counter
from pathlib import Path

from .three_d_project import MAX_MESH_SIZE_MM, MIN_MESH_SIZE_MM, step_face_tags

MAX_ESTIMATED_VOLUME_ELEMENTS = 3_000_000
MAX_VOLUME_ELEMENTS = 2_000_000
MAX_BOUNDARY_FACETS = 300_000
MAX_WIREFRAME_EDGES = 250_000
LOW_TET_QUALITY_THRESHOLD = 0.1


def _tetra_quality(vertices, connectivity):
    """Return the normalized mean-ratio quality for tetrahedral elements."""
    import numpy as np

    if connectivity.ndim != 2 or connectivity.shape[1] != 4:
        return None
    points = vertices[connectivity]
    edge_pairs = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))
    edge_sum = sum(
        np.einsum("ij,ij->i", points[:, left] - points[:, right], points[:, left] - points[:, right])
        for left, right in edge_pairs
    )
    determinant = np.einsum(
        "ij,ij->i",
        points[:, 1] - points[:, 0],
        np.cross(points[:, 2] - points[:, 0], points[:, 3] - points[:, 0]),
    )
    volume = np.abs(determinant) / 6.0
    with np.errstate(divide="ignore", invalid="ignore"):
        quality = 12.0 * np.power(3.0 * volume, 2.0 / 3.0) / edge_sum
    return np.clip(np.nan_to_num(quality, nan=0.0, posinf=0.0), 0.0, 1.0)


def _named_regions(ids, counts, names, fallback, *, include_boundary_tags=False):
    """Summarize region indices with unique display names and element counts."""
    regions = []
    used_names = set()
    for region_id in sorted(set(ids)):
        raw_name = str(names[region_id]).strip() if region_id < len(names) else ""
        boundary_tag = (
            raw_name
            if include_boundary_tags and raw_name.startswith("MMFEM_FACE_")
            else None
        )
        if boundary_tag:
            raw_name = f"{fallback} {region_id + 1}"
        if not raw_name or raw_name.casefold() == "default" or raw_name.casefold() in used_names:
            raw_name = f"{fallback} {region_id + 1}"
        used_names.add(raw_name.casefold())
        region = {
            "id": int(region_id),
            "name": raw_name,
            "elements": int(counts.get(region_id, 0)),
        }
        if include_boundary_tags:
            region["boundary_tag"] = boundary_tag
        regions.append(region)
    return regions


def generate_step_mesh(data, *, maxh_mm=30.0, domain_maxh_mm=None):
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
        from netgen.occ import Glue, OCCGeometry
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
            domain_sizes = {}
            for region_id, value in (domain_maxh_mm or {}).items():
                if type(region_id) is not int or not 0 <= region_id < len(solids):
                    raise ValueError(
                        "A local mesh size refers to a domain that is not in this STEP model."
                    )
                try:
                    local_maxh = float(value)
                except (TypeError, ValueError, OverflowError) as error:
                    raise ValueError(
                        "Local maximum element sizes must be finite numbers in millimetres."
                    ) from error
                if not math.isfinite(local_maxh) or not MIN_MESH_SIZE_MM <= local_maxh <= MAX_MESH_SIZE_MM:
                    raise ValueError(
                        f"Local maximum element size must be between {MIN_MESH_SIZE_MM:g} "
                        f"and {MAX_MESH_SIZE_MM:g} mm."
                    )
                domain_sizes[region_id] = local_maxh

            estimated_elements = 0
            for region_id, solid in enumerate(solids):
                local_maxh = domain_sizes.get(region_id, maxh)
                solid.maxh = local_maxh
                estimated_elements += 12.0 * float(solid.mass) / local_maxh**3
            estimated_elements = math.ceil(estimated_elements)
            if estimated_elements > MAX_ESTIMATED_VOLUME_ELEMENTS:
                suggested = (12.0 * volume / MAX_ESTIMATED_VOLUME_ELEMENTS) ** (1 / 3)
                raise ValueError(
                    "The requested sizes are estimated to create an impractically "
                    f"large mesh. Increase it to about {suggested:.3g} mm or larger."
                )
            for tag, face in step_face_tags(solids):
                face.bc(tag)
            netgen_mesh = OCCGeometry(Glue(solids)).GenerateMesh()
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
        volume_connectivity = np.asarray(
            [[int(vertex.nr) for vertex in element.vertices] for element in volume_elements],
            dtype=np.int32,
        )
        domain_ids = [int(element.index) for element in volume_elements]
        domain_regions = _named_regions(
            domain_ids,
            Counter(domain_ids),
            list(mesh.GetMaterials()),
            "Domain",
        )
        tetra_quality = _tetra_quality(vertices_mm, volume_connectivity)
        quality = None
        if tetra_quality is not None and len(tetra_quality):
            quality = {
                "metric": "tetrahedral mean ratio (0–1)",
                "minimum": float(np.min(tetra_quality)),
                "p05": float(np.percentile(tetra_quality, 5)),
                "median": float(np.median(tetra_quality)),
                "mean": float(np.mean(tetra_quality)),
                "below_0_1": int(np.count_nonzero(tetra_quality < LOW_TET_QUALITY_THRESHOLD)),
                "below_0_1_percent": float(
                    100.0 * np.count_nonzero(tetra_quality < LOW_TET_QUALITY_THRESHOLD)
                    / len(tetra_quality)
                ),
            }
        surface_triangles = []
        surface_region_ids = []
        for element in mesh.Elements(ngs.BND):
            indices = [int(vertex.nr) for vertex in element.vertices]
            if len(indices) < 3:
                continue
            for index in range(1, len(indices) - 1):
                surface_triangles.append((indices[0], indices[index], indices[index + 1]))
                surface_region_ids.append(int(element.index))
        if not surface_triangles:
            raise ValueError("The generated volume mesh has no displayable boundary facets.")
        if len(surface_triangles) > MAX_BOUNDARY_FACETS:
            raise ValueError(
                "The generated boundary has more than 300,000 facets, which is "
                "too large to display interactively. Increase the maximum element size."
            )
        triangles = np.asarray(surface_triangles, dtype=np.int32)
        boundary_regions = _named_regions(
            surface_region_ids,
            Counter(surface_region_ids),
            list(mesh.GetBoundaries()),
            "Boundary",
            include_boundary_tags=True,
        )

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
            "boundary_region_ids": np.asarray(surface_region_ids, dtype=np.int32),
            "edges": edges,
            "volume_elements": len(volume_elements),
            "domain_regions": domain_regions,
            "boundary_regions": boundary_regions,
            "tetra_quality": quality,
            "surface_faces": len(triangles),
            "wireframe_available": wireframe_available,
            "estimated_volume_elements": estimated_elements,
            "maxh_mm": maxh,
            "domain_maxh_mm": domain_sizes,
        }
    except ValueError:
        raise
    except Exception as error:
        raise RuntimeError(f"Could not extract the generated mesh boundary: {error}") from error
