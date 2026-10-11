"""Portable mesh and solution bundles used by the MM-FEM desktop UI."""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tempfile
import zipfile


MESH_SCHEMA = "mm-fem.mesh"
MESH_SUFFIX = ".mmfem-mesh"
SOLUTION_SCHEMA = "mm-fem.solutions"
SOLUTION_SUFFIX = ".mmfem-solutions"
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 20_000


def problem_fingerprint(model: dict) -> str:
    """Return a stable hash for the exact problem definition used by a mesh."""
    from .axisymmetric_model import migrate_legacy_model

    model = migrate_legacy_model(model)
    encoded = json.dumps(
        model, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return output.getvalue()


def _read_archive(data: bytes) -> zipfile.ZipFile:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data)
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("The selected bundle is larger than the 2 GB limit.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise ValueError("The selected file is not a valid MM-FEM bundle.") from error
    infos = archive.infolist()
    if len(infos) > MAX_ENTRIES:
        archive.close()
        raise ValueError("The bundle contains too many files.")
    names = [item.filename for item in infos]
    if len(names) != len(set(names)):
        archive.close()
        raise ValueError("The bundle contains duplicate file entries.")
    total_size = 0
    for info in infos:
        name = PurePosixPath(info.filename)
        if name.is_absolute() or ".." in name.parts or "\\" in info.filename:
            archive.close()
            raise ValueError(f"The bundle contains an unsafe file path: {info.filename}")
        total_size += info.file_size
        if total_size > MAX_ARCHIVE_BYTES:
            archive.close()
            raise ValueError("The uncompressed bundle is larger than the 2 GB limit.")
    return archive


def make_mesh_bundle(mesh_data: bytes, model: dict) -> bytes:
    """Create a mesh file tied to the exact problem definition that made it."""
    mesh_data = bytes(mesh_data)
    if not mesh_data:
        raise ValueError("The mesh file is empty.")
    manifest = {
        "schema": MESH_SCHEMA,
        "schema_version": 1,
        "problem_sha256": problem_fingerprint(model),
        "mesh_sha256": hashlib.sha256(mesh_data).hexdigest(),
    }
    return _zip_bytes({
        "manifest.json": (json.dumps(manifest, indent=2) + "\n").encode("utf-8"),
        "problem.json": (json.dumps(model, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        "mesh.vol": mesh_data,
    })


def read_mesh_bundle(data: bytes, expected_model: dict) -> bytes:
    """Validate a mesh bundle and return its mesh only for the matching model."""
    with _read_archive(data) as archive:
        names = set(archive.namelist())
        if names != {"manifest.json", "problem.json", "mesh.vol"}:
            raise ValueError("The MM-FEM mesh bundle is missing required files or contains unsupported files.")
        try:
            manifest = json.loads(archive.read("manifest.json"))
            bundled_model = json.loads(archive.read("problem.json"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("The mesh bundle metadata is invalid.") from error
        if manifest.get("schema") != MESH_SCHEMA or manifest.get("schema_version") != 1:
            raise ValueError("This MM-FEM mesh bundle version is not supported.")
        recorded_hash = manifest.get("problem_sha256")
        if recorded_hash != problem_fingerprint(bundled_model):
            raise ValueError("The mesh bundle problem data failed its integrity check.")
        if recorded_hash != problem_fingerprint(expected_model):
            raise ValueError("This mesh was saved for a different problem file. Load the matching problem before loading its mesh.")
        mesh_data = archive.read("mesh.vol")
        if hashlib.sha256(mesh_data).hexdigest() != manifest.get("mesh_sha256"):
            raise ValueError("The mesh file failed its integrity check.")
        if not mesh_data:
            raise ValueError("The mesh file is empty.")
        return mesh_data


def make_solution_bundle(run_directory: str | Path, expected_model: dict) -> tuple[bytes, str]:
    """Package one completed solver run, including all GUI fields and mesh."""
    run_directory = Path(run_directory)
    try:
        run_manifest = json.loads((run_directory / "run_manifest.json").read_text(encoding="utf-8"))
        model = json.loads((run_directory / "problem.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("The selected run is missing its problem or run manifest.") from error
    if run_manifest.get("status") != "complete" or run_manifest.get("kind") != "study":
        raise ValueError("Only completed study runs can be saved as solution bundles.")
    expected_hash = problem_fingerprint(expected_model)
    if problem_fingerprint(model) != expected_hash:
        raise ValueError("The latest study belongs to a different problem. Run this problem before saving its solutions.")

    fields_directory = run_directory / "ngsolve_gui" / "fields"
    field_paths = sorted(fields_directory.glob("*.pkl"))
    if not field_paths:
        raise ValueError("This study has no GUI-ready solution fields to save.")

    entries: dict[str, bytes] = {
        "problem.json": (json.dumps(model, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        "run_manifest.json": (json.dumps(run_manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    }
    studies_path = run_directory / "studies.json"
    if studies_path.is_file():
        entries["studies.json"] = studies_path.read_bytes()
    mesh_path = run_directory / "mesh.vol"
    if mesh_path.is_file():
        entries["mesh.vol"] = mesh_path.read_bytes()
    metadata_path = run_directory / "ngsolve_gui" / "metadata.json"
    if metadata_path.is_file():
        entries["ngsolve_gui/metadata.json"] = metadata_path.read_bytes()
    field_names = []
    for path in field_paths:
        relative = path.relative_to(run_directory).as_posix()
        if not relative.startswith("ngsolve_gui/fields/"):
            raise ValueError("A solution field is outside the supported fields folder.")
        entries[relative] = path.read_bytes()
        field_names.append(relative)
    manifest = {
        "schema": SOLUTION_SCHEMA,
        "schema_version": 1,
        "problem_sha256": expected_hash,
        "fields": field_names,
        "sha256": {name: hashlib.sha256(content).hexdigest() for name, content in entries.items()},
    }
    entries["solution_manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    title = "".join(char if char.isalnum() or char in "-_ " else "_" for char in model.get("name", "axisymmetric-model")).strip()
    return _zip_bytes(entries), (title or "axisymmetric-model") + ".mmfem-solutions"


def extract_solution_bundle(data: bytes) -> tuple[str, list[str]]:
    """Validate and safely extract fields from a saved solution bundle."""
    with _read_archive(data) as archive:
        names = set(archive.namelist())
        if "solution_manifest.json" not in names or "problem.json" not in names:
            raise ValueError("This file is not a complete MM-FEM solution bundle.")
        try:
            manifest = json.loads(archive.read("solution_manifest.json"))
            model = json.loads(archive.read("problem.json"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("The solution bundle metadata is invalid.") from error
        if manifest.get("schema") != SOLUTION_SCHEMA or manifest.get("schema_version") != 1:
            raise ValueError("This MM-FEM solution bundle version is not supported.")
        if problem_fingerprint(model) != manifest.get("problem_sha256"):
            raise ValueError("The solution bundle problem data failed its integrity check.")
        fields = manifest.get("fields")
        checksums = manifest.get("sha256")
        if not isinstance(fields, list) or not fields or not isinstance(checksums, dict):
            raise ValueError("The solution bundle contains no fields or an invalid file manifest.")
        required = {"solution_manifest.json", "problem.json", "run_manifest.json"}
        optional = {"studies.json", "mesh.vol", "ngsolve_gui/metadata.json"}
        allowed = required | optional | set(fields)
        if not required.issubset(names) or not names.issubset(allowed):
            raise ValueError("The solution bundle contains missing or unsupported files.")
        if len(fields) != len(set(fields)):
            raise ValueError("The solution bundle repeats a field file.")
        if not set(fields).issubset(names):
            raise ValueError("The solution bundle is missing one or more field files.")
        for field in fields:
            path = PurePosixPath(field)
            if path.is_absolute() or ".." in path.parts or not field.startswith("ngsolve_gui/fields/") or not field.endswith(".pkl"):
                raise ValueError(f"The solution bundle contains an invalid field path: {field}")
        for name, expected_hash in checksums.items():
            if name not in names or hashlib.sha256(archive.read(name)).hexdigest() != expected_hash:
                raise ValueError(f"The solution bundle file failed its integrity check: {name}")
        if set(checksums) != names - {"solution_manifest.json"}:
            raise ValueError("The solution bundle file manifest is incomplete.")

        destination = Path(tempfile.mkdtemp(prefix="mmfem_solutions_"))
        for name in names - {"solution_manifest.json"}:
            target = destination.joinpath(*PurePosixPath(name).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    field_paths = [str(destination.joinpath(*PurePosixPath(name).parts)) for name in fields]
    return str(destination), field_paths
