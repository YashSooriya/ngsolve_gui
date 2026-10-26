"""Import and preview STEP geometry with solver-ready wire traces."""

from __future__ import annotations

import copy
import math
import threading
from pathlib import Path
from types import SimpleNamespace

from ngapp.components import Div, QBtn, QCard, QCardActions, QDialog, QInput, QSelect, QTooltip

from . import cerbsim_style as cb
from .three_d_project import (
    LENGTH_UNITS,
    PROJECT_SUFFIX,
    add_step_geometry,
    add_wire_trace,
    detect_step_unit,
    new_3d_project,
    package_3d_project,
    unpack_3d_project,
)


_UNIT_OPTIONS = [
    {"label": "Metres", "value": "m"},
    {"label": "Centimetres", "value": "cm"},
    {"label": "Millimetres", "value": "mm"},
    {"label": "Micrometres", "value": "um"},
    {"label": "Nanometres", "value": "nm"},
    {"label": "Inches", "value": "in"},
    {"label": "Feet", "value": "ft"},
]
_STEP_UNIT_OPTIONS = [{"label": "Auto detect", "value": "auto"}, *_UNIT_OPTIONS]


def _button(label, icon, callback, *, color=None, tooltip=None):
    button = QBtn(
        QTooltip(tooltip or label),
        ui_label=label,
        ui_icon=icon,
        ui_color=color,
        ui_flat=color is None,
        ui_dense=True,
        ui_no_caps=True,
        ui_class="q-px-sm",
    )
    button.on_click(callback)
    return button


def _input(label, value, callback, *, number=False, width="118px"):
    widget = QInput(
        ui_label=label,
        ui_model_value=value,
        ui_type="number" if number else "text",
        ui_dense=True,
        ui_filled=True,
        ui_hide_bottom_space=True,
        ui_style=f"width:{width};",
    )
    widget.on_update_model_value(callback)
    return widget


class ThreeDProjectDialog(QDialog):
    """A bounded import workspace; STEP tessellation never blocks the UI."""

    def __init__(self, app):
        self.app = app
        self.project = new_3d_project()
        self.assets = {}
        self._preview_generation = 0
        self._saved = True
        self._status = Div("Import a STEP file and optional wire traces to start.", ui_class="cb-muted")
        self._project_name = QInput(
            ui_label="Project name",
            ui_model_value=self.project["name"],
            ui_dense=True,
            ui_filled=True,
            ui_hide_bottom_space=True,
            ui_style="width:min(330px, 45vw);",
        )
        self._project_name.on_update_model_value(self._rename_project)
        self._geometry_units = QSelect(
            ui_label="STEP units",
            ui_options=_STEP_UNIT_OPTIONS,
            ui_option_label="label",
            ui_option_value="value",
            ui_emit_value=True,
            ui_map_options=True,
            ui_model_value="auto",
            ui_dense=True,
            ui_filled=True,
            ui_hide_bottom_space=True,
            ui_style="width:160px;",
        )
        self._geometry_unit = "auto"
        self._geometry_units.on_update_model_value(
            lambda event: setattr(self, "_geometry_unit", getattr(event, "value", event))
        )
        self._trace_units = QSelect(
            ui_label="Trace units",
            ui_options=_UNIT_OPTIONS,
            ui_option_label="label",
            ui_option_value="value",
            ui_emit_value=True,
            ui_map_options=True,
            ui_model_value="m",
            ui_dense=True,
            ui_filled=True,
            ui_hide_bottom_space=True,
            ui_style="width:160px;",
        )
        self._trace_unit = "m"
        self._trace_units.on_update_model_value(
            lambda event: setattr(self, "_trace_unit", getattr(event, "value", event))
        )
        self._items = Div(
            Div("Geometry and wire traces", ui_style="font-weight:650; margin-bottom:8px;"),
            ui_style="min-width:290px; max-width:360px; overflow:auto; padding:12px;",
        )

        from .plot import PlotComponent

        plot_app_data = SimpleNamespace(get_tab=lambda _name: {"title": "3D project preview"})
        self._preview = PlotComponent(
            "mmfem_3d_project_preview", {"obj": []}, plot_app_data
        )
        preview_host = Div(
            self._preview,
            ui_style="flex:1 1 auto; min-width:0; min-height:420px; height:100%;",
        )

        actions = Div(
            _button("Import STEP", "mdi-cube-scan", self.import_step),
            _button("Add wire trace", "mdi-vector-polyline", self.import_wire_trace),
            _button("Open project", "mdi-folder-open-outline", self.open_project),
            _button("Save project", "mdi-content-save-outline", self.save_project, color="primary"),
            ui_style="display:flex; flex-wrap:wrap; align-items:center; gap:6px;",
        )
        close = _button("Close", "mdi-close", self._close)
        body = Div(
            Div(
                Div(self._project_name, actions, ui_style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;"),
                Div(self._geometry_units, self._trace_units,
                    ui_style="display:flex; gap:10px; align-items:center; margin-top:12px;"),
                ui_style="padding:16px 18px 12px; border-bottom:1px solid var(--border);",
            ),
            self._status,
            Div(
                self._items,
                preview_host,
                ui_style=(
                    "display:flex; flex:1 1 auto; min-height:420px; min-width:0; "
                    "overflow:hidden; border-top:1px solid var(--border);"
                ),
            ),
            ui_style="display:flex; flex-direction:column; height:min(84vh, 900px); min-height:520px; width:min(1320px, 96vw);",
        )
        super().__init__(
            QCard(body, QCardActions(close, ui_align="right"), ui_style="padding:0;"),
            ui_model_value=False,
            ui_persistent=True,
        )

    @property
    def dirty(self):
        return not self._saved

    def show_new(self):
        if self.dirty:
            self._set_status("Save the current 3D project before starting another one.", error=True)
            self.ui_model_value = True
            return False
        self.project = new_3d_project()
        self.assets = {}
        self._saved = True
        self._project_name.ui_model_value = self.project["name"]
        self._rebuild_items()
        self._set_status("Import a STEP file and optional wire traces to start.")
        self._show_empty_preview()
        self.ui_model_value = True
        return True

    def _close(self, *_args):
        self.ui_model_value = False

    def _rename_project(self, event):
        value = str(getattr(event, "value", event) or "").strip()
        self.project["name"] = value or "Untitled 3D project"
        self._saved = False

    def _set_status(self, message, *, error=False):
        self._status.ui_children = [message]
        self._status.ui_class = "cb-muted"
        self._status.ui_style = (
            "padding:8px 18px; min-height:34px; "
            + ("color:var(--negative);" if error else "")
        )

    def _mark_changed(self):
        self._saved = False

    def _read_units(self, widget):
        value = self._geometry_unit if widget is self._geometry_units else self._trace_unit
        if widget is self._geometry_units and value == "auto":
            return value
        if value not in LENGTH_UNITS:
            value = "m"
        return value

    def import_step(self, *_args):
        try:
            path, data = self.app._pick_artifact(
                title="Import 3D STEP geometry",
                description="STEP geometry (.step or .stp)",
                extension=".step",
                additional_extensions=(".stp",),
            )
            if not path:
                return
            selected_units = self._read_units(self._geometry_units)
            units = selected_units if selected_units != "auto" else detect_step_unit(data)
            unit_source = "manual" if selected_units != "auto" else "detected"
            if units is None:
                units = "mm"
                unit_source = "default-mm"
            item = add_step_geometry(
                self.project,
                self.assets,
                Path(path).name,
                data,
                units=units,
                material="Shield",
            )
            item["unit_source"] = unit_source
            self._mark_changed()
            self._rebuild_items()
            self._set_status(f"Imported {item['name']}. Building the 3D preview…")
            self._rebuild_preview()
        except Exception as error:
            self._set_status(f"Could not import STEP geometry: {error}", error=True)

    def import_wire_trace(self, *_args):
        try:
            path, data = self.app._pick_artifact(
                title="Import wire trace",
                description="XYZ wire trace (.txt, .csv, .dat, .xyz)",
                extension=".txt",
                additional_extensions=(".csv", ".dat", ".xyz"),
            )
            if not path:
                return
            trace_set = add_wire_trace(
                self.project,
                self.assets,
                Path(path).name,
                data,
                units=self._read_units(self._trace_units),
                material="Air",
            )
            self._mark_changed()
            self._rebuild_items()
            self._set_status(
                f"Imported {trace_set['name']} with {trace_set['traces'][0]['points']:,} points."
            )
            self._rebuild_preview()
        except Exception as error:
            self._set_status(f"Could not import wire trace: {error}", error=True)

    def open_project(self, *_args):
        if self.dirty:
            self._set_status("Save the current 3D project before opening another one.", error=True)
            return False
        try:
            path, data = self.app._pick_artifact(
                title="Open MM-FEM 3D project",
                description="MM-FEM 3D project",
                extension=PROJECT_SUFFIX,
            )
            if not path:
                return
            self.load_project_bytes(data)
        except Exception as error:
            self._set_status(f"Could not open 3D project: {error}", error=True)

    def load_project_bytes(self, data):
        if self.dirty:
            self._set_status("Save the current 3D project before opening another one.", error=True)
            return False
        project, assets = unpack_3d_project(data)
        self._preview_generation += 1
        self.project = project
        self.assets = assets
        self._saved = True
        self._project_name.ui_model_value = project.get("name", "Untitled 3D project")
        self._rebuild_items()
        self.ui_model_value = True
        self._set_status("Project opened. Building the 3D preview…")
        self._rebuild_preview()
        return True

    def save_project(self, *_args):
        try:
            self.project["name"] = str(self.project.get("name", "")).strip() or "3D project"
            safe_name = "".join(
                character if character.isalnum() or character in "-_ " else "_"
                for character in self.project["name"]
            ).strip() or "3D project"
            archive = package_3d_project(self.project, self.assets)
            if self.app._save_artifact(archive, safe_name + PROJECT_SUFFIX):
                self._saved = True
                self._set_status("3D project saved with its STEP and wire trace files.")
        except Exception as error:
            self._set_status(f"Could not save 3D project: {error}", error=True)

    def _remove_geometry(self, asset):
        self.project["geometries"] = [item for item in self.project["geometries"] if item["asset"] != asset]
        self.assets.pop(asset, None)
        self._mark_changed()
        self._rebuild_items()
        self._rebuild_preview()

    def _remove_trace_set(self, trace_set):
        self.project["wire_trace_sets"].remove(trace_set)
        for trace in trace_set["traces"]:
            self.assets.pop(trace["filename"], None)
        self._mark_changed()
        self._rebuild_items()
        self._rebuild_preview()

    def _rebuild_items(self):
        children = [Div("Geometry and wire traces", ui_style="font-weight:650; margin-bottom:8px;")]
        if not self.project["geometries"] and not self.project["wire_trace_sets"]:
            children.append(Div("No imported files yet.", ui_class="cb-muted"))
        for geometry in self.project["geometries"]:
            material_input = _input(
                "Material", geometry.get("material", "Shield"),
                lambda event, item=geometry: self._set_value(item, "material", event),
                width="112px",
            )
            remove = _button("", "mdi-delete-outline", lambda *_a, asset=geometry["asset"]: self._remove_geometry(asset), tooltip="Remove geometry")
            unit_note = " · assumed" if geometry.get("unit_source") == "default-mm" else ""
            children.append(Div(
                Div(geometry["name"], ui_style="font-size:12px; overflow-wrap:anywhere; flex:1;"),
                material_input, remove,
                Div(f"STEP · {geometry.get('units', 'm')}{unit_note}", ui_class="cb-muted", ui_style="font-size:11px;"),
                ui_style="display:flex; flex-wrap:wrap; align-items:center; gap:6px; border:1px solid var(--border); border-radius:6px; padding:8px; margin:6px 0;",
            ))
        for trace_set in self.project["wire_trace_sets"]:
            trace = trace_set["traces"][0]
            material_input = _input(
                "Material", trace.get("material", "Air"),
                lambda event, item=trace: self._set_value(item, "material", event),
                width="100px",
            )
            current_input = _input(
                "Current (A)", trace_set.get("current", 1.0),
                lambda event, item=trace_set: self._set_number(item, "current", event),
                number=True,
                width="105px",
            )
            gradient_input = _input(
                "Target gradient (T/m)", trace_set.get("target_gradient", ""),
                lambda event, item=trace_set: self._set_optional_number(item, "target_gradient", event),
                number=True,
                width="142px",
            )
            dsv_input = _input(
                "DSV (m)", trace_set.get("dsv", 0.4),
                lambda event, item=trace_set: self._set_number(item, "dsv", event),
                number=True,
                width="92px",
            )
            direction = QSelect(
                ui_label="Gradient",
                ui_options=[{"label": name.upper(), "value": name} for name in "xyz"],
                ui_option_label="label", ui_option_value="value",
                ui_emit_value=True, ui_map_options=True,
                ui_model_value=trace_set.get("direction", "x"),
                ui_dense=True, ui_filled=True, ui_hide_bottom_space=True,
                ui_style="width:90px;",
            )
            direction.on_update_model_value(
                lambda event, item=trace_set: self._set_value(item, "direction", event)
            )
            remove = _button("", "mdi-delete-outline", lambda *_a, item=trace_set: self._remove_trace_set(item), tooltip="Remove wire trace")
            children.append(Div(
                Div(trace.get("name", trace_set["name"]), ui_style="font-size:12px; overflow-wrap:anywhere; flex:1 1 100%;"),
                material_input, current_input, gradient_input, dsv_input, direction, remove,
                Div(f"{trace.get('points', 0):,} points · metres · wire-trace source", ui_class="cb-muted", ui_style="font-size:11px;"),
                ui_style="display:flex; flex-wrap:wrap; align-items:center; gap:6px; border:1px solid var(--border); border-radius:6px; padding:8px; margin:6px 0;",
            ))
        self._items.ui_children = children

    def _set_value(self, item, key, event):
        value = getattr(event, "value", event)
        value = str(value or "").strip()
        if not value:
            self._set_status(f"{key.title()} cannot be empty.", error=True)
            return
        item[key] = value
        self._mark_changed()

    def _set_number(self, item, key, event):
        value = getattr(event, "value", event)
        try:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("Value must be finite.")
            item[key] = number
            self._mark_changed()
        except (TypeError, ValueError, OverflowError) as error:
            self._set_status(f"Invalid {key.replace('_', ' ')}: {error}", error=True)

    def _set_optional_number(self, item, key, event):
        value = getattr(event, "value", event)
        if value is None or str(value).strip() == "":
            item.pop(key, None)
            self._mark_changed()
            return
        self._set_number(item, key, event)

    def _show_empty_preview(self):
        self._preview.data = {"obj": []}
        self._preview.container.ui_children = [
            Div(
                "The combined STEP and wire trace preview will appear here.",
                ui_class="cb-muted",
                ui_style="display:grid; place-items:center; height:100%; min-height:420px;",
            )
        ]

    def _rebuild_preview(self):
        self._preview_generation += 1
        generation = self._preview_generation
        project = copy.deepcopy(self.project)
        assets = dict(self.assets)
        if not project["geometries"] and not project["wire_trace_sets"]:
            self._show_empty_preview()
            return

        def build():
            try:
                from .three_d_preview import preview_figure, project_preview_payload

                payload = project_preview_payload(project, assets)
                figure = preview_figure(payload)
                if generation != self._preview_generation:
                    return
                self._preview.data = {"obj": figure}
                self._preview.draw()
                self._set_status("Preview ready. STEP surfaces are display meshes; original STEP files are kept for export.")
            except Exception as error:
                if generation == self._preview_generation:
                    self._set_status(f"Could not build the 3D preview: {error}", error=True)

        threading.Thread(target=build, name="MMFEM-3D-preview", daemon=True).start()
