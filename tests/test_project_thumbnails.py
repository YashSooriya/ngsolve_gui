import base64

from ngsolve_gui import project_thumbnails as thumbnails
from ngsolve_gui.axisymmetric_model import (
    new_model,
    new_studies,
    builtin_materials,
    serialize_model_json,
    validate_model,
)
from ngsolve_gui.three_d_project import (
    add_step_geometry,
    add_wire_trace,
    new_3d_project,
    package_3d_project,
)


def _axisymmetric_project_bytes(*, width=0.03):
    model = new_model("Axisymmetric thumbnail")
    model["materials"] = builtin_materials()
    r_min, z_min, height = 0.02, -0.1, 0.2
    vertices = [
        [r_min, z_min],
        [r_min + width, z_min],
        [r_min + width, z_min + height],
        [r_min, z_min + height],
    ]
    region = {
        "id": "thumbnail-region",
        "name": "Shield",
        "material_id": "material-air",
        "parent_id": None,
        "mechanical": False,
        "vertices": vertices,
        "shape": {
            "type": "rectangle",
            "r_min": r_min,
            "z_min": z_min,
            "width": width,
            "height": height,
            "dimension_expressions": {
                "r_min": str(r_min), "z_min": str(z_min),
                "width": str(width), "height": str(height),
            },
        },
        "sources": {},
        "edge_ids": [],
    }
    model["geometry"]["regions"] = [region]
    for index, start in enumerate(vertices):
        end = vertices[(index + 1) % len(vertices)]
        edge_id = f"thumb-edge-{index}"
        region["edge_ids"].append(edge_id)
        model["geometry"]["edges"].append({
            "id": edge_id,
            "name": edge_id,
            "vertices": [start, end],
            "boundary_condition_ids": [],
        })
    assert validate_model(model, scope="draft") == []
    return serialize_model_json(
        model,
        new_studies(),
        {"schema_version": 1, "active_section": "geometry", "camera": "fit"},
    )


def test_axisymmetric_thumbnail_is_cached_and_invalidated_after_geometry_save(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(thumbnails, "cache_directory", lambda: tmp_path / "cache")
    project_path = tmp_path / "Meridian.json"
    first = _axisymmetric_project_bytes(width=0.03)
    project_path.write_bytes(first)

    assert thumbnails.generate_project_thumbnail(
        project_path, first, kind="axisymmetric"
    )
    first_uri = thumbnails.cached_thumbnail(project_path)
    assert first_uri.startswith("data:image/png;base64,")
    first_png = base64.b64decode(first_uri.split(",", 1)[1])
    assert first_png.startswith(b"\x89PNG\r\n\x1a\n")

    second = _axisymmetric_project_bytes(width=0.05)
    project_path.write_bytes(second)
    assert thumbnails.cached_thumbnail(project_path) is None
    assert thumbnails.generate_project_thumbnail(
        project_path, second, kind="axisymmetric"
    )
    second_uri = thumbnails.cached_thumbnail(project_path)
    assert second_uri is not None
    assert base64.b64decode(second_uri.split(",", 1)[1]) != first_png
    assert project_path.read_bytes() == second


def test_3d_thumbnail_uses_geometry_from_project_assets(monkeypatch, tmp_path):
    fixtures = __import__("pathlib").Path(__file__).parent / "fixtures"
    step_path = fixtures / "magnet_shield_4k.step"
    project = new_3d_project("Shield assembly")
    assets = {}
    add_step_geometry(
        project,
        assets,
        step_path.name,
        step_path.read_bytes(),
        units="mm",
    )
    payload = package_3d_project(project, assets)
    project_path = tmp_path / "Shield.mmfem3d"
    project_path.write_bytes(payload)
    monkeypatch.setattr(thumbnails, "cache_directory", lambda: tmp_path / "cache")

    assert thumbnails.generate_project_thumbnail(
        project_path, payload, kind="3d"
    )
    uri = thumbnails.cached_thumbnail(project_path)
    assert uri is not None
    png = base64.b64decode(uri.split(",", 1)[1])
    from PIL import Image
    from io import BytesIO

    with Image.open(BytesIO(png)) as image:
        assert image.size == thumbnails._SIZE
        assert len(image.getcolors(maxcolors=100_000) or []) > 4
    assert project_path.read_bytes() == payload


def test_3d_thumbnail_shows_wire_trace_only_projects(monkeypatch, tmp_path):
    project = new_3d_project("Wire trace assembly")
    assets = {}
    add_wire_trace(
        project,
        assets,
        "coil-path.txt",
        b"0 0 0\n0.1 0 0\n0.1 0.1 0\n0 0.1 0\n",
        units="m",
    )
    payload = package_3d_project(project, assets)
    project_path = tmp_path / "Wire trace only.mmfem3d"
    project_path.write_bytes(payload)
    monkeypatch.setattr(thumbnails, "cache_directory", lambda: tmp_path / "cache")

    assert thumbnails.generate_project_thumbnail(project_path, payload, kind="3d")
    uri = thumbnails.cached_thumbnail(project_path)
    assert uri is not None
    png = base64.b64decode(uri.split(",", 1)[1])
    from PIL import Image
    from io import BytesIO

    with Image.open(BytesIO(png)) as image:
        colors = image.getcolors(maxcolors=100_000)
        assert image.size == thumbnails._SIZE
        assert colors is not None and len(colors) > 2
        assert any(color not in {(247, 249, 252), (0, 0, 0)} for _count, color in colors)
    assert project_path.read_bytes() == payload


def test_thumbnail_generation_rejects_a_project_changed_mid_render(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(thumbnails, "cache_directory", lambda: tmp_path / "cache")
    project_path = tmp_path / "Meridian.json"
    first = _axisymmetric_project_bytes(width=0.03)
    project_path.write_bytes(first)
    stat = project_path.stat()
    project_path.write_bytes(_axisymmetric_project_bytes(width=0.05))

    assert not thumbnails.generate_project_thumbnail(
        project_path,
        first,
        kind="axisymmetric",
        expected_stat=(stat.st_size, stat.st_mtime_ns),
    )
    assert thumbnails.cached_thumbnail(project_path) is None
