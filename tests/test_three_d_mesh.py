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
    assert result["edges"].shape[1] == 2
    assert result["wireframe_available"] is True
    assert result["triangles"].max() < len(result["vertices_mm"])


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
