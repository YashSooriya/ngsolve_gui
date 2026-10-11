"""Persistent per-user material presets, stored outside the installed package."""

from __future__ import annotations

import copy


class MaterialLibrary:
    """Small JSON-backed library in ngapp's per-user configuration directory.

    Keeping this file alongside user settings means package or release updates
    do not replace custom materials.  Project files still contain their own
    copies of every material assigned to a region.
    """

    def __init__(self, settings=None):
        if settings is None:
            from ngapp.utils import UserSettings

            settings = UserSettings(app_id="NGSolve GUI").json_file("material_library")
        self._settings = settings

    @property
    def path(self):
        return self._settings.path

    def load(self) -> list[dict]:
        entries = self._settings.get("materials", [])
        if not isinstance(entries, list):
            return []

        materials = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            material_id = entry.get("id")
            name = entry.get("name")
            properties = entry.get("properties")
            if (
                not isinstance(material_id, str)
                or not material_id
                or material_id in seen
                or not isinstance(name, str)
                or not name.strip()
                or not isinstance(properties, dict)
            ):
                continue
            seen.add(material_id)
            materials.append(copy.deepcopy(entry))
        return materials

    def save(self, materials: list[dict]) -> None:
        self._settings.set("materials", copy.deepcopy(materials))
