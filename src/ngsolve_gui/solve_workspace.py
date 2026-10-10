"""Axisymmetric model authoring workspace."""

from __future__ import annotations

import copy
import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from threading import Lock, Timer, get_ident
from pathlib import Path

from ngapp.components import Component, Div, QBtn, QCheckbox, QDialog, QInput, QSelect, QSeparator, QTooltip, QCard, QCardSection, QSplitter
from ngapp.utils import EnvironmentType, get_environment

from . import cerbsim_style as cb
from .axisymmetric_model import builtin_materials, evaluate_expression, new_id, new_model, new_studies, validate_model, validate_studies


_SECTIONS = [
    ("geometry", "Geometry", "mdi-vector-square"),
    ("parameters", "Parameters", "mdi-variable"),
    ("materials", "Materials", "mdi-cube-scan"),
    ("physics", "Physics & coupling", "mdi-magnet"),
    ("boundaries", "Boundary conditions", "mdi-vector-link"),
    ("sources", "Sources & loads", "mdi-flash-outline"),
    ("mesh", "Mesh", "mdi-vector-triangle"),
    ("solver", "Solver settings", "mdi-tune-variant"),
    ("studies", "Study", "mdi-play-box-multiple-outline"),
    ("runs", "Run history", "mdi-history"),
]

_TREE_GROUPS = (
    ("MODEL", ("geometry", "parameters", "materials", "physics", "boundaries", "sources")),
    ("ANALYSIS", ("mesh", "studies", "solver")),
    ("RESULTS", ("runs",)),
)

_MATERIAL_COLORS = ["#6886ac", "#d58651", "#69a77b", "#ae8bb7", "#d1b44f", "#4cabb0"]


def _round_sketch_mm(value):
    """Round a mouse-derived sketch value to hundredths of a millimetre."""
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _input(label, value, callback, *, number=False, suffix=None, width=None, hint=None):
    widget = QInput(
        ui_label=label,
        ui_model_value=value,
        ui_type="number" if number else "text",
        ui_debounce=250,
        ui_suffix=suffix,
        ui_hint=hint,
        ui_dense=True,
        ui_filled=True,
        ui_hide_bottom_space=True,
        ui_style=f"width:{width};" if width else "width:100%;",
    )
    widget.on_update_model_value(callback)
    return widget


def _button(label, icon, callback, *, color=None, disable=False, tooltip=None, style=None, align=None):
    button = QBtn(
        QTooltip(tooltip or label),
        ui_label=label,
        ui_icon=icon,
        ui_dense=True,
        ui_no_caps=True,
        ui_flat=color is None,
        ui_color=color,
        ui_disable=disable,
        ui_class="q-px-sm",
        ui_style=style,
        ui_align=align,
    )
    if callback:
        button.on_click(callback)
    return button


def _svg(tag, **props):
    """Create a small standard SVG element through ngapp's generic component."""
    children = props.pop("children", None)
    component = Component(tag)
    component._props.update({key.replace("_", "-"): value for key, value in props.items() if value is not None})
    if children is not None:
        if tag == "text":
            # Generic ngapp slots wrap strings in HTML divs, which are invalid
            # children of SVG <text>. Vue maps textContent to the native DOM
            # property, keeping the text inside the SVG namespace.
            component._props["textContent"] = str(children)
        else:
            component.ui_slots["default"] = [children]
    return component


def _section_title(title, subtitle=None):
    return Div(
        Div(title, ui_style="font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;"),
        Div(subtitle, ui_style="font-size:11px; color:var(--fg-muted); margin-top:3px;") if subtitle else Div(),
        ui_style="padding:14px 14px 10px; border-bottom:1px solid var(--border);",
    )


class SolveWorkspace(Div):
    """Editable axisymmetric r-z model with a selectable sketch viewport."""

    def __init__(self, on_log=None, on_run=None, on_mesh=None, on_open_file=None):
        self.model = new_model()
        self.studies = new_studies()
        self.layout = {"schema_version": 1, "active_section": "geometry", "camera": "fit"}
        self.on_log = on_log
        self.on_run = on_run
        self.on_mesh = on_mesh
        self.on_open_file = on_open_file
        self.active_section = "geometry"
        self.selected_region_id = None
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self.message = "Choose Rectangle or Circle in the viewport toolbar to begin. Dimensions are entered in mm."
        self.message_is_error = False
        self._log_visible = False
        self._log_messages = ["Axisymmetric model editor ready."]
        self.runs = []
        self.selected_material_id = None
        self.selected_boundary_id = None
        self.selected_parameter_id = None
        self._selected_condition_id = None
        self._tree_panel_width = 240
        self._properties_panel_width = 340
        self.snap_to_grid = False
        self._view_center = None
        self._view_world_width = None
        self._inspector_view = None
        self._geometry_inspector_ready = False
        self._tree_entry_buttons = {}
        self._messages_visible = False
        self._status_flash_variant = 0
        self.validation_issues = []
        self.sketch_tool = "select"
        self._canvas_drag = None
        self._screen_to_svg = None
        self._canvas_view_commit_version = 0
        self._canvas_zoom_timer = None
        self._canvas_zoom_timer_lock = Lock()
        self._canvas_zoom_generation = 0
        self._preview_3d_active = False
        self._preview_3d_component = None

        self._canvas_background = _svg(
            "rect", x=0, y=0, width=900, height=640,
            fill="var(--canvas-bg, #f4f6f8)",
        )
        self._canvas_grid = Component("g")
        self._canvas_scene = Component("g")
        self._canvas_dimensions = Component("g")
        self._canvas_preview = Component(
            "g",
            _svg(
                "rect", id="solve-sketch-drag-rect", x=0, y=0, width=0, height=0,
                display="none", fill="#2385bd20", stroke="#0877b9",
                stroke_width="2", stroke_dasharray="6 4",
                style="pointer-events:none;",
            ),
            _svg(
                "circle", id="solve-sketch-drag-circle", cx=0, cy=0, r=0,
                display="none", fill="#2385bd20", stroke="#0877b9",
                stroke_width="2", stroke_dasharray="6 4",
                style="pointer-events:none;",
            ),
        )
        for component, element_id in (
            (self._canvas_grid, "solve-sketch-grid"),
            (self._canvas_scene, "solve-sketch-scene"),
            (self._canvas_dimensions, "solve-sketch-dimensions"),
            (self._canvas_preview, "solve-sketch-preview"),
        ):
            component._props["id"] = element_id

        self._section_buttons = {}
        for key, label, icon in _SECTIONS:
            button = _button(label, icon, lambda *_, selected=key: self.select_section(selected))
            button.ui_align = "left"
            button.ui_no_wrap = True
            button.ui_class = "full-width q-px-sm text-left"
            button.ui_style = "width:100%; justify-content:flex-start; text-align:left; min-height:34px;"
            self._section_buttons[key] = button

        self._left_items = Div(ui_style="display:flex; flex-direction:column; gap:12px; padding:8px;")
        self._canvas = Component(
            "svg",
            self._canvas_background,
            self._canvas_grid,
            self._canvas_scene,
            self._canvas_dimensions,
            self._canvas_preview,
            ui_class="solve-sketch-canvas",
            ui_style="display:block; width:100%; height:100%; min-height:0; background:var(--canvas-bg, #f4f6f8); cursor:default; user-select:none; -webkit-user-select:none; touch-action:none; overscroll-behavior:contain;",
        )
        self._canvas._props.update({
            "viewBox": "0 0 900 640",
            "preserveAspectRatio": "xMidYMid meet",
            "role": "img",
            "aria-label": "Axisymmetric radial-axial sketch",
        })
        self._canvas.on_mounted(self._install_canvas_pointer_capture)
        self._canvas.on("mouseup", self._on_canvas_mouse_up)
        self._canvas.on("wheel", self._on_canvas_wheel)
        self._inspector = Div(ui_style="display:flex; flex-direction:column; gap:12px; padding:12px; overflow:auto; min-height:0;")
        self._status = Div(ui_style="display:flex; align-items:center; gap:8px; min-width:0; overflow:hidden; white-space:nowrap;")
        self._log_body = Div(
            *[Div(line) for line in self._log_messages],
            ui_style="flex:1 1 auto; min-height:0; overflow:auto; padding:6px 14px 10px; font:12px/1.5 monospace;",
        )
        self._log_close_button = _button(
            "", "mdi-close", self.hide_log,
            tooltip="Minimise solver log",
            style="margin-left:auto;",
        )
        log_header = Div(
            Div("Log", ui_style="font-size:12px; font-weight:600;"),
            self._log_close_button,
            ui_style="display:flex; align-items:center; gap:8px; flex:none; min-height:34px; padding:0 8px 0 14px; border-bottom:1px solid var(--border);",
        )
        self._log_panel = Div(
            log_header,
            self._log_body,
            ui_hidden=True,
            ui_style="display:flex; flex-direction:column; flex:0 0 170px; min-height:0; overflow:hidden; border-top:1px solid var(--border); background:var(--surface);",
        )
        self._messages_panel = Div(ui_hidden=True, ui_style="max-height:180px; flex:none; overflow:auto; border-top:1px solid var(--border); background:var(--surface); padding:8px 14px;")
        self._canvas_host = Div(
            self._canvas,
            ui_class="relative-position",
            ui_style="position:relative; display:flex; flex:1 1 0%; height:0; flex-direction:column; min-width:0; min-height:0; overflow:hidden;",
        )
        tree_header = Div(
            Div("MODEL TREE", ui_style="font-size:11px; font-weight:700; letter-spacing:.08em;"),
            _button("", "mdi-chevron-left", self._toggle_tree_panel, tooltip="Collapse model tree", style="margin-left:auto;"),
            ui_style="display:flex; align-items:center; padding:14px 12px 10px; border-bottom:1px solid var(--border); color:var(--fg-muted);",
        )
        self._tree = Div(
            tree_header,
            self._left_items,
            ui_style="display:flex; flex-direction:column; min-width:0; min-height:0; overflow:auto; background:var(--surface);",
        )
        self._right_header = Div(
            Div("PROPERTIES", ui_style="font-size:11px; font-weight:700; letter-spacing:.08em; color:var(--fg-muted);"),
            _button("", "mdi-chevron-right", self._toggle_properties_panel, tooltip="Collapse properties", style="margin-left:auto;"),
            ui_style="display:flex; align-items:center; padding:14px 12px 10px; border-bottom:1px solid var(--border); flex:none;",
        )
        self._right = Div(
            self._right_header,
            self._inspector,
            ui_style="display:flex; flex-direction:column; min-width:0; min-height:0; overflow:hidden; background:var(--surface);",
        )

        select_button = _button(
            "Select", "mdi-cursor-default-outline",
            lambda *a: self.set_sketch_tool("select"),
            tooltip="Drag a region to move it; drag empty space to pan. Hold Shift and drag to select edges.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        rectangle_button = _button(
            "Rectangle", "mdi-rectangle-outline",
            lambda *a: self.set_sketch_tool("rectangle"),
            tooltip="Draw a rectangle by dragging between opposite corners.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        circle_button = _button(
            "Circle", "mdi-circle-outline",
            lambda *a: self.set_sketch_tool("circle"),
            tooltip="Draw a circle by dragging from its centre to its radius.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        snap_button = _button("Snap", "mdi-magnet", self.toggle_snap_to_grid, tooltip="Snap sketch points to grid intersections", style="flex:0 0 auto; white-space:nowrap;")
        sketch_toolbar_separator = QSeparator(ui_vertical=True)
        fit_view_button = _button(
            "Fit view", "mdi-fit-to-screen-outline",
            self.fit_canvas_view,
            tooltip="Fit the sketch to the viewport.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        zoom_out_button = _button("Zoom out", "mdi-minus", lambda *a: self.zoom_canvas(1.2), tooltip="Zoom out one step", style="flex:0 0 auto;")
        zoom_in_button = _button("Zoom in", "mdi-plus", lambda *a: self.zoom_canvas(1 / 1.2), tooltip="Zoom in one step", style="flex:0 0 auto;")
        self._preview_3d_button = _button(
            "Preview in 3D", "mdi-cube-scan", self.toggle_3d_preview,
            tooltip="Preview the axisymmetric regions revolved into 3D.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        self._preview_3d_tooltip = self._preview_3d_button.ui_slots["default"][0]
        model_button = _button("Model", "mdi-file-tree-outline", self._toggle_tree_panel, tooltip="Show or hide the model tree", style="flex:0 0 auto;")
        properties_button = _button("Properties", "mdi-tune-variant", self._toggle_properties_panel, tooltip="Show or hide properties", style="flex:0 0 auto;")
        toolbar = Div(
            select_button,
            rectangle_button,
            circle_button,
            snap_button,
            sketch_toolbar_separator,
            fit_view_button,
            zoom_out_button,
            zoom_in_button,
            self._preview_3d_button,
            model_button,
            properties_button,
            ui_style="display:flex; flex-wrap:wrap; align-items:center; gap:4px; flex:none; min-width:0; min-height:42px; padding:4px 8px; border-bottom:1px solid var(--border); background:var(--surface);",
        )
        self._tool_buttons = {
            "select": select_button,
            "rectangle": rectangle_button,
            "circle": circle_button,
        }
        self._snap_button = snap_button
        self._sketch_view_controls = [
            select_button, rectangle_button, circle_button, snap_button,
            sketch_toolbar_separator, fit_view_button, zoom_out_button,
            zoom_in_button,
        ]
        self._canvas_panel = Div(
            toolbar,
            self._canvas_host,
            ui_style="display:flex; flex:1 1 auto; flex-direction:column; width:100%; height:100%; min-width:0; min-height:0;",
        )

        self._rectangle_dialog = self._make_primitive_dialog("rectangle")
        self._circle_dialog = self._make_primitive_dialog("circle")
        self._model_title_editing = False
        self._model_title_draft = self.model.get("name", "Untitled axisymmetric model")
        self._model_title = QInput(
            ui_model_value=self._model_title_draft,
            ui_readonly=True,
            ui_dense=True,
            ui_borderless=True,
            ui_hide_bottom_space=True,
            ui_input_style="font-size:13px; font-weight:600; padding:0; min-width:180px; cursor:text;",
            ui_style="width:min(420px, 100%); min-height:30px;",
        )
        self._model_title._props["title"] = "Double-click to rename this model"
        self._model_title.on("dblclick", self._begin_model_rename)
        self._model_title.on_update_model_value(self._update_model_rename_draft)
        self._model_title.on_blur(self._commit_model_rename)
        self._model_title.on("keydown", self._on_model_rename_keydown)
        self._model_title_input = self._model_title
        self._validate_button = _button(
            "Check setup",
            "mdi-check-decagram-outline",
            self.validate_action,
            tooltip="Check model inputs and solver prerequisites. This does not generate a mesh or run the solver.",
        )
        self._mesh_button = _button(
            "Generate mesh",
            "mdi-vector-triangle",
            self.run_mesh_action,
            tooltip="Check the geometry and mesh settings, then build the finite-element mesh.",
        )
        self._run_button = _button(
            "Run study",
            "mdi-play",
            self.run_study_action,
            color="primary",
            tooltip="Check the full setup, then run the selected study.",
        )

        self._status_text = Div(self.message, ui_style="overflow:hidden; text-overflow:ellipsis; font-size:11px;")
        self._messages_button = QBtn(QTooltip("Show setup issues and validation results"), ui_icon="mdi-message-alert-outline", ui_label="Messages", ui_flat=True, ui_dense=True, ui_no_caps=True)
        self._messages_button.on_click(self.toggle_messages)
        log_button = QBtn(QTooltip("Show solver output"), ui_icon="mdi-console", ui_label="Log", ui_flat=True, ui_dense=True, ui_no_caps=True)
        log_button.on_click(self.toggle_log)
        self._bottom_bar = Div(
            self._status_text,
            Div(ui_style="flex:1;"),
            Div(f"Regions: 0  ·  Mesh order: {self.model['mesh']['polynomial_order']}", ui_style="font-size:11px; color:var(--fg-muted);"),
            self._messages_button,
            log_button,
            # ngapp's powered-by link is positioned over the root's lower
            # right corner, so leave it room rather than drawing controls
            # underneath it.
            ui_class="mmfem-solve-status-bar",
            ui_style="display:flex; align-items:center; gap:8px; flex:0 0 34px; min-height:34px; padding:0 225px 0 10px; border-top:1px solid var(--border); background:var(--surface);",
        )
        self._bottom_count = self._bottom_bar.ui_slots["default"][2]
        self._bottom_count.ui_style += " margin-left:12px;"

        top = Div(
            self._model_title,
            Div(ui_style="flex:1;"),
            self._validate_button,
            self._mesh_button,
            self._run_button,
            ui_style="display:flex; align-items:center; gap:8px; flex:none; min-height:40px; padding:4px 12px; border-bottom:1px solid var(--border); background:var(--surface);",
        )
        self._content = Div(
            ui_style="display:flex; flex:1 1 auto; min-height:0; min-width:0;",
        )
        self._properties_splitter = QSplitter(
            ui_model_value=self._properties_panel_width,
            ui_reverse=True,
            ui_unit="px",
            ui_limits=[240, 500],
            ui_emit_immediately=True,
            ui_slots={"before": [self._canvas_panel], "after": [self._right]},
            ui_class="solve-properties-splitter",
            ui_style="flex:1 1 auto; width:100%; min-width:0; min-height:0;",
        )
        self._properties_splitter.on_update_model_value(self._on_properties_width_change)
        self._tree_splitter = QSplitter(
            ui_model_value=self._tree_panel_width,
            ui_unit="px",
            ui_limits=[180, 420],
            ui_emit_immediately=True,
            ui_slots={"before": [self._tree], "after": [self._properties_splitter]},
            ui_class="solve-tree-splitter",
            ui_style="flex:1 1 auto; width:100%; min-width:0; min-height:0;",
        )
        self._tree_splitter.on_update_model_value(self._on_tree_width_change)
        self._content.ui_children = [self._tree_splitter]
        super().__init__(
            top,
            self._content,
            self._messages_panel,
            self._log_panel,
            self._bottom_bar,
            self._rectangle_dialog,
            self._circle_dialog,
            ui_style="display:flex; flex-direction:column; width:100%; height:100%; min-height:0; overflow:hidden; background:var(--app-bg, #f7f8fa); color:var(--fg, #202631);",
        )
        self._refresh_model_tree()
        self._render_inspector()
        self._refresh_sketch_tool_buttons()
        self.render_canvas()

    @property
    def dirty(self):
        return True

    def set_model(self, model, studies=None, layout=None):
        self._cancel_canvas_zoom_render()
        errors = validate_model(model)
        if errors:
            raise ValueError("Invalid model: " + " ".join(errors))
        studies = copy.deepcopy(studies or new_studies())
        parameter_map = {
            item.get("name"): item
            for item in model.get("parameters", [])
            if isinstance(item, dict) and item.get("name")
        }
        study_errors = validate_studies(studies, parameter_map)
        if study_errors:
            raise ValueError("Invalid studies: " + " ".join(study_errors))
        self._cancel_model_rename()
        self.model = copy.deepcopy(model)
        self.studies = studies
        self.layout = copy.deepcopy(layout or {"schema_version": 1, "active_section": "geometry", "camera": "fit"})
        self._load_canvas_view_from_layout()
        self.runs = []
        self.selected_material_id = None
        self.selected_boundary_id = None
        self.selected_parameter_id = None
        self._selected_condition_id = None
        requested_section = self.layout.get("active_section", "geometry")
        self.active_section = requested_section if requested_section in {key for key, _, _ in _SECTIONS} else "geometry"
        self.selected_region_id = None
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self._canvas_drag = None
        self._screen_to_svg = None
        self.set_sketch_tool("select", announce=False)
        self._message(f"Loaded {self.model.get('name', 'axisymmetric model')}.")
        self._refresh_model_tree()
        self._render_inspector()
        self.render_canvas()

    def select_section(self, section):
        if section not in dict((key, label) for key, label, _ in _SECTIONS):
            return
        self.active_section = section
        self.layout["active_section"] = section
        if section != "boundaries":
            self._selected_condition_id = None
        for key, button in self._section_buttons.items():
            button.ui_color = "primary" if key == section else None
            button.ui_flat = key != section
        self._render_inspector()
        self._update_model_tree_selection()
        if section == "boundaries":
            self._selected_condition_id = self.selected_boundary_id
        self._sync_canvas_selection()

    def _begin_model_rename(self, event=None):
        """Enable the inline model-name input on double-click."""
        if self._model_title_editing:
            return
        self._model_title_editing = True
        self._model_title_draft = str(self.model.get("name", "Untitled axisymmetric model"))
        self._model_title.ui_model_value = self._model_title_draft
        self._model_title.ui_readonly = False
        if get_environment().type == EnvironmentType.LOCAL_APP:
            self._model_title.ui_focus()
            self._model_title.ui_select()

    def _update_model_rename_draft(self, event):
        self._model_title_draft = str(getattr(event, "value", "") or "")

    def _on_model_rename_keydown(self, event):
        payload = getattr(event, "value", None)
        key = payload.get("key") if isinstance(payload, dict) else getattr(payload, "key", None)
        key = str(key or "").lower()
        if key in {"enter", "numpadenter"}:
            self._commit_model_rename()
        elif key == "escape":
            self._cancel_model_rename()

    def _commit_model_rename(self, event=None):
        if not self._model_title_editing:
            return
        name = str(self._model_title_draft or "").strip()
        if name:
            self.model["name"] = name
        self._finish_model_rename()

    def _cancel_model_rename(self, event=None):
        if not getattr(self, "_model_title_editing", False):
            return
        self._finish_model_rename()

    def _finish_model_rename(self):
        self._model_title_editing = False
        self._model_title_draft = str(self.model.get("name", "Untitled axisymmetric model"))
        if hasattr(self, "_model_title"):
            self._model_title.ui_model_value = self._model_title_draft
            self._model_title.ui_readonly = True

    def _tree_selected_entry(self):
        if self.active_section == "geometry" and self.selected_region_id:
            return f"geometry:{self.selected_region_id}"
        if self.active_section == "sources" and self.selected_region_id:
            return f"sources:{self.selected_region_id}"
        if self.active_section == "parameters" and self.selected_parameter_id:
            return f"parameters:{self.selected_parameter_id}"
        if self.active_section == "materials" and self.selected_material_id:
            return f"materials:{self.selected_material_id}"
        if self.active_section == "boundaries" and self.selected_boundary_id:
            return f"boundaries:{self.selected_boundary_id}"
        return None

    def _update_model_tree_selection(self):
        """Highlight the selected tree rows without replacing the tree slots."""
        for key, button in self._section_buttons.items():
            selected = key == self.active_section
            button.ui_color = "primary" if selected else None
            button.ui_flat = not selected
        selected_entry = self._tree_selected_entry()
        for key, button in self._tree_entry_buttons.items():
            selected = key == selected_entry
            button.ui_color = "primary" if selected else None
            button.ui_flat = not selected

    def _on_tree_width_change(self, event):
        value = float(event.value)
        if value > 30:
            self._tree_panel_width = value

    def _on_properties_width_change(self, event):
        value = float(event.value)
        if value > 30:
            self._properties_panel_width = value

    def _toggle_tree_panel(self, *_):
        if self._tree_splitter.ui_model_value > 30:
            self._tree_panel_width = float(self._tree_splitter.ui_model_value)
            self._tree_splitter.ui_limits = [0, 420]
            self._tree_splitter.ui_model_value = 0
        else:
            self._tree_splitter.ui_model_value = self._tree_panel_width
            self._tree_splitter.ui_limits = [180, 420]

    def _toggle_properties_panel(self, *_):
        if self._properties_splitter.ui_model_value > 30:
            self._properties_panel_width = float(self._properties_splitter.ui_model_value)
            self._properties_splitter.ui_limits = [0, 500]
            self._properties_splitter.ui_model_value = 0
        else:
            self._properties_splitter.ui_model_value = self._properties_panel_width
            self._properties_splitter.ui_limits = [240, 500]

    def set_sketch_tool(self, tool, *, announce=True):
        if tool not in {"select", "rectangle", "circle"}:
            return
        self.sketch_tool = tool
        self._canvas_drag = None
        self._screen_to_svg = None
        try:
            self.js.eval(
                "if (window.__ngsolveCancelSketchGesture) window.__ngsolveCancelSketchGesture(); "
                f"window.__ngsolveSketchTool = '{tool}'"
            )
        except Exception:
            # Standalone tests and non-browser frontends have no JS runtime.
            pass
        self._canvas.ui_style = (
            "display:block; width:100%; height:100%; min-height:0; "
            "background:var(--canvas-bg, #f4f6f8); touch-action:none; "
            "overscroll-behavior:contain; cursor:"
            + ("crosshair;" if tool != "select" else "default;")
            + " user-select:none; -webkit-user-select:none;"
        )
        self._refresh_sketch_tool_buttons()
        self._sync_canvas_interaction_settings()
        if announce:
            help_text = {
                "select": "Drag a region to move it; drag empty space to pan. Hold Shift and drag to select edges.",
                "rectangle": "Drag between opposite corners to sketch a rectangle; its dimensions remain editable.",
                "circle": "Drag from the circle centre to its radius; its position and radius remain editable.",
            }
            self._message(help_text[tool])

    def _refresh_sketch_tool_buttons(self):
        for tool, button in getattr(self, "_tool_buttons", {}).items():
            button.ui_color = "primary" if tool == self.sketch_tool else None
            button.ui_flat = tool != self.sketch_tool

    def _refresh_snap_button(self):
        button = getattr(self, "_snap_button", None)
        if button is not None:
            button.ui_color = "primary" if self.snap_to_grid else None
            button.ui_flat = not self.snap_to_grid

    def _sync_canvas_interaction_settings(self):
        try:
            project, _ = self._canvas_projection()
            origin = project((0.0, 0.0))
            plot = self._canvas_plot_bounds()
            world_center, world_width, _ = self._current_canvas_world_view()
            del world_center
            pixels_per_world = (plot[2] - plot[0]) / world_width
            grid = {
                "enabled": bool(self.snap_to_grid),
                "originX": origin[0],
                "originY": origin[1],
                "stepPx": self._canvas_grid_step() * pixels_per_world,
            }
            self.js.eval(f"window.__ngsolveSketchSnap = {json.dumps(grid, allow_nan=False)}")
            move_step = 0.00001 * pixels_per_world  # 0.01 mm in canvas units.
            self.js.eval(f"window.__ngsolveSketchMoveStep = {move_step!r}")
        except Exception:
            # Tests and non-browser frontends have no JavaScript runtime.
            pass

    def toggle_snap_to_grid(self, *args):
        self.snap_to_grid = not self.snap_to_grid
        self._refresh_snap_button()
        self._sync_canvas_interaction_settings()
        self._message(
            "Grid snapping is on; sketch points snap to visible grid intersections."
            if self.snap_to_grid else "Grid snapping is off."
        )

    def _load_canvas_view_from_layout(self):
        view = self.layout.get("view") if isinstance(self.layout, dict) else None
        try:
            center = (float(view["center_r"]), float(view["center_z"]))
            width = float(view["width"])
            if not all(math.isfinite(value) for value in (*center, width)) or width <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError, OverflowError):
            center, width = None, None
        self._view_center = center
        self._view_world_width = width

    def _save_canvas_view_to_layout(self):
        if self._view_center is None or self._view_world_width is None:
            self.layout.pop("view", None)
            return
        self.layout["view"] = {
            "center_r": self._view_center[0],
            "center_z": self._view_center[1],
            "width": self._view_world_width,
        }

    def fit_canvas_view(self, *args):
        self._cancel_canvas_zoom_render()
        self._view_center = None
        self._view_world_width = None
        self._save_canvas_view_to_layout()
        self.render_canvas()

    @staticmethod
    def _canvas_plot_bounds():
        return (72.0, 34.0, 854.0, 566.0)

    def _fit_canvas_world_view(self):
        plot = self._canvas_plot_bounds()
        target_ratio = (plot[2] - plot[0]) / (plot[3] - plot[1])
        regions = self.model.get("geometry", {}).get("regions", [])
        points = [point for region in regions for point in region.get("vertices", [])]
        if points:
            rmin = 0.0
            rmax = max(max(point[0] for point in points), 0.01)
            zmin = min(point[1] for point in points)
            zmax = max(point[1] for point in points)
            pad_r = max(rmax, 0.01) * 0.08
            # The meridian model is expected to be centred on z = 0. Keep
            # that plane at the middle of the default view even when the
            # current geometry only occupies positive or negative z.
            z_extent = max(abs(zmin), abs(zmax), 0.01)
            pad_z = z_extent * 0.08
            rmax += pad_r
            zmin = -(z_extent + pad_z)
            zmax = z_extent + pad_z
        else:
            rmin, rmax, zmin, zmax = 0.0, 0.024, -0.012, 0.012
        width = max(rmax - rmin, (zmax - zmin) * target_ratio, 1e-12)
        # Keep r = 0 on the left edge of the sketch grid while fitting the
        # region extents into the remaining positive-r area.
        center = (width / 2, 0.0)
        return center, width

    def _current_canvas_world_view(self):
        plot = self._canvas_plot_bounds()
        target_ratio = (plot[2] - plot[0]) / (plot[3] - plot[1])
        if self._view_center is None or self._view_world_width is None:
            center, width = self._fit_canvas_world_view()
        else:
            center, width = self._view_center, self._view_world_width
        return center, width, width / target_ratio

    def zoom_canvas(self, factor, anchor=None, *, render=True):
        """Zoom in-place, preserving the world coordinate beneath the anchor."""
        try:
            factor = float(factor)
        except (TypeError, ValueError):
            return
        if not math.isfinite(factor) or factor <= 0 or factor == 1:
            return
        plot = self._canvas_plot_bounds()
        if anchor is None:
            anchor = ((plot[0] + plot[2]) / 2, (plot[1] + plot[3]) / 2)
        project, unproject = self._canvas_projection()
        del project
        anchor_world = unproject(anchor)
        old_center, old_width, _ = self._current_canvas_world_view()
        new_width = min(1e300, max(1e-300, old_width * factor))
        new_height = new_width / ((plot[2] - plot[0]) / (plot[3] - plot[1]))
        fx = (anchor[0] - (plot[0] + plot[2]) / 2) / (plot[2] - plot[0])
        fy = (anchor[1] - (plot[1] + plot[3]) / 2) / (plot[3] - plot[1])
        center = (
            anchor_world[0] - fx * new_width,
            anchor_world[1] + fy * new_height,
        )
        if not all(math.isfinite(value) for value in (*center, new_width)):
            return
        self._view_center = center
        self._view_world_width = new_width
        self._save_canvas_view_to_layout()
        if render:
            self._cancel_canvas_zoom_render()
            self.render_canvas()

    def _schedule_canvas_zoom_render(self):
        """Rebuild grid labels once wheel input settles, not once per tick."""
        with self._canvas_zoom_timer_lock:
            self._canvas_zoom_generation += 1
            generation = self._canvas_zoom_generation
            if self._canvas_zoom_timer is not None:
                self._canvas_zoom_timer.cancel()
            timer = Timer(0.12, self._flush_canvas_zoom_render, args=(generation,))
            timer.daemon = True
            self._canvas_zoom_timer = timer
            timer.start()

    def _cancel_canvas_zoom_render(self):
        with self._canvas_zoom_timer_lock:
            self._canvas_zoom_generation += 1
            if self._canvas_zoom_timer is not None:
                self._canvas_zoom_timer.cancel()
                self._canvas_zoom_timer = None

    def _flush_canvas_zoom_render(self, generation):
        with self._canvas_zoom_timer_lock:
            if generation != self._canvas_zoom_generation:
                return
            self._canvas_zoom_timer = None
        self.render_canvas()

    def _pan_canvas_by_pixels(self, start, end):
        """Pan the meridian view by a drag measured in SVG canvas units."""
        dx, dy = end[0] - start[0], end[1] - start[1]
        if math.hypot(dx, dy) < 1e-9:
            return
        plot = self._canvas_plot_bounds()
        center, width, _ = self._current_canvas_world_view()
        pixels_per_world = (plot[2] - plot[0]) / width
        if not math.isfinite(pixels_per_world) or pixels_per_world <= 0:
            return
        self._view_center = (
            center[0] - dx / pixels_per_world,
            center[1] + dy / pixels_per_world,
        )
        self._view_world_width = width
        self._save_canvas_view_to_layout()
        self.render_canvas()

    def _read_browser_canvas_view(self):
        """Read the browser's coalesced view transform after a local gesture."""
        try:
            values = self.js.eval(
                "(() => window.__ngsolveSketchViewTransform || null)()"
            )
            if values is None or len(values) < 4:
                return None
            scale, tx, ty, version = (float(values[index]) for index in range(4))
            if not all(math.isfinite(value) for value in (scale, tx, ty, version)) or scale <= 0:
                return None
            return scale, tx, ty, int(version)
        except Exception:
            return None

    def _commit_browser_canvas_view(self, view=None, *, render=True):
        """Commit a browser-side pan/zoom matrix to the world-space viewport."""
        view = view or self._read_browser_canvas_view()
        if view is None:
            return False
        scale, tx, ty, version = view
        if version <= self._canvas_view_commit_version:
            return False
        self._canvas_view_commit_version = version
        if abs(scale - 1.0) < 1e-12 and abs(tx) < 1e-9 and abs(ty) < 1e-9:
            return False
        plot = self._canvas_plot_bounds()
        old_center, old_width, _ = self._current_canvas_world_view()
        _, unproject = self._canvas_projection()
        pixel_center = ((plot[0] + plot[2]) / 2, (plot[1] + plot[3]) / 2)
        center = unproject(((pixel_center[0] - tx) / scale, (pixel_center[1] - ty) / scale))
        width = old_width / scale
        if not all(math.isfinite(value) for value in (*center, width)) or width <= 0:
            return False
        self._view_center = center
        self._view_world_width = width
        self._save_canvas_view_to_layout()
        if render:
            self.render_canvas()
        return True

    def _on_canvas_wheel(self, event):
        """Commit one settled browser-side wheel gesture, never each tick."""
        if self._commit_browser_canvas_view(render=True):
            return
        # Retain the event-driven fallback for frontends without the local SVG
        # handler and for unit tests that send a native wheel event directly.
        value = getattr(event, "value", None)
        if not isinstance(value, dict):
            return
        try:
            delta = float(value.get("deltaY", 0.0) or 0.0)
        except (TypeError, ValueError):
            return
        if not math.isfinite(delta) or delta == 0:
            return
        delta = min(300.0, max(-300.0, delta))
        anchor = self._canvas_event_point(event, refresh_transform=True)
        factor = math.exp(delta * 0.0015)
        self.zoom_canvas(factor, anchor, render=False)
        self._schedule_canvas_zoom_render()

    def _read_screen_to_svg_transform(self):
        """Read the current SVG screen matrix once when a pointer gesture starts."""
        try:
            values = self.js.eval(
                "(() => { const svg = document.querySelector('svg.solve-sketch-canvas'); "
                "const m = svg && svg.getScreenCTM(); "
                "return m ? [m.a, m.b, m.c, m.d, m.e, m.f] : null; })()"
            )
            if values is None or len(values) != 6:
                return None
            return tuple(float(values[index]) for index in range(6))
        except Exception:
            return None

    def _install_canvas_pointer_capture(self, *_):
        """Keep sketch gestures alive through release and render previews locally.

        Pointer capture makes sketch gestures independent of which SVG child
        is beneath the pointer. Preview updates stay local; Python receives one
        compact event after release rather than the full stream of mouse moves.
        """
        script = r"""
(() => {
  window.__ngsolveSketchTool = '__SKETCH_TOOL__';
  if (window.__ngsolveSketchPointerCaptureVersion === 4) return;
  window.__ngsolveSketchPointerCaptureVersion = 4;
  let active = false;
  let activePointerId = null;
  let dragStart = null;
  let dragStartScreen = null;
  let dragBaseScale = 1;
  let dragBaseTranslateX = 0;
  let dragBaseTranslateY = 0;
  let dragRegionId = null;
  let dragMode = null;
  let gestureNumber = 0;
  let suppressCanvasClickUntil = 0;
  let previewFrame = 0;
  let pendingPreview = null;
  let wheelCommitTimer = 0;
  let visualScale = 1;
  let visualTranslateX = 0;
  let visualTranslateY = 0;
  let viewVersion = 0;
  const committedRegionOffsets = new Map();
  window.__ngsolveSketchViewTransform = [1, 0, 0, viewVersion];
  window.__ngsolveSketchCompletedGesture = null;
  const pointerEvents = window.__ngsolveSketchPointerEvents = [];
  const forwardedEvents = new WeakSet();
  const selector = 'svg.solve-sketch-canvas';
  const groupIds = [
    'solve-sketch-grid', 'solve-sketch-scene',
    'solve-sketch-dimensions', 'solve-sketch-preview',
  ];
  const pointerSupported = typeof window.PointerEvent === 'function';
  const currentSvg = () => document.querySelector(selector);
  const targetIsSvg = (event, svg) => Boolean(
    svg && event.target && event.target.closest
      && event.target.closest(selector) === svg
  );
  const applyCanvasTransform = (scale, tx, ty, svg = currentSvg()) => {
    if (!svg) return;
    visualScale = scale;
    visualTranslateX = tx;
    visualTranslateY = ty;
    viewVersion += 1;
    window.__ngsolveSketchViewTransform = [scale, tx, ty, viewVersion];
    const transform = `matrix(${scale} 0 0 ${scale} ${tx} ${ty})`;
    for (const id of groupIds) {
      const group = svg.querySelector('#' + id);
      if (group) group.setAttribute('transform', transform);
    }
  };
  const resetCanvasTransform = (svg = currentSvg()) => {
    if (!svg) return;
    visualScale = 1;
    visualTranslateX = 0;
    visualTranslateY = 0;
    window.__ngsolveSketchViewTransform = [1, 0, 0, viewVersion];
    for (const id of groupIds) {
      const group = svg.querySelector('#' + id);
      if (group) group.removeAttribute('transform');
    }
    for (const regionId of committedRegionOffsets.keys()) {
      for (const shape of svg.querySelectorAll('[data-sketch-region-id]')) {
        if (shape.getAttribute('data-sketch-region-id') === regionId) {
          shape.removeAttribute('transform');
        }
      }
    }
    committedRegionOffsets.clear();
  };
  const remember = (event) => {
    window.__ngsolveSketchPointer = [event.clientX, event.clientY];
    const targetRegion = event.target && event.target.closest
      ? (event.target.closest('[data-sketch-region-id]') || {}).getAttribute('data-sketch-region-id')
      : null;
    const fallbackRegion = event.type === 'mousedown'
      && window.__ngsolveSketchRegionForNextMouseDownKnown
      ? window.__ngsolveSketchRegionForNextMouseDown
      : null;
    const regionKnown = event.type === 'mousedown'
      ? Boolean(window.__ngsolveSketchRegionForNextMouseDownKnown)
      : targetIsSvg(event, currentSvg());
    pointerEvents.push({
      type: event.type,
      timeStamp: event.timeStamp,
      x: event.clientX,
      y: event.clientY,
      regionId: targetRegion || fallbackRegion || null,
      regionKnown,
    });
    if (event.type === 'mousedown') {
      window.__ngsolveSketchRegionForNextMouseDown = null;
      window.__ngsolveSketchRegionForNextMouseDownKnown = false;
    }
    if (pointerEvents.length > 256) pointerEvents.splice(0, pointerEvents.length - 256);
  };
  const svgPoint = (svg, event) => {
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    const matrix = svg.getScreenCTM();
    if (!matrix) return null;
    const mapped = point.matrixTransform(matrix.inverse());
    return [mapped.x, mapped.y];
  };
  const setRegionOffset = (svg, regionId, offset) => {
    if (!svg || !regionId) return;
    for (const shape of svg.querySelectorAll('[data-sketch-region-id]')) {
      if (shape.getAttribute('data-sketch-region-id') !== regionId) continue;
      if (offset && (offset[0] !== 0 || offset[1] !== 0)) {
        shape.setAttribute('transform', `translate(${offset[0]} ${offset[1]})`);
      } else {
        shape.removeAttribute('transform');
      }
    }
  };
  const clearPreview = (svg = currentSvg(), resetView = true, preserveCommitted = false) => {
    if (!svg) return;
    for (const id of ['solve-sketch-drag-rect', 'solve-sketch-drag-circle']) {
      const shape = svg.querySelector('#' + id);
      if (shape) shape.setAttribute('display', 'none');
    }
    if (dragRegionId) {
      setRegionOffset(
        svg,
        dragRegionId,
        preserveCommitted ? committedRegionOffsets.get(dragRegionId) || null : null,
      );
    }
    window.__ngsolveSketchLastRegionId = null;
    if (resetView) resetCanvasTransform(svg);
    dragStart = null;
    dragStartScreen = null;
    dragRegionId = null;
    dragMode = null;
  };
  const snapPoint = (point) => {
    const grid = window.__ngsolveSketchSnap;
    if (!point || !grid || !grid.enabled || !Number.isFinite(grid.stepPx) || grid.stepPx <= 0) return point;
    const snappedX = grid.originX + Math.round((point[0] - grid.originX) / grid.stepPx) * grid.stepPx;
    const snappedY = grid.originY + Math.round((point[1] - grid.originY) / grid.stepPx) * grid.stepPx;
    return [Math.max(grid.originX, snappedX), snappedY];
  };
  const sketchPoint = (point) =>
    (window.__ngsolveSketchTool || 'select') === 'select' ? point : snapPoint(point);
  const toScenePoint = (point) => [
    (point[0] - visualTranslateX) / visualScale,
    (point[1] - visualTranslateY) / visualScale,
  ];
  const toViewPoint = (point) => [
    visualScale * point[0] + visualTranslateX,
    visualScale * point[1] + visualTranslateY,
  ];
  const updatePreview = (svg, point, screenPoint = null) => {
    if (!active || !dragStart || !point) return;
    if (dragMode === 'region' && dragRegionId) {
      const grid = window.__ngsolveSketchSnap;
      const start = grid && grid.enabled ? snapPoint(dragStart) : dragStart;
      const end = grid && grid.enabled ? snapPoint(point) : point;
      const step = Math.max(1e-12, Number(window.__ngsolveSketchMoveStep) || 0);
      const quantize = (value) => Math.sign(value) * Math.round(Math.abs(value) / step) * step;
      const dx = grid && grid.enabled ? end[0] - start[0] : quantize(end[0] - start[0]);
      const dy = grid && grid.enabled ? end[1] - start[1] : quantize(end[1] - start[1]);
      const baseOffset = committedRegionOffsets.get(dragRegionId) || [0, 0];
      for (const shape of svg.querySelectorAll('[data-sketch-region-id]')) {
        if (shape.getAttribute('data-sketch-region-id') === dragRegionId) {
          const totalX = baseOffset[0] + dx;
          const totalY = baseOffset[1] + dy;
          if (totalX === 0 && totalY === 0) shape.removeAttribute('transform');
          else shape.setAttribute('transform', `translate(${totalX} ${totalY})`);
        }
      }
      return;
    }
    if (dragMode === 'pan') {
      const screenStart = dragStartScreen || toViewPoint(dragStart);
      const screenEnd = screenPoint || toViewPoint(point);
      applyCanvasTransform(
        dragBaseScale,
        dragBaseTranslateX + screenEnd[0] - screenStart[0],
        dragBaseTranslateY + screenEnd[1] - screenStart[1],
        svg,
      );
      svg.style.cursor = 'grabbing';
      return;
    }
    if (dragMode !== 'marquee') point = sketchPoint(point);
    const dx = point[0] - dragStart[0];
    const dy = point[1] - dragStart[1];
    const tool = window.__ngsolveSketchTool || 'select';
    const isSelection = dragMode === 'marquee';
    const rect = svg.querySelector('#solve-sketch-drag-rect');
    const circle = svg.querySelector('#solve-sketch-drag-circle');
    if (Math.hypot(dx, dy) < 4) {
      if (rect) rect.setAttribute('display', 'none');
      if (circle) circle.setAttribute('display', 'none');
      return;
    }
    if (tool === 'circle') {
      if (rect) rect.setAttribute('display', 'none');
      if (!circle) return;
      circle.setAttribute('display', 'inline');
      circle.setAttribute('cx', dragStart[0]);
      circle.setAttribute('cy', dragStart[1]);
      circle.setAttribute('r', Math.hypot(dx, dy));
      return;
    }
    if (circle) circle.setAttribute('display', 'none');
    if (!rect) return;
    const windowSelection = point[0] >= dragStart[0];
    const fill = isSelection
      ? (windowSelection ? '#2385bd22' : '#dd8a3222')
      : '#2385bd20';
    const stroke = isSelection
      ? (windowSelection ? '#2385bd' : '#c66f16')
      : '#0877b9';
    rect.setAttribute('display', 'inline');
    rect.setAttribute('x', Math.min(dragStart[0], point[0]));
    rect.setAttribute('y', Math.min(dragStart[1], point[1]));
    rect.setAttribute('width', Math.abs(dx));
    rect.setAttribute('height', Math.abs(dy));
    rect.setAttribute('fill', fill);
    rect.setAttribute('stroke', stroke);
    rect.setAttribute('stroke-width', isSelection ? '1.5' : '2');
  };
  const beginGesture = (event, svg) => {
    if (!targetIsSvg(event, svg) || event.button !== 0 || event.isPrimary === false) return;
    if (active) clearPreview(svg, false, true);
    window.__ngsolveSketchCompletedGesture = null;
    active = true;
    activePointerId = event.pointerId === undefined ? null : event.pointerId;
    gestureNumber += 1;
    clearPreview(svg, false, true);
    const screenStart = svgPoint(svg, event);
    dragStartScreen = screenStart;
    dragBaseScale = visualScale;
    dragBaseTranslateX = visualTranslateX;
    dragBaseTranslateY = visualTranslateY;
    dragStart = sketchPoint(toScenePoint(screenStart));
    const region = event.target && event.target.closest
      ? event.target.closest('[data-sketch-region-id]')
      : null;
    dragRegionId = (window.__ngsolveSketchTool || 'select') === 'select' && region
      ? region.getAttribute('data-sketch-region-id')
      : null;
    window.__ngsolveSketchLastRegionId = dragRegionId;
    const tool = window.__ngsolveSketchTool || 'select';
    dragMode = tool !== 'select'
      ? 'draw'
      : dragRegionId
        ? 'region'
        : (event.shiftKey || event.ctrlKey ? 'marquee' : 'pan');
    window.__ngsolveSketchGestureActive = true;
    if (dragMode === 'pan') svg.style.cursor = 'grabbing';
    if (tool === 'select') {
      window.__ngsolveSketchRegionForNextMouseDown = dragRegionId || '';
      window.__ngsolveSketchRegionForNextMouseDownKnown = true;
    }
    remember(event);
    if (activePointerId !== null && svg.setPointerCapture) {
      try { svg.setPointerCapture(activePointerId); } catch (_) { /* capture may already be ending */ }
    }
  };
  const moveGesture = (event, svg) => {
    if (!active || (activePointerId !== null && event.pointerId !== activePointerId)) return;
    pendingPreview = {event, svg};
    if (previewFrame) return;
    previewFrame = requestAnimationFrame(() => {
      previewFrame = 0;
      const pending = pendingPreview;
      pendingPreview = null;
      if (!active || !pending) return;
      const screenPoint = svgPoint(pending.svg, pending.event);
      if (screenPoint) updatePreview(pending.svg, toScenePoint(screenPoint), screenPoint);
    });
  };
  const finishGesture = (event, svg, cancelled = false) => {
    if (!active || (activePointerId !== null && event.pointerId !== undefined && event.pointerId !== activePointerId)) return;
    if (previewFrame) cancelAnimationFrame(previewFrame);
    previewFrame = 0;
    pendingPreview = null;
    const screenPoint = svgPoint(svg, event);
    const point = screenPoint ? toScenePoint(screenPoint) : null;
    if (!cancelled && point) updatePreview(svg, point, screenPoint);
    const moved = Boolean(!cancelled && dragStartScreen && screenPoint
      && Math.hypot(screenPoint[0] - dragStartScreen[0], screenPoint[1] - dragStartScreen[1]) >= 4);
    const finishedGesture = gestureNumber;
    const finishedRegionId = dragMode === 'region' ? dragRegionId : null;
    const preserveVisual = moved && ['draw', 'pan', 'region'].includes(dragMode);
    const shouldForward = active && !targetIsSvg(event, svg) && event.type !== 'pointercancel';
    if (!cancelled && dragStartScreen && screenPoint) {
      const operation = dragMode;
      const tool = window.__ngsolveSketchTool || 'select';
      window.__ngsolveSketchCompletedGesture = [
        operation, tool,
        dragStartScreen[0], dragStartScreen[1], screenPoint[0], screenPoint[1],
        dragRegionId || '', Boolean(event.shiftKey || event.ctrlKey), moved,
        visualScale, visualTranslateX, visualTranslateY, viewVersion,
      ];
      if (wheelCommitTimer) window.clearTimeout(wheelCommitTimer);
      wheelCommitTimer = 0;
    }
    if (moved) {
      // The browser's click follows mouseup. Suppress it after any drag so
      // the completed gesture cannot select a region beneath its endpoint.
      suppressCanvasClickUntil = performance.now() + 1000;
    }
    active = false;
    activePointerId = null;
    window.__ngsolveSketchGestureActive = false;
    if (svg) svg.style.cursor = (window.__ngsolveSketchTool || 'select') === 'select' ? 'default' : 'crosshair';
    if (svg) requestAnimationFrame(() => {
      if (!active) svg.style.cursor = (window.__ngsolveSketchTool || 'select') === 'select' ? 'default' : 'crosshair';
    });
    if (!preserveVisual) clearPreview(svg, false);
    else window.setTimeout(() => {
      if (gestureNumber === finishedGesture) {
        clearPreview(svg, false, Boolean(
          finishedRegionId && committedRegionOffsets.has(finishedRegionId)
        ));
      }
    }, 2500);
    if (shouldForward) {
      const forwarded = new MouseEvent('mouseup', {
        bubbles: true,
        button: event.button,
        clientX: event.clientX,
        clientY: event.clientY,
        shiftKey: event.shiftKey,
        ctrlKey: event.ctrlKey,
      });
      forwardedEvents.add(forwarded);
      remember(forwarded);
      svg.dispatchEvent(forwarded);
    }
  };
  const cancelGesture = (notifyPython = false) => {
    const svg = currentSvg();
    if (!svg) return;
    const wasActive = active;
    const pointerId = activePointerId;
    if (previewFrame) cancelAnimationFrame(previewFrame);
    previewFrame = 0;
    pendingPreview = null;
    active = false;
    activePointerId = null;
    window.__ngsolveSketchGestureActive = false;
    clearPreview(svg, false, true);
    svg.style.cursor = (window.__ngsolveSketchTool || 'select') === 'select' ? 'default' : 'crosshair';
    if (wasActive && notifyPython) {
      const cancelled = new PointerEvent('pointercancel', {
        bubbles: true, pointerId: pointerId === null ? 1 : pointerId,
        pointerType: 'mouse', button: 0,
        clientX: (window.__ngsolveSketchPointer || [0, 0])[0],
        clientY: (window.__ngsolveSketchPointer || [0, 0])[1],
      });
      svg.dispatchEvent(cancelled);
    }
  };
  window.__ngsolveCancelSketchGesture = () => cancelGesture(true);
  window.__ngsolveCommitSketchRegionMove = (regionId, dx, dy) => {
    if (!regionId || !Number.isFinite(dx) || !Number.isFinite(dy)) return;
    const previous = committedRegionOffsets.get(regionId) || [0, 0];
    const offset = [previous[0] + dx, previous[1] + dy];
    committedRegionOffsets.set(regionId, offset);
    const svg = currentSvg();
    setRegionOffset(svg, regionId, offset);
    const dimensions = svg && svg.querySelector('#solve-sketch-dimensions');
    const selected = window.__ngsolveSketchSelectedRegionId;
    if (dimensions && selected === regionId) {
      if (offset[0] || offset[1]) dimensions.setAttribute('transform', `translate(${offset[0]} ${offset[1]})`);
      else dimensions.removeAttribute('transform');
    }
  };
  window.__ngsolveSyncSketchDimensionOffset = (regionId) => {
    const svg = currentSvg();
    const dimensions = svg && svg.querySelector('#solve-sketch-dimensions');
    if (!dimensions) return;
    window.__ngsolveSketchSelectedRegionId = regionId || null;
    const offset = regionId ? committedRegionOffsets.get(regionId) : null;
    if (offset && (offset[0] || offset[1])) dimensions.setAttribute('transform', `translate(${offset[0]} ${offset[1]})`);
    else dimensions.removeAttribute('transform');
  };
  window.__ngsolveRollbackSketchRegionMove = (regionId) => {
    const svg = currentSvg();
    setRegionOffset(svg, regionId, committedRegionOffsets.get(regionId) || null);
    const dimensions = svg && svg.querySelector('#solve-sketch-dimensions');
    if (dimensions && window.__ngsolveSketchSelectedRegionId === regionId) {
      const offset = committedRegionOffsets.get(regionId);
      if (offset && (offset[0] || offset[1])) dimensions.setAttribute('transform', `translate(${offset[0]} ${offset[1]})`);
      else dimensions.removeAttribute('transform');
    }
  };
  window.__ngsolveResetSketchPreview = () => {
    if (active) return;
    clearPreview(currentSvg());
  };
  const handleWheel = (event, svg) => {
    if (!targetIsSvg(event, svg)) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (active) {
      return;
    }
    const delta = Number(event.deltaY) || 0;
    if (!Number.isFinite(delta) || delta === 0) return;
    const point = svgPoint(svg, event);
    if (!point) return;
    const clampedDelta = Math.max(-300, Math.min(300, delta));
    const factor = Math.exp(-clampedDelta * 0.0015);
    const tx = point[0] + factor * (visualTranslateX - point[0]);
    const ty = point[1] + factor * (visualTranslateY - point[1]);
    applyCanvasTransform(visualScale * factor, tx, ty, svg);
    if (wheelCommitTimer) window.clearTimeout(wheelCommitTimer);
    wheelCommitTimer = window.setTimeout(() => {
      wheelCommitTimer = 0;
      if (active) return;
      const target = currentSvg();
      if (!target) return;
      const settled = new WheelEvent('wheel', {bubbles: true, cancelable: true, deltaY: 0});
      forwardedEvents.add(settled);
      target.dispatchEvent(settled);
    }, 140);
  };
  const capture = (event) => {
    if (forwardedEvents.has(event)) return;
    const svg = currentSvg();
    if (!svg) return;
    const targetIsCanvas = targetIsSvg(event, svg);
    if (event.type === 'wheel') {
      handleWheel(event, svg);
      return;
    }
    if (event.type === 'click') {
      if (targetIsCanvas && performance.now() < suppressCanvasClickUntil) {
        suppressCanvasClickUntil = 0;
        event.preventDefault();
        event.stopImmediatePropagation();
      }
      return;
    }
    if (pointerSupported) {
      if (event.type === 'mousedown' && targetIsCanvas && event.button === 0) remember(event);
      else if (event.type === 'mouseup' && (targetIsCanvas || active)) remember(event);
      return;
    }
    if (event.type === 'mousedown') {
      beginGesture(event, svg);
      return;
    }
    if (!active) return;
    if (event.type === 'mousemove') moveGesture(event, svg);
    if (event.type === 'mouseup') finishGesture(event, svg);
  };
  if (pointerSupported) {
    document.addEventListener('pointerdown', event => beginGesture(event, currentSvg()), true);
    document.addEventListener('pointermove', event => moveGesture(event, currentSvg()), true);
    document.addEventListener('pointerup', event => finishGesture(event, currentSvg()), true);
    document.addEventListener('pointercancel', event => finishGesture(event, currentSvg(), true), true);
    document.addEventListener('lostpointercapture', event => {
      if (active && event.pointerId === activePointerId) cancelGesture(true);
    }, true);
  }
  window.addEventListener('blur', () => cancelGesture(true));
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) cancelGesture(true);
  });
  document.addEventListener('mousedown', capture, true);
  document.addEventListener('mousemove', capture, true);
  document.addEventListener('mouseup', capture, true);
  document.addEventListener('click', capture, true);
  document.addEventListener('wheel', capture, {capture:true, passive:false});
})()
"""
        try:
            self.js.eval(script.replace("__SKETCH_TOOL__", self.sketch_tool))
        except Exception:
            # Tests and non-browser frontends may not expose a JS runtime.
            return

    def _read_canvas_pointer(self, value):
        try:
            event_type = value.get("type")
            timestamp = value.get("timeStamp")
            if event_type in {"mousedown", "mousemove", "mouseup"} and timestamp is not None:
                values = self.js.eval(
                    "((type, timestamp) => { "
                    "const events = window.__ngsolveSketchPointerEvents || []; "
                    "const index = events.findIndex(event => event.type === type "
                    "&& Math.abs(event.timeStamp - timestamp) < 1); "
                    "if (index >= 0) { const event = events.splice(index, 1)[0]; "
                    "return [event.x, event.y]; } "
                    "return window.__ngsolveSketchPointer || null; })"
                    f"('{event_type}', {float(timestamp)!r})"
                )
            else:
                values = self.js.eval(
                    "(() => window.__ngsolveSketchPointer || null)()"
                )
            if values is None or len(values) != 2:
                return None
            return float(values[0]), float(values[1])
        except Exception:
            return None

    def _read_canvas_region_id(self, value):
        """Return the sketch region hit by a native pointer event, if any."""
        if not isinstance(value, dict):
            return None
        event_type = value.get("type")
        timestamp = value.get("timeStamp")
        if event_type not in {None, "mousedown"}:
            return value.get("region_id") or value.get("regionId")
        try:
            timestamp_js = "null" if timestamp is None else repr(float(timestamp))
            region_id = self.js.eval(
                "((timestamp) => { "
                "const events = window.__ngsolveSketchPointerEvents || []; "
                "const event = (timestamp === null ? null : events.find(item => item.type === 'mousedown' "
                "&& Math.abs(item.timeStamp - timestamp) < 1)) "
                "|| [...events].reverse().find(item => item.type === 'mousedown'); "
                "return event ? (event.regionKnown ? (event.regionId || '') : null) : null; })"
                f"({timestamp_js})"
            )
            return str(region_id) if region_id is not None else None
        except Exception:
            return value.get("region_id") or value.get("regionId")

    def _canvas_event_point(self, event, *, refresh_transform=False):
        value = getattr(event, "value", None)
        if not isinstance(value, dict):
            return None
        try:
            coordinates = (value.get("x"), value.get("y"))
            if any(coordinate is None for coordinate in coordinates):
                coordinates = self._read_canvas_pointer(value)
            if coordinates is None:
                return None
            screen_x, screen_y = (float(coordinate) for coordinate in coordinates)
            if refresh_transform or self._screen_to_svg is None:
                self._screen_to_svg = self._read_screen_to_svg_transform()
            if self._screen_to_svg is None:
                return None
            a, b, c, d, e, f = self._screen_to_svg
            determinant = a * d - b * c
            if abs(determinant) <= 1e-15:
                return None
            dx, dy = screen_x - e, screen_y - f
            return (
                (d * dx - c * dy) / determinant,
                (-b * dx + a * dy) / determinant,
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None

    def _on_canvas_mouse_down(self, event):
        value = getattr(event, "value", None)
        if not isinstance(value, dict) or int(value.get("button", 0) or 0) != 0:
            return
        tool = self.sketch_tool
        # Resolve the hit target before _canvas_event_point consumes the
        # matching browser pointer record to obtain clientX/clientY.
        region_id = self._read_canvas_region_id(value) if tool == "select" else None
        browser_hit_is_known = region_id == ""
        if browser_hit_is_known:
            region_id = None
        point = self._canvas_event_point(event, refresh_transform=True)
        if point is None:
            return
        if tool != "select":
            point = self._snap_canvas_point(point)
        elif not region_id and not browser_hit_is_known:
            region_id = self._region_at_canvas_point(point)
        if tool != "select":
            operation = "draw"
        elif region_id:
            operation = "region"
        elif value.get("shiftKey") or value.get("ctrlKey"):
            operation = "marquee"
        else:
            operation = "pan"
        self._canvas_drag = {
            "tool": tool,
            "operation": operation,
            "start": point,
            "current": point,
            "region_id": region_id,
            "additive": bool(value.get("shiftKey") or value.get("ctrlKey")),
            "moved": False,
        }

    def _region_at_canvas_point(self, point):
        """Hit-test a canvas position against regions, preferring the innermost."""
        _, unproject = self._canvas_projection()
        world_point = unproject(point)
        candidates = [
            (abs(_polygon_area(region.get("vertices", []))), region["id"])
            for region in self.model["geometry"].get("regions", [])
            if len(region.get("vertices", [])) >= 3
            and _point_in_or_on_polygon(world_point, region["vertices"])
        ]
        return min(candidates)[1] if candidates else None

    def _on_canvas_mouse_move(self, event):
        if self._canvas_drag is None:
            return
        point = self._canvas_event_point(event)
        if point is None:
            return
        if self._canvas_drag["tool"] != "select":
            point = self._snap_canvas_point(point)
        self._canvas_drag["current"] = point
        start = self._canvas_drag["start"]
        moved = math.hypot(point[0] - start[0], point[1] - start[1]) >= 4.0
        if moved != self._canvas_drag["moved"]:
            self._canvas_drag["moved"] = moved

    def _on_canvas_mouse_up(self, event):
        try:
            completed = self.js.eval(
                "(() => { const gesture = window.__ngsolveSketchCompletedGesture || null; "
                "window.__ngsolveSketchCompletedGesture = null; return gesture; })()"
            )
        except Exception:
            completed = None
        if isinstance(completed, (list, tuple)) and len(completed) >= 13:
            operation, tool = str(completed[0]), str(completed[1])
            try:
                start = (float(completed[2]), float(completed[3]))
                end = (float(completed[4]), float(completed[5]))
                region_id = str(completed[6]) or None
                additive = bool(completed[7])
                moved = bool(completed[8])
                view = tuple(float(completed[index]) for index in (9, 10, 11)) + (int(completed[12]),)
            except (TypeError, ValueError, OverflowError):
                completed = None
            else:
                self._canvas_drag = None
                self._screen_to_svg = None
                if moved and operation == "region" and region_id:
                    self._move_region_from_canvas_drag(region_id, start, end, preserve_canvas=True)
                    # Interpret the pointer coordinates in the view that was
                    # visible during the drag. If a wheel transform was still
                    # pending, commit it after the region move and redraw once.
                    if self._commit_browser_canvas_view(view, render=False):
                        self.render_canvas()
                elif moved and operation == "marquee":
                    view_changed = self._commit_browser_canvas_view(view, render=False)
                    self._select_edges_in_canvas_box(start, end, additive=additive)
                    if view_changed:
                        self.render_canvas()
                elif moved and operation == "pan":
                    self._commit_browser_canvas_view(view, render=False)
                    # The pan has already been applied to the local SVG matrix.
                    self.render_canvas()
                elif moved and operation == "draw":
                    view_changed = self._commit_browser_canvas_view(view, render=False)
                    region_count = len(self.model["geometry"].get("regions", []))
                    self._create_region_from_canvas_drag(tool, start, end)
                    if len(self.model["geometry"].get("regions", [])) == region_count:
                        if view_changed:
                            self.render_canvas()
                        else:
                            self._schedule_canvas_interaction_reset()
                elif self._commit_browser_canvas_view(view, render=False):
                    self.render_canvas()
                return
        drag = self._canvas_drag
        if drag is None:
            self._screen_to_svg = None
            return
        point = self._canvas_event_point(event)
        if point is not None:
            if drag["tool"] != "select":
                point = self._snap_canvas_point(point)
            drag["current"] = point
        start, end = drag["start"], drag["current"]
        moved = math.hypot(end[0] - start[0], end[1] - start[1]) >= 4.0
        self._canvas_drag = None
        self._screen_to_svg = None
        if moved and drag["operation"] == "region" and drag.get("region_id"):
            self._move_region_from_canvas_drag(drag["region_id"], start, end)
        elif moved and drag["operation"] == "marquee":
            self._select_edges_in_canvas_box(start, end, additive=drag["additive"])
        elif moved and drag["operation"] == "pan":
            self._pan_canvas_by_pixels(start, end)
        elif moved and drag["operation"] == "draw":
            self._create_region_from_canvas_drag(drag["tool"], start, end)

    def _on_canvas_pointer_cancel(self, event=None):
        """Discard an incomplete sketch gesture after a cancelled pointer."""
        self._canvas_drag = None
        self._screen_to_svg = None

    @contextmanager
    def _batch_frontend_updates(self):
        """Flush one interaction's related component changes together.

        Shape creation refreshes the model tree, inspector, and sketch. Sending
        those slot replacements serially lets the browser paint the temporary
        empty state between each request, so coalesce repeated component
        updates and submit the finished patches concurrently.
        """
        frontend = get_environment().frontend
        send_update = frontend.update_component
        owner_thread = get_ident()
        queued = {}

        def collect(component, data, method, blocking=True):
            if get_ident() != owner_thread:
                return send_update(component, data, method, blocking=blocking)
            key = (id(component), method)
            if key not in queued:
                queued[key] = [component, data, method, blocking]
                return
            previous = queued[key][1]
            if previous is None or data is None:
                queued[key][1] = None
                return
            combined = dict(previous)
            for key, value in data.items():
                if key in {"props", "slots"} and isinstance(value, dict):
                    merged = dict(combined.get(key, {}))
                    merged.update(value)
                    combined[key] = merged
                else:
                    combined[key] = value
            queued[(id(component), method)][1] = combined

        frontend.update_component = collect
        try:
            yield
        finally:
            frontend.update_component = send_update
            updates = list(queued.values())
            if updates:
                canvas_components = {
                    id(self._canvas_grid),
                    id(self._canvas_scene),
                    id(self._canvas_dimensions),
                }
                canvas_updates = [item for item in updates if id(item[0]) in canvas_components]
                panel_updates = [item for item in updates if id(item[0]) not in canvas_components]

                def flush(batch):
                    if not batch:
                        return
                    with ThreadPoolExecutor(max_workers=min(8, len(batch))) as pool:
                        futures = [
                            pool.submit(send_update, component, data, method, blocking=blocking)
                            for component, data, method, blocking in batch
                        ]
                        for future in futures:
                            future.result()

                # Draw the new geometry while the current tree and properties
                # remain visible. Let that canvas patch settle before replacing
                # the two side panels, avoiding a frame where all three panes
                # are empty at once.
                flush(canvas_updates)
                if canvas_updates and panel_updates:
                    time.sleep(0.12)
                flush(panel_updates)

    def _select_edges_in_canvas_box(self, start, end, *, additive=False):
        project, _ = self._canvas_projection()
        box = (min(start[0], end[0]), max(start[0], end[0]), min(start[1], end[1]), max(start[1], end[1]))
        window_selection = end[0] >= start[0]
        selected = []
        for edge in self.model["geometry"].get("edges", []):
            points = [project(point) for point in edge.get("vertices", [])]
            if len(points) != 2:
                continue
            if window_selection:
                matches = all(_point_in_box(point, box) for point in points)
            else:
                matches = _segment_intersects_box(points[0], points[1], box)
            if matches:
                selected.append(edge["id"])
        if additive:
            selected = list(dict.fromkeys([*self.selected_edge_ids, *selected]))
        self.selected_edge_ids = selected
        self.selected_edge_id = selected[-1] if selected else None
        self.selected_region_id = None
        self._selected_condition_id = None
        self.active_section = "geometry"
        self.layout["active_section"] = self.active_section
        kind = "enclosed" if window_selection else "crossed"
        self._message(f"Selected {len(selected)} {kind} edge{'s' if len(selected) != 1 else ''}.")
        self._update_model_tree_selection()
        self._update_geometry_inspector()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()

    def _create_region_from_canvas_drag(self, tool, start, end):
        if self.snap_to_grid:
            start = self._snap_canvas_point(start)
            end = self._snap_canvas_point(end)
        _, unproject = self._canvas_projection()
        # Mouse placement is intentionally limited to 0.01 mm. The property
        # editor remains full precision for users who enter dimensions directly.
        first = tuple(_round_sketch_mm(coordinate * 1000) / 1000 for coordinate in unproject(start))
        second = tuple(_round_sketch_mm(coordinate * 1000) / 1000 for coordinate in unproject(end))
        before = len(self.model["geometry"]["regions"])
        if tool == "rectangle":
            r_min, r_max = sorted((first[0], second[0]))
            z_min, z_max = sorted((first[1], second[1]))
            values = {
                "r_min": _round_sketch_mm(r_min * 1000),
                "z_min": _round_sketch_mm(z_min * 1000),
                "width": _round_sketch_mm((r_max - r_min) * 1000),
                "height": _round_sketch_mm((z_max - z_min) * 1000),
            }
        else:
            radius = math.hypot(second[0] - first[0], second[1] - first[1])
            values = {
                "r_center": _round_sketch_mm(first[0] * 1000),
                "z_center": _round_sketch_mm(first[1] * 1000),
                "radius": _round_sketch_mm(radius * 1000),
            }
        for key, value in values.items():
            self._primitive_values[(tool, key)] = value
        self._add_primitive(tool)
        if len(self.model["geometry"]["regions"]) > before:
            self.set_sketch_tool("select", announce=False)

    @staticmethod
    def _dimension_literal_mm(value_m):
        """Format a stored metre coordinate as a stable millimetre expression."""
        return format(value_m * 1000, ".12g")

    def _move_region_from_canvas_drag(self, region_id, start, end, *, preserve_canvas=False):
        region = next(
            (item for item in self.model["geometry"]["regions"] if item["id"] == region_id),
            None,
        )
        if region is None:
            return
        if self.snap_to_grid:
            start = self._snap_canvas_point(start)
            end = self._snap_canvas_point(end)
        _, unproject = self._canvas_projection()
        start_world, end_world = unproject(start), unproject(end)
        dr = _round_sketch_mm((end_world[0] - start_world[0]) * 1000) / 1000
        dz = _round_sketch_mm((end_world[1] - start_world[1]) * 1000) / 1000
        if dr == 0 and dz == 0:
            if preserve_canvas:
                self._rollback_canvas_region_move(region_id)
            else:
                self.render_canvas()
            return False

        old_shape = copy.deepcopy(region["shape"])
        old_vertices = copy.deepcopy(region["vertices"])
        old_parents = {item["id"]: item.get("parent_id") for item in self.model["geometry"]["regions"]}
        old_edges = copy.deepcopy(self.model["geometry"].get("edges", []))
        try:
            shape = region["shape"]
            expressions = shape.setdefault("dimension_expressions", {})
            if shape["type"] == "rectangle":
                shape["r_min"] += dr
                shape["z_min"] += dz
                if shape["r_min"] < -1e-12:
                    raise ValueError("A region cannot be moved left of the r = 0 axis.")
                shape["r_min"] = max(0.0, shape["r_min"])
                expressions["r_min"] = self._dimension_literal_mm(shape["r_min"])
                expressions["z_min"] = self._dimension_literal_mm(shape["z_min"])
                r0, z0 = shape["r_min"], shape["z_min"]
                width, height = shape["width"], shape["height"]
                region["vertices"] = [[r0, z0], [r0 + width, z0], [r0 + width, z0 + height], [r0, z0 + height]]
            else:
                shape["r_center"] += dr
                shape["z_center"] += dz
                if shape["r_center"] < shape["radius"] - 1e-12:
                    raise ValueError("The circle must remain at r ≥ 0.")
                shape["r_center"] = max(shape["radius"], shape["r_center"])
                expressions["r_center"] = self._dimension_literal_mm(shape["r_center"])
                expressions["z_center"] = self._dimension_literal_mm(shape["z_center"])
                radius = shape["radius"]
                rcenter, zcenter = shape["r_center"], shape["z_center"]
                segments = int(shape.get("segments", 48))
                region["vertices"] = [
                    [rcenter + radius * math.cos(2 * math.pi * index / segments),
                     zcenter + radius * math.sin(2 * math.pi * index / segments)]
                    for index in range(segments)
                ]

            # Moving existing edges before rebuilding preserves their IDs,
            # names, and electromagnetic/mechanical boundary assignments.
            edge_ids = set(region.get("edge_ids", []))
            for edge in self.model["geometry"].get("edges", []):
                if edge["id"] in edge_ids:
                    edge["vertices"] = [[point[0] + dr, point[1] + dz] for point in edge["vertices"]]
            self._recompute_parent_links()
            self._rebuild_edges()
            self.selected_region_id = region_id
            self.selected_edge_id = None
            self.selected_edge_ids = []
            self._selected_condition_id = None
            self.active_section = "geometry"
            self.layout["active_section"] = self.active_section
            with self._batch_frontend_updates():
                self._refresh_model_tree()
                self._render_inspector()
                if preserve_canvas:
                    self._sync_canvas_selection()
                    self._render_canvas_dimensions()
                    self._commit_canvas_region_move(region_id, dr, dz)
                else:
                    self.render_canvas()
            self._message(f"Moved {region['name']} by {dr * 1000:g} mm radially and {dz * 1000:g} mm axially.")
            return True
        except (ValueError, TypeError, OverflowError) as error:
            region["shape"] = old_shape
            region["vertices"] = old_vertices
            self.model["geometry"]["edges"] = old_edges
            for item in self.model["geometry"]["regions"]:
                item["parent_id"] = old_parents[item["id"]]
            self._message(str(error), error=True)
            if preserve_canvas:
                self._rollback_canvas_region_move(region_id)
            else:
                self.render_canvas()
            return False

    def _commit_canvas_region_move(self, region_id, dr, dz):
        """Keep the live SVG scene mounted and commit its region preview offset."""
        project, _ = self._canvas_projection()
        origin = project((0.0, 0.0))
        moved = project((dr, dz))
        dx, dy = moved[0] - origin[0], moved[1] - origin[1]
        try:
            self.js.eval(
                "(() => { const commit = window.__ngsolveCommitSketchRegionMove; "
                f"if (commit) commit({json.dumps(region_id)}, {dx!r}, {dy!r}); "
                "})()"
            )
        except Exception:
            # Standalone tests and non-browser frontends have no JS runtime.
            pass

    def _rollback_canvas_region_move(self, region_id):
        """Restore the committed SVG offset when a proposed move is rejected."""
        try:
            self.js.eval(
                "(() => { const rollback = window.__ngsolveRollbackSketchRegionMove; "
                f"if (rollback) rollback({json.dumps(region_id)}); "
                "})()"
            )
        except Exception:
            # Standalone tests and non-browser frontends have no JS runtime.
            pass

    def _message(self, text, error=False):
        self.message = str(text)
        self.message_is_error = error
        self._status_text.ui_children = [self.message]
        self._status_text.ui_style = (
            "overflow:hidden; text-overflow:ellipsis; font-size:11px; "
            + ("color:var(--negative);" if error else "color:var(--fg-muted);")
        )
        self._log_messages.append(self.message)
        self._log_messages = self._log_messages[-200:]
        self._log_body.ui_children = [Div(line) for line in self._log_messages]
        if self.on_log:
            self.on_log(self.message, error)

    def toggle_messages(self, *args):
        self._messages_visible = not self._messages_visible
        self._messages_panel.ui_hidden = not self._messages_visible

    def _show_validation_issues(self, errors):
        self.validation_issues = list(errors)
        self._messages_button.ui_label = f"Messages ({len(errors)})" if errors else "Messages"
        if not errors:
            self._messages_panel.ui_children = [Div(
                "Setup checks passed. Meshing and solving still need to complete successfully.",
                ui_style="font-size:12px; color:var(--positive); padding:5px 0;",
            )]
            self._messages_visible = True
            self._messages_panel.ui_hidden = False
            return
        rows = []
        for issue in errors:
            section = self._validation_issue_section(issue)
            rows.append(_button(
                issue,
                "mdi-alert-circle-outline",
                lambda *a, selected=section: self.select_section(selected),
                style="width:100%; justify-content:flex-start; text-align:left; white-space:normal;",
                align="left",
            ))
        self._messages_panel.ui_children = rows
        self._messages_visible = True
        self._messages_panel.ui_hidden = False

    @staticmethod
    def _validation_issue_section(issue):
        text = issue.lower()
        if "parameter" in text:
            return "parameters"
        if "material" in text:
            return "materials"
        if "source" in text or "current density" in text or "body force" in text:
            return "sources"
        if "study" in text or "frequency" in text:
            return "studies"
        if "mesh" in text:
            return "mesh"
        if "solver" in text or "anderson" in text or "iteration" in text or "tolerance" in text:
            return "solver"
        if "boundary" in text or "edge" in text or "support" in text:
            return "boundaries"
        if "physics" in text or "magnetic" in text or "mechanic" in text or "coupl" in text:
            return "physics"
        return "geometry"

    def append_solver_output(self, lines):
        additions = [str(line).rstrip() for line in lines if str(line).strip()]
        if not additions:
            return
        self._log_messages.extend(additions)
        self._log_messages = self._log_messages[-200:]
        self._log_body.ui_children = [Div(line) for line in self._log_messages]

    def toggle_log(self, *args):
        if self._log_visible:
            self.hide_log()
        else:
            self.show_log()

    def show_log(self, *args):
        self._log_visible = True
        self._log_panel.ui_hidden = False

    def hide_log(self, *args):
        self._log_visible = False
        self._log_panel.ui_hidden = True

    def _flash_status_bar(self):
        self._status_flash_variant = 1 - self._status_flash_variant
        variant = "a" if self._status_flash_variant else "b"
        self._bottom_bar.ui_class = f"mmfem-solve-status-bar mmfem-solve-status-flash-{variant}"

    def _make_primitive_dialog(self, kind):
        rectangle = kind == "rectangle"
        fields = (
            [
                ("r_min", "Inner radius", "0", "mm"),
                ("z_min", "Bottom", "0", "mm"),
                ("width", "Radial width", "20", "mm"),
                ("height", "Axial height", "20", "mm"),
            ]
            if rectangle
            else [
                ("r_center", "Centre radius", "15", "mm"),
                ("z_center", "Centre height", "10", "mm"),
                ("radius", "Radius", "5", "mm"),
            ]
        )
        self._primitive_values = getattr(self, "_primitive_values", {})
        inputs = []
        for key, label, value, unit in fields:
            self._primitive_values[(kind, key)] = float(value)
            widget = QInput(
                ui_label=label,
                ui_model_value=value,
                ui_type="number",
                ui_suffix=unit,
                ui_dense=True,
                ui_filled=True,
                ui_style="width:100%;",
            )
            widget.on_update_model_value(lambda event, k=key, primitive=kind: self._set_primitive_value(primitive, k, event.value))
            inputs.append(widget)
        cancel = QBtn(ui_label="Cancel", ui_flat=True, ui_no_caps=True)
        cancel.on_click(lambda *a, primitive=kind: self._close_dialog(primitive))
        confirm = QBtn(ui_label="Create region", ui_color="primary", ui_no_caps=True)
        confirm.on_click(lambda *a, primitive=kind: self._add_primitive(primitive))
        content = QCard(
            QCardSection(Div(f"Create {'rectangle' if rectangle else 'circle'} region", ui_style="font-weight:600; font-size:16px;")),
            QCardSection(*inputs),
            QCardSection(Div(cancel, confirm, ui_style="display:flex; justify-content:flex-end; gap:8px;")),
            ui_style="width:min(420px, 90vw);",
        )
        dialog = QDialog(content, ui_model_value=False, ui_persistent=False)
        dialog._solve_kind = kind
        return dialog

    def _set_primitive_value(self, kind, key, value):
        try:
            self._primitive_values[(kind, key)] = float(value)
        except (TypeError, ValueError):
            self._primitive_values[(kind, key)] = math.nan

    def open_rectangle_dialog(self, *args):
        self._rectangle_dialog.ui_model_value = True

    def open_circle_dialog(self, *args):
        self._circle_dialog.ui_model_value = True

    def _add_primitive(self, kind):
        try:
            vals = {key: value for (primitive, key), value in self._primitive_values.items() if primitive == kind}
            if not all(math.isfinite(value) for value in vals.values()):
                raise ValueError("Enter a number in each dimension.")
            if kind == "rectangle":
                r0 = vals["r_min"] / 1000
                z0 = vals["z_min"] / 1000
                width = vals["width"] / 1000
                height = vals["height"] / 1000
                if r0 < 0 or width <= 0 or height <= 0:
                    raise ValueError("Radius must be non-negative and both dimensions must be positive.")
                vertices = [[r0, z0], [r0 + width, z0], [r0 + width, z0 + height], [r0, z0 + height]]
                primitive_data = {
                    "type": "rectangle", "r_min": r0, "z_min": z0, "width": width, "height": height,
                    "dimension_expressions": {key: str(vals[key]) for key in ("r_min", "z_min", "width", "height")},
                }
                label = "square"
            else:
                radius = vals["radius"] / 1000
                rcenter = vals["r_center"] / 1000
                zcenter = vals["z_center"] / 1000
                if radius <= 0 or rcenter < radius:
                    raise ValueError("Radius must be positive and the circle must stay at r ≥ 0.")
                vertices = [
                    [rcenter + radius * math.cos(2 * math.pi * i / 48), zcenter + radius * math.sin(2 * math.pi * i / 48)]
                    for i in range(48)
                ]
                primitive_data = {
                    "type": "circle", "r_center": rcenter, "z_center": zcenter, "radius": radius, "segments": 48,
                    "dimension_expressions": {key: str(vals[key]) for key in ("r_center", "z_center", "radius")},
                }
                label = "circle"
            parent_id = self._find_containing_region(vertices)
            if self._has_unsupported_overlap(vertices, parent_id):
                raise ValueError("Regions must be nested or disjoint; crossing material boundaries are not supported yet.")
            used_indices = []
            for existing in self.model["geometry"]["regions"]:
                if existing.get("shape", {}).get("type") != primitive_data["type"]:
                    continue
                match = re.fullmatch(rf"{label} (\d+)", existing.get("name", ""))
                if match:
                    used_indices.append(int(match.group(1)))
            index = max(used_indices, default=0) + 1
            region = {
                "id": new_id("region"),
                "name": f"{label} {index}",
                "shape": primitive_data,
                "vertices": vertices,
                "material_id": None,
                "parent_id": parent_id,
                "mechanical": False,
                "sources": {
                    "dc_current_density": "0",
                    "ac_current_density_real": "0",
                    "ac_current_density_imaginary": "0",
                    "mechanical_body_force": {"r": "0", "z": "0"},
                },
                "edge_ids": [],
                "constraints": self._primitive_constraints(kind),
            }
            with self._batch_frontend_updates():
                self.model["geometry"]["regions"].append(region)
                try:
                    self._recompute_parent_links()
                except ValueError:
                    self.model["geometry"]["regions"].pop()
                    raise
                self._rebuild_edges()
                self.selected_region_id = region["id"]
                self.selected_edge_id = None
                self.selected_edge_ids = []
                self._selected_condition_id = None
                self.active_section = "geometry"
                self.layout["active_section"] = self.active_section
                self._close_dialog(kind)
                self._message(f"Created {region['name']} with {len(region['constraints'])} driving sketch constraints.")
                self._refresh_model_tree()
                self._render_inspector()
                self.render_canvas()
        except (KeyError, ValueError, OverflowError) as error:
            self._message(str(error), error=True)

    def _close_dialog(self, kind):
        dialog = self._rectangle_dialog if kind == "rectangle" else self._circle_dialog
        dialog.ui_model_value = False

    def _primitive_constraints(self, kind):
        if kind == "rectangle":
            return [
                {"type": "horizontal", "target": "edge-0", "driving": False},
                {"type": "vertical", "target": "edge-1", "driving": False},
                {"type": "horizontal", "target": "edge-2", "driving": False},
                {"type": "vertical", "target": "edge-3", "driving": False},
                {"type": "radial_position", "target": "shape", "driving": True},
                {"type": "axial_position", "target": "shape", "driving": True},
                {"type": "width", "target": "shape", "driving": True},
                {"type": "height", "target": "shape", "driving": True},
            ]
        return [
            {"type": "radial_position", "target": "shape", "driving": True},
            {"type": "axial_position", "target": "shape", "driving": True},
            {"type": "radius", "target": "shape", "driving": True},
            {"type": "equal_segments", "target": "tessellation", "driving": False},
        ]

    def _find_containing_region(self, vertices):
        candidates = []
        for region in self.model["geometry"]["regions"]:
            polygon = region["vertices"]
            if _strictly_contains(polygon, vertices):
                area = abs(_polygon_area(polygon))
                candidates.append((area, region["id"]))
        return min(candidates)[1] if candidates else None

    def _has_unsupported_overlap(self, vertices, parent_id):
        for region in self.model["geometry"]["regions"]:
            existing = region["vertices"]
            if _strictly_contains(existing, vertices) or _strictly_contains(vertices, existing):
                continue
            if _polygons_overlap(vertices, existing):
                return True
        return False

    def _recompute_parent_links(self):
        regions = self.model["geometry"]["regions"]
        for index, first in enumerate(regions):
            for second in regions[index + 1:]:
                a, b = first["vertices"], second["vertices"]
                if _strictly_contains(a, b) or _strictly_contains(b, a):
                    continue
                if _polygons_overlap(a, b):
                    raise ValueError("Regions must be strictly nested or disjoint; crossing or touching material boundaries are not supported.")
        parents = {}
        for child in regions:
            child_area = abs(_polygon_area(child["vertices"]))
            containers = [
                (abs(_polygon_area(outer["vertices"])), outer["id"])
                for outer in regions
                if outer["id"] != child["id"]
                and abs(_polygon_area(outer["vertices"])) > child_area
                and _strictly_contains(outer["vertices"], child["vertices"])
            ]
            parents[child["id"]] = min(containers)[1] if containers else None
        for region in regions:
            region["parent_id"] = parents[region["id"]]

    def _rebuild_edges(self):
        old_edges = self.model["geometry"].get("edges", [])
        old_by_key = {_edge_key(edge["vertices"]): edge for edge in old_edges}
        edges = []
        for region in self.model["geometry"]["regions"]:
            region_edges = []
            points = region["vertices"]
            for index, start in enumerate(points):
                end = points[(index + 1) % len(points)]
                key = _edge_key([start, end])
                edge = old_by_key.get(key)
                if edge is None:
                    edge = {
                        "id": new_id("edge"),
                        "vertices": [list(start), list(end)],
                        "name": f"Edge {len(edges) + 1}",
                        "boundary_condition_ids": [],
                    }
                else:
                    edge = copy.deepcopy(edge)
                    if "boundary_condition_ids" not in edge:
                        legacy_condition = edge.pop("boundary_condition_id", None)
                        edge["boundary_condition_ids"] = [legacy_condition] if legacy_condition else []
                if all(existing["id"] != edge["id"] for existing in edges):
                    edges.append(edge)
                region_edges.append(edge["id"])
            region["edge_ids"] = region_edges
        self.model["geometry"]["edges"] = edges
        if hasattr(self, "selected_edge_ids"):
            live_ids = {edge["id"] for edge in edges}
            self.selected_edge_ids = [edge_id for edge_id in self.selected_edge_ids if edge_id in live_ids]
            if self.selected_edge_id not in live_ids:
                self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None

    def _refresh_model_tree(self):
        labels = {key: label for key, label, _ in _SECTIONS}
        for key, button in self._section_buttons.items():
            button.ui_label = labels[key]
            button.ui_color = "primary" if key == self.active_section else None
            button.ui_flat = key != self.active_section
        tree_groups = []
        self._tree_entry_buttons = {}
        for group_label, section_keys in _TREE_GROUPS:
            rows = []
            for key in section_keys:
                row_children = [self._section_buttons[key]]
                subsection = self._tree_subsection(key)
                if subsection:
                    title, entries = subsection
                    row_children.append(Div(
                        Div(title, ui_style="font-size:10px; font-weight:700; letter-spacing:.06em; color:var(--fg-muted); padding:7px 8px 3px 10px;"),
                        *[
                            self._make_tree_entry_button(key, entry_id, label, icon, callback, selected)
                            for entry_id, label, icon, callback, selected in entries
                        ],
                        ui_style="display:flex; flex-direction:column; gap:1px; margin:0 0 4px 12px; border-left:1px solid var(--border); padding-left:4px;",
                    ))
                rows.append(Div(*row_children, ui_style="display:flex; flex-direction:column; gap:1px;"))
            tree_groups.append(Div(
                Div(group_label, ui_style="font-size:10px; font-weight:700; letter-spacing:.09em; color:var(--fg-muted); padding:7px 8px 5px;"),
                *rows,
                ui_style="display:flex; flex-direction:column; gap:2px;",
            ))
        self._left_items.ui_children = tree_groups
        self._update_model_tree_selection()
        if hasattr(self, "_bottom_count"):
            self._bottom_count.ui_children = [
                f"Regions: {len(self.model['geometry']['regions'])}  ·  Mesh order: {self.model['mesh']['polynomial_order']}"
            ]
        if hasattr(self, "_model_title") and not self._model_title_editing:
            self._model_title.ui_model_value = self.model.get("name", "Untitled axisymmetric model")

    def _make_tree_entry_button(self, section, entry_id, label, icon, callback, selected):
        button = _button(
            label,
            icon,
            callback,
            color="primary" if selected else None,
            style="width:100%; justify-content:flex-start; text-align:left; padding-left:10px; min-height:30px;",
            align="left",
        )
        self._tree_entry_buttons[f"{section}:{entry_id}"] = button
        return button

    def _tree_subsection(self, section):
        if section == "geometry":
            entries = [
                (region["id"], region["name"], "mdi-vector-square-outline", lambda *a, rid=region["id"]: self.select_region(rid), region["id"] == self.selected_region_id)
                for region in self.model["geometry"]["regions"]
            ]
            return f"REGIONS · {len(entries)}", entries
        if section == "parameters":
            entries = [
                (item["id"], item.get("name", "Parameter"), "mdi-variable", lambda *a, pid=item["id"]: self.select_parameter(pid), item["id"] == self.selected_parameter_id)
                for item in self.model.get("parameters", [])
            ]
            return f"PARAMETERS · {len(entries)}", entries
        if section == "materials":
            entries = []
            for item in self.model.get("materials", []):
                assigned = sum(region.get("material_id") == item["id"] for region in self.model["geometry"]["regions"])
                noun = "region" if assigned == 1 else "regions"
                entries.append((
                    item["id"],
                    f"{item['name']} · {assigned} {noun}",
                    "mdi-cube-outline",
                    lambda *a, mid=item["id"]: self.select_material(mid),
                    item["id"] == self.selected_material_id,
                ))
            return f"MATERIALS · {len(entries)}", entries
        if section == "boundaries":
            entries = []
            for item in self.model.get("boundary_conditions", []):
                assigned = sum(item["id"] in _edge_condition_ids(edge) for edge in self.model["geometry"].get("edges", []))
                noun = "edge" if assigned == 1 else "edges"
                entries.append((
                    item["id"],
                    f"{item['name']} · {assigned} {noun}",
                    "mdi-vector-link",
                    lambda *a, bid=item["id"]: self.select_boundary(bid),
                    item["id"] == self.selected_boundary_id,
                ))
            return f"CONDITIONS · {len(entries)}", entries
        if section == "sources":
            entries = [
                (region["id"], region["name"], "mdi-flash-outline", lambda *a, rid=region["id"]: self.select_source_region(rid), region["id"] == self.selected_region_id)
                for region in self.model["geometry"].get("regions", [])
            ]
            return f"REGION LOADS · {len(entries)}", entries
        if section == "studies":
            entries = [
                (item.get("id", item.get("name", "Study")), item.get("name", "Study"), "mdi-play-box-outline", lambda *a: self.select_section("studies"), False)
                for item in self.studies.get("studies", [])
            ]
            return f"STUDY · {len(entries)}", entries
        if section == "runs":
            entries = [
                (item.get("id", item.get("name", "Run")), item.get("name", "Run"), "mdi-file-chart-outline", lambda *a: self.select_section("runs"), False)
                for item in reversed(self.runs)
            ]
            return f"RUNS · {len(entries)}", entries
        return None

    def select_material(self, material_id):
        self.selected_material_id = material_id
        self.active_section = "materials"
        self.layout["active_section"] = self.active_section
        self._selected_condition_id = None
        self._update_model_tree_selection()
        self._render_inspector()
        self._sync_canvas_selection()

    def select_boundary(self, boundary_id):
        self.selected_boundary_id = boundary_id
        self._selected_condition_id = boundary_id
        self.active_section = "boundaries"
        self.layout["active_section"] = self.active_section
        self._update_model_tree_selection()
        self._render_inspector()
        self._sync_canvas_selection()

    def select_parameter(self, parameter_id):
        self.selected_parameter_id = parameter_id
        self.active_section = "parameters"
        self.layout["active_section"] = self.active_section
        self._update_model_tree_selection()
        self._render_inspector()

    def select_source_region(self, region_id):
        self.selected_region_id = region_id
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self._selected_condition_id = None
        self.active_section = "sources"
        self.layout["active_section"] = self.active_section
        self._update_model_tree_selection()
        self._render_inspector()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()

    def select_region(self, region_id):
        self.selected_region_id = region_id
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self._selected_condition_id = None
        self.active_section = "geometry"
        self.layout["active_section"] = self.active_section
        self._update_model_tree_selection()
        if self._inspector_view != "geometry":
            self._render_inspector()
        else:
            self._update_geometry_inspector()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()

    def select_edge(self, edge_id, additive=False):
        if additive:
            selected = list(self.selected_edge_ids)
            if edge_id in selected:
                selected.remove(edge_id)
            else:
                selected.append(edge_id)
            self.selected_edge_ids = selected
        else:
            self.selected_edge_ids = [edge_id]
        self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None
        self.selected_region_id = None
        self._selected_condition_id = None
        self.active_section = "geometry"
        self.layout["active_section"] = self.active_section
        self._update_model_tree_selection()
        if self._inspector_view != "geometry":
            self._render_inspector()
        else:
            self._update_geometry_inspector()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()

    def _sync_canvas_selection(self):
        """Update selected sketch styling without replacing canvas geometry."""
        edge_ids = json.dumps(self.selected_edge_ids)
        region_id = json.dumps(self.selected_region_id)
        highlighted_edges = [
            edge["id"] for edge in self.model.get("geometry", {}).get("edges", [])
            if self._selected_condition_id in _edge_condition_ids(edge)
        ]
        highlighted_ids = json.dumps(highlighted_edges)
        script = f"""
(() => {{
  const svg = document.querySelector('svg.solve-sketch-canvas');
  if (!svg) return;
  const selectedEdges = new Set({edge_ids});
  const selectedRegion = {region_id};
  const highlightedEdges = new Set({highlighted_ids});
  svg.querySelectorAll('[data-sketch-edge-id]').forEach((line) => {{
    const id = line.getAttribute('data-sketch-edge-id');
    const selected = selectedEdges.has(id);
    const highlighted = highlightedEdges.has(id);
    line.setAttribute('stroke', selected ? '#0877b9' : (highlighted ? '#c66f16' : '#263746'));
    line.setAttribute('stroke-width', selected || highlighted ? '2.2' : '1.6');
  }});
  svg.querySelectorAll('[data-sketch-region-id]').forEach((shape) => {{
    const selected = shape.getAttribute('data-sketch-region-id') === selectedRegion;
    shape.setAttribute('fill-opacity', selected ? '0.48' : '0.33');
    shape.setAttribute('stroke', selected ? '#1b73ae' : '#344658');
    shape.setAttribute('stroke-width', selected ? '2.2' : '1.6');
  }});
}})()
"""
        try:
            self.js.eval(script)
        except Exception:
            # Standalone tests and non-browser frontends have no JS runtime.
            pass

    def _selected_region(self):
        return next((region for region in self.model["geometry"]["regions"] if region["id"] == self.selected_region_id), None)

    def _selected_edge(self):
        return next((edge for edge in self.model["geometry"]["edges"] if edge["id"] == self.selected_edge_id), None)

    def _selected_edges(self):
        selected = set(self.selected_edge_ids)
        return [edge for edge in self.model["geometry"]["edges"] if edge["id"] in selected]

    def _canvas_dimension_components(self):
        """Build the selected region's dimension annotations separately."""
        region = self._selected_region()
        return self._region_dimension_components(region)

    def _region_dimension_components(self, region):
        """Build dimensions for a region, whether or not it is selected."""
        if region is None:
            return []

        xy, _ = self._canvas_projection()
        shape_data = region.get("shape", {})
        if shape_data.get("type") == "rectangle":
            p0, p1, p2, _ = [xy(point) for point in region["vertices"][:4]]
            width_mm = float(shape_data.get("width", 0)) * 1000
            height_mm = float(shape_data.get("height", 0)) * 1000
            dim_style = "stroke:#455f78; stroke-width:1; fill:none; pointer-events:none;"
            text_style = "fill:#263746; font-size:11px; font-weight:600; pointer-events:none;"
            offset = 18
            return [
                _svg("line", x1=p0[0], y1=p0[1] + offset, x2=p1[0], y2=p1[1] + offset, style=dim_style),
                _svg("line", x1=p0[0], y1=p0[1] + 4, x2=p0[0], y2=p0[1] + offset + 3, style=dim_style),
                _svg("line", x1=p1[0], y1=p1[1] + 4, x2=p1[0], y2=p1[1] + offset + 3, style=dim_style),
                _svg("text", x=(p0[0] + p1[0]) / 2, y=p0[1] + offset + 14, text_anchor="middle", style=text_style, children=f"W {width_mm:.4g} mm"),
                _svg("line", x1=p1[0] + offset, y1=p1[1], x2=p2[0] + offset, y2=p2[1], style=dim_style),
                _svg("line", x1=p1[0] + 4, y1=p1[1], x2=p1[0] + offset + 3, y2=p1[1], style=dim_style),
                _svg("line", x1=p2[0] + 4, y1=p2[1], x2=p2[0] + offset + 3, y2=p2[1], style=dim_style),
                _svg("text", x=p1[0] + offset + 5, y=(p1[1] + p2[1]) / 2, style=text_style, children=f"H {height_mm:.4g} mm"),
            ]
        if shape_data.get("type") == "circle":
            center = xy((shape_data["r_center"], shape_data["z_center"]))
            radial = xy((shape_data["r_center"] + shape_data["radius"], shape_data["z_center"]))
            radius_mm = float(shape_data.get("radius", 0)) * 1000
            return [
                _svg("line", x1=center[0], y1=center[1], x2=radial[0], y2=radial[1], stroke="#455f78", stroke_width="1", style="pointer-events:none;"),
                _svg("text", x=(center[0] + radial[0]) / 2, y=center[1] - 7, text_anchor="middle", fill="#263746", font_size="11", font_weight="600", style="pointer-events:none;", children=f"R {radius_mm:.4g} mm"),
            ]
        return []

    def _render_canvas_dimensions(self):
        """Update selected dimensions without replacing the full sketch scene."""
        region = self._selected_region()
        self._canvas_dimensions.ui_children = self._region_dimension_components(region)
        region_id = region.get("id") if region else None
        try:
            self.js.eval(
                "(() => { const sync = window.__ngsolveSyncSketchDimensionOffset; "
                f"if (sync) sync({json.dumps(region_id)}); "
                "})()"
            )
        except Exception:
            # Standalone tests and non-browser frontends have no JS runtime.
            pass

    def _build_geometry_inspector(self):
        self._geometry_empty_panel = Div(
            _section_title("Geometry", "Click a region or edge to edit it."),
            Div("Choose Rectangle or Circle in the viewport toolbar. Select a region to edit its dimensions and assignments; select an edge to set boundary conditions.", ui_style="font-size:12px; line-height:1.5; color:var(--fg-muted);"),
            Div("r ≥ 0 is enforced for axisymmetric geometry. Nested regions must be strictly contained or disjoint.", ui_style="font-size:11px; color:var(--fg-muted); line-height:1.45;"),
        )

        self._region_subtitle = Div("", ui_style="font-size:11px; color:var(--fg-muted); margin-top:3px;")
        self._region_title = Div(
            Div("Region properties", ui_style="font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;"),
            self._region_subtitle,
            ui_style="padding:14px 14px 10px; border-bottom:1px solid var(--border);",
        )
        self._region_name_input = _input("Region name", "", lambda event: self._set_selected_region_value("name", event.value))
        self._region_dimension_groups = {}
        for shape_type, fields in {
            "rectangle": (("r_min", "Inner radius"), ("z_min", "Bottom"), ("width", "Radial width"), ("height", "Axial height")),
            "circle": (("r_center", "Centre radius"), ("z_center", "Centre height"), ("radius", "Radius")),
        }.items():
            inputs = []
            for key, label in fields:
                inputs.append(_input(
                    label,
                    "",
                    lambda event, dimension=key: self._set_selected_region_dimension(dimension, event.value),
                    suffix="mm",
                    hint="Enter mm. SI parameters in m need ×1000 here.",
                ))
                self._region_dimension_groups.setdefault(shape_type, {})[key] = inputs[-1]
            self._region_dimension_groups[shape_type]["group"] = Div(
                *inputs,
                ui_hidden=True,
                ui_style="display:flex; flex-direction:column; gap:7px;",
            )

        self._region_material_select = QSelect(
            ui_label="Material",
            ui_options=self._material_options(),
            ui_option_label="label",
            ui_option_value="value",
            ui_model_value=None,
            ui_emit_value=True,
            ui_map_options=True,
            ui_dense=True,
            ui_filled=True,
        )
        self._region_material_select.on_update_model_value(lambda event: self._set_selected_region_value("material_id", event.value))
        self._region_mechanical_checkbox = QCheckbox(ui_label="Include in mechanics", ui_model_value=False, ui_dense=True)
        self._region_mechanical_checkbox.on_update_model_value(lambda event: self._set_selected_region_value("mechanical", bool(event.value)))
        self._region_constraints = Div(ui_style="display:flex; flex-direction:column; gap:2px;")
        self._region_panel = Div(
            self._region_title,
            Div("GEOMETRY", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted);"),
            self._region_name_input,
            Div("Driving dimensions", ui_style="font-size:11px; font-weight:700; letter-spacing:.05em; padding-top:4px;"),
            Div("Dimensions are shown in mm. The sketch updates while preserving the current rectangle or circle shape.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
            *[group["group"] for group in self._region_dimension_groups.values()],
            Div("ASSIGNMENTS", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:6px;"),
            self._region_material_select,
            self._region_mechanical_checkbox,
            Div("SKETCH CONSTRAINTS", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:6px;"),
            Div("These are built-in relationships for the rectangle and circle tools. This version does not solve general user-defined sketch constraints.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
            self._region_constraints,
            _button("Delete region", "mdi-delete-outline", lambda *a: self.delete_region(self.selected_region_id) if self.selected_region_id else None, color="negative"),
            ui_hidden=True,
            ui_style="display:flex; flex-direction:column; gap:12px;",
        )

        self._edge_subtitle = Div("", ui_style="font-size:11px; color:var(--fg-muted); margin-top:3px;")
        self._edge_title = Div(
            Div("Boundary assignment", ui_style="font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;"),
            self._edge_subtitle,
            ui_style="padding:14px 14px 10px; border-bottom:1px solid var(--border);",
        )
        self._edge_name_input = _input("Edge name", "", lambda event: self._set_selected_edge_name(event.value))
        em_options = self._edge_condition_options("electromagnetic")
        mech_options = self._edge_condition_options("mechanical")
        self._edge_em_select = QSelect(
            ui_label="Electromagnetic condition", ui_options=em_options,
            ui_option_label="label", ui_option_value="value", ui_model_value=None,
            ui_emit_value=True, ui_map_options=True, ui_dense=True, ui_filled=True,
        )
        self._edge_em_select.on_update_model_value(lambda event: self._set_edge_conditions(self.selected_edge_ids, "electromagnetic", event.value))
        self._edge_mech_select = QSelect(
            ui_label="Mechanical condition", ui_options=mech_options,
            ui_option_label="label", ui_option_value="value", ui_model_value=None,
            ui_emit_value=True, ui_map_options=True, ui_dense=True, ui_filled=True,
        )
        self._edge_mech_select.on_update_model_value(lambda event: self._set_edge_conditions(self.selected_edge_ids, "mechanical", event.value))
        self._edge_panel = Div(
            self._edge_title,
            self._edge_name_input,
            self._edge_em_select,
            self._edge_mech_select,
            Div("Electromagnetic and mechanical conditions are assigned independently, so one edge can participate in both physics. The axis is excluded from mechanical support assignments in this version.", ui_style="font-size:11px; color:var(--fg-muted); line-height:1.45;"),
            ui_hidden=True,
            ui_style="display:flex; flex-direction:column; gap:12px;",
        )
        self._geometry_inspector_children = [self._geometry_empty_panel, self._region_panel, self._edge_panel]
        self._geometry_inspector_ready = True

    def _edge_condition_options(self, physics):
        if physics == "mechanical":
            kinds = lambda item: item.get("type", "").startswith("mechanical_")
            empty = "No mechanical condition"
        else:
            kinds = lambda item: item.get("type") in {"axis_of_symmetry", "magnetic_potential_zero", "natural"}
            empty = "Natural / material interface"
        return [{"label": empty, "value": None}] + [
            {"label": item["name"], "value": item["id"]}
            for item in self.model.get("boundary_conditions", [])
            if kinds(item)
        ]

    def _material_options(self):
        """Built-ins are choices; model materials are the project-specific set."""
        options = [{"label": "Unassigned", "value": None}]
        seen = set()
        for material in [*self.model.get("materials", []), *builtin_materials()]:
            material_id = material.get("id")
            if material_id in seen:
                continue
            seen.add(material_id)
            options.append({"label": material.get("name", "Material"), "value": material_id})
        return options

    def _set_selected_region_value(self, key, value):
        region = self._selected_region()
        if region is not None:
            self._set_region_value(region["id"], key, value)

    def _set_selected_region_dimension(self, key, value):
        region = self._selected_region()
        if region is not None:
            self._set_region_dimension(region["id"], key, value)

    def _set_selected_edge_name(self, value):
        edge = self._selected_edge()
        if edge is not None and len(self.selected_edge_ids) == 1:
            self._set_edge_name(edge["id"], value)

    def _update_geometry_inspector(self):
        if not self._geometry_inspector_ready or self.active_section != "geometry":
            return
        region = self._selected_region()
        edges = self._selected_edges()
        show_region = region is not None and not edges
        show_edges = bool(edges)
        self._geometry_empty_panel.ui_hidden = show_region or show_edges
        self._region_panel.ui_hidden = not show_region
        self._edge_panel.ui_hidden = not show_edges
        if show_region:
            shape = region.get("shape", {})
            shape_type = shape.get("type")
            parent = next((item["name"] for item in self.model["geometry"]["regions"] if item["id"] == region.get("parent_id")), "Exterior")
            self._region_subtitle.ui_children = [f"{shape_type.title() if shape_type else 'Region'} · parent: {parent}"]
            self._region_name_input.ui_model_value = region.get("name", "")
            self._region_material_select.ui_options = self._material_options()
            self._region_material_select.ui_model_value = region.get("material_id")
            self._region_mechanical_checkbox.ui_model_value = bool(region.get("mechanical", False))
            expressions = shape.get("dimension_expressions", {})
            for dimension, widget in self._region_dimension_groups.get(shape_type, {}).items():
                if dimension in shape:
                    widget.ui_model_value = expressions.get(dimension, str(1000 * float(shape[dimension])))
            for type_name, group in self._region_dimension_groups.items():
                group["group"].ui_hidden = type_name != shape_type
            self._region_constraints.ui_children = [
                Div(
                    f"{constraint['type'].replace('_', ' ').title()}  ·  {'driving' if constraint.get('driving') else 'reference'}",
                    ui_style="font-size:11px; padding:3px 0; color:var(--fg-muted);",
                )
                for constraint in region.get("constraints", [])
            ]
        elif show_edges:
            if len(edges) == 1:
                edge = edges[0]
                self._edge_subtitle.ui_children = [edge.get("name", "Edge") + " · single edge"]
                self._edge_name_input.ui_model_value = edge.get("name", "")
                self._edge_name_input.ui_hidden = False
            else:
                self._edge_subtitle.ui_children = [f"{len(edges)} edges selected · assignments apply to all"]
                self._edge_name_input.ui_hidden = True
            conditions = {item["id"]: item for item in self.model.get("boundary_conditions", [])}
            assignments = [_edge_condition_ids(edge) for edge in edges]
            em_values = [next((cid for cid in ids if conditions.get(cid, {}).get("type") in {"axis_of_symmetry", "magnetic_potential_zero", "natural"}), None) for ids in assignments]
            mech_values = [next((cid for cid in ids if conditions.get(cid, {}).get("type", "").startswith("mechanical_")), None) for ids in assignments]
            self._edge_em_select.ui_options = self._edge_condition_options("electromagnetic")
            self._edge_em_select.ui_model_value = em_values[0] if all(value == em_values[0] for value in em_values) else None
            self._edge_mech_select.ui_options = self._edge_condition_options("mechanical")
            self._edge_mech_select.ui_model_value = mech_values[0] if all(value == mech_values[0] for value in mech_values) else None
            on_axis = any(all(abs(float(point[0])) <= 1e-12 for point in edge.get("vertices", [])) for edge in edges)
            self._edge_mech_select.ui_disable = on_axis

    def _render_inspector(self):
        if self.active_section == "geometry":
            if not self._geometry_inspector_ready:
                self._build_geometry_inspector()
            self._update_geometry_inspector()
            if self._inspector_view != "geometry":
                self._inspector.ui_children = self._geometry_inspector_children
                self._inspector_view = "geometry"
            return
        elif self.active_section == "parameters":
            children = self._parameter_properties()
        elif self.active_section == "materials":
            children = self._material_properties()
        elif self.active_section == "boundaries":
            children = self._boundary_properties()
        elif self.active_section == "physics":
            children = self._physics_properties()
        elif self.active_section == "sources":
            children = self._sources_properties()
        elif self.active_section == "mesh":
            children = self._mesh_properties()
        elif self.active_section == "solver":
            children = self._solver_properties()
        elif self.active_section == "studies":
            children = self._study_properties()
        else:
            children = self._run_properties()
        self._inspector.ui_children = children
        self._inspector_view = self.active_section

    def _run_properties(self):
        children = [_section_title("Run history", "Mesh and study outputs are saved in NGSolve_results.")]
        if not self.runs:
            children.append(Div("No mesh or solver runs yet.", ui_style="font-size:12px; color:var(--fg-muted);"))
            return children
        for run in reversed(self.runs):
            status_color = "var(--negative)" if run["status"] == "Failed" else "var(--positive)"
            children.append(Div(
                Div(
                    Div(run["name"], ui_style="font-weight:600; font-size:12px; overflow-wrap:anywhere;"),
                    Div(run["status"], ui_style=f"color:{status_color}; font-size:11px; font-weight:600;"),
                    ui_style="display:flex; justify-content:space-between; gap:8px;",
                ),
                Div(run["finished"], ui_style="font-size:10px; color:var(--fg-muted); margin-top:4px;"),
                Div(run["output_path"], ui_style="font-size:10px; line-height:1.35; color:var(--fg-muted); overflow-wrap:anywhere; margin-top:4px;"),
                ui_style="padding:9px; border:1px solid var(--border); border-radius:6px;",
            ))
            fields_dir = Path(run.get("output_path", "")) / "ngsolve_gui" / "fields"
            try:
                field_files = sorted(fields_dir.glob("*.pkl")) if fields_dir.is_dir() else []
            except OSError:
                field_files = []
            for field_file in field_files:
                if self.on_open_file:
                    children.append(Div(
                        Div(field_file.name, ui_style="font-size:11px; overflow-wrap:anywhere; flex:1;"),
                        _button("Open in Post Process", "mdi-open-in-app", lambda *a, path=str(field_file): self.on_open_file(path)),
                        ui_style="display:flex; align-items:center; gap:6px; padding:3px 6px 3px 12px;",
                    ))
        return children

    def _geometry_properties(self):
        region = self._selected_region()
        edge = self._selected_edge()
        selected_edges = self._selected_edges()
        if len(selected_edges) > 1:
            conditions = {item["id"]: item for item in self.model.get("boundary_conditions", [])}
            assignments = [_edge_condition_ids(item) for item in selected_edges]
            em_values = [next((cid for cid in ids if conditions.get(cid, {}).get("type") in {"axis_of_symmetry", "magnetic_potential_zero", "natural"}), None) for ids in assignments]
            mech_values = [next((cid for cid in ids if conditions.get(cid, {}).get("type", "").startswith("mechanical_")), None) for ids in assignments]
            em_value = em_values[0] if all(value == em_values[0] for value in em_values) else None
            mech_value = mech_values[0] if all(value == mech_values[0] for value in mech_values) else None
            em_options = [{"label": "Natural / material interface", "value": None}] + [
                {"label": item["name"], "value": item["id"]}
                for item in self.model.get("boundary_conditions", [])
                if item.get("type") in {"axis_of_symmetry", "magnetic_potential_zero", "natural"}
            ]
            mech_options = [{"label": "No mechanical condition", "value": None}] + [
                {"label": item["name"], "value": item["id"]}
                for item in self.model.get("boundary_conditions", [])
                if item.get("type", "").startswith("mechanical_")
            ]
            edge_ids = [item["id"] for item in selected_edges]
            em_select = QSelect(ui_label="Electromagnetic condition", ui_options=em_options, ui_option_label="label", ui_option_value="value", ui_model_value=em_value, ui_emit_value=True, ui_map_options=True, ui_dense=True, ui_filled=True)
            em_select.on_update_model_value(lambda event, ids=edge_ids: self._set_edge_conditions(ids, "electromagnetic", event.value))
            mech_select = QSelect(ui_label="Mechanical condition", ui_options=mech_options, ui_option_label="label", ui_option_value="value", ui_model_value=mech_value, ui_emit_value=True, ui_map_options=True, ui_dense=True, ui_filled=True)
            mech_select.on_update_model_value(lambda event, ids=edge_ids: self._set_edge_conditions(ids, "mechanical", event.value))
            return [
                _section_title(f"{len(selected_edges)} edges selected", "Shift-click more edges; condition changes apply to this selection."),
                em_select,
                mech_select,
            ]
        if edge:
            selected_ids = _edge_condition_ids(edge)
            conditions = {item["id"]: item for item in self.model.get("boundary_conditions", [])}
            em_condition = next((cid for cid in selected_ids if conditions.get(cid, {}).get("type") in {"axis_of_symmetry", "magnetic_potential_zero", "natural"}), None)
            mech_condition = next((cid for cid in selected_ids if conditions.get(cid, {}).get("type", "").startswith("mechanical_")), None)
            em_options = [{"label": "Natural / material interface", "value": None}] + [
                {"label": item["name"], "value": item["id"]}
                for item in self.model.get("boundary_conditions", [])
                if item.get("type") in {"axis_of_symmetry", "magnetic_potential_zero", "natural"}
            ]
            mech_options = [{"label": "No mechanical condition", "value": None}] + [
                {"label": item["name"], "value": item["id"]}
                for item in self.model.get("boundary_conditions", [])
                if item.get("type", "").startswith("mechanical_")
            ]
            on_axis = all(abs(float(point[0])) <= 1e-12 for point in edge.get("vertices", []))
            if on_axis:
                mech_options = mech_options[:1]
            em_select = QSelect(ui_label="Electromagnetic condition", ui_options=em_options, ui_option_label="label", ui_option_value="value", ui_model_value=em_condition, ui_emit_value=True, ui_map_options=True, ui_dense=True, ui_filled=True)
            em_select.on_update_model_value(lambda event, eid=edge["id"]: self._set_edge_condition(eid, "electromagnetic", event.value))
            mech_select = QSelect(ui_label="Mechanical condition", ui_options=mech_options, ui_option_label="label", ui_option_value="value", ui_model_value=mech_condition, ui_emit_value=True, ui_map_options=True, ui_dense=True, ui_filled=True, ui_disable=on_axis)
            mech_select.on_update_model_value(lambda event, eid=edge["id"]: self._set_edge_condition(eid, "mechanical", event.value))
            name_input = _input("Edge name", edge.get("name", ""), lambda event, eid=edge["id"]: self._set_edge_name(eid, event.value))
            return [
                _section_title(edge.get("name", "Edge"), "Boundary assignment"),
                name_input,
                em_select,
                mech_select,
                Div("Electromagnetic and mechanical conditions are assigned independently, so one edge can participate in both physics." + (" The axis is excluded from mechanical support assignments in this version." if on_axis else ""), ui_style="font-size:11px; color:var(--fg-muted); line-height:1.45;"),
            ]
        if region:
            name = _input("Region name", region["name"], lambda event, rid=region["id"]: self._set_region_value(rid, "name", event.value))
            material = QSelect(
                ui_label="Material",
                ui_options=[{"label": item["name"], "value": item["id"]} for item in self.model["materials"]],
                ui_option_label="label",
                ui_option_value="value",
                ui_model_value=region.get("material_id"),
                ui_emit_value=True,
                ui_map_options=True,
                ui_dense=True,
                ui_filled=True,
            )
            material.on_update_model_value(lambda event, rid=region["id"]: self._set_region_value(rid, "material_id", event.value))
            mechanical = QCheckbox(ui_label="Include in mechanics", ui_model_value=bool(region.get("mechanical", False)), ui_dense=True)
            mechanical.on_update_model_value(lambda event, rid=region["id"]: self._set_region_value(rid, "mechanical", bool(event.value)))
            shape_fields = []
            shape = region.get("shape", {})
            if shape.get("type") == "rectangle":
                expressions = shape.get("dimension_expressions", {})
                for key, label, display in (("r_min", "Inner radius", "mm"), ("z_min", "Bottom", "mm"), ("width", "Radial width", "mm"), ("height", "Axial height", "mm")):
                    value = expressions.get(key, str(1000 * float(shape[key])))
                    shape_fields.append(_input(label, value, lambda event, rid=region["id"], k=key: self._set_region_dimension(rid, k, event.value), suffix=display, hint="Enter mm. SI parameters in m need ×1000 here."))
            elif shape.get("type") == "circle":
                expressions = shape.get("dimension_expressions", {})
                for key, label in (("r_center", "Centre radius"), ("z_center", "Centre height"), ("radius", "Radius")):
                    value = expressions.get(key, str(1000 * float(shape[key])))
                    shape_fields.append(_input(label, value, lambda event, rid=region["id"], k=key: self._set_region_dimension(rid, k, event.value), suffix="mm", hint="Enter mm. SI parameters in m need ×1000 here."))
            delete_button = _button("Delete region", "mdi-delete-outline", lambda *a, rid=region["id"]: self.delete_region(rid), color="negative")
            constraints = [Div(f"{constraint['type'].replace('_', ' ').title()}  ·  {'driving' if constraint.get('driving') else 'reference'}", ui_style="font-size:11px; padding:3px 0; color:var(--fg-muted);") for constraint in region.get("constraints", [])]
            parent = next((item["name"] for item in self.model["geometry"]["regions"] if item["id"] == region.get("parent_id")), "Exterior")
            return [
                _section_title("Region properties", f"{region.get('shape', {}).get('type', 'polygon').title()} · parent: {parent}"),
                Div("GEOMETRY", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted);"),
                name,
                Div("Driving dimensions", ui_style="font-size:11px; font-weight:700; letter-spacing:.05em; padding-top:4px;"),
                Div("Dimensions are shown in mm. The sketch updates while preserving the current rectangle or circle shape.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
                *shape_fields,
                Div("ASSIGNMENTS", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:6px;"),
                material,
                mechanical,
                Div("SKETCH CONSTRAINTS", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:6px;"),
                Div("These are built-in relationships for the rectangle and circle tools. This version does not solve general user-defined sketch constraints.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
                *constraints,
                delete_button,
            ]
        return [
            _section_title("Geometry", "Click a region or edge to edit it."),
            Div("Choose Rectangle or Circle in the viewport toolbar. Select a region to edit its dimensions and assignments; select an edge to set boundary conditions.", ui_style="font-size:12px; line-height:1.5; color:var(--fg-muted);"),
            Div("r ≥ 0 is enforced for axisymmetric geometry. Nested regions must be strictly contained or disjoint.", ui_style="font-size:11px; color:var(--fg-muted); line-height:1.45;"),
        ]

    def _parameter_properties(self):
        children = [_section_title("Parameters", "Named values you can reuse throughout this model.")]
        parameter_map = {item["name"]: item for item in self.model.get("parameters", [])}
        if not parameter_map:
            children.append(Div(
                "No parameters yet. Add a named value such as coil_radius, then reuse it in dimensions, material properties, sources, or study frequencies.",
                ui_style="font-size:12px; line-height:1.5; color:var(--fg-muted); padding:4px 0 8px;",
            ))
        for parameter in self.model.get("parameters", []):
            try:
                value = f"{evaluate_expression(parameter['expression'], parameter_map):.6g}"
                evaluated_color = "var(--fg-muted);"
            except (ValueError, TypeError, SyntaxError, OverflowError):
                value = "Invalid"
                evaluated_color = "var(--negative);"
            selected = parameter["id"] == self.selected_parameter_id
            children.append(Div(
                Div(
                    _input("Name", parameter["name"], lambda event, pid=parameter["id"]: self._set_parameter(pid, "name", event.value)),
                    _input("Unit label", parameter.get("unit", ""), lambda event, pid=parameter["id"]: self._set_parameter(pid, "unit", event.value)),
                    ui_style="display:grid; grid-template-columns:minmax(0,1fr) minmax(70px,.45fr); gap:7px;",
                ),
                _input("Expression", parameter["expression"], lambda event, pid=parameter["id"]: self._set_parameter(pid, "expression", event.value)),
                Div(
                    Div("Evaluated value", ui_style="font-size:10px; color:var(--fg-muted);"),
                    Div(f"{value} {parameter.get('unit', '')}".strip(), ui_style=f"font-size:12px; font-weight:600; color:{evaluated_color}"),
                    ui_style="min-width:0;",
                ),
                _button(
                    "Remove parameter",
                    "mdi-delete-outline",
                    lambda *a, pid=parameter["id"]: self.remove_parameter(pid),
                    color="negative",
                    style="align-self:flex-start;",
                ),
                ui_style=(
                    "display:flex; flex-direction:column; gap:8px; padding:9px; border:1px solid "
                    + ("var(--primary);" if selected else "var(--border);")
                    + " border-radius:6px;"
                ),
            ))
        children.extend([
            _button("Add parameter", "mdi-plus", self.add_parameter, color="primary"),
            Div(
                "Expressions are numeric. The unit label documents the value but does not convert it. Geometry dimensions use mm; for example, if coil_radius is stored in m, enter coil_radius*1000 for a radius in mm.",
                ui_style="font-size:11px; line-height:1.45; color:var(--fg-muted);",
            ),
        ])
        return children

    def _material_properties(self):
        children = [_section_title("Materials", "Define material values once, then assign them to regions.")]
        if not self.model["materials"]:
            return children + [
                Div("Choose Air or Copper from a region's Material list, or add a custom material here.", ui_style="font-size:12px; color:var(--fg-muted); line-height:1.45;"),
                _button("Add material", "mdi-plus", self.add_material),
            ]
        valid_ids = {item["id"] for item in self.model["materials"]}
        if self.selected_material_id not in valid_ids:
            self.selected_material_id = self.model["materials"][0]["id"]
        children.append(Div(
            Div("Material", ui_style="font-size:10px; font-weight:700; color:var(--fg-muted);"),
            Div("Assigned regions", ui_style="font-size:10px; font-weight:700; color:var(--fg-muted); text-align:right;"),
            ui_style="display:grid; grid-template-columns:1fr 110px; gap:8px; padding:4px 7px;",
        ))
        for material in self.model["materials"]:
            assigned = sum(region.get("material_id") == material["id"] for region in self.model["geometry"]["regions"])
            row = _button(
                f"{material['name']}  ·  {assigned} assigned",
                "mdi-cube-outline",
                lambda *a, mid=material["id"]: self.select_material(mid),
                color="primary" if material["id"] == self.selected_material_id else None,
                style="width:100%; justify-content:flex-start; text-align:left;",
                align="left",
            )
            children.append(row)
        material = next(item for item in self.model["materials"] if item["id"] == self.selected_material_id)
        properties = material.get("properties", {})
        children.extend([
            Div(ui_style="height:1px; background:var(--border); margin:5px 0;"),
            _input("Material name", material["name"], lambda event, mid=material["id"]: self._set_material_name(mid, event.value)),
            Div("ELECTROMAGNETIC", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:4px;"),
            _input("Relative permeability", properties.get("relative_permeability", ""), lambda event, mid=material["id"]: self._set_material_property(mid, "relative_permeability", event.value)),
            _input("Electrical conductivity", properties.get("electrical_conductivity", ""), lambda event, mid=material["id"]: self._set_material_property(mid, "electrical_conductivity", event.value), suffix="S/m"),
            Div("MECHANICAL", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:4px;"),
            _input("Young's modulus", properties.get("youngs_modulus", ""), lambda event, mid=material["id"]: self._set_material_property(mid, "youngs_modulus", event.value), suffix="Pa"),
            _input("Poisson's ratio", properties.get("poissons_ratio", ""), lambda event, mid=material["id"]: self._set_material_property(mid, "poissons_ratio", event.value)),
            _input("Density", properties.get("density", ""), lambda event, mid=material["id"]: self._set_material_property(mid, "density", event.value), suffix="kg/m³"),
            _button("Add material", "mdi-plus", self.add_material),
        ])
        children.append(_button("Remove material", "mdi-delete-outline", lambda *a, mid=material["id"]: self.remove_material(mid)))
        return children

    def _boundary_properties(self):
        children = [_section_title("Boundary conditions", "Name a condition once, then assign it to edges in the sketch.")]
        kinds = [
            ("axis_of_symmetry", "Axis of symmetry"),
            ("magnetic_potential_zero", "Magnetic potential = 0"),
            ("natural", "Natural / free boundary"),
            ("mechanical_fixed", "Fixed displacement"),
            ("mechanical_prescribed", "Prescribed displacement"),
            ("mechanical_traction", "Vector traction"),
            ("mechanical_robin", "Robin support"),
        ]
        conditions = self.model.get("boundary_conditions", [])
        valid_ids = {item["id"] for item in conditions}
        if self.selected_boundary_id not in valid_ids:
            self.selected_boundary_id = conditions[0]["id"] if conditions else None
        if conditions:
            children.append(Div("Condition definitions · assigned edges", ui_style="font-size:10px; font-weight:700; color:var(--fg-muted); padding:3px 6px;"))
        else:
            children.append(Div("No boundary conditions yet. Add a condition, then assign it to one or more sketch edges.", ui_style="font-size:12px; color:var(--fg-muted); line-height:1.45;"))
        for condition in conditions:
            assigned = sum(condition["id"] in _edge_condition_ids(edge) for edge in self.model["geometry"].get("edges", []))
            children.append(_button(
                f"{condition['name']}  ·  {assigned} edges",
                "mdi-vector-link",
                lambda *a, bid=condition["id"]: self.select_boundary(bid),
                color="primary" if condition["id"] == self.selected_boundary_id else None,
                style="width:100%; justify-content:flex-start; text-align:left;",
                align="left",
            ))
        condition = next((item for item in conditions if item["id"] == self.selected_boundary_id), None)
        if condition:
            type_select = QSelect(
                ui_label="Condition type",
                ui_options=[{"label": label, "value": value} for value, label in kinds],
                ui_option_label="label",
                ui_option_value="value",
                ui_model_value=condition.get("type"),
                ui_emit_value=True,
                ui_map_options=True,
                ui_dense=True,
                ui_filled=True,
            )
            type_select.on_update_model_value(lambda event, bid=condition["id"]: self._set_boundary_value(bid, "type", event.value))
            children.extend([
                Div(ui_style="height:1px; background:var(--border); margin:5px 0;"),
                _input("Condition name", condition["name"], lambda event, bid=condition["id"]: self._set_boundary_value(bid, "name", event.value)),
                type_select,
            ])
            if condition.get("type") == "mechanical_prescribed":
                children.extend([
                    _input("Radial displacement", condition.get("displacement_r", "0"), lambda event, bid=condition["id"]: self._set_boundary_value(bid, "displacement_r", event.value), suffix="m"),
                    _input("Axial displacement", condition.get("displacement_z", "0"), lambda event, bid=condition["id"]: self._set_boundary_value(bid, "displacement_z", event.value), suffix="m"),
                ])
            elif condition.get("type") == "mechanical_traction":
                children.extend([
                    _input("Radial traction", condition.get("traction_r", "0"), lambda event, bid=condition["id"]: self._set_boundary_value(bid, "traction_r", event.value), suffix="N/m²"),
                    _input("Axial traction", condition.get("traction_z", "0"), lambda event, bid=condition["id"]: self._set_boundary_value(bid, "traction_z", event.value), suffix="N/m²"),
                ])
            elif condition.get("type") == "mechanical_robin":
                children.extend([
                    _input("Normal stiffness", condition.get("stiffness_normal", "0"), lambda event, bid=condition["id"]: self._set_boundary_value(bid, "stiffness_normal", event.value), suffix="N/m³"),
                    _input("Tangential stiffness", condition.get("stiffness_tangential", "0"), lambda event, bid=condition["id"]: self._set_boundary_value(bid, "stiffness_tangential", event.value), suffix="N/m³"),
                ])
            children.append(Div("Highlighted orange edges use this condition. Select edges in Geometry to assign or change their conditions.", ui_style="font-size:11px; line-height:1.45; color:var(--fg-muted);"))
            children.append(_button("Remove condition", "mdi-delete-outline", lambda *a, bid=condition["id"]: self.remove_boundary(bid)))
        children.append(_button("Add boundary condition", "mdi-plus", self.add_boundary))
        return children

    def _physics_properties(self):
        physics = self.model["physics"]
        children = [_section_title("Physics & coupling", "Choose which equations the study will solve.")]
        options = [
            ("dc_magnetic", "Static magnetic field", "DC magnetic solution"),
            ("harmonic_electromagnetic", "Time-harmonic EM", "Complex-valued EM field"),
            ("mechanics", "Time-harmonic mechanics", "Elastic displacement response"),
            ("coupling", "EM-mechanical coupling", "Coupled electromagnetic and mechanical solve"),
        ]
        for key, label, hint in options:
            toggle = QCheckbox(ui_model_value=bool(physics[key].get("enabled")), ui_label=label, ui_dense=True)
            toggle.on_update_model_value(lambda event, selected=key: self._set_physics(selected, "enabled", bool(event.value)))
            children.append(Div(toggle, Div(hint, ui_style="font-size:11px; color:var(--fg-muted); margin-left:30px; margin-top:-5px;"), ui_style="padding:7px 0; border-bottom:1px solid var(--border);"))
        children.extend([
            Div("Set frequency points and sweep values in Study.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted); padding-top:8px;"),
            _button("Configure study frequencies", "mdi-play-box-outline", lambda *a: self.select_section("studies")),
            Div("Regional current densities and mechanical body forces are configured separately under Sources & loads.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
        ])
        children.append(Div("Coupling requires DC magnetic, time-harmonic EM and mechanics to be enabled.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted); padding-top:5px;"))
        return children

    def _sources_properties(self):
        children = [_section_title("Sources & loads", "Assign current densities and body forces by region.")]
        regions = self.model["geometry"]["regions"]
        selected_region = next((item for item in regions if item["id"] == self.selected_region_id), None)
        if regions:
            region_select = QSelect(
                ui_label="Region sources apply to",
                ui_options=[{"label": item["name"], "value": item["id"]} for item in regions],
                ui_option_label="label",
                ui_option_value="value",
                ui_model_value=selected_region["id"] if selected_region else regions[0]["id"],
                ui_emit_value=True,
                ui_map_options=True,
                ui_dense=True,
                ui_filled=True,
            )
            region_select.on_update_model_value(lambda event: self.select_source_region(event.value))
            source_region = selected_region or regions[0]
            sources = source_region.setdefault("sources", {})
            body_force = sources.setdefault("mechanical_body_force", {"r": "0", "z": "0"})
            children.extend([
                region_select,
                _input("DC current density", sources.get("dc_current_density", "0"), lambda event, rid=source_region["id"]: self._set_region_source(rid, "dc_current_density", event.value), suffix="A/m²"),
                _input("AC current density (real)", sources.get("ac_current_density_real", "0"), lambda event, rid=source_region["id"]: self._set_region_source(rid, "ac_current_density_real", event.value), suffix="A/m²"),
                _input("AC current density (imaginary)", sources.get("ac_current_density_imaginary", "0"), lambda event, rid=source_region["id"]: self._set_region_source(rid, "ac_current_density_imaginary", event.value), suffix="A/m²"),
                Div("MECHANICAL BODY FORCE", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:4px;"),
                _input("Radial body force", body_force.get("r", "0"), lambda event, rid=source_region["id"]: self._set_region_body_force(rid, "r", event.value), suffix="N/m³"),
                _input("Axial body force", body_force.get("z", "0"), lambda event, rid=source_region["id"]: self._set_region_body_force(rid, "z", event.value), suffix="N/m³"),
            ])
        else:
            children.extend([
                Div("Create a geometry region before setting regional sources.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
                _button("Open Geometry", "mdi-vector-square-outline", lambda *a: self.select_section("geometry")),
            ])
        return children

    def _mesh_properties(self):
        mesh = self.model["mesh"]
        order = QSelect(ui_label="Polynomial order", ui_options=[1, 2, 3, 4, 5, 6], ui_model_value=mesh["polynomial_order"], ui_dense=True, ui_filled=True)
        order.on_update_model_value(lambda event: self._set_mesh("polynomial_order", int(event.value)))
        return [
            _section_title("Mesh", "Control mesh density and approximation order."),
            _input("Target element size", mesh["element_size"], lambda event: self._set_mesh("element_size", event.value), suffix="m"),
            order,
            Div("The sketch must contain valid, non-overlapping regions before mesh generation.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
        ]

    def _solver_properties(self):
        solver = self.model["solver"]
        anderson = QCheckbox(ui_label="Use Anderson acceleration", ui_model_value=solver["anderson"], ui_dense=True)
        anderson.on_update_model_value(lambda event: self._set_solver("anderson", bool(event.value)))
        depth = QInput(ui_label="Anderson depth", ui_type="number", ui_model_value=solver["anderson_depth"], ui_dense=True, ui_filled=True)
        depth.on_update_model_value(lambda event: self._set_solver("anderson_depth", int(event.value)))
        beta = QInput(ui_label="Anderson relaxation", ui_type="number", ui_model_value=solver["anderson_beta"], ui_dense=True, ui_filled=True)
        beta.on_update_model_value(lambda event: self._set_solver("anderson_beta", float(event.value)))
        tolerance = QInput(ui_label="Relative tolerance", ui_type="number", ui_model_value=solver["relative_tolerance"], ui_dense=True, ui_filled=True)
        tolerance.on_update_model_value(lambda event: self._set_solver("relative_tolerance", float(event.value)))
        iterations = QInput(ui_label="Maximum iterations", ui_type="number", ui_model_value=solver["maximum_iterations"], ui_dense=True, ui_filled=True)
        iterations.on_update_model_value(lambda event: self._set_solver("maximum_iterations", int(event.value)))
        return [
            _section_title("Solver settings", "Controls for nonlinear and fixed-point iterations."),
            Div("Nonlinear / fixed-point solver", ui_style="font-weight:600; font-size:12px; padding-top:6px;"),
            anderson,
            depth,
            beta,
            tolerance,
            iterations,
            Div("Direct sparse linear solver", ui_style="font-size:12px; padding:5px 0;"),
            Div("The axisymmetric solver currently uses its sparse direct backend.", ui_style="font-size:11px; color:var(--fg-muted); line-height:1.4;"),
        ]

    def _study_properties(self):
        studies = self.studies.get("studies", [])
        children = [_section_title("Study", "One DC + time-harmonic study; enter a single frequency or a comma-separated sweep.")]
        for study in studies:
            children.append(Div(
                _input("Study name", study.get("name", ""), lambda event, sid=study["id"]: self._set_study(sid, "name", event.value)),
                _input("Frequency points (comma separated)", ", ".join(str(v) for v in study.get("frequency_hz", [])), lambda event, sid=study["id"]: self._set_study_frequencies(sid, event.value), suffix="Hz"),
                ui_style="display:flex; flex-direction:column; gap:7px; padding:8px; border:1px solid var(--border); border-radius:6px;",
            ))
        children.append(Div("Transient and time-integration studies are not supported in this workflow yet.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"))
        return children

    def _render_canvas(self):
        return self.render_canvas()

    def _canvas_projection(self):
        plot = self._canvas_plot_bounds()
        center, world_width, world_height = self._current_canvas_world_view()
        rmin, rmax = center[0] - world_width / 2, center[0] + world_width / 2
        zmin, zmax = center[1] - world_height / 2, center[1] + world_height / 2

        def xy(point):
            r, z = point
            x = plot[0] + (r - rmin) / (rmax - rmin) * (plot[2] - plot[0])
            y = plot[3] - (z - zmin) / (zmax - zmin) * (plot[3] - plot[1])
            return x, y

        def rz(point):
            x, y = point
            r = rmin + (x - plot[0]) / (plot[2] - plot[0]) * (rmax - rmin)
            z = zmin + (plot[3] - y) / (plot[3] - plot[1]) * (zmax - zmin)
            return r, z

        return xy, rz

    @staticmethod
    def _coordinate_step(raw_step):
        if not math.isfinite(raw_step) or raw_step <= 0:
            return 1.0
        power = 10 ** math.floor(math.log10(raw_step))
        fraction = raw_step / power
        multiplier = 1 if fraction <= 1 else 2 if fraction <= 2 else 5 if fraction <= 5 else 10
        return multiplier * power

    @classmethod
    def _coordinate_ticks(cls, minimum, maximum, target_count=10):
        span = maximum - minimum
        if not math.isfinite(span) or span <= 0:
            return []
        raw_step = span / max(target_count, 1)
        step = cls._coordinate_step(raw_step)
        return cls._ticks_for_step(minimum, maximum, step)

    @staticmethod
    def _ticks_for_step(minimum, maximum, step):
        span = maximum - minimum
        if not all(math.isfinite(value) for value in (minimum, maximum, step)) or span <= 0 or step <= 0:
            return []
        first = math.ceil((minimum - step * 1e-10) / step) * step
        count = min(200, max(0, int(math.floor((maximum - first) / step)) + 1))
        return [first + index * step for index in range(count)]

    def _canvas_grid_step(self):
        _, world_width, _ = self._current_canvas_world_view()
        plot = self._canvas_plot_bounds()
        return self._coordinate_step(world_width * 80.0 / (plot[2] - plot[0]))

    def _snap_canvas_point(self, point):
        if not self.snap_to_grid:
            return point
        project, unproject = self._canvas_projection()
        radial, axial = unproject(point)
        step = self._canvas_grid_step()
        radial = max(0.0, round(radial / step) * step)
        axial = round(axial / step) * step
        return project((radial, axial))

    def _schedule_canvas_interaction_reset(self):
        """Clear temporary SVG transforms after the committed scene is patched."""
        try:
            self.js.eval(
                "(() => { const reset = window.__ngsolveResetSketchPreview; "
                "if (!reset) return; "
                "requestAnimationFrame(() => requestAnimationFrame(reset)); })()"
            )
        except Exception:
            # Standalone tests and non-browser frontends do not expose JS.
            pass

    def render_canvas(self):
        width, height = 900, 640
        plot = self._canvas_plot_bounds()
        xy, unproject = self._canvas_projection()
        regions = self.model["geometry"]["regions"]
        lower_left = unproject((plot[0], plot[3]))
        upper_right = unproject((plot[2], plot[1]))
        grid_step = self._canvas_grid_step()
        radial_ticks = self._ticks_for_step(max(0.0, lower_left[0]), max(0.0, upper_right[0]), grid_step)
        axial_ticks = self._ticks_for_step(lower_left[1], upper_right[1], grid_step)
        grid_text_style = "pointer-events:none; user-select:none; -webkit-user-select:none;"
        grid_children = []
        scene_children = []
        for value in radial_ticks:
            if abs(value) <= grid_step * 1e-10:
                continue
            x = xy((value, 0))[0]
            grid_children.append(_svg("line", x1=x, y1=plot[1], x2=x, y2=plot[3], stroke="var(--border, #d9dfe7)", stroke_width="1"))
            grid_children.append(_svg("text", x=x, y=plot[3] + 18, fill="var(--fg-muted, #697586)", font_size="10", text_anchor="middle", style=grid_text_style, children=f"{value:.5g}"))
        for value in axial_ticks:
            if abs(value) <= grid_step * 1e-10:
                continue
            y = xy((0, value))[1]
            grid_children.append(_svg("line", x1=plot[0], y1=y, x2=plot[2], y2=y, stroke="var(--border, #d9dfe7)", stroke_width="1"))
            grid_children.append(_svg("text", x=plot[0] - 8, y=y + 4, fill="var(--fg-muted, #697586)", font_size="10", text_anchor="end", style=grid_text_style, children=f"{value:.5g}"))
        if lower_left[0] <= 0 <= upper_right[0]:
            xaxis = xy((0, 0))[0]
            grid_children.append(_svg("line", x1=xaxis, y1=plot[1], x2=xaxis, y2=plot[3], stroke="#557187", stroke_width="2", stroke_dasharray="6 4"))
            grid_children.append(_svg("text", x=xaxis + 7, y=plot[1] + 15, fill="#405e75", font_size="12", font_weight="600", style=grid_text_style, children="Axis of rotation  ·  r = 0"))
        if lower_left[1] <= 0 <= upper_right[1]:
            zaxis = xy((0, 0))[1]
            grid_children.append(_svg("line", x1=plot[0], y1=zaxis, x2=plot[2], y2=zaxis, stroke="#557187", stroke_width="1.5"))
            grid_children.append(_svg("text", x=plot[2] - 4, y=zaxis - 5, fill="#405e75", font_size="10", text_anchor="end", style=grid_text_style, children="z = 0"))
        grid_children.append(_svg("text", x=plot[2], y=height - 8, fill="var(--fg-muted, #697586)", font_size="12", text_anchor="end", style=grid_text_style, children="r  [m]"))
        grid_children.append(_svg("text", x=12, y=plot[1] - 8, fill="var(--fg-muted, #697586)", font_size="12", style=grid_text_style, children="z  [m]"))

        material_index = {item["id"]: index for index, item in enumerate(self.model["materials"])}
        for region in regions:
            polygon = " ".join(f"{x:.3f},{y:.3f}" for x, y in (xy(point) for point in region["vertices"]))
            color = _MATERIAL_COLORS[material_index.get(region.get("material_id"), 0) % len(_MATERIAL_COLORS)]
            selected = region["id"] == self.selected_region_id
            shape = _svg(
                "polygon",
                points=polygon,
                fill=color,
                fill_opacity="0.33" if not selected else "0.48",
                stroke="#344658" if not selected else "#1b73ae",
                stroke_width="2.2" if selected else "1.6",
                data_sketch_region_id=region["id"],
                tabindex="0",
                style="cursor:pointer;",
            )
            shape.on("click", lambda event, rid=region["id"]: self.select_region(rid))
            scene_children.append(shape)

        region_for_edge = {
            edge_id: region["id"]
            for region in regions
            for edge_id in region.get("edge_ids", [])
        }
        for edge in self.model["geometry"].get("edges", []):
            start, end = edge["vertices"]
            x1, y1 = xy(start)
            x2, y2 = xy(end)
            selected = edge["id"] in self.selected_edge_ids
            edge_color = "#0877b9" if selected else "#263746"
            region_id = region_for_edge.get(edge["id"])
            visible = _svg("line", x1=x1, y1=y1, x2=x2, y2=y2, stroke=edge_color, stroke_width="2.2" if selected else "1.6", data_sketch_edge_id=edge["id"], data_sketch_region_id=region_id, style="pointer-events:none;")
            hit = _svg("line", x1=x1, y1=y1, x2=x2, y2=y2, stroke="transparent", stroke_width="12", data_sketch_region_id=region_id, style="cursor:pointer; pointer-events:stroke;")
            hit.on("click", lambda event, eid=edge["id"]: self.select_edge(eid, additive=bool((getattr(event, "value", None) or {}).get("shiftKey", False))))
            scene_children.extend([visible, hit])

        if not regions:
            scene_children.append(_svg("text", x=width / 2, y=height / 2 - 8, text_anchor="middle", fill="var(--fg-muted, #697586)", font_size="16", children="Choose Rectangle or Circle in the toolbar to begin"))
            scene_children.append(_svg("text", x=width / 2, y=height / 2 + 18, text_anchor="middle", fill="var(--fg-muted, #697586)", font_size="12", children="Drag blank space to pan; hold Shift while dragging to select edges"))
        self._canvas_grid.ui_children = grid_children
        self._canvas_scene.ui_children = scene_children
        self._render_canvas_dimensions()
        self._sync_canvas_interaction_settings()
        self._schedule_canvas_interaction_reset()

    def _build_3d_preview_component(self):
        """Revolve the current meridian regions and show them without editing tools."""
        from netgen.occ import Axis, Compound, Face, MakePolygon, OCCGeometry, Pnt, Revolve, Vec, Vertex
        from ngapp.components import WebgpuComponent
        from ngsolve_webgpu import GeometryRenderer
        from webgpu import CoordinateAxes, NavigationCube

        regions = self.model.get("geometry", {}).get("regions", [])
        material_names = {
            material.get("id"): material.get("name", "")
            for material in self.model.get("materials", [])
        }
        revolved = {}
        for region in regions:
            vertices = region.get("vertices", [])
            if len(vertices) < 3:
                raise ValueError(f"Region '{region.get('name', 'unnamed')}' needs at least three vertices.")
            profile_vertices = [
                Vertex(Pnt(float(radius), 0.0, float(axial)))
                for radius, axial in vertices
            ]
            profile_vertices.append(profile_vertices[0])
            profile = Face(MakePolygon(profile_vertices))
            revolved[region["id"]] = Revolve(
                profile,
                Axis(Pnt(0.0, 0.0, 0.0), Vec(0.0, 0.0, 1.0)),
                360.0,
            )

        # A nested 2D region denotes a separate material domain. Cut its
        # immediate children from the parent before combining the solids so
        # that both the outer boundary and the inner domain can be seen.
        preview_solids = []
        for region in regions:
            solid = revolved[region["id"]]
            for child in regions:
                if child.get("parent_id") == region["id"]:
                    solid = solid - revolved[child["id"]]
            solid = solid.mat(material_names.get(region.get("material_id")) or region.get("name", "Region"))
            preview_solids.append(solid)

        renderers = []
        if preview_solids:
            import numpy as np

            geometry = OCCGeometry(Compound(preview_solids))
            renderer = GeometryRenderer(geometry)
            renderer.faces.active = True
            renderer.edges.active = True

            # Parent domains enclose their child materials. Keep those faces
            # translucent so the inner regions are visible from outside.
            material_index = {
                material.get("id"): index
                for index, material in enumerate(self.model.get("materials", []))
            }
            parent_ids = {region.get("parent_id") for region in regions}
            face_color_by_hash = {}
            for region, solid in zip(regions, preview_solids):
                palette = _MATERIAL_COLORS[
                    material_index.get(region.get("material_id"), 0) % len(_MATERIAL_COLORS)
                ]
                rgb = tuple(int(palette[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
                alpha = 0.20 if region["id"] in parent_ids else 0.92
                for face in solid.faces:
                    face_color_by_hash[hash(face)] = (*rgb, alpha)
            faces = list(geometry.faces)
            face_colors = np.tile(np.array([0.42, 0.55, 0.68, 0.20], dtype=np.float32), len(faces))
            for face_index, face in enumerate(faces):
                color = face_color_by_hash.get(hash(face))
                if color is not None:
                    face_colors[4 * face_index:4 * face_index + 4] = color
            renderer.faces.set_colors(face_colors)
            renderers.append(renderer)
        axes = CoordinateAxes()
        navigation_cube = NavigationCube()
        renderers.extend([axes, navigation_cube])

        preview = WebgpuComponent(
            ui_style="display:block; flex:1 1 auto; width:100%; height:100%; min-width:0; min-height:0;",
        )
        preview.ui_class = "fit"
        preview.on_mounted(lambda *_: self._resize_3d_preview(preview))
        scene = preview.draw(renderers)
        if preview_solids:
            scene.options.camera.reset(*scene.bounding_box)
            scene.render()
        return preview

    @staticmethod
    def _resize_3d_preview(preview):
        """Resize after the canvas has entered the visible flex viewport."""
        try:
            if preview.canvas is not None:
                preview.canvas.resize()
            if preview.scene is not None:
                preview.scene.render()
        except Exception:
            # Browser resize/render can race component mount in some frontends.
            pass

    def toggle_3d_preview(self, *args):
        """Toggle between the editable meridian canvas and read-only 3D view."""
        if self._preview_3d_active:
            self._preview_3d_active = False
            self._canvas_host.ui_children = [self._canvas]
            self._preview_3d_component = None
            self._preview_3d_button.ui_label = "Preview in 3D"
            self._preview_3d_button.ui_icon = "mdi-cube-scan"
            self._preview_3d_button.ui_color = None
            self._preview_3d_button.ui_flat = True
            self._preview_3d_tooltip.ui_children = ["Preview the axisymmetric regions revolved into 3D."]
            for control in self._sketch_view_controls:
                control.ui_hidden = False
            self.render_canvas()
            self._message("Returned to the editable meridian sketch.")
            return

        try:
            preview = self._build_3d_preview_component()
        except Exception as error:
            self._message(f"Could not create the 3D preview: {error}", error=True)
            return

        self._preview_3d_component = preview
        self._canvas_host.ui_children = [preview]
        self._preview_3d_active = True
        self._preview_3d_button.ui_icon = "mdi-cube-outline"
        self._preview_3d_button.ui_color = "primary"
        self._preview_3d_button.ui_flat = False
        self._preview_3d_tooltip.ui_children = ["Return to the editable meridian sketch."]
        for control in self._sketch_view_controls:
            control.ui_hidden = True
        self._message("3D preview: left-drag to rotate, middle-drag or Shift+left-drag to pan, scroll to zoom.")

    def _set_region_value(self, region_id, key, value):
        region = next((item for item in self.model["geometry"]["regions"] if item["id"] == region_id), None)
        if region is None:
            return
        if key == "material_id" and value:
            material = next((item for item in self.model["materials"] if item["id"] == value), None)
            if material is None:
                material = next((item for item in builtin_materials() if item["id"] == value), None)
                if material is None:
                    self._message("Choose a material from the available list.", error=True)
                    return
                self.model["materials"].append(material)
        region[key] = str(value).strip() if key == "name" else value
        self._refresh_model_tree()
        if key == "material_id":
            self._render_inspector()
        self.render_canvas()

    def _set_region_dimension(self, region_id, key, value):
        region = next((item for item in self.model["geometry"]["regions"] if item["id"] == region_id), None)
        if region is None:
            return
        old_shape = copy.deepcopy(region["shape"])
        old_vertices = copy.deepcopy(region["vertices"])
        old_parents = {item["id"]: item.get("parent_id") for item in self.model["geometry"]["regions"]}
        try:
            expression = str(value).strip()
            parameters = {item["name"]: item for item in self.model.get("parameters", [])}
            dimension = evaluate_expression(expression, parameters) / 1000
            if not math.isfinite(dimension) or dimension <= 0 and key not in {"r_min", "z_min", "r_center", "z_center"}:
                raise ValueError("Dimensions must be finite and positive.")
            shape = region["shape"]
            shape[key] = dimension
            shape.setdefault("dimension_expressions", {})[key] = expression
            if shape["type"] == "rectangle":
                r0, z0, width, height = shape["r_min"], shape["z_min"], shape["width"], shape["height"]
                if r0 < 0 or width <= 0 or height <= 0:
                    raise ValueError("Radius must be non-negative and dimensions positive.")
                region["vertices"] = [[r0, z0], [r0 + width, z0], [r0 + width, z0 + height], [r0, z0 + height]]
            else:
                radius, rcenter, zcenter = shape["radius"], shape["r_center"], shape["z_center"]
                if radius <= 0 or rcenter < radius:
                    raise ValueError("Circle must remain at r ≥ 0.")
                region["vertices"] = [
                    [rcenter + radius * math.cos(2 * math.pi * i / 48), zcenter + radius * math.sin(2 * math.pi * i / 48)]
                    for i in range(48)
                ]
            self._recompute_parent_links()
            self._rebuild_edges()
            self._message("Updated sketch dimension.")
        except (ValueError, TypeError, OverflowError) as error:
            region["shape"] = old_shape
            region["vertices"] = old_vertices
            for item in self.model["geometry"]["regions"]:
                item["parent_id"] = old_parents[item["id"]]
            self._message(str(error), error=True)
        self.render_canvas()

    def _set_edge_name(self, edge_id, value):
        edge = next((item for item in self.model["geometry"]["edges"] if item["id"] == edge_id), None)
        if edge:
            edge["name"] = str(value).strip()

    def _set_edge_condition(self, edge_id, physics, value):
        self._set_edge_conditions([edge_id], physics, value)

    def _set_edge_conditions(self, edge_ids, physics, value):
        selected = set(edge_ids)
        edges = [edge for edge in self.model["geometry"]["edges"] if edge["id"] in selected]
        if not edges:
            return
        conditions = {item["id"]: item for item in self.model.get("boundary_conditions", [])}
        condition = conditions.get(value) if value else None
        if value and condition is None:
            return
        if condition:
            kind = condition.get("type", "")
            is_mechanical = str(kind).startswith("mechanical_")
            if is_mechanical != (physics == "mechanical"):
                self._message("Choose a condition for the selected physics.", error=True)
                return
            if physics == "mechanical" and any(
                all(abs(float(point[0])) <= 1e-12 for point in edge.get("vertices", []))
                for edge in edges
            ):
                self._message("Mechanical support conditions on the r=0 axis are not supported yet.", error=True)
                return
        for edge in edges:
            condition_ids = [
                condition_id for condition_id in _edge_condition_ids(edge)
                if conditions.get(condition_id, {}).get("type", "").startswith("mechanical_") != (physics == "mechanical")
            ]
            if value:
                condition_ids.append(value)
            edge["boundary_condition_ids"] = condition_ids
        self._rebuild_edges()
        self._refresh_model_tree()
        self._render_inspector()
        self.render_canvas()

    def delete_region(self, region_id):
        region = next((item for item in self.model["geometry"]["regions"] if item["id"] == region_id), None)
        if region is None:
            return
        for child in self.model["geometry"]["regions"]:
            if child.get("parent_id") == region_id:
                child["parent_id"] = region.get("parent_id")
        self.model["geometry"]["regions"] = [item for item in self.model["geometry"]["regions"] if item["id"] != region_id]
        self.selected_region_id = None
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self._recompute_parent_links()
        self._rebuild_edges()
        self._message(f"Deleted {region['name']}.")
        self._refresh_model_tree()
        self._render_inspector()
        self.render_canvas()

    def add_parameter(self, *args):
        index = len(self.model["parameters"]) + 1
        parameter = {"id": new_id("parameter"), "name": f"length_{index}", "expression": "0.01", "unit": "m"}
        self.model["parameters"].append(parameter)
        self.selected_parameter_id = parameter["id"]
        self._message("Added a parameter. Its unit label is descriptive; values are not converted automatically.")
        self._refresh_model_tree()
        self._render_inspector()

    def remove_parameter(self, parameter_id):
        parameter = next((item for item in self.model["parameters"] if item["id"] == parameter_id), None)
        if parameter is None:
            return
        references = self._parameter_reference_locations(parameter)
        if references:
            shown = "; ".join(references[:3])
            if len(references) > 3:
                shown += f"; and {len(references) - 3} more"
            message = f"Cannot remove '{parameter['name']}'; it is used by {shown}. Update those expressions first."
            self._show_validation_issues([message])
            self._message(message, error=True)
            return
        self.model["parameters"] = [item for item in self.model["parameters"] if item["id"] != parameter_id]
        if self.selected_parameter_id == parameter_id:
            self.selected_parameter_id = None
        self._refresh_model_tree()
        self._render_inspector()
        self._message(f"Removed parameter '{parameter['name']}'.")

    def _parameter_reference_locations(self, parameter):
        name = parameter.get("name", "")
        if not name:
            return []
        token = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
        references = []

        def check(label, expression):
            if isinstance(expression, str) and token.search(expression):
                references.append(label)

        for other in self.model.get("parameters", []):
            if other.get("id") != parameter.get("id"):
                check(f"parameter '{other.get('name', 'unnamed')}'", other.get("expression"))
        for region in self.model.get("geometry", {}).get("regions", []):
            shape = region.get("shape", {})
            for dimension, expression in shape.get("dimension_expressions", {}).items():
                check(f"region '{region.get('name', 'unnamed')}' {dimension}", expression)
            sources = region.get("sources", {})
            for key in ("dc_current_density", "ac_current_density_real", "ac_current_density_imaginary"):
                check(f"region '{region.get('name', 'unnamed')}' {key.replace('_', ' ')}", sources.get(key))
            for component, expression in sources.get("mechanical_body_force", {}).items():
                check(f"region '{region.get('name', 'unnamed')}' {component} body force", expression)
        for material in self.model.get("materials", []):
            for key, expression in material.get("properties", {}).items():
                check(f"material '{material.get('name', 'unnamed')}' {key.replace('_', ' ')}", expression)
        for condition in self.model.get("boundary_conditions", []):
            for key in ("displacement_r", "displacement_z", "traction_r", "traction_z", "stiffness_normal", "stiffness_tangential"):
                check(f"boundary '{condition.get('name', 'unnamed')}' {key.replace('_', ' ')}", condition.get(key))
        for study in self.studies.get("studies", []):
            points = study.get("frequency_hz", [])
            if isinstance(points, str):
                points = [part.strip() for part in points.split(",") if part.strip()]
            for index, expression in enumerate(points, start=1):
                check(f"study '{study.get('name', 'unnamed')}' frequency {index}", expression)
        check("default harmonic frequency", self.model.get("physics", {}).get("harmonic_electromagnetic", {}).get("frequency_hz"))
        check("target mesh size", self.model.get("mesh", {}).get("element_size"))
        return list(dict.fromkeys(references))

    def _set_parameter(self, parameter_id, key, value):
        parameter = next((item for item in self.model["parameters"] if item["id"] == parameter_id), None)
        if parameter:
            parameter[key] = str(value).strip()
            self._refresh_expression_geometry()

    def _refresh_expression_geometry(self):
        old_geometry = copy.deepcopy(self.model["geometry"])
        parameters = {item["name"]: item for item in self.model.get("parameters", [])}
        try:
            for region in self.model["geometry"]["regions"]:
                shape = region.get("shape", {})
                expressions = shape.get("dimension_expressions", {})
                for key, expression in expressions.items():
                    shape[key] = evaluate_expression(expression, parameters) / 1000
                if shape.get("type") == "rectangle":
                    r0, z0, width, height = (float(shape[key]) for key in ("r_min", "z_min", "width", "height"))
                    if r0 < 0 or width <= 0 or height <= 0:
                        raise ValueError(f"Region '{region['name']}' dimensions must keep r ≥ 0 and have positive width and height.")
                    region["vertices"] = [[r0, z0], [r0 + width, z0], [r0 + width, z0 + height], [r0, z0 + height]]
                elif shape.get("type") == "circle":
                    radius, rcenter, zcenter = (float(shape[key]) for key in ("radius", "r_center", "z_center"))
                    if radius <= 0 or rcenter < radius:
                        raise ValueError(f"Region '{region['name']}' must have positive radius and stay at r ≥ 0.")
                    region["vertices"] = [
                        [rcenter + radius * math.cos(2 * math.pi * index / 48), zcenter + radius * math.sin(2 * math.pi * index / 48)]
                        for index in range(48)
                    ]
            self._recompute_parent_links()
            self._rebuild_edges()
            self._refresh_model_tree()
            self._render_inspector()
            self.render_canvas()
            self._message("Updated geometry from parameter expressions.")
        except (ValueError, TypeError, OverflowError) as error:
            self.model["geometry"] = old_geometry
            self._rebuild_edges()
            self._render_inspector()
            self._message(f"Geometry was not updated: {error}", error=True)

    def add_material(self, *args):
        index = len(self.model["materials"]) + 1
        material = builtin_materials()[0]
        material.update({"id": new_id("material"), "name": f"Material {index}"})
        self.model["materials"].append(material)
        self.selected_material_id = material["id"]
        self._message(f"Added {material['name']}.")
        self._refresh_model_tree()
        self._render_inspector()

    def remove_material(self, material_id):
        if any(region.get("material_id") == material_id for region in self.model["geometry"]["regions"]):
            self._message("This material is assigned to a region. Reassign the region before deleting it.", error=True)
            return
        self.model["materials"] = [item for item in self.model["materials"] if item["id"] != material_id]
        if self.selected_material_id == material_id:
            self.selected_material_id = self.model["materials"][0]["id"] if self.model["materials"] else None
        self._refresh_model_tree()
        self._render_inspector()

    def _set_material_name(self, material_id, value):
        material = next((item for item in self.model["materials"] if item["id"] == material_id), None)
        if material:
            material["name"] = str(value).strip()
            self._refresh_model_tree()

    def _set_material_property(self, material_id, key, value):
        material = next((item for item in self.model["materials"] if item["id"] == material_id), None)
        if material:
            material.setdefault("properties", {})[key] = str(value).strip()

    def add_boundary(self, *args):
        index = len(self.model.get("boundary_conditions", [])) + 1
        condition = {"id": new_id("boundary"), "name": f"Boundary {index}", "type": "natural"}
        self.model.setdefault("boundary_conditions", []).append(condition)
        self.selected_boundary_id = condition["id"]
        self._selected_condition_id = condition["id"]
        self.active_section = "boundaries"
        self.layout["active_section"] = self.active_section
        self._refresh_model_tree()
        self._render_inspector()
        self._sync_canvas_selection()

    def remove_boundary(self, boundary_id):
        if any(boundary_id in _edge_condition_ids(edge) for edge in self.model["geometry"].get("edges", [])):
            self._message("This condition is assigned to an edge. Reassign the edge before deleting it.", error=True)
            return
        self.model["boundary_conditions"] = [item for item in self.model.get("boundary_conditions", []) if item["id"] != boundary_id]
        if self.selected_boundary_id == boundary_id:
            self.selected_boundary_id = self.model["boundary_conditions"][0]["id"] if self.model["boundary_conditions"] else None
        if self._selected_condition_id == boundary_id:
            self._selected_condition_id = None
        self._refresh_model_tree()
        self._render_inspector()
        self._sync_canvas_selection()

    def _set_boundary_value(self, boundary_id, key, value):
        condition = next((item for item in self.model.get("boundary_conditions", []) if item["id"] == boundary_id), None)
        if condition:
            condition[key] = str(value).strip() if key != "type" else value
            self._refresh_model_tree()
            if key == "type":
                self._render_inspector()
                self._sync_canvas_selection()

    def _set_region_source(self, region_id, key, value):
        region = next((item for item in self.model["geometry"]["regions"] if item["id"] == region_id), None)
        if region:
            region.setdefault("sources", {})[key] = str(value).strip()

    def _set_region_body_force(self, region_id, component, value):
        region = next((item for item in self.model["geometry"]["regions"] if item["id"] == region_id), None)
        if region:
            region.setdefault("sources", {}).setdefault("mechanical_body_force", {"r": "0", "z": "0"})[component] = str(value).strip()

    def _set_physics(self, physics, key, value):
        self.model["physics"].setdefault(physics, {})[key] = value

    def _set_mesh(self, key, value):
        self.model["mesh"][key] = value
        self._refresh_model_tree()

    def _set_solver(self, key, value):
        self.model["solver"][key] = value

    def _set_study(self, study_id, key, value):
        study = next((item for item in self.studies["studies"] if item["id"] == study_id), None)
        if study:
            study[key] = str(value).strip()

    def _set_study_frequencies(self, study_id, value):
        study = next((item for item in self.studies["studies"] if item["id"] == study_id), None)
        if study:
            study["frequency_hz"] = [part.strip() for part in str(value).split(",") if part.strip()]

    def validation_errors(self):
        errors = validate_model(self.model)
        if not self.model["geometry"]["regions"]:
            errors.append("Create at least one material region.")
        physics = self.model.get("physics", {})
        if not any(item.get("enabled") for item in physics.values() if isinstance(item, dict)):
            errors.append("Select at least one physics problem.")
        mechanics = physics.get("mechanics", {}).get("enabled", False)
        harmonic = physics.get("harmonic_electromagnetic", {}).get("enabled", False)
        coupling = physics.get("coupling", {}).get("enabled", False)
        electromagnetic = physics.get("dc_magnetic", {}).get("enabled", False) or harmonic
        if electromagnetic:
            condition_types = {
                condition.get("id"): condition.get("type")
                for condition in self.model.get("boundary_conditions", [])
                if isinstance(condition, dict)
            }
            has_magnetic_reference = any(
                condition_types.get(condition_id) == "magnetic_potential_zero"
                for edge in self.model["geometry"].get("edges", [])
                for condition_id in _edge_condition_ids(edge)
            )
            if not has_magnetic_reference:
                errors.append("Assign magnetic potential = 0 to at least one exterior edge to anchor the electromagnetic solution.")
        if mechanics and not harmonic:
            errors.append("Time-harmonic mechanics currently requires Time-harmonic EM.")
        mechanical_regions = [region for region in self.model["geometry"]["regions"] if region.get("mechanical")]
        if mechanics and not mechanical_regions:
            errors.append("Choose at least one region to include in mechanics.")
        if coupling and not (physics.get("dc_magnetic", {}).get("enabled") and harmonic and mechanics):
            errors.append("Coupling requires DC magnetic, time-harmonic EM, and mechanics.")
        if mechanics and not any(
            bool(set(_edge_condition_ids(edge)) & {item.get("id") for item in self.model.get("boundary_conditions", []) if item.get("type") in {"mechanical_fixed", "mechanical_prescribed"}})
            for edge in self.model["geometry"].get("edges", [])
        ):
            errors.append("Assign a fixed or prescribed mechanical boundary condition to at least one edge.")
        parameter_map = {
            item.get("name"): item
            for item in self.model.get("parameters", [])
            if isinstance(item, dict) and item.get("name")
        }
        errors.extend(validate_studies(self.studies, parameter_map))
        return errors

    def mesh_validation_errors(self):
        errors = validate_model(self.model)
        if not self.model["geometry"].get("regions"):
            errors.append("Create at least one material region before generating a mesh.")
        return errors

    def validate_action(self, *args):
        errors = self.validation_errors()
        self._show_validation_issues(errors)
        if errors:
            self._message(f"Setup check found {len(errors)} issue{'s' if len(errors) != 1 else ''}. Select a message for guidance.", error=True)
        else:
            self._message("Setup checks passed. Mesh and solver results are reported separately.")
        self._flash_status_bar()

    def run_mesh_action(self, *args):
        errors = self.mesh_validation_errors()
        if errors:
            self._show_validation_issues(errors)
            self._message(f"Mesh setup found {len(errors)} issue{'s' if len(errors) != 1 else ''}.", error=True)
            return
        if self.on_mesh:
            self._mesh_button.ui_loading = True
            self.on_mesh()
        else:
            self._message("Mesh generation is unavailable in this environment.", error=True)

    def run_study_action(self, *args):
        errors = self.validation_errors()
        if errors:
            self._show_validation_issues(errors)
            self._message(f"Study setup found {len(errors)} issue{'s' if len(errors) != 1 else ''}.", error=True)
            return
        if self.on_run:
            self._run_button.ui_loading = True
            self.show_log()
            self.on_run()
        else:
            self._message("The solver is unavailable in this environment.", error=True)

    def finish_solver_job(self, message, error=False, *, run_kind=None, output_path=None, run_name=None):
        self._mesh_button.ui_loading = False
        self._run_button.ui_loading = False
        self._message(message, error=error)
        if run_kind:
            self.runs.append({
                "name": run_name or run_kind,
                "kind": run_kind,
                "status": "Failed" if error else "Complete",
                "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
                "output_path": str(output_path or ""),
            })
            self._refresh_model_tree()
            if self.active_section == "runs":
                self._render_inspector()


def _bounds(points):
    return min(p[0] for p in points), max(p[0] for p in points), min(p[1] for p in points), max(p[1] for p in points)


def _point_in_box(point, box):
    left, right, top, bottom = box
    return left <= point[0] <= right and top <= point[1] <= bottom


def _segment_intersects_box(start, end, box):
    left, right, top, bottom = box
    corners = ((left, top), (right, top), (right, bottom), (left, bottom))
    if _point_in_box(start, box) or _point_in_box(end, box):
        return True
    return any(
        _segments_intersect(start, end, corners[index], corners[(index + 1) % 4])
        for index in range(4)
    )


def _polygon_area(points):
    return 0.5 * sum(points[i][0] * points[(i + 1) % len(points)][1] - points[(i + 1) % len(points)][0] * points[i][1] for i in range(len(points)))


def _point_in_polygon(point, polygon):
    x, y = point
    inside = False
    j = len(polygon) - 1
    for i, (xi, yi) in enumerate(polygon):
        xj, yj = polygon[j]
        if (yi > y) != (yj > y):
            xcross = (xj - xi) * (y - yi) / ((yj - yi) or 1e-300) + xi
            if x < xcross:
                inside = not inside
        j = i
    return inside


def _point_on_segment(point, start, end, tolerance=1e-12):
    px, py = point
    ax, ay = start
    bx, by = end
    cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax)
    scale = max(abs(bx - ax), abs(by - ay), 1.0)
    if abs(cross) > tolerance * scale:
        return False
    return (
        min(ax, bx) - tolerance <= px <= max(ax, bx) + tolerance
        and min(ay, by) - tolerance <= py <= max(ay, by) + tolerance
    )


def _point_in_or_on_polygon(point, polygon):
    return _point_in_polygon(point, polygon) or any(
        _point_on_segment(point, polygon[index], polygon[(index + 1) % len(polygon)])
        for index in range(len(polygon))
    )


def _segments_intersect(a, b, c, d, tolerance=1e-12):
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    if ((o1 > tolerance and o2 < -tolerance) or (o1 < -tolerance and o2 > tolerance)) and ((o3 > tolerance and o4 < -tolerance) or (o3 < -tolerance and o4 > tolerance)):
        return True
    return (
        (abs(o1) <= tolerance and _point_on_segment(c, a, b, tolerance))
        or (abs(o2) <= tolerance and _point_on_segment(d, a, b, tolerance))
        or (abs(o3) <= tolerance and _point_on_segment(a, c, d, tolerance))
        or (abs(o4) <= tolerance and _point_on_segment(b, c, d, tolerance))
    )


def _strictly_contains(outer, inner):
    if len(outer) < 3 or len(inner) < 3:
        return False
    if not all(_point_in_polygon(point, outer) for point in inner):
        return False
    return not any(
        _segments_intersect(outer[i], outer[(i + 1) % len(outer)], inner[j], inner[(j + 1) % len(inner)])
        for i in range(len(outer))
        for j in range(len(inner))
    )


def _polygons_overlap(first, second):
    a, b = _bounds(first), _bounds(second)
    if a[1] < b[0] or b[1] < a[0] or a[3] < b[2] or b[3] < a[2]:
        return False
    if any(_point_in_or_on_polygon(point, second) for point in first):
        return True
    if any(_point_in_or_on_polygon(point, first) for point in second):
        return True
    return any(
        _segments_intersect(first[i], first[(i + 1) % len(first)], second[j], second[(j + 1) % len(second)])
        for i in range(len(first))
        for j in range(len(second))
    )


def _edge_key(points):
    coords = [tuple(round(float(value), 12) for value in point) for point in points]
    return tuple(sorted(coords))


def _edge_condition_ids(edge):
    values = edge.get("boundary_condition_ids")
    if values is None:
        legacy = edge.get("boundary_condition_id")
        return [legacy] if legacy else []
    if isinstance(values, str):
        return [values]
    return [value for value in values if value]
