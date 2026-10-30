from pathlib import Path

import pytest

from ngsolve_gui.three_d_mesh import generate_step_mesh


def _box_step(tmp_path: Path):
    from netgen.occ import Box, Pnt

    path = tmp_path / "box.step"
    Box(Pnt(0, 0, 0), Pnt(10, 10, 10)).WriteStep(str(path))
    return path.read_bytes()


def test_generates_a_volume_mesh_and_exterior_triangles_for_step_solid(tmp_path):
    result = generate_step_mesh(_box_step(tmp_path), maxh_mm=2.5)

    assert result["mesh"].dim == 3
    assert result["volume_elements"] > 0
    assert result["surface_faces"] > 0
    assert result["vertices_mm"].shape[1] == 3
    assert result["triangles"].shape[1] == 3
    assert result["boundary_region_ids"].shape == (result["surface_faces"],)
    assert result["edges"].shape[1] == 2
    assert result["wireframe_available"] is True
    assert result["triangles"].max() < len(result["vertices_mm"])
    assert result["domain_regions"][0]["elements"] == result["volume_elements"]
    assert len(result["boundary_regions"]) == 6
    assert sum(region["elements"] for region in result["boundary_regions"]) == result["surface_faces"]
    assert 0 <= result["tetra_quality"]["minimum"] <= result["tetra_quality"]["median"] <= 1
    assert result["tetra_quality"]["below_0_1"] <= result["volume_elements"]


def test_tetrahedral_mean_ratio_quality_is_one_for_a_regular_tetrahedron():
    import numpy as np

    from ngsolve_gui.three_d_mesh import _tetra_quality

    vertices = np.asarray([
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.5, 3**0.5 / 2, 0.0],
        [0.5, 3**0.5 / 6, (2 / 3) ** 0.5],
    ])

    quality = _tetra_quality(vertices, np.asarray([[0, 1, 2, 3]]))

    assert quality[0] == pytest.approx(1.0)


def test_rejects_an_open_step_sheet_as_a_volume_mesh_input(tmp_path):
    from netgen.occ import Face, Pnt, Segment, Wire

    path = tmp_path / "sheet.step"
    wire = Wire([
        Segment(Pnt(0, 0, 0), Pnt(10, 0, 0)),
        Segment(Pnt(10, 0, 0), Pnt(10, 10, 0)),
        Segment(Pnt(10, 10, 0), Pnt(0, 10, 0)),
        Segment(Pnt(0, 10, 0), Pnt(0, 0, 0)),
    ])
    Face(wire).WriteStep(str(path))

    with pytest.raises(ValueError, match="does not contain a closed solid"):
        generate_step_mesh(path.read_bytes(), maxh_mm=2.5)


@pytest.mark.parametrize("maxh", [0, -1, float("nan"), float("inf"), "invalid"])
def test_rejects_invalid_maximum_element_size_before_meshing(maxh):
    with pytest.raises(ValueError, match="Maximum element size"):
        generate_step_mesh(b"not a STEP file", maxh_mm=maxh)


def test_rejects_a_mesh_size_that_would_create_an_impractical_volume_mesh(tmp_path):
    with pytest.raises(ValueError, match="impractically large"):
        generate_step_mesh(_box_step(tmp_path), maxh_mm=0.01)
