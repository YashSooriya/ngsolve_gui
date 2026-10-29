"""Import and preview STEP geometry with solver-ready wire traces."""

from __future__ import annotations

import copy
import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

from ngapp.components import Div, QBtn, QIcon, QInput, QSelect, QSplitter, QTooltip

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


class ThreeDWorkspace(Div):
    """Persistent 3D project workspace with a model tree and inspector."""

    def __init__(self, app):
        self.app = app
        self.project = new_3d_project()
        self.assets = {}
        self._preview_generation = 0
        self._preview_lock = threading.Lock()
        self._preview_running = False
        self._preview_pending = None
        self._preview_cache = {}
        self._preview_cache_order = []
        self._preview_cache_size = 0
        self._tree_width = 250
        self._property_width = 300
        self._selection = ("project", None)
        self._camera_view = "isometric"
        self._axes_visible = True
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
        self._project_node = Div()
        self._project_node.on("click", lambda _event=None: self.select_item("project", None))
        self._geometry_items = Div(ui_style="display:flex; flex-direction:column; gap:2px;")
        self._trace_items = Div(ui_style="display:flex; flex-direction:column; gap:2px;")
        self._items = Div(
            self._project_node,
            Div(
                Div(QIcon(ui_name="mdi-cube-outline", ui_size="16px"), "Geometry", ui_style="display:flex; align-items:center; gap:7px; font-weight:650;"),
                _button("", "mdi-plus", self.import_step, tooltip="Import STEP geometry"),
                ui_style="display:flex; align-items:center; justify-content:space-between; padding:5px 8px;",
            ),
            self._geometry_items,
            Div(
                Div(QIcon(ui_name="mdi-vector-polyline", ui_size="16px"), "Coils", ui_style="display:flex; align-items:center; gap:7px; font-weight:650;"),
                _button("", "mdi-plus", self.import_wire_trace, tooltip="Import wire traces"),
                ui_style="display:flex; align-items:center; justify-content:space-between; padding:5px 8px; margin-top:10px;",
            ),
            self._trace_items,
            ui_style="display:flex; flex-direction:column; flex:1 1 auto; min-height:0; overflow:auto; padding:8px;",
        )
        self._tree_panel = Div(
            Div("Model tree", ui_style="font-weight:650; padding:12px 14px; border-bottom:1px solid var(--border);"),
            self._items,
            ui_style="display:flex; flex-direction:column; min-width:0; min-height:0; height:100%; border-right:1px solid var(--border); background:var(--panel-bg);",
        )
        self._properties = Div(ui_style="display:flex; flex-direction:column; gap:10px; padding:12px; overflow:auto; min-height:0;")
        self._properties_panel = Div(
            Div("Properties", ui_style="font-weight:650; padding:12px 14px; border-bottom:1px solid var(--border);"),
            self._properties,
            ui_style="display:flex; flex-direction:column; min-width:0; min-height:0; height:100%; border-left:1px solid var(--border); background:var(--panel-bg);",
        )

        from .plot import PlotComponent

        plot_app_data = SimpleNamespace(get_tab=lambda _name: {"title": "3D project preview"})
        self._preview = PlotComponent(
            "mmfem_3d_project_preview", {"obj": []}, plot_app_data
        )
        self._preview.ui_class = "fit mmfem-3d-project-preview"
        self._transparent_shield = False
        self._transparency_button = _button("Transparent shield", "mdi-opacity", self.toggle_transparency)
        preview_host = Div(
            self._preview,
            ui_style="display:flex; flex:1 1 auto; min-width:0; min-height:0; height:100%; overflow:hidden; background:var(--canvas-bg, #f4f6f8);",
        )

        actions = Div(
            _button("Import STEP", "mdi-cube-scan", self.import_step),
            _button("Import wire traces", "mdi-vector-polyline", self.import_wire_trace),
            _button("Open project", "mdi-folder-open-outline", self.open_project),
            _button("Save project", "mdi-content-save-outline", self.save_project, color="primary"),
            _button("Fit view", "mdi-fit-to-screen-outline", self.fit_view),
            self._transparency_button,
            ui_style="display:flex; flex-wrap:wrap; align-items:center; gap:6px;",
        )
        self._toolbar = Div(
            Div(self._project_name, ui_style="font-size:15px; font-weight:650; min-width:150px;"),
            actions,
            Div(self._geometry_units, self._trace_units, ui_style="display:flex; align-items:center; gap:8px;"),
            ui_style="display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:8px; padding:8px 12px; border-bottom:1px solid var(--border); background:var(--panel-header);",
        )
        inner_splitter = QSplitter(
            ui_model_value=300,
            ui_unit="px",
            ui_reverse=True,
            ui_limits=[0, 520],
            ui_emit_immediately=True,
            ui_slots={"before": [preview_host], "after": [self._properties_panel]},
            ui_class=cb.body_inner,
        )
        self._tree_splitter = QSplitter(
            ui_model_value=250,
            ui_unit="px",
            ui_limits=[150, 460],
            ui_emit_immediately=True,
            ui_slots={"before": [self._tree_panel], "after": [inner_splitter]},
            ui_class=cb.body_fill,
        )
        self._inner_splitter = inner_splitter
        super().__init__(
            self._toolbar,
            self._status,
            self._tree_splitter,
            ui_style="display:flex; flex-direction:column; flex:1 1 auto; width:100%; min-width:0; min-height:0; overflow:hidden;",
        )
        self.ui_hidden = True
        self._rebuild_items()
        self._rebuild_properties()

    @property
    def dirty(self):
        return not self._saved

    def show_new(self):
        if self.dirty:
            self._set_status("Save the current 3D project before starting another one.", error=True)
            self.ui_hidden = False
            return False
        self.project = new_3d_project()
        self.assets = {}
        self._saved = True
        self._project_name.ui_model_value = self.project["name"]
        self._rebuild_items()
        self._set_status("Import a STEP file and optional wire traces to start.")
        self._show_empty_preview()
        self._selection = ("project", None)
        self._rebuild_properties()
        self.ui_hidden = False
        return True

    def _rename_project(self, event):
        value = str(getattr(event, "value", event) or "").strip()
        self.project["name"] = value or "Untitled 3D project"
        self._saved = False
        if hasattr(self, "_project_node"):
            self._rebuild_items()

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
                raise ValueError(
                    "The STEP file does not declare length units. Choose STEP units above and import again."
                )
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
            self._selection = ("geometry", item["id"])
            self._rebuild_items()
            self._rebuild_properties()
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
            self._selection = ("trace_set", trace_set["id"])
            self._rebuild_items()
            self._rebuild_properties()
            self._set_status(
                f"Imported {trace_set['name']} with {trace_set['traces'][0]['points']:,} points."
            )
            self._rebuild_preview()
        except Exception as error:
            self._set_status(f"Could not import wire trace: {error}", error=True)

    def open_project(self, *_args):
        return self.app.open_project_file(*_args)

    def load_project_bytes(self, data):
        if self.dirty:
            self._set_status("Save the current 3D project before opening another one.", error=True)
            return False
        project, assets = unpack_3d_project(data)
        return self.load_project_data(project, assets)

    def load_project_data(self, project, assets):
        if self.dirty:
            self._set_status("Save the current 3D project before opening another one.", error=True)
            return False
        project = copy.deepcopy(project)
        assets = dict(assets)
        self._preview_generation += 1
        self.project = project
        self.assets = assets
        self._saved = True
        self._selection = ("project", None)
        self._project_name.ui_model_value = project.get("name", "Untitled 3D project")
        self._rebuild_items()
        self._rebuild_properties()
        self.ui_hidden = False
        self._set_status("Project opened. Building the 3D preview…")
        self._rebuild_preview()
        return True

    def discard_project(self):
        self._preview_generation += 1
        self.project = new_3d_project()
        self.assets = {}
        self._saved = True
        self._project_name.ui_model_value = self.project["name"]
        self._rebuild_items()
        self._selection = ("project", None)
        self._rebuild_properties()
        self._set_status("Import a STEP file and optional wire traces to start.")
        self._show_empty_preview()
        self.ui_hidden = True

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
                remember = getattr(self.app, "_remember_recent_project", None)
                if callable(remember):
                    remember(
                        getattr(self.app, "_last_saved_path", None),
                        kind="3d",
                        name=self.project["name"],
                    )
                return True
            return False
        except Exception as error:
            self._set_status(f"Could not save 3D project: {error}", error=True)
            return False

    def _remove_geometry(self, asset):
        removed = [item for item in self.project["geometries"] if item["asset"] == asset]
        self.project["geometries"] = [item for item in self.project["geometries"] if item["asset"] != asset]
        self.assets.pop(asset, None)
        if removed and self._selection == ("geometry", removed[0]["id"]):
            self._selection = ("project", None)
        self._mark_changed()
        self._rebuild_items()
        self._rebuild_properties()
        self._rebuild_preview()

    def _remove_trace_set(self, trace_set):
        self.project["wire_trace_sets"].remove(trace_set)
        for trace in trace_set["traces"]:
            self.assets.pop(trace["filename"], None)
        if self._selection == ("trace_set", trace_set["id"]):
            self._selection = ("project", None)
        self._mark_changed()
        self._rebuild_items()
        self._rebuild_properties()
        self._rebuild_preview()

    def _tree_item(self, label, kind, item_id, icon, on_delete):
        selected = self._selection == (kind, item_id)
        row = Div(
            QIcon(ui_name=icon, ui_size="15px", ui_color="primary" if selected else "grey-7"),
            Div(label, ui_style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px;"),
            _button("", "mdi-delete-outline", on_delete, tooltip="Remove from project"),
            ui_style=(
                "display:flex; align-items:center; gap:6px; min-width:0; padding:4px 5px 4px 18px; "
                "border-radius:5px; cursor:pointer; "
                + ("background:var(--accent-subtle); color:var(--accent);" if selected else "")
            ),
        )
        row.on("click", lambda _event=None, k=kind, key=item_id: self.select_item(k, key))
        return row

    def _rebuild_items(self):
        selected_project = self._selection == ("project", None)
        self._project_node.ui_children = [
            QIcon(ui_name="mdi-cube-scan", ui_size="15px", ui_color="primary" if selected_project else "grey-7"),
            Div(
                self.project.get("name", "Untitled 3D project"),
                ui_style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px;",
            ),
        ]
        self._project_node.ui_style = (
            "display:flex; align-items:center; gap:6px; min-width:0; padding:5px 8px; border-radius:5px; cursor:pointer; "
            + ("background:var(--accent-subtle); color:var(--accent);" if selected_project else "")
        )
        geometry_rows = [
            self._tree_item(
                geometry["name"], "geometry", geometry["id"], "mdi-cube-outline",
                lambda *_a, asset=geometry["asset"]: self._remove_geometry(asset),
            )
            for geometry in self.project.get("geometries", [])
        ]
        trace_rows = [
            self._tree_item(
                trace_set["name"], "trace_set", trace_set["id"], "mdi-vector-polyline",
                lambda *_a, item=trace_set: self._remove_trace_set(item),
            )
            for trace_set in self.project.get("wire_trace_sets", [])
        ]
        if not geometry_rows:
            geometry_rows = [Div("No geometry models", ui_class="cb-muted", ui_style="padding:5px 18px; font-size:12px;")]
        if not trace_rows:
            trace_rows = [Div("No wire traces", ui_class="cb-muted", ui_style="padding:5px 18px; font-size:12px;")]
        self._geometry_items.ui_children = geometry_rows
        self._trace_items.ui_children = trace_rows

    def _select_options(self, label, value, callback, options):
        widget = QSelect(
            ui_label=label,
            ui_options=options,
            ui_option_label="label",
            ui_option_value="value",
            ui_emit_value=True,
            ui_map_options=True,
            ui_model_value=value,
            ui_dense=True,
            ui_filled=True,
            ui_hide_bottom_space=True,
            ui_style="width:100%;",
        )
        widget.on_update_model_value(callback)
        return widget

    def _rebuild_properties(self):
        kind, item_id = self._selection
        children = []
        if kind == "geometry":
            item = next((entry for entry in self.project["geometries"] if entry["id"] == item_id), None)
            if item is None:
                self._selection = ("project", None)
                return self._rebuild_properties()
            children.extend([
                Div("Geometry model", ui_style="font-size:13px; font-weight:650;"),
                _input("Name", item["name"], lambda event, obj=item: self._set_value(obj, "name", event), width="100%"),
                _input("Material", item.get("material", "Shield"), lambda event, obj=item: self._set_value(obj, "material", event), width="100%"),
                self._select_options(
                    "Length units", item.get("units", "m"),
                    lambda event, obj=item: self._set_geometry_units(obj, event),
                    _UNIT_OPTIONS,
                ),
                Div(Path(item["asset"]).name, ui_class="cb-muted", ui_style="font-size:11px; overflow-wrap:anywhere;"),
            ])
        elif kind == "trace_set":
            item = next((entry for entry in self.project["wire_trace_sets"] if entry["id"] == item_id), None)
            if item is None:
                self._selection = ("project", None)
                return self._rebuild_properties()
            trace = item["traces"][0]
            direction = self._select_options(
                "Gradient direction", item.get("direction", "x"),
                lambda event, obj=item: self._set_value(obj, "direction", event),
                [{"label": name.upper(), "value": name} for name in "xyz"],
            )
            children.extend([
                Div("Wire trace set", ui_style="font-size:13px; font-weight:650;"),
                _input("Name", item["name"], lambda event, obj=item: self._set_value(obj, "name", event), width="100%"),
                _input("Material", item.get("material", "Air"), lambda event, obj=item: self._set_trace_material(obj, event), width="100%"),
                _input("Current (A)", item.get("current", 1.0), lambda event, obj=item: self._set_number(obj, "current", event), number=True, width="100%"),
                _input("Target gradient (T/m)", item.get("target_gradient", ""), lambda event, obj=item: self._set_optional_number(obj, "target_gradient", event), number=True, width="100%"),
                _input("DSV (m)", item.get("dsv", 0.4), lambda event, obj=item: self._set_number(obj, "dsv", event), number=True, width="100%"),
                direction,
                Div(f"{trace.get('points', 0):,} points · source: {trace.get('name', item['name'])}", ui_class="cb-muted", ui_style="font-size:11px; overflow-wrap:anywhere;"),
            ])
        else:
            children.extend([
                Div("3D project", ui_style="font-size:13px; font-weight:650;"),
                Div(f"{len(self.project['geometries'])} geometry models", ui_class="cb-muted"),
                Div(f"{len(self.project['wire_trace_sets'])} wire trace sets", ui_class="cb-muted"),
                Div("Select Geometry or Coils in the model tree to add files.", ui_class="cb-muted", ui_style="font-size:12px; margin-top:6px;"),
            ])
        self._properties.ui_children = children

    def select_item(self, kind, item_id=None):
        self._selection = (kind, item_id)
        self._rebuild_items()
        self._rebuild_properties()

    def delete_selected(self):
        kind, item_id = self._selection
        if kind == "geometry":
            item = next((entry for entry in self.project["geometries"] if entry["id"] == item_id), None)
            if item is not None:
                self._remove_geometry(item["asset"])
                return True
        elif kind == "trace_set":
            item = next((entry for entry in self.project["wire_trace_sets"] if entry["id"] == item_id), None)
            if item is not None:
                self._remove_trace_set(item)
                return True
        return False

    def _set_geometry_units(self, item, event):
        value = getattr(event, "value", event)
        if value not in LENGTH_UNITS:
            return
        item["units"] = value
        item["unit_source"] = "manual"
        self._mark_changed()
        self._rebuild_preview()

    def _set_trace_material(self, item, event):
        value = getattr(event, "value", event)
        value = str(value or "").strip()
        if not value:
            self._set_status("Material cannot be empty.", error=True)
            return
        item["material"] = value
        for trace in item.get("traces", []):
            trace["material"] = value
        self._mark_changed()

    def fit_view(self, *_args):
        return self.reset_camera()

    def reset_camera(self, *_args):
        self._camera_view = "isometric"
        self._update_preview_view(camera=True)
        return True

    def set_camera_view(self, view):
        if view not in {"isometric", "right", "left", "front", "back", "top", "bottom"}:
            return False
        self._camera_view = view
        self._update_preview_view(camera=True)
        return True

    def toggle_axes(self, *_args):
        self._axes_visible = not self._axes_visible
        self._update_preview_view()
        return self._axes_visible

    def toggle_transparency(self, *_args):
        self._transparent_shield = not self._transparent_shield
        self._transparency_button.ui_label = "Opaque shield" if self._transparent_shield else "Transparent shield"
        self._update_preview_view(transparency=True)
        return self._transparent_shield

    def _view_layout(self, *, camera=False):
        updates = {f"scene.{axis}axis.visible": self._axes_visible for axis in "xyz"}
        if camera:
            eyes = {
                "isometric": (1.45, 1.45, 1.15), "right": (1.8, 0, 0), "left": (-1.8, 0, 0),
                "front": (0, -1.8, 0), "back": (0, 1.8, 0), "top": (0, 0, 1.8), "bottom": (0, 0, -1.8),
            }
            x, y, z = eyes[self._camera_view]
            # A complete camera resets any previous pan and camera roll.
            updates["scene.camera"] = {"eye": {"x": x, "y": y, "z": z},
                                       "center": {"x": 0, "y": 0, "z": 0},
                                       "up": {"x": 0, "y": 1, "z": 0} if z and not x and not y else {"x": 0, "y": 0, "z": 1}}
        return updates

    def _update_preview_view(self, *, camera=False, transparency=False):
        updates = {} if transparency else self._view_layout(camera=camera)
        opacity = 0.4 if self._transparent_shield else 1.0
        for figure in self._preview._figures:
            for axis in "xyz":
                figure.layout.scene[f"{axis}axis"].visible = self._axes_visible
            if camera:
                figure.layout.scene.camera = updates["scene.camera"]
            for trace in figure.data:
                if trace.type == "mesh3d":
                    trace.opacity = opacity
        plots = getattr(self._preview, "_plots", ())
        plot = plots[0] if plots else None
        plotly = getattr(type(plot), "_plotly", None) if plot is not None else None
        target = getattr(plot, "_js_component", None) if plot is not None else None
        if plotly is None or target is None:
            self._preview.redraw()
            return

        try:
            if updates:
                plotly._call_method_ignore_return("relayout", [target, updates])
            if transparency:
                mesh_indices = [index for index, trace in enumerate(self._preview._figures[0].data)
                                if trace.type == "mesh3d"]
                if mesh_indices:
                    plotly._call_method_ignore_return(
                        "restyle", [target, {"opacity": opacity}, mesh_indices]
                    )
        except Exception:
            self._preview.redraw()

    def toggle_tree(self):
        if self._tree_splitter.ui_model_value:
            self._tree_width = self._tree_splitter.ui_model_value
            self._tree_splitter.ui_limits = [0, 0]
            self._tree_splitter.ui_model_value = 0
        else:
            self._tree_splitter.ui_limits = [150, 460]
            self._tree_splitter.ui_model_value = self._tree_width

    def toggle_properties(self):
        if self._inner_splitter.ui_model_value:
            self._property_width = self._inner_splitter.ui_model_value
            self._inner_splitter.ui_limits = [0, 0]
            self._inner_splitter.ui_model_value = 0
        else:
            self._inner_splitter.ui_limits = [0, 520]
            self._inner_splitter.ui_model_value = self._property_width

    def _set_value(self, item, key, event):
        value = getattr(event, "value", event)
        value = str(value or "").strip()
        if not value:
            self._set_status(f"{key.title()} cannot be empty.", error=True)
            return
        item[key] = value
        self._mark_changed()
        if key == "name":
            self._rebuild_items()

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
        self._preview._plots = []
        self._preview._figures = []
        self._preview.container.ui_children = [
            Div(
                "The combined STEP and wire trace preview will appear here.",
                ui_class="cb-muted",
                ui_style="display:grid; place-items:center; height:100%; min-height:0;",
            )
        ]

    def _rebuild_preview(self):
        project = copy.deepcopy(self.project)
        assets = dict(self.assets)
        if not project["geometries"] and not project["wire_trace_sets"]:
            with self._preview_lock:
                self._preview_generation += 1
                self._preview_pending = None
            self._show_empty_preview()
            return
        with self._preview_lock:
            self._preview_generation += 1
            generation = self._preview_generation
            self._preview_pending = (generation, project, assets)
            if self._preview_running:
                return
            self._preview_running = True
        threading.Thread(target=self._run_preview_queue, name="MMFEM-3D-preview", daemon=True).start()

    def _cache_preview_item(self, key, value):
        if key in self._preview_cache:
            old_value, old_size = self._preview_cache.pop(key)
            self._preview_cache_size -= old_size
            self._preview_cache_order.remove(key)
        if isinstance(value, dict) and "vertices" in value:
            size = sum(
                int(getattr(value[name], "nbytes", 0))
                for name in ("vertices", "triangles")
            )
        else:
            size = sys.getsizeof(value) + sum(
                sys.getsizeof(item)
                + (sum(sys.getsizeof(part) for part in item) if isinstance(item, tuple) else 0)
                for item in value
            )
        budget = 128 * 1024 * 1024
        if size > budget:
            return
        self._preview_cache[key] = (value, size)
        self._preview_cache_order.append(key)
        self._preview_cache_size += size
        while self._preview_cache_size > budget and self._preview_cache_order:
            oldest = self._preview_cache_order.pop(0)
            _old_value, old_size = self._preview_cache.pop(oldest)
            self._preview_cache_size -= old_size

    def _get_preview_item(self, key, build):
        entry = self._preview_cache.pop(key, None)
        if entry is not None:
            self._preview_cache[key] = entry
            self._preview_cache_order.remove(key)
            self._preview_cache_order.append(key)
            return entry[0]
        value = build()
        self._cache_preview_item(key, value)
        return value

    def _build_preview_figure(self, project, assets):
        from .three_d_preview import _sample_trace_points, preview_figure, tessellate_step
        from .three_d_project import parse_wire_trace

        geometry_payload = []
        for item in project.get("geometries", []):
            data = assets[item["asset"]]
            key = ("geometry", item.get("sha256"), item.get("units", "m"))
            mesh = self._get_preview_item(
                key,
                lambda source=data, units=item.get("units", "m"): tessellate_step(source, units=units),
            )
            geometry_payload.append({
                "name": item["name"],
                "material": item.get("material", "Shield"),
                **mesh,
            })
        trace_payload = []
        for trace_set in project.get("wire_trace_sets", []):
            for trace in trace_set.get("traces", []):
                data = assets[trace["filename"]]
                key = ("trace", trace.get("sha256"))
                points = self._get_preview_item(
                    key,
                    lambda source=data: _sample_trace_points(parse_wire_trace(source, "m"), 20_000),
                )
                trace_payload.append({
                    "name": trace.get("name", trace_set.get("name", "Wire trace")),
                    "material": trace.get("material", trace_set.get("material", "Air")),
                    "points": points,
                })
        figure = preview_figure({"geometry": geometry_payload, "traces": trace_payload})
        camera_eyes = {
            "isometric": {"x": 1.45, "y": 1.45, "z": 1.15},
            "right": {"x": 1.8, "y": 0, "z": 0},
            "left": {"x": -1.8, "y": 0, "z": 0},
            "front": {"x": 0, "y": -1.8, "z": 0},
            "back": {"x": 0, "y": 1.8, "z": 0},
            "top": {"x": 0, "y": 0, "z": 1.8},
            "bottom": {"x": 0, "y": 0, "z": -1.8},
        }
        figure.update_layout(
            scene_camera={"eye": camera_eyes[self._camera_view]},
            scene_xaxis_visible=self._axes_visible,
            scene_yaxis_visible=self._axes_visible,
            scene_zaxis_visible=self._axes_visible,
        )
        return figure

    def _run_preview_queue(self):
        while True:
            with self._preview_lock:
                job = self._preview_pending
                self._preview_pending = None
                if job is None:
                    self._preview_running = False
                    return
            generation, project, assets = job
            try:
                figure = self._build_preview_figure(project, assets)
                self._dispatch_preview_result(generation, figure=figure)
            except Exception as error:
                self._dispatch_preview_result(generation, error=error)

    def _dispatch_preview_result(self, generation, *, figure=None, error=None):
        """Publish worker results on the UI loop and discard superseded previews."""
        loop = getattr(self.app, "loop", None)
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._publish_preview_result, generation, figure, error)
                return
            except RuntimeError:
                # The application may be closing while OCC finishes its current call.
                return
        self._publish_preview_result(generation, figure, error)

    def _publish_preview_result(self, generation, figure, error):
        with self._preview_lock:
            if generation != self._preview_generation:
                return
        if error is not None:
            self._set_status(f"Could not build the 3D preview: {error}", error=True)
            return
        # Apply the latest toggles even if they changed while OCC was working.
        figure.update_layout(**{f"scene_{axis}axis_visible": self._axes_visible for axis in "xyz"})
        for trace in figure.data:
            if trace.type == "mesh3d":
                trace.opacity = 0.4 if self._transparent_shield else 1.0
        self._preview.update_figure(figure)
        self._set_status("Preview ready. Original STEP and wire trace files remain in the project.")


# Compatibility name retained for existing integrations importing the old class.
ThreeDProjectDialog = ThreeDWorkspace
