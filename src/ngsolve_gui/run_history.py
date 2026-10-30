"""Helpers for turning a saved solver run into navigable result entries."""

import json
from pathlib import Path


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _output_file(output_root, base_directory, value):
    if not isinstance(value, str) or not value.strip():
        return None
    path = Path(value)
    if not path.is_absolute():
        path = base_directory / path
    try:
        path = path.resolve()
        path.relative_to(output_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    return path if path.is_file() else None


def load_run_result_tree(run):
    """Return one history record enriched with its existing result files.

    Solver metadata paths are resolved relative to their owning output folder
    and constrained to that run. This keeps a stale or malformed manifest from
    turning a navigator action into an arbitrary file load.
    """
    output_value = run.get("output_path") if isinstance(run, dict) else None
    if not output_value:
        return {**(run or {}), "default_path": None, "results": []}

    try:
        output_root = Path(output_value).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return {**run, "default_path": None, "results": []}
    manifest = _read_json(output_root / "run_manifest.json")
    gui_directory = output_root / "ngsolve_gui"
    metadata = _read_json(gui_directory / "metadata.json")

    fields = []
    seen_paths = set()
    field_files = metadata.get("field_files")
    if not isinstance(field_files, list):
        field_files = []
    for item in field_files:
        if not isinstance(item, dict):
            continue
        path = _output_file(output_root, gui_directory, item.get("file"))
        if path is None:
            continue
        key = str(path).casefold()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        fields.append({
            "kind": "field",
            "path": str(path),
            "label": str(item.get("label") or item.get("field_key") or path.name),
            "field_key": item.get("field_key"),
            "snapshot_index": item.get("snapshot_index"),
        })

    if not fields:
        raw_fields = manifest.get("fields")
        if isinstance(raw_fields, list):
            for value in raw_fields:
                path = _output_file(output_root, output_root, value)
                if path is None:
                    continue
                key = str(path).casefold()
                if key in seen_paths:
                    continue
                seen_paths.add(key)
                fields.append({
                    "kind": "field",
                    "path": str(path),
                    "label": path.name,
                    "field_key": None,
                    "snapshot_index": None,
                })

    default_path = _output_file(
        output_root, gui_directory, metadata.get("default_solution_file")
    )
    default_field = metadata.get("default_field")
    if not isinstance(default_field, dict):
        default_field = {}
    default_key = default_field.get("field_key")
    default_snapshot = default_field.get("snapshot_index")

    results = []
    matched_default = False
    for field in fields:
        matches_default = (
            default_path is not None
            and (
                str(default_path).casefold() == field["path"].casefold()
                or (
                    default_key is not None
                    and field.get("field_key") == default_key
                    and field.get("snapshot_index") == default_snapshot
                )
            )
        )
        if matches_default:
            default_path = Path(field["path"])
            results.append({
                **field,
                "id": field["path"],
                "label": f"{field['label']} (default)",
                "is_default": True,
            })
            matched_default = True
            continue
        results.append({**field, "id": field["path"], "is_default": False})

    if default_path is not None and not matched_default:
        default_label = default_field.get("label") or "Default solution"
        results.insert(0, {
            "id": str(default_path),
            "kind": "field",
            "path": str(default_path),
            "label": f"{default_label} (default)",
            "field_key": default_key,
            "snapshot_index": default_snapshot,
            "is_default": True,
        })
    elif default_path is None and fields:
        default_path = Path(fields[0]["path"])
        results[0]["is_default"] = True
        results[0]["label"] = f"{results[0]['label']} (default)"

    mesh_data = manifest.get("mesh")
    mesh_value = mesh_data.get("file") if isinstance(mesh_data, dict) else None
    mesh_path = _output_file(output_root, output_root, mesh_value)
    if mesh_path is None:
        mesh_path = _output_file(output_root, gui_directory, metadata.get("mesh_file"))
    if mesh_path is None:
        for candidate in (output_root / "mesh.vol", gui_directory / "mesh.vol"):
            mesh_path = _output_file(output_root, output_root, str(candidate))
            if mesh_path is not None:
                break
    if mesh_path is not None:
        results.append({
            "id": str(mesh_path),
            "kind": "mesh",
            "path": str(mesh_path),
            "label": "Generated mesh",
            "is_default": False,
        })

    return {
        **run,
        "default_path": str(default_path) if default_path is not None else None,
        "results": results,
    }
