"""Small cached previews for the Recent Projects chooser.

The cache lives outside project files so thumbnails do not change either
project format. Rendering happens on save or on a background cache refresh,
never while the launch screen is being constructed.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
from pathlib import Path
import tempfile


_SIZE = (288, 176)
_SCALE = 2
_MAX_THUMBNAIL_TRIANGLES = 12_000
_MAX_THUMBNAIL_TRACE_POINTS = 2_000
_MATERIAL_COLORS = ("#6688ad", "#ce8158", "#6b9b7e", "#9b82a8", "#c4a950", "#4b9da4")
_TRACE_COLORS = ("#14877d", "#d45b35", "#7553a8", "#2871a1", "#9a7421")
_REGION_COLORS = ("#d7e4f0", "#efddce", "#d9e8dc", "#e7deec", "#eee8d0", "#d4e8e9")


def cache_directory() -> Path:
    """Return the existing per-user thumbnail cache directory."""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "MagMechSim" / "ProjectThumbnails"
    cache = os.environ.get("XDG_CACHE_HOME")
    return (Path(cache) if cache else Path.home() / ".cache") / "MagMechSim" / "ProjectThumbnails"


def _normalized_path(path) -> str:
    value = os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))
    return os.path.normcase(value)


def _path_key(path) -> str:
    return hashlib.sha256(_normalized_path(path).encode("utf-8", "surrogatepass")).hexdigest()


def _cache_paths(path):
    directory = cache_directory()
    key = _path_key(path)
    return directory, directory / f"{key}.json"


def cached_thumbnail(path):
    """Return a data URI for a valid cache entry, or ``None`` if stale/missing."""
    try:
        source = Path(path)
        stat = source.stat()
        directory, manifest_path = _cache_paths(source)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            manifest.get("path") != _normalized_path(source)
            or manifest.get("size") != stat.st_size
            or manifest.get("mtime_ns") != stat.st_mtime_ns
        ):
            return None
        filename = manifest.get("thumbnail")
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        image_path = directory / filename
        if not image_path.is_file() or image_path.stat().st_size > 2_000_000:
            return None
        payload = image_path.read_bytes()
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def generate_project_thumbnail(path, project_data=None, *, kind=None, expected_stat=None):
    """Render a bounded actual-geometry thumbnail and atomically cache it.

    ``project_data`` is the exact byte string just saved, when available. If
    omitted, the current file is read for a legacy/missing-cache refresh.
    Returns ``True`` on success and ``False`` when the file has no renderable
    geometry or optional preview dependencies are unavailable.
    """
    source = Path(path)
    try:
        before = source.stat()
        before_signature = (before.st_size, before.st_mtime_ns)
        if expected_stat is not None and before_signature != tuple(expected_stat):
            return False
        payload = bytes(project_data) if project_data is not None else source.read_bytes()
        if project_data is None:
            after_read = source.stat()
            if (after_read.st_size, after_read.st_mtime_ns) != before_signature:
                return False
        digest = hashlib.sha256(payload).hexdigest()
        if project_data is not None and _file_sha256(source) != digest:
            return False
        if kind not in {"axisymmetric", "3d"}:
            from .app import NGSolveGui

            kind, _content, _assets = NGSolveGui._decode_project_file(payload)
        if kind == "axisymmetric":
            image = _axisymmetric_image(payload)
        elif kind == "3d":
            image = _three_d_image(payload)
        else:
            return False
        if image is None:
            return False

        stat = source.stat()
        if (stat.st_size, stat.st_mtime_ns) != before_signature:
            return False
        if _file_sha256(source) != digest:
            return False
        directory, manifest_path = _cache_paths(source)
        directory.mkdir(parents=True, exist_ok=True)
        image_buffer = io.BytesIO()
        image.save(image_buffer, format="PNG", optimize=True)
        image_bytes = image_buffer.getvalue()
        filename = f"{_path_key(source)}-{digest[:20]}.png"
        image_path = directory / filename
        _atomic_write(image_path, image_bytes)
        manifest = {
            "version": 1,
            "path": _normalized_path(source),
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "project_sha256": digest,
            "kind": kind,
            "thumbnail": filename,
        }
        _atomic_write(manifest_path, json.dumps(manifest, separators=(",", ":")).encode("utf-8"))
        _prune_cache_versions(directory, _path_key(source), filename)
        return True
    except Exception:
        # A thumbnail must never make project save/open fail. The project
        # remains usable with the chooser's neutral preview placeholder.
        return False


def _atomic_write(path: Path, payload: bytes):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _prune_cache_versions(directory: Path, key: str, current: str):
    """Keep the current preview and one previous version for concurrent readers."""
    candidates = sorted(
        (item for item in directory.glob(f"{key}-*.png") if item.name != current),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for item in candidates[1:]:
        item.unlink(missing_ok=True)


def _canvas():
    from PIL import Image, ImageDraw

    width, height = (value * _SCALE for value in _SIZE)
    image = Image.new("RGB", (width, height), "#f7f9fc")
    return image, ImageDraw.Draw(image)


def _finish(image):
    from PIL import Image

    resampling = getattr(Image, "Resampling", Image).LANCZOS
    return image.resize(_SIZE, resampling)


def _fit_2d(points, *, padding=14):
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    width = max(x_max - x_min, 1e-15)
    height = max(y_max - y_min, 1e-15)
    canvas_width, canvas_height = (value * _SCALE for value in _SIZE)
    scale = min((canvas_width - 2 * padding * _SCALE) / width,
                (canvas_height - 2 * padding * _SCALE) / height)
    offset_x = (canvas_width - width * scale) / 2
    offset_y = (canvas_height - height * scale) / 2
    return lambda point: (
        round(offset_x + (point[0] - x_min) * scale),
        round(canvas_height - offset_y - (point[1] - y_min) * scale),
    )


def _axisymmetric_image(payload):
    try:
        from .axisymmetric_model import unpack_model, sample_sketch_entity

        model, _studies, _layout = unpack_model(payload)
        geometry = model.get("geometry", {})
        regions = geometry.get("regions", [])
        polygons = []
        bounds = []
        for index, region in enumerate(regions):
            vertices = region.get("vertices", [])
            points = []
            for point in vertices:
                try:
                    r, z = float(point[0]), float(point[1])
                except (TypeError, ValueError, IndexError, OverflowError):
                    continue
                if math.isfinite(r) and math.isfinite(z):
                    points.append((r, z))
            if len(points) >= 3:
                polygons.append((points, _REGION_COLORS[index % len(_REGION_COLORS)]))
                bounds.extend(points)

        sketch_paths = []
        if not polygons:
            for entity in geometry.get("sketch_entities", []):
                points = sample_sketch_entity(entity, count=48)
                if len(points) >= 2:
                    path = [(float(point[0]), float(point[1])) for point in points]
                    if all(math.isfinite(value) for point in path for value in point):
                        sketch_paths.append(path)
                        bounds.extend(path)
        if not bounds:
            return None

        image, draw = _canvas()
        project = _fit_2d(bounds)
        for points, fill in polygons:
            draw.polygon([project(point) for point in points], fill=fill,
                         outline="#38516c", width=2 * _SCALE)
        for path in sketch_paths:
            draw.line([project(point) for point in path], fill="#38516c",
                      width=3 * _SCALE, joint="curve")
        return _finish(image)
    except Exception:
        return None


def _three_d_image(payload):
    try:
        import numpy as np
        from netgen.occ import OCCGeometry
        from .three_d_project import parse_wire_trace, step_unit_scale, unpack_3d_project
        from .three_d_preview import _sample_trace_points

        project, assets = unpack_3d_project(payload)
        meshes = []
        for index, geometry in enumerate(project.get("geometries", [])):
            data = assets[geometry["asset"]]
            scale = step_unit_scale(data, units=geometry.get("units", "m"))
            with tempfile.TemporaryDirectory(prefix="mmfem_recent_thumb_") as directory:
                source = Path(directory) / "geometry.step"
                source.write_bytes(data)
                visual = OCCGeometry(str(source))._visualizationData()
            raw = visual.get("vertices")
            if raw is None or len(raw) < 9 or len(raw) % 9:
                continue
            triangle_count = len(raw) // 9
            stride = max(1, math.ceil(triangle_count / _MAX_THUMBNAIL_TRIANGLES))
            corners = np.asarray(raw, dtype=np.float32).reshape((-1, 3, 3))[::stride]
            corners = corners[:_MAX_THUMBNAIL_TRIANGLES] * scale
            if np.isfinite(corners).all():
                meshes.append((corners, _MATERIAL_COLORS[index % len(_MATERIAL_COLORS)]))
        traces = []
        trace_index = 0
        for trace_set in project.get("wire_trace_sets", []):
            for trace in trace_set.get("traces", []):
                points = parse_wire_trace(assets[trace["filename"]], "m")
                points = _sample_trace_points(points, _MAX_THUMBNAIL_TRACE_POINTS)
                if any(point is not None for point in points):
                    traces.append((
                        points,
                        _TRACE_COLORS[trace_index % len(_TRACE_COLORS)],
                    ))
                    trace_index += 1
        if not meshes and not traces:
            return None

        point_clouds = [triangles.reshape((-1, 3)) for triangles, _ in meshes]
        point_clouds.extend(
            np.asarray([point for point in points if point is not None], dtype=np.float32)
            for points, _ in traces
        )
        points = np.concatenate(point_clouds, axis=0)
        camera = np.asarray((1.0, -1.0, 1.0), dtype=np.float32)
        camera /= np.linalg.norm(camera)
        right = np.asarray((1.0, 1.0, 0.0), dtype=np.float32)
        right /= np.linalg.norm(right)
        up = np.cross(camera, right)
        up /= np.linalg.norm(up)
        projected_x = points @ right
        projected_y = points @ up
        x_min, x_max = float(projected_x.min()), float(projected_x.max())
        y_min, y_max = float(projected_y.min()), float(projected_y.max())
        width, height = max(x_max - x_min, 1e-15), max(y_max - y_min, 1e-15)
        image, draw = _canvas()
        canvas_width, canvas_height = image.size
        margin = 0.09 * min(canvas_width, canvas_height)
        scale = min((canvas_width - 2 * margin) / width,
                    (canvas_height - 2 * margin) / height)
        x_offset = (canvas_width - width * scale) / 2
        y_offset = (canvas_height - height * scale) / 2
        light = np.asarray((0.4, -0.6, 0.7), dtype=np.float32)
        light /= np.linalg.norm(light)
        draw_records = []
        for triangles, color in meshes:
            count = len(triangles)
            flat = triangles.reshape((-1, 3))
            tri_x = (flat @ right).reshape((-1, 3))
            tri_y = (flat @ up).reshape((-1, 3))
            depth = (triangles.mean(axis=1) @ camera)
            normals = np.cross(triangles[:, 1] - triangles[:, 0],
                               triangles[:, 2] - triangles[:, 0])
            normal_lengths = np.linalg.norm(normals, axis=1)
            normal_lengths[normal_lengths == 0] = 1.0
            normals /= normal_lengths[:, None]
            brightness = 0.42 + 0.58 * np.clip(normals @ light, 0.0, 1.0)
            base = tuple(int(color[offset:offset + 2], 16) for offset in (1, 3, 5))
            for local_index in range(count):
                x_values = tri_x[local_index]
                y_values = tri_y[local_index]
                polygon = [
                    (round(x_offset + (float(x) - x_min) * scale),
                     round(canvas_height - y_offset - (float(y) - y_min) * scale))
                    for x, y in zip(x_values, y_values)
                ]
                shade = float(brightness[local_index])
                fill = tuple(max(0, min(255, round(channel * shade))) for channel in base)
                draw_records.append((float(depth[local_index]), polygon, fill))
        for _depth, polygon, fill in sorted(draw_records, key=lambda item: item[0]):
            draw.polygon(polygon, fill=fill)
        for trace_points, color in traces:
            previous = None
            for point in trace_points:
                if point is None:
                    previous = None
                    continue
                projected = (
                    round(x_offset + (float(np.dot(point, right)) - x_min) * scale),
                    round(canvas_height - y_offset - (float(np.dot(point, up)) - y_min) * scale),
                )
                if previous is not None:
                    draw.line((previous, projected), fill=color, width=3 * _SCALE)
                previous = projected
        return _finish(image)
    except Exception:
        return None
