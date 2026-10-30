"""Import and preview STEP geometry with solver-ready wire traces."""

from __future__ import annotations

import copy
import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from ngapp.components import Div, QBtn, QIcon, QInput, QSelect, QSplitter, QTooltip

from . import cerbsim_style as cb
from .axisymmetric_model import builtin_materials
from .material_library import MaterialLibrary
from .three_d_mesh import generate_step_mesh
from .three_d_project import (
    LENGTH_UNITS,
    MAX_MESH_SIZE_MM,
    MIN_MESH_SIZE_MM,
    PROJECT_SUFFIX,
    add_step_geometry,
    add_wire_trace,
    detect_step_unit,
    new_3d_project,
    package_3d_project,
    snapshot_material,
    step_unit_scale,
    unpack_3d_project,
    validate_3d_project,
)
from .workspace_components import panel_header, property_editor_body


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
_VIEW_OPTIONS = [
    {"label": "Isometric", "value": "isometric"},
    {"label": "Right", "value": "right"},
    {"label": "Left", "value": "left"},
    {"label": "Front", "value": "front"},
    {"label": "Back", "value": "back"},
    {"label": "Top", "value": "top"},
    {"label": "Bottom", "value": "bottom"},
]


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

    def __init__(self, app, *, material_library=None):
        self.app = app
        self._material_library = material_library or MaterialLibrary()
        self.project = new_3d_project()
        self.assets = {}
        self._preview_generation = 0
        self._mesh_generation = 0
        self._mesh_job = None
        self._mesh_worker_active = False
        self._mesh_results = {}
        self._mesh_surface_visible = True
        self._mesh_edges_visible = True
        self._selection_draft = {
            "geometry_id": None,
            "kind": "boundary",
            "selection_id": None,
            "name": "",
            "region_ids": [],
            "material": None,
        }
        self._study_running = False
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
        self._status = Div(
            "Import a STEP model or wire traces to start.",
            ui_class="cb-muted",
            ui_style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;",
        )
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
        self._view_select = QSelect(
            ui_label="View",
            ui_options=_VIEW_OPTIONS,
            ui_option_label="label",
            ui_option_value="value",
            ui_emit_value=True,
            ui_map_options=True,
            ui_model_value=self._camera_view,
            ui_dense=True,
            ui_filled=True,
            ui_hide_bottom_space=True,
            ui_style="width:136px;",
        )
        self._view_select.on_update_model_value(
            lambda event: self.set_camera_view(getattr(event, "value", event))
        )
        self._project_node = Div()
        self._project_node.on("click", lambda _event=None: self.select_item("project", None))
        self._study_node = Div()
        self._study_node.on("click", lambda _event=None: self.select_item("study", None))
        self._geometry_items = Div(ui_style="display:flex; flex-direction:column; gap:2px;")
        self._trace_items = Div(ui_style="display:flex; flex-direction:column; gap:2px;")
        self._items = Div(
            self._project_node,
            self._study_node,
            Div(
                Div(QIcon(ui_name="mdi-cube-outline", ui_size="16px"), "Geometry", ui_style="display:flex; align-items:center; gap:7px; font-weight:650;"),
                _button("", "mdi-plus", self.import_step, tooltip="Import STEP geometry"),
                ui_style="display:flex; align-items:center; justify-content:space-between; padding:5px 8px;",
            ),
            self._geometry_items,
            Div(
                Div(QIcon(ui_name="mdi-vector-polyline", ui_size="16px"), "Wire traces", ui_style="display:flex; align-items:center; gap:7px; font-weight:650;"),
                _button("", "mdi-plus", self.import_wire_trace, tooltip="Import wire traces"),
                ui_style="display:flex; align-items:center; justify-content:space-between; padding:5px 8px; margin-top:10px;",
            ),
            self._trace_items,
            ui_class="mmfem-3d-model-tree",
            ui_style="display:flex; flex-direction:column; flex:1 1 auto; min-height:0; overflow:auto; padding:8px;",
        )
        self._tree_panel = Div(
            panel_header(
                "Model tree",
                on_collapse=self.toggle_tree,
                icon="mdi-chevron-left",
                tooltip="Collapse model tree",
            ),
            self._items,
            ui_style="display:flex; flex-direction:column; flex:1 1 0%; width:100%; height:100%; min-width:0; min-height:0; overflow:hidden; border-right:1px solid var(--border); background:var(--surface);",
        )
        self._properties = property_editor_body(gap=12)
        self._properties_panel = Div(
            panel_header(
                "Properties",
                on_collapse=self.toggle_properties,
                icon="mdi-chevron-right",
                tooltip="Collapse properties",
            ),
            self._properties,
            ui_style="display:flex; flex-direction:column; flex:1 1 0%; width:100%; height:100%; min-width:0; min-height:0; overflow:hidden; border-left:1px solid var(--border); background:var(--surface);",
        )

        from .plot import PlotComponent

        plot_app_data = SimpleNamespace(get_tab=lambda _name: {"title": "3D project preview"})
        self._preview = PlotComponent(
            "mmfem_3d_project_preview",
            {"obj": []},
            plot_app_data,
            on_plotly_click=self._on_preview_plotly_click,
        )
        self._preview.ui_class = "fit mmfem-3d-project-preview"
        self._preview.ui_style = (
            "position:absolute; inset:0; width:100%; height:100%; "
            "min-width:0; min-height:0; overflow:hidden;"
        )
        self._preview.ui_hidden = True
        self._preview_empty_title = Div(
            "No 3D assets yet",
            ui_style="font-size:15px; font-weight:650; color:var(--fg);",
        )
        self._preview_empty_detail = Div(
            "Import a STEP model or wire traces to preview the project.",
            ui_class="cb-muted",
            ui_style="max-width:340px; text-align:center; line-height:1.5;",
        )
        self._preview_empty_state = Div(
            QIcon(ui_name="mdi-cube-scan", ui_size="30px", ui_color="grey-6"),
            self._preview_empty_title,
            self._preview_empty_detail,
            ui_style=(
                "display:flex; flex-direction:column; align-items:center; "
                "justify-content:center; gap:10px; position:absolute; inset:0; "
                "padding:24px; box-sizing:border-box; text-align:center;"
            ),
        )
        self._transparent_shield = False
        self._transparency_button = _button(
            "Transparent shield", "mdi-opacity", self.toggle_transparency,
            tooltip="Toggle transparency for imported STEP geometry",
        )
        self._axes_button = _button(
            "Axes", "mdi-axis-arrow", self.toggle_axes,
            tooltip="Toggle coordinate axes",
        )
        preview_host = Div(
            self._preview,
            self._preview_empty_state,
            ui_class="mmfem-3d-viewport",
            ui_style="position:relative; display:flex; flex:1 1 0%; width:100%; height:100%; min-width:0; min-height:0; overflow:hidden; background:var(--canvas-bg, #f4f6f8);",
        )

        self._open_project_button = _button(
            "Open project", "mdi-folder-open-outline", self.open_project
        )
        self._save_project_button = _button(
            "Save project", "mdi-content-save-outline", self.save_project,
            color="primary",
        )
        self._run_study_button = _button(
            "Run study", "mdi-play", self.run_study,
            color="primary",
            tooltip="Run the 3D time-harmonic electromagnetic study",
        )
        self._cancel_study_button = _button(
            "Cancel", "mdi-stop", self.cancel_study,
            tooltip="Stop the running 3D study",
        )
        self._cancel_study_button.ui_disable = True
        self._import_step_button = _button(
            "Import STEP", "mdi-cube-scan", self.import_step
        )
        self._import_trace_button = _button(
            "Import wire traces", "mdi-vector-polyline", self.import_wire_trace
        )
        self._fit_view_button = _button(
            "Fit view", "mdi-fit-to-screen-outline", self.fit_view
        )
        self._generate_mesh_button = _button(
            "Generate mesh",
            "mdi-grid",
            self.generate_mesh,
            color="primary",
            tooltip="Generate a Netgen volume mesh for the selected geometry",
        )
        self._generate_mesh_button.ui_disable = True
        self._mesh_surface_button = _button(
            "Surface mesh",
            "mdi-eye-outline",
            self.toggle_mesh_surface,
            color="primary",
            tooltip="Show or hide generated mesh facets",
        )
        self._mesh_edges_button = _button(
            "Mesh edges",
            "mdi-vector-square",
            self.toggle_mesh_edges,
            color="primary",
            tooltip="Show or hide generated mesh edges",
        )
        self._mesh_surface_button.ui_disable = True
        self._mesh_edges_button.ui_disable = True
        self._unit_controls = Div(
            self._geometry_units,
            self._trace_units,
            ui_style="display:flex; align-items:center; flex:none; gap:6px;",
        )
        self._project_toolbar = Div(
            self._project_name,
            Div(
                self._run_study_button,
                self._cancel_study_button,
                self._open_project_button,
                self._save_project_button,
                ui_style="display:flex; align-items:center; gap:6px; margin-left:auto;",
            ),
            ui_style=(
                "display:flex; flex-wrap:wrap; align-items:center; gap:8px; "
                "min-height:48px; padding:6px 12px; box-sizing:border-box; "
                "border-bottom:1px solid var(--border); background:var(--surface);"
            ),
        )
        self._model_toolbar = Div(
            self._import_step_button,
            self._import_trace_button,
            Div(ui_style="width:1px; height:24px; margin:0 2px; background:var(--border);"),
            self._fit_view_button,
            self._generate_mesh_button,
            self._mesh_surface_button,
            self._mesh_edges_button,
            self._view_select,
            self._axes_button,
            self._transparency_button,
            Div(ui_style="flex:1 1 auto; min-width:8px;"),
            self._unit_controls,
            ui_style=(
                "display:flex; flex-wrap:wrap; align-items:center; gap:6px; "
                "min-height:44px; padding:6px 12px; box-sizing:border-box; "
                "border-bottom:1px solid var(--border); background:var(--panel-header);"
            ),
        )
        self._toolbar = Div(
            self._project_toolbar,
            self._model_toolbar,
            ui_style="display:flex; flex-direction:column; flex:none; min-width:0; width:100%;",
        )
        inner_splitter = QSplitter(
            ui_model_value=300,
            ui_unit="px",
            ui_reverse=True,
            ui_limits=[220, 480],
            ui_emit_immediately=True,
            ui_slots={"before": [preview_host], "after": [self._properties_panel]},
            ui_class=cb.body_inner,
            ui_style="flex:1 1 auto; width:100%; min-width:0; min-height:0;",
        )
        self._tree_splitter = QSplitter(
            ui_model_value=self._tree_width,
            ui_unit="px",
            ui_limits=[180, 380],
            ui_emit_immediately=True,
            ui_slots={"before": [self._tree_panel], "after": [inner_splitter]},
            ui_class=cb.body_fill,
            ui_style="flex:1 1 auto; width:100%; min-width:0; min-height:0;",
        )
        self._inner_splitter = inner_splitter
        self._tree_splitter.on_update_model_value(self._on_tree_width_change)
        self._inner_splitter.on_update_model_value(self._on_property_width_change)
        self._body = Div(
            self._tree_splitter,
            ui_style=(
                "position:relative; display:flex; flex:1 1 auto; width:100%; "
                "min-width:0; min-height:0; overflow:hidden;"
            ),
        )
        self._tree_reopen_tab = _button(
            "Model tree", "mdi-chevron-right", self.toggle_tree,
            tooltip="Show the model tree",
        )
        self._tree_reopen_tab.ui_class = "mmfem-panel-reopen-tab mmfem-panel-reopen-tab-left"
        self._tree_reopen_tab.ui_style = (
            "position:absolute; top:50%; left:0; z-index:20; transform:translateY(-50%); "
            "width:36px; min-width:36px; height:136px; padding:8px 4px; "
            "writing-mode:vertical-rl; text-orientation:mixed; "
            "border:1px solid var(--border); border-left:0; border-radius:0 8px 8px 0; "
            "background:var(--surface); color:var(--fg); box-shadow:0 2px 8px rgba(18, 32, 48, .12);"
        )
        self._tree_reopen_tab.ui_hidden = True
        self._tree_reopen_tab._props["aria-label"] = "Show the model tree"
        self._properties_reopen_tab = _button(
            "Properties", "mdi-chevron-left", self.toggle_properties,
            tooltip="Show properties",
        )
        self._properties_reopen_tab.ui_class = "mmfem-panel-reopen-tab mmfem-panel-reopen-tab-right"
        self._properties_reopen_tab.ui_style = (
            "position:absolute; top:50%; right:0; z-index:20; transform:translateY(-50%); "
            "width:36px; min-width:36px; height:136px; padding:8px 4px; "
            "writing-mode:vertical-rl; text-orientation:mixed; "
            "border:1px solid var(--border); border-right:0; border-radius:8px 0 0 8px; "
            "background:var(--surface); color:var(--fg); box-shadow:0 2px 8px rgba(18, 32, 48, .12);"
        )
        self._properties_reopen_tab.ui_hidden = True
        self._properties_reopen_tab._props["aria-label"] = "Show properties"
        self._body.ui_children = [
            self._tree_splitter,
            self._tree_reopen_tab,
            self._properties_reopen_tab,
        ]
        self._status_bar = Div(
            self._status,
            ui_style=(
                "display:flex; align-items:center; flex:none; min-height:34px; "
                "padding:0 16px 0 12px; box-sizing:border-box; border-top:1px solid var(--border); "
                "background:var(--surface);"
            ),
        )
        super().__init__(
            self._toolbar,
            self._body,
            self._status_bar,
            ui_style=(
                "display:flex; flex-direction:column; width:100%; height:100%; "
                "min-width:0; min-height:0; overflow:hidden; background:var(--app-bg, #f7f8fa); color:var(--fg, #202631);"
            ),
        )
        self.ui_hidden = True
        self._rebuild_items()
        self._rebuild_properties()
        self._refresh_axes_button()
        self._refresh_mesh_controls()

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
        self._reset_meshes()
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
        self._status.ui_class = "mmfem-3d-status-error" if error else "cb-muted"
        self._status.ui_style = (
            "flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"
            + (" color:var(--negative);" if error else "")
        )

    def _update_study_controls(self):
        running = bool(self._study_running)
        if hasattr(self, "_run_study_button"):
            self._run_study_button.ui_disable = (
                running
                or not self.project.get("geometries")
                or not self.project.get("wire_trace_sets")
            )
        if hasattr(self, "_cancel_study_button"):
            self._cancel_study_button.ui_disable = not running

    def run_study(self, *_args):
        callback = getattr(self.app, "run_3d_study", None)
        if not callable(callback):
            self._set_status("3D study execution is unavailable in this session.", error=True)
            return False
        return callback(self)

    def cancel_study(self, *_args):
        callback = getattr(self.app, "cancel_3d_study", None)
        return callback(self) if callable(callback) else False

    def _material_records(self):
        records = [*builtin_materials(), *self._material_library.load()]
        by_name = {}
        for record in records:
            name = str(record.get("name", "")).strip()
            if name:
                by_name.setdefault(name.casefold(), record)
        for record in self.project.get("materials", []):
            name = str(record.get("name", "")).strip()
            if name:
                by_name[name.casefold()] = record
        return by_name

    def _snapshot_material_reference(self, name):
        material = self._material_records().get(str(name or "").strip().casefold())
        return snapshot_material(self.project, material) if material else False

    def _snapshot_referenced_materials(self):
        changed = False
        names = set()
        for geometry in self.project.get("geometries", []):
            names.add(geometry.get("material", ""))
            names.update(
                selection.get("material", "")
                for selection in geometry.get("domain_selections", [])
            )
        for trace_set in self.project.get("wire_trace_sets", []):
            names.add(trace_set.get("material", "Air"))
            names.update(
                trace.get("material", trace_set.get("material", "Air"))
                for trace in trace_set.get("traces", [])
            )
        for name in names:
            changed = self._snapshot_material_reference(name) or changed
        if changed:
            self._mark_changed()
        return changed

    def _set_study_value(self, key, event, *, number=False):
        study = self.project.setdefault("study", {})
        old_value = study.get(key)
        value = getattr(event, "value", event)
        try:
            value = float(value) if number else int(value)
            if number and not math.isfinite(value):
                raise ValueError("Enter a finite number.")
            study[key] = value
            validate_3d_project(self.project, self.assets)
        except (TypeError, ValueError, OverflowError) as error:
            study[key] = old_value
            self._set_status(str(error), error=True)
            self._rebuild_properties()
            return False
        self._mark_changed()
        self._set_status("3D study settings updated.")
        return True

    def _mark_changed(self):
        self._saved = False

    def _reset_meshes(self):
        self._mesh_generation += 1
        self._mesh_job = None
        self._mesh_results = {}
        self._mesh_surface_visible = True
        self._mesh_edges_visible = True
        self._selection_draft = {
            "geometry_id": None,
            "kind": "boundary",
            "selection_id": None,
            "name": "",
            "region_ids": [],
            "material": None,
        }
        self._update_study_controls()
        self._refresh_mesh_controls()

    def _refresh_mesh_controls(self):
        if getattr(self, "_generate_mesh_button", None) is None:
            return
        geometry = self._selected_geometry()
        self._generate_mesh_button.ui_disable = (
            self._mesh_worker_active or geometry is None
        )
        has_mesh = bool(
            geometry
            and self._mesh_results.get(geometry["id"], {}).get("status") == "ready"
        )
        self._mesh_surface_button.ui_disable = self._mesh_worker_active or not has_mesh
        self._mesh_surface_button.ui_color = (
            "primary" if self._mesh_surface_visible else None
        )
        self._mesh_surface_button.ui_flat = not self._mesh_surface_visible
        wireframe_available = bool(
            has_mesh
            and self._mesh_results.get(geometry["id"], {}).get("wireframe_available", True)
        )
        self._mesh_edges_button.ui_disable = (
            self._mesh_worker_active or not wireframe_available
        )
        self._mesh_edges_button.ui_color = "primary" if self._mesh_edges_visible else None
        self._mesh_edges_button.ui_flat = not self._mesh_edges_visible
        self._update_study_controls()

    def _selected_geometry(self):
        kind, item_id = getattr(self, "_selection", ("project", None))
        if kind == "geometry":
            return next(
                (item for item in self.project.get("geometries", []) if item.get("id") == item_id),
                None,
            )
        if kind == "named_selection":
            context = self._find_named_selection(item_id)
            return context[0] if context else None
        return None

    def _find_named_selection(self, selection_id):
        for geometry in self.project.get("geometries", []):
            for kind in ("domain", "boundary"):
                for selection in geometry.get(f"{kind}_selections", []):
                    if selection.get("id") == selection_id:
                        return geometry, kind, selection
        return None

    def toggle_mesh_surface(self, *_args):
        self._mesh_surface_visible = not self._mesh_surface_visible
        self._refresh_mesh_controls()
        self._update_preview_selection()

    def toggle_mesh_edges(self, *_args):
        self._mesh_edges_visible = not self._mesh_edges_visible
        self._refresh_mesh_controls()
        self._update_preview_selection()

    def _mesh_tree_label(self, item_id):
        result = self._mesh_results.get(item_id, {})
        state = result.get("status")
        if state == "generating":
            return "Meshing"
        if state == "ready":
            return "Meshed"
        if state == "error":
            return "Mesh error"
        return None

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
            self._snapshot_material_reference(trace_set.get("material", "Air"))
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
        self._reset_meshes()
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
        self._reset_meshes()
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
            self._snapshot_referenced_materials()
            safe_name = "".join(
                character if character.isalnum() or character in "-_ " else "_"
                for character in self.project["name"]
            ).strip() or "3D project"
            archive = package_3d_project(self.project, self.assets)
            if self.app._save_artifact(archive, safe_name + PROJECT_SUFFIX):
                self._saved = True
                self._set_status("3D project saved with geometry, material data and study settings.")
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
        for item in removed:
            self._mesh_results.pop(item["id"], None)
            if self._mesh_job and self._mesh_job["geometry_id"] == item["id"]:
                self._mesh_generation += 1
                self._mesh_job = None
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

    def _tree_item(
        self, label, kind, item_id, icon, on_delete, material=None, mesh_status=None
    ):
        selected = self._selection == (kind, item_id)
        label_node = Div(
            label,
            ui_style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px;",
        )
        label_node._props["title"] = label
        label_node._props["aria-label"] = label
        children = [
            QIcon(ui_name=icon, ui_size="15px", ui_color="primary" if selected else "grey-7"),
            label_node,
        ]
        if material:
            children.append(Div(
                material,
                ui_class="cb-muted",
                ui_style=(
                    "max-width:76px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; "
                    "font-size:10px; padding:1px 4px; border-radius:4px; background:var(--panel-header);"
                ),
            ))
        if mesh_status:
            if mesh_status == "Meshed":
                status_icon = QIcon(
                    ui_name="mdi-grid",
                    ui_size="14px",
                    ui_color="positive",
                )
                status_icon._props["title"] = "Volume mesh ready"
                children.append(status_icon)
            else:
                children.append(Div(
                    mesh_status,
                    ui_class="cb-muted",
                    ui_style=(
                        "max-width:58px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; "
                        "font-size:10px; padding:1px 4px; border-radius:4px; "
                        "background:var(--accent-subtle); color:var(--accent);"
                    ),
                ))
        children.append(_button("", "mdi-delete-outline", on_delete, tooltip="Remove from project"))
        row = Div(
            *children,
            ui_style=(
                "display:flex; align-items:center; gap:6px; min-width:0; padding:4px 5px 4px 18px; "
                "border-radius:5px; cursor:pointer; "
                + ("background:var(--accent-subtle); color:var(--accent);" if selected else "")
            ),
        )
        row.on("click", lambda _event=None, k=kind, key=item_id: self.select_item(k, key))
        return row

    def _named_selection_tree_item(self, geometry, kind, selection):
        selected = self._selection == ("named_selection", selection["id"])
        label = Div(
            selection["name"],
            ui_style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:11px;",
        )
        label._props["title"] = selection["name"]
        row = Div(
            QIcon(
                ui_name="mdi-select-group",
                ui_size="14px",
                ui_color="primary" if selected else "grey-7",
            ),
            label,
            Div(
                "Domain" if kind == "domain" else "Boundary",
                ui_class="cb-muted",
                ui_style="font-size:9px; padding:1px 4px; border-radius:4px; background:var(--panel-header);",
            ),
            _button(
                "",
                "mdi-delete-outline",
                lambda *_args, g=geometry, k=kind, s=selection: self._remove_named_selection(g, k, s),
                tooltip="Remove named selection",
            ),
            ui_style=(
                "display:flex; align-items:center; gap:5px; min-width:0; padding:3px 5px 3px 36px; "
                "border-radius:5px; cursor:pointer; "
                + ("background:var(--accent-subtle); color:var(--accent);" if selected else "")
            ),
        )
        row.on(
            "click",
            lambda _event=None, selection_id=selection["id"]: self.select_item(
                "named_selection", selection_id
            ),
        )
        return row

    def _remove_named_selection(self, geometry, kind, selection):
        collection = geometry.get(f"{kind}_selections", [])
        if selection in collection:
            collection.remove(selection)
            if self._selection == ("named_selection", selection["id"]):
                self._selection = ("geometry", geometry["id"])
                self._selection_draft = {
                    "geometry_id": geometry["id"],
                    "kind": kind,
                    "selection_id": None,
                    "name": "",
                    "region_ids": [],
                    "material": None,
                }
            self._mark_changed()
            self._rebuild_items()
            self._rebuild_properties()
            self._rebuild_preview()

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
        selected_study = self._selection == ("study", None)
        self._study_node.ui_children = [
            QIcon(
                ui_name="mdi-waveform",
                ui_size="15px",
                ui_color="primary" if selected_study else "grey-7",
            ),
            Div(
                "Time-harmonic EM",
                ui_style="flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:12px;",
            ),
        ]
        self._study_node.ui_style = (
            "display:flex; align-items:center; gap:6px; min-width:0; padding:5px 8px 5px 20px; border-radius:5px; cursor:pointer; "
            + ("background:var(--accent-subtle); color:var(--accent);" if selected_study else "")
        )
        geometry_rows = []
        for geometry in self.project.get("geometries", []):
            geometry_rows.append(self._tree_item(
                geometry["name"], "geometry", geometry["id"], "mdi-cube-outline",
                lambda *_a, asset=geometry["asset"]: self._remove_geometry(asset),
                material=(
                    geometry.get("material")
                    if self._mesh_tree_label(geometry["id"]) is None else None
                ),
                mesh_status=self._mesh_tree_label(geometry["id"]),
            ))
            selections = [
                (kind, selection)
                for kind in ("domain", "boundary")
                for selection in geometry.get(f"{kind}_selections", [])
            ]
            if selections:
                geometry_rows.append(Div(
                    "NAMED SELECTIONS",
                    ui_class="cb-muted",
                    ui_style="padding:6px 4px 1px 36px; font-size:9px; letter-spacing:.06em; font-weight:650;",
                ))
                geometry_rows.extend(
                    self._named_selection_tree_item(geometry, kind, selection)
                    for kind, selection in selections
                )
        trace_rows = [
            self._tree_item(
                trace_set["name"], "trace_set", trace_set["id"], "mdi-vector-polyline",
                lambda *_a, item=trace_set: self._remove_trace_set(item),
                material=trace_set.get("material"),
            )
            for trace_set in self.project.get("wire_trace_sets", [])
        ]
        if not geometry_rows:
            geometry_rows = [Div("No geometry models", ui_class="cb-muted", ui_style="padding:5px 18px; font-size:12px;")]
        if not trace_rows:
            trace_rows = [Div("No wire traces", ui_class="cb-muted", ui_style="padding:5px 18px; font-size:12px;")]
        self._geometry_items.ui_children = geometry_rows
        self._trace_items.ui_children = trace_rows
        self._update_study_controls()
        self._update_preview_selection()
        self._refresh_mesh_controls()

    def _material_options(self, current):
        options = []
        names = set()
        material_records = [
            *self.project.get("materials", []),
            *builtin_materials(),
            *self._material_library.load(),
        ]
        for material in material_records:
            name = str(material.get("name", "")).strip()
            if not name or name.casefold() in names:
                continue
            names.add(name.casefold())
            options.append({"label": name, "value": name})
        current = str(current or "").strip()
        if current and current.casefold() not in names:
            options.append({"label": f"{current} (current project value)", "value": current})
        return options

    @staticmethod
    def _material_note():
        return Div(
            "Material properties are copied into the project when assigned.",
            ui_class="cb-muted",
            ui_style="font-size:11px; line-height:1.4; margin-top:-7px;",
        )

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

    def _multi_select_options(self, label, values, callback, options, *, disabled=False):
        widget = QSelect(
            ui_label=label,
            ui_options=options,
            ui_option_label="label",
            ui_option_value="value",
            ui_emit_value=True,
            ui_map_options=True,
            ui_multiple=True,
            ui_use_chips=True,
            ui_hide_selected=True,
            ui_model_value=values,
            ui_dense=True,
            ui_filled=True,
            ui_hide_bottom_space=True,
            ui_disable=disabled,
            ui_style="width:100%;",
        )
        widget.on_update_model_value(callback)
        return widget

    def _ensure_selection_draft(self, geometry):
        if self._selection_draft.get("geometry_id") != geometry["id"]:
            self._selection_draft = {
                "geometry_id": geometry["id"],
                "kind": "boundary",
                "selection_id": None,
                "name": "",
                "region_ids": [],
                "material": None,
            }
        return self._selection_draft

    def _set_selection_draft_kind(self, geometry, event):
        kind = str(getattr(event, "value", event) or "boundary")
        if kind not in {"domain", "boundary"}:
            return
        draft = self._ensure_selection_draft(geometry)
        if draft["kind"] == kind:
            return
        draft.update({
            "kind": kind,
            "selection_id": None,
            "name": "",
            "region_ids": [],
            "material": None,
        })
        self._selection = ("geometry", geometry["id"])
        self._rebuild_properties()
        self._rebuild_preview()

    def _set_selection_draft_regions(self, geometry, event):
        draft = self._ensure_selection_draft(geometry)
        value = getattr(event, "value", event)
        if value is None:
            value = []
        if not isinstance(value, (list, tuple, set)):
            value = [value]
        region_ids = []
        for region_id in value:
            try:
                region_id = int(region_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if region_id >= 0 and region_id not in region_ids:
                region_ids.append(region_id)
        draft["region_ids"] = sorted(region_ids)
        self._rebuild_preview()

    def _set_selection_draft_name(self, event):
        value = getattr(event, "value", event)
        self._selection_draft["name"] = str(value or "").strip()

    def _set_selection_draft_material(self, event):
        value = str(getattr(event, "value", event) or "").strip()
        options = {option["value"] for option in self._material_options(value)}
        options.add("__inherit__")
        if value not in options:
            self._set_status("Choose a material from the available library references.", error=True)
            return
        value = None if value == "__inherit__" else value
        self._selection_draft["material"] = value
        if value and self._snapshot_material_reference(value):
            self._mark_changed()

    def _new_named_selection(self, geometry):
        draft = self._ensure_selection_draft(geometry)
        draft.update({
            "selection_id": None,
            "name": "",
            "region_ids": [],
            "material": None,
        })
        self._selection = ("geometry", geometry["id"])
        self._rebuild_items()
        self._rebuild_properties()
        self._rebuild_preview()

    def _save_named_selection(self, geometry):
        draft = self._ensure_selection_draft(geometry)
        kind = draft["kind"]
        name = str(draft.get("name", "")).strip()
        region_ids = sorted(set(draft.get("region_ids", [])))
        if not name or len(name) > 100:
            self._set_status("Enter a name up to 100 characters for this selection.", error=True)
            return False
        if not region_ids:
            self._set_status("Select at least one domain or boundary region.", error=True)
            return False
        result = self._mesh_results.get(geometry["id"], {})
        if result.get("status") != "ready":
            self._set_status("Generate a volume mesh before saving region selections.", error=True)
            return False
        region_key = "domain_regions" if kind == "domain" else "boundary_regions"
        valid_ids = {int(region["id"]) for region in result.get(region_key, [])}
        if not set(region_ids).issubset(valid_ids):
            self._set_status("One or more selected regions are not in the current mesh.", error=True)
            return False

        collection_key = f"{kind}_selections"
        collection = geometry.setdefault(collection_key, [])
        selection_id = draft.get("selection_id")
        existing = next(
            (selection for selection in collection if selection.get("id") == selection_id),
            None,
        ) if selection_id else None
        if any(
            selection is not existing
            and selection.get("name", "").casefold() == name.casefold()
            for selection in collection
        ):
            self._set_status(f"A {kind} selection named {name!r} already exists.", error=True)
            return False
        if existing is None:
            selection_id = uuid4().hex
            existing = {"id": selection_id}
            collection.append(existing)
        existing["name"] = name
        existing["domain_ids" if kind == "domain" else "boundary_ids"] = region_ids
        if kind == "domain":
            assigned_material = str(draft.get("material") or "").strip()
            if assigned_material:
                existing["material"] = assigned_material
                self._snapshot_material_reference(assigned_material)
            else:
                existing.pop("material", None)
        else:
            existing.pop("material", None)
        draft.update({"selection_id": selection_id, "region_ids": region_ids})
        self._selection = ("named_selection", selection_id)
        self._mark_changed()
        validate_3d_project(self.project, self.assets)
        self._rebuild_items()
        self._rebuild_properties()
        self._rebuild_preview()
        self._set_status(f"Saved {kind} selection {name!r}.")
        return True

    def _active_boundary_region_ids(self, geometry_id):
        kind, item_id = self._selection
        draft = self._selection_draft
        if (
            draft.get("geometry_id") == geometry_id
            and draft.get("kind") == "boundary"
            and (kind == "geometry" or draft.get("selection_id") == item_id)
        ):
            return draft.get("region_ids", [])
        if kind == "named_selection":
            context = self._find_named_selection(item_id)
            if context and context[0]["id"] == geometry_id and context[1] == "boundary":
                return context[2].get("boundary_ids", [])
        return []

    def _mesh_quality_message(self, item_id):
        result = self._mesh_results.get(item_id, {})
        quality = result.get("tetra_quality")
        if result.get("status") != "ready":
            return None
        if quality is None:
            return "Quality metrics are unavailable for this volume element type."
        return (
            "Tetrahedral mean ratio (0 to 1) · "
            f"minimum {quality['minimum']:.3f} · 5th percentile {quality['p05']:.3f} · "
            f"median {quality['median']:.3f} · "
            f"{quality['below_0_1']:,} below 0.100 "
            f"({quality['below_0_1_percent']:.2f}%)"
        )

    def _mesh_setup_properties(self, geometry):
        item_id = geometry["id"]
        result = self._mesh_results.get(item_id, {})
        ready = result.get("status") == "ready"
        draft = self._ensure_selection_draft(geometry)
        kind = draft["kind"]
        regions = result.get(
            "domain_regions" if kind == "domain" else "boundary_regions", []
        )
        region_options = [
            {
                "label": (
                    f"{region['name']} · {region['elements']:,} "
                    f"{'volume elements' if kind == 'domain' else 'surface triangles'}"
                ),
                "value": int(region["id"]),
            }
            for region in regions
        ]
        children = []
        quality_message = self._mesh_quality_message(item_id)
        if quality_message:
            children.extend([
                Div("Mesh quality", ui_style="font-size:13px; font-weight:650; margin-top:8px;"),
                Div(
                    quality_message,
                    ui_class="cb-muted",
                    ui_style="font-size:11px; line-height:1.4; overflow-wrap:anywhere;",
                ),
            ])
        children.extend([
            Div("Named selections", ui_style="font-size:13px; font-weight:650; margin-top:8px;"),
            Div(
                "Choose volume domains to create material assignments, or select boundaries for named groups.",
                ui_class="cb-muted",
                ui_style="font-size:10px; line-height:1.35;",
            ),
        ])
        if kind == "domain":
            region_material = draft.get("material")
            children.extend([
                self._select_options(
                    "Region material",
                    region_material or "__inherit__",
                    self._set_selection_draft_material,
                    [
                        {"label": "Use default material", "value": "__inherit__"},
                        *self._material_options(None),
                    ],
                ),
                self._material_note(),
            ])
        children.extend([
            self._select_options(
                "Entity type",
                kind,
                lambda event, item=geometry: self._set_selection_draft_kind(item, event),
                [
                    {"label": "Boundary regions", "value": "boundary"},
                    {"label": "Volume domains", "value": "domain"},
                ],
            ),
            self._multi_select_options(
                "Boundary regions" if kind == "boundary" else "Volume domains",
                draft.get("region_ids", []),
                lambda event, item=geometry: self._set_selection_draft_regions(item, event),
                region_options,
                disabled=not ready,
            ),
            _input(
                "Selection name",
                draft.get("name", ""),
                self._set_selection_draft_name,
                width="100%",
            ),
            Div(
                _button(
                    "Update selection" if draft.get("selection_id") else "Save selection",
                    "mdi-content-save-outline",
                    lambda *_args, item=geometry: self._save_named_selection(item),
                    color="primary",
                ),
                _button(
                    "New selection",
                    "mdi-plus",
                    lambda *_args, item=geometry: self._new_named_selection(item),
                    tooltip="Clear the editor and create another named selection",
                ),
                ui_style="display:flex; flex-wrap:wrap; gap:6px;",
            ),
        ])
        if not ready:
            children.append(Div(
                "Generate a volume mesh to identify domains and boundaries.",
                ui_class="cb-muted",
                ui_style="font-size:10px; line-height:1.35;",
            ))
        elif not region_options:
            children.append(Div(
                "The generated mesh did not expose any named regions for this selection type.",
                ui_class="cb-muted",
                ui_style="font-size:10px; line-height:1.35;",
            ))
        return children

    def _mesh_property_message(self, item_id):
        result = self._mesh_results.get(item_id, {})
        state = result.get("status")
        if state == "generating":
            return "Netgen is generating a 3D volume mesh…"
        if state == "error":
            return f"Mesh failed: {result.get('error', 'Unknown meshing error')}"
        if state == "ready":
            message = (
                f"Mesh ready · {result['volume_elements']:,} volume elements · "
                f"{result['surface_faces']:,} surface triangles"
            )
            if not result.get("wireframe_available", True):
                message += ". Mesh edges are hidden to keep the viewport responsive."
            return message
        return (
            "No mesh generated. Mesh data stays in memory and is regenerated "
            "when the project is reopened."
        )

    def _rebuild_properties(self):
        kind, item_id = self._selection
        children = []
        self._mesh_property_status = None
        named_context = (
            self._find_named_selection(item_id) if kind == "named_selection" else None
        )
        if kind == "geometry":
            item = next(
                (entry for entry in self.project["geometries"] if entry["id"] == item_id),
                None,
            )
        elif named_context:
            item = named_context[0]
        else:
            item = None
        if kind in {"geometry", "named_selection"}:
            if item is None:
                self._selection = ("project", None)
                return self._rebuild_properties()
            draft = self._ensure_selection_draft(item)
            if named_context and draft.get("selection_id") != item_id:
                geometry, selection_kind, selection = named_context
                draft.update({
                    "geometry_id": geometry["id"],
                    "kind": selection_kind,
                    "selection_id": selection["id"],
                    "name": selection["name"],
                    "region_ids": list(selection.get(
                        "domain_ids" if selection_kind == "domain" else "boundary_ids", []
                    )),
                    "material": selection.get("material"),
                })
            name_target = named_context[2] if named_context else item
            name_label = "Selection name" if named_context else "Name"
            name_callback = (
                (lambda event, obj=name_target: self._set_named_selection_name(obj, event))
                if named_context
                else (lambda event, obj=name_target: self._set_value(obj, "name", event))
            )
            mesh_size_input = _input(
                "Maximum element size (mm)",
                item.get("mesh_maxh_mm", 30.0),
                lambda event, obj=item: self._set_mesh_maxh(obj, event),
                number=True,
                width="100%",
            )
            mesh_size_input.ui_disable = self._mesh_worker_active
            heading = (
                "Named boundary selection" if named_context and named_context[1] == "boundary"
                else "Named domain selection" if named_context
                else "Geometry model"
            )
            children.extend([
                Div(
                    heading,
                    ui_style="font-size:13px; font-weight:650;",
                ),
                _input(name_label, name_target["name"], name_callback, width="100%"),
            ])
            if named_context:
                geometry, selection_kind, selection = named_context
                region_ids = selection.get(
                    "domain_ids" if selection_kind == "domain" else "boundary_ids", []
                )
                region_label = "volume" if selection_kind == "domain" else "boundary"
                children.append(Div(
                    f"{len(region_ids)} {region_label} region"
                    f"{'s' if len(region_ids) != 1 else ''} assigned",
                    ui_class="cb-muted",
                    ui_style="font-size:11px;",
                ))
                if selection_kind == "domain":
                    geometry_material = geometry.get("material", "Shield")
                    children.extend([
                        self._select_options(
                            "Region material",
                            selection.get("material") or "__inherit__",
                            lambda event, obj=selection: self._set_named_selection_material(obj, event),
                            [
                                {
                                    "label": f"Use geometry default ({geometry_material})",
                                    "value": "__inherit__",
                                },
                                *self._material_options(
                                    selection.get("material") or geometry_material
                                ),
                            ],
                        ),
                        self._material_note(),
                    ])
                self._mesh_property_status = None
            else:
                children.extend([
                    self._select_options(
                        "Default material",
                        item.get("material", "Shield"),
                        lambda event, obj=item: self._set_material_reference(obj, event),
                        self._material_options(item.get("material", "Shield")),
                    ),
                    self._material_note(),
                    self._select_options(
                        "Length units", item.get("units", "m"),
                        lambda event, obj=item: self._set_geometry_units(obj, event),
                        _UNIT_OPTIONS,
                    ),
                    Div(
                        Path(item["asset"]).name,
                        ui_class="cb-muted",
                        ui_style=(
                            "font-size:11px; overflow:hidden; text-overflow:ellipsis; "
                            "white-space:nowrap;"
                        ),
                    ),
                    Div("3D mesh", ui_style="font-size:13px; font-weight:650; margin-top:8px;"),
                    mesh_size_input,
                    Div(
                        self._mesh_property_message(item["id"]),
                        ui_class="cb-muted",
                        ui_style="font-size:11px; line-height:1.4; overflow-wrap:anywhere;",
                    ),
                    Div(
                        "Netgen uses this as an upper bound. Mesh data is not saved; "
                        "it is regenerated from these settings when reopened.",
                        ui_class="cb-muted",
                        ui_style="font-size:10px; line-height:1.4; overflow-wrap:anywhere;",
                    ),
                ])
                self._mesh_property_status = children[-2]
                children.extend(self._mesh_setup_properties(item))
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
                self._select_options(
                    "Material reference", item.get("material", "Air"),
                    lambda event, obj=item: self._set_trace_material(obj, event),
                    self._material_options(item.get("material", "Air")),
                ),
                self._material_note(),
                _input("Current (A)", item.get("current", 1.0), lambda event, obj=item: self._set_number(obj, "current", event), number=True, width="100%"),
                _input("Target gradient (T/m)", item.get("target_gradient", ""), lambda event, obj=item: self._set_optional_number(obj, "target_gradient", event), number=True, width="100%"),
                _input("DSV (m)", item.get("dsv", 0.4), lambda event, obj=item: self._set_number(obj, "dsv", event), number=True, width="100%"),
                direction,
                Div(f"{trace.get('points', 0):,} points · source: {trace.get('name', item['name'])}", ui_class="cb-muted", ui_style="font-size:11px; overflow-wrap:anywhere;"),
            ])
        elif kind == "study":
            study = self.project.setdefault("study", {})
            children.extend([
                Div("Time-harmonic electromagnetic study", ui_style="font-size:13px; font-weight:650;"),
                Div(
                    "Solves magnetic vector potential and flux density at one frequency using the existing 3D EM solver.",
                    ui_class="cb-muted",
                    ui_style="font-size:11px; line-height:1.45;",
                ),
                _input(
                    "Frequency (Hz)", study.get("frequency_hz", 500.0),
                    lambda event: self._set_study_value("frequency_hz", event, number=True),
                    number=True, width="100%",
                ),
                self._select_options(
                    "Finite-element order", study.get("polynomial_order", 1),
                    lambda event: self._set_study_value("polynomial_order", event),
                    [{"label": f"{order} · {'linear' if order == 1 else 'quadratic' if order == 2 else 'order ' + str(order)}", "value": order}
                     for order in (1, 2, 3, 4)],
                ),
                _input(
                    "Air-region padding (model spans)", study.get("air_padding", 2.0),
                    lambda event: self._set_study_value("air_padding", event, number=True),
                    number=True, width="100%",
                ),
                Div(
                    "The study creates a surrounding Air volume and applies the magnetic boundary condition to its exterior. Mechanical and DC solves are not enabled in this workflow.",
                    ui_class="cb-muted",
                    ui_style="font-size:10px; line-height:1.45; padding:8px; background:var(--panel-header); border-radius:5px;",
                ),
            ])
        else:
            children.extend([
                Div("3D project", ui_style="font-size:13px; font-weight:650;"),
                Div(f"{len(self.project['geometries'])} geometry models", ui_class="cb-muted"),
                Div(f"{len(self.project['wire_trace_sets'])} wire trace sets", ui_class="cb-muted"),
                Div(
                    "Select Geometry or Wire traces in the model tree to add files.",
                    ui_class="cb-muted",
                    ui_style="font-size:12px; margin-top:6px;",
                ),
                Div(
                    "Assign material properties to the imported solids, add wire traces, then select the time-harmonic EM study to configure and run it.",
                    ui_style=(
                        "font-size:12px; line-height:1.5; margin-top:10px; padding:10px; "
                        "border:1px solid var(--border); border-radius:6px; "
                        "background:var(--panel-header); color:var(--fg-muted);"
                    ),
                ),
            ])
        self._properties.ui_children = children

    def select_item(self, kind, item_id=None):
        if kind == "geometry":
            geometry = next(
                (entry for entry in self.project.get("geometries", []) if entry.get("id") == item_id),
                None,
            )
            if geometry:
                self._ensure_selection_draft(geometry)
        elif kind == "named_selection":
            context = self._find_named_selection(item_id)
            if context:
                geometry, selection_kind, selection = context
                self._selection_draft = {
                    "geometry_id": geometry["id"],
                    "kind": selection_kind,
                    "selection_id": selection["id"],
                    "name": selection["name"],
                    "region_ids": list(selection.get(
                        "domain_ids" if selection_kind == "domain" else "boundary_ids", []
                    )),
                    "material": selection.get("material"),
                }
        self._selection = (kind, item_id)
        self._rebuild_items()
        self._rebuild_properties()
        if kind == "named_selection":
            self._rebuild_preview()

    def _on_preview_plotly_click(self, figure_index, curve_number, point_number=None):
        if figure_index != 0 or not self._preview._figures:
            return
        traces = self._preview._figures[0].data
        if not 0 <= curve_number < len(traces):
            return
        metadata = traces[curve_number].meta or {}
        kind = metadata.get("mmfem_kind")
        item_id = metadata.get("mmfem_item_id")
        if kind not in {"geometry", "trace_set"} or not item_id:
            return
        if (
            kind == "geometry"
            and metadata.get("mmfem_generated_mesh")
            and not metadata.get("mmfem_mesh_edges")
            and point_number is not None
        ):
            result = self._mesh_results.get(item_id, {})
            region_ids = result.get("boundary_region_ids", [])
            if 0 <= point_number < len(region_ids):
                context = (
                    self._find_named_selection(self._selection[1])
                    if self._selection[0] == "named_selection" else None
                )
                continue_editing = bool(
                    context
                    and context[0]["id"] == item_id
                    and context[1] == "boundary"
                )
                draft = self._selection_draft
                if not continue_editing and not (
                    draft.get("geometry_id") == item_id and draft.get("kind") == "boundary"
                    and self._selection == ("geometry", item_id)
                ):
                    self._selection_draft = {
                        "geometry_id": item_id,
                        "kind": "boundary",
                        "selection_id": None,
                        "name": "",
                        "region_ids": [],
                        "material": next(
                            (item.get("material", "Shield") for item in self.project.get("geometries", []) if item.get("id") == item_id),
                            "Shield",
                        ),
                    }
                region_id = int(region_ids[point_number])
                draft_ids = self._selection_draft["region_ids"]
                if region_id in draft_ids:
                    draft_ids.remove(region_id)
                else:
                    draft_ids.append(region_id)
                    draft_ids.sort()
                self._selection = (
                    ("named_selection", context[2]["id"])
                    if continue_editing else ("geometry", item_id)
                )
                self._rebuild_items()
                self._rebuild_properties()
                self._rebuild_preview()
                return
        exists = any(
            item.get("id") == item_id
            for item in self.project.get("geometries" if kind == "geometry" else "wire_trace_sets", [])
        )
        if exists:
            self.select_item(kind, item_id)

    def _style_preview_figure(self, figure):
        kind, item_id = self._selection
        trace_updates = []
        for trace in figure.data:
            metadata = trace.meta or {}
            selected = (
                metadata.get("mmfem_kind") == kind
                and metadata.get("mmfem_item_id") == item_id
            )
            base_color = metadata.get("mmfem_base_color", "#6886ac")
            if trace.type == "mesh3d":
                trace.opacity = 0.4 if self._transparent_shield else 1.0
                color = "#f59e0b" if selected else base_color
                trace.color = color
                generated_mesh = bool(metadata.get("mmfem_generated_mesh"))
                if generated_mesh:
                    trace.visible = self._mesh_surface_visible
                updates = {"color": color, "opacity": trace.opacity}
                if generated_mesh:
                    updates["visible"] = trace.visible
            else:
                if metadata.get("mmfem_mesh_edges"):
                    color = base_color
                    width = 2.5 if selected else 1
                    trace.visible = self._mesh_edges_visible
                elif metadata.get("mmfem_boundary_overlay"):
                    color = "#f59e0b"
                    width = 4
                    trace.visible = self._mesh_surface_visible
                else:
                    color = "#f59e0b" if selected else base_color
                    width = 8 if selected else 5
                trace.line.color = color
                trace.line.width = width
                updates = {"line.color": color, "line.width": width}
                if metadata.get("mmfem_mesh_edges") or metadata.get("mmfem_boundary_overlay"):
                    updates["visible"] = trace.visible
            trace_updates.append(updates)
        return trace_updates

    def _update_preview_selection(self):
        figures = getattr(self._preview, "_figures", ())
        plots = getattr(self._preview, "_plots", ())
        if not figures or not plots:
            return
        plot = plots[0]
        plotly = getattr(type(plot), "_plotly", None)
        target = getattr(plot, "_js_component", None)
        if plotly is None or target is None:
            return

        for index, updates in enumerate(self._style_preview_figure(figures[0])):
            try:
                plotly._call_method_ignore_return("restyle", [target, updates, [index]])
            except Exception:
                # The next figure draw will apply the updated trace properties.
                pass

    def _schedule_preview_selection_update(self):
        loop = getattr(self.app, "loop", None)
        if loop is not None and loop.is_running():
            loop.call_later(0.75, self._update_preview_selection)
        else:
            self._update_preview_selection()

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

    def _set_mesh_maxh(self, item, event):
        if self._mesh_worker_active:
            self._set_status("Wait for the current mesh generation to finish before changing its size.", error=True)
            return
        value = getattr(event, "value", event)
        try:
            maxh = float(value)
        except (TypeError, ValueError, OverflowError):
            self._set_status("Maximum element size must be a number in millimetres.", error=True)
            return
        if not math.isfinite(maxh) or not MIN_MESH_SIZE_MM <= maxh <= MAX_MESH_SIZE_MM:
            self._set_status(
                f"Maximum element size must be between {MIN_MESH_SIZE_MM:g} and {MAX_MESH_SIZE_MM:g} mm.",
                error=True,
            )
            return
        current = float(item.get("mesh_maxh_mm", 30.0))
        if math.isclose(maxh, current, rel_tol=1e-12, abs_tol=1e-12):
            return
        item["mesh_maxh_mm"] = maxh
        had_mesh = self._mesh_results.get(item["id"], {}).get("status") == "ready"
        self._mesh_results.pop(item["id"], None)
        self._mark_changed()
        self._rebuild_items()
        if had_mesh:
            self._rebuild_preview()
        if self._mesh_property_status is not None:
            self._mesh_property_status.ui_children = [
                self._mesh_property_message(item["id"])
            ]
        self._set_status("Mesh size updated. Generate a new mesh to apply it.")

    def generate_mesh(self, *_args):
        item = self._selected_geometry()
        item_id = item.get("id") if item else None
        if item is None:
            self._set_status("Select a geometry model before generating a mesh.", error=True)
            return False
        if self._mesh_worker_active:
            self._set_status("A 3D mesh is already being generated.", error=True)
            return False
        try:
            maxh = float(item.get("mesh_maxh_mm", 30.0))
            if not math.isfinite(maxh) or not MIN_MESH_SIZE_MM <= maxh <= MAX_MESH_SIZE_MM:
                raise ValueError(
                    f"Maximum element size must be between {MIN_MESH_SIZE_MM:g} "
                    f"and {MAX_MESH_SIZE_MM:g} mm."
                )
            step_data = bytes(self.assets[item["asset"]])
        except Exception as error:
            self._set_status(f"Could not prepare the mesh job: {error}", error=True)
            return False

        had_mesh = self._mesh_results.get(item_id, {}).get("status") == "ready"
        item["mesh_maxh_mm"] = maxh
        self._mark_changed()
        self._mesh_generation += 1
        generation = self._mesh_generation
        checksum = item.get("sha256")
        self._mesh_job = {
            "generation": generation,
            "geometry_id": item_id,
            "sha256": checksum,
            "maxh_mm": maxh,
        }
        self._mesh_worker_active = True
        self._mesh_results[item_id] = {"status": "generating", "maxh_mm": maxh}
        self._refresh_mesh_controls()
        self._rebuild_items()
        self._rebuild_properties()
        self._set_status(f"Generating a volume mesh for {item['name']} at max size {maxh:g} mm…")
        if had_mesh:
            self._rebuild_preview()
        threading.Thread(
            target=self._run_mesh_generation,
            args=(generation, item_id, checksum, maxh, step_data),
            name="MMFEM-3D-mesh-generation",
            daemon=True,
        ).start()
        return True

    def _run_mesh_generation(self, generation, item_id, checksum, maxh, step_data):
        try:
            mesh_result = generate_step_mesh(step_data, maxh_mm=maxh)
            error = None
        except Exception as exception:
            mesh_result = None
            error = str(exception).strip() or type(exception).__name__
        self._dispatch_mesh_result(
            generation, item_id, checksum, maxh, mesh_result, error
        )

    def _dispatch_mesh_result(
        self, generation, item_id, checksum, maxh, mesh_result, error
    ):
        loop = getattr(self.app, "loop", None)
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(
                    self._publish_mesh_result,
                    generation,
                    item_id,
                    checksum,
                    maxh,
                    mesh_result,
                    error,
                )
                return
            except RuntimeError:
                return
        self._publish_mesh_result(
            generation, item_id, checksum, maxh, mesh_result, error
        )

    def _publish_mesh_result(
        self, generation, item_id, checksum, maxh, mesh_result, error
    ):
        job = self._mesh_job
        if (
            generation != self._mesh_generation
            or job is None
            or job.get("generation") != generation
        ):
            self._mesh_worker_active = False
            self._refresh_mesh_controls()
            return
        item = next(
            (entry for entry in self.project.get("geometries", []) if entry.get("id") == item_id),
            None,
        )
        self._mesh_job = None
        self._mesh_worker_active = False
        if item is None or item.get("sha256") != checksum:
            self._refresh_mesh_controls()
            return
        if error is not None:
            self._mesh_results[item_id] = {
                "status": "error",
                "maxh_mm": maxh,
                "error": error,
            }
            self._set_status(f"Could not generate the mesh for {item['name']}: {error}", error=True)
        else:
            self._mesh_results[item_id] = {
                "status": "ready",
                "maxh_mm": maxh,
                "sha256": checksum,
                **mesh_result,
            }
            self._set_status(
                f"Mesh ready: {mesh_result['volume_elements']:,} volume elements and "
                f"{mesh_result['surface_faces']:,} boundary facets."
            )
        self._rebuild_items()
        self._rebuild_properties()
        self._refresh_mesh_controls()
        if error is None:
            self._rebuild_preview()

    def _set_trace_material(self, item, event):
        value = getattr(event, "value", event)
        value = str(value or "").strip()
        if not value:
            self._set_status("Material cannot be empty.", error=True)
            return
        if value not in {option["value"] for option in self._material_options(item.get("material"))}:
            self._set_status("Choose a material from the available library references.", error=True)
            return
        item["material"] = value
        for trace in item.get("traces", []):
            trace["material"] = value
        self._snapshot_material_reference(value)
        self._mark_changed()
        self._rebuild_items()

    def _set_material_reference(self, item, event):
        value = str(getattr(event, "value", event) or "").strip()
        if not value:
            self._set_status("Material reference cannot be empty.", error=True)
            return
        if value not in {option["value"] for option in self._material_options(item.get("material"))}:
            self._set_status("Choose a material from the available library references.", error=True)
            return
        item["material"] = value
        self._snapshot_material_reference(value)
        self._mark_changed()
        self._rebuild_items()

    def _set_named_selection_material(self, selection, event):
        value = str(getattr(event, "value", event) or "").strip()
        options = {option["value"] for option in self._material_options(value)}
        options.add("__inherit__")
        if value not in options:
            self._set_status("Choose a material from the available library references.", error=True)
            return False
        if value == "__inherit__":
            selection.pop("material", None)
            material = None
        else:
            selection["material"] = value
            material = value
            self._snapshot_material_reference(value)
        self._selection_draft["material"] = material
        self._mark_changed()
        self._rebuild_items()
        self._rebuild_properties()
        return True

    def _set_named_selection_name(self, item, event):
        value = str(getattr(event, "value", event) or "").strip()
        if not value:
            self._set_status("Selection name cannot be empty.", error=True)
            return
        item["name"] = value
        self._selection_draft["name"] = value
        self._mark_changed()
        self._rebuild_items()

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
        if hasattr(self, "_view_select"):
            self._view_select.ui_model_value = view
        self._update_preview_view(camera=True)
        return True

    def toggle_axes(self, *_args):
        self._axes_visible = not self._axes_visible
        self._refresh_axes_button()
        self._update_preview_view()
        return self._axes_visible

    def _refresh_axes_button(self):
        button = getattr(self, "_axes_button", None)
        if button is not None:
            button.ui_color = "primary" if self._axes_visible else None
            button.ui_flat = not self._axes_visible

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
            self._tree_reopen_tab.ui_hidden = False
        else:
            self._tree_splitter.ui_limits = [180, 380]
            self._tree_splitter.ui_model_value = self._tree_width
            self._tree_reopen_tab.ui_hidden = True

    def toggle_properties(self):
        if self._inner_splitter.ui_model_value:
            self._property_width = self._inner_splitter.ui_model_value
            self._inner_splitter.ui_limits = [0, 0]
            self._inner_splitter.ui_model_value = 0
            self._properties_reopen_tab.ui_hidden = False
        else:
            self._inner_splitter.ui_limits = [220, 480]
            self._inner_splitter.ui_model_value = self._property_width
            self._properties_reopen_tab.ui_hidden = True

    def _on_tree_width_change(self, event):
        value = getattr(event, "value", event)
        if value:
            self._tree_width = int(value)

    def _on_property_width_change(self, event):
        value = getattr(event, "value", event)
        if value:
            self._property_width = int(value)

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
        self._preview.ui_hidden = True
        self._preview_empty_state.ui_hidden = False
        self._preview_empty_title.ui_children = ["No 3D assets yet"]
        self._preview_empty_detail.ui_children = [
            "Import a STEP model or wire traces to preview the project."
        ]

    def _rebuild_preview(self):
        project = copy.deepcopy(self.project)
        assets = dict(self.assets)
        meshes = {
            item_id: result
            for item_id, result in self._mesh_results.items()
            if result.get("status") == "ready"
        }
        highlighted_regions = {
            item["id"]: list(self._active_boundary_region_ids(item["id"]))
            for item in project.get("geometries", [])
        }
        if not project["geometries"] and not project["wire_trace_sets"]:
            with self._preview_lock:
                self._preview_generation += 1
                self._preview_pending = None
            self._show_empty_preview()
            return
        if not self._preview._figures:
            self._preview.ui_hidden = True
            self._preview_empty_state.ui_hidden = False
            self._preview_empty_title.ui_children = ["Preparing the 3D preview…"]
            self._preview_empty_detail.ui_children = [
                "Imported geometry and wire-trace data are being processed."
            ]
        with self._preview_lock:
            self._preview_generation += 1
            generation = self._preview_generation
            self._preview_pending = (generation, project, assets, meshes, highlighted_regions)
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

    def _build_preview_figure(self, project, assets, meshes=None, highlighted_regions=None):
        from .three_d_preview import _sample_trace_points, preview_figure, tessellate_step
        from .three_d_project import parse_wire_trace

        meshes = meshes or {}
        highlighted_regions = highlighted_regions or {}
        geometry_payload = []
        for item in project.get("geometries", []):
            data = assets[item["asset"]]
            generated_mesh = meshes.get(item["id"])
            if generated_mesh and generated_mesh.get("sha256") == item.get("sha256"):
                import numpy as np

                vertices = generated_mesh["vertices_mm"] * step_unit_scale(
                    data, units=item.get("units", "m")
                )
                geometry_payload.append({
                    "id": item["id"],
                    "name": item["name"],
                    "material": item.get("material", "Shield"),
                    "vertices": vertices.astype(np.float32, copy=False),
                    "triangles": generated_mesh["triangles"],
                    "edges": generated_mesh.get("edges"),
                    "generated_mesh": True,
                    "boundary_region_ids": generated_mesh.get("boundary_region_ids"),
                    "highlighted_boundary_ids": highlighted_regions.get(item["id"], []),
                })
                continue
            key = ("geometry", item.get("sha256"), item.get("units", "m"))
            mesh = self._get_preview_item(
                key,
                lambda source=data, units=item.get("units", "m"): tessellate_step(source, units=units),
            )
            geometry_payload.append({
                "id": item["id"],
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
                    "trace_set_id": trace_set["id"],
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
            generation, project, assets, meshes, highlighted_regions = job
            try:
                figure = self._build_preview_figure(
                    project, assets, meshes, highlighted_regions
                )
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
            self._preview.ui_hidden = True
            self._preview_empty_state.ui_hidden = False
            self._preview_empty_title.ui_children = ["Preview could not be built"]
            self._preview_empty_detail.ui_children = [str(error)]
            self._set_status(f"Could not build the 3D preview: {error}", error=True)
            return
        # Apply the latest toggles even if they changed while OCC was working.
        figure.update_layout(**{f"scene_{axis}axis_visible": self._axes_visible for axis in "xyz"})
        for trace in figure.data:
            if trace.type == "mesh3d":
                trace.opacity = 0.4 if self._transparent_shield else 1.0
        self._style_preview_figure(figure)
        self._preview_empty_state.ui_hidden = True
        self._preview.ui_hidden = False
        self._preview.update_figure(figure)
        self._schedule_preview_selection_update()
        if self._mesh_job is not None or any(
            result.get("status") == "error"
            for result in self._mesh_results.values()
        ):
            return
        if any(result.get("status") == "ready" for result in self._mesh_results.values()):
            self._set_status("Volume mesh preview ready. Mesh data remains in memory for this session.")
        else:
            self._set_status("Preview ready. Original STEP and wire trace files remain in the project.")


# Compatibility name retained for existing integrations importing the old class.
ThreeDProjectDialog = ThreeDWorkspace
