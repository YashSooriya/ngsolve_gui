"""Axisymmetric model authoring workspace."""

from __future__ import annotations

import copy
from functools import wraps
import json
import math
import os
import re
import time
from contextlib import contextmanager
from decimal import Decimal, ROUND_HALF_UP
from threading import Lock, Timer, get_ident
from pathlib import Path

from ngapp.components import (
    Component,
    Div,
    QBtn,
    QBtnToggle,
    QCheckbox,
    QDialog,
    QInput,
    QSelect,
    QSeparator,
    QTooltip,
    QCard,
    QCardSection,
    QSplitter,
    QResizeObserver,
    QIcon,
    QList,
    QItem,
    QItemSection,
    QMenu,
)
from ngapp.utils import EnvironmentType, get_environment

from . import cerbsim_style as cb
from .axisymmetric_model import (
    builtin_materials,
    evaluate_expression,
    evaluate_sketch_entity,
    sample_sketch_entity,
    find_closed_sketch_loops,
    migrate_legacy_model,
    migrate_regional_loads,
    migrate_legacy_studies,
    new_id,
    new_model,
    new_studies,
    validate_model,
    validate_studies,
)
from .units import UNIT_OPTIONS
from .material_library import MaterialLibrary


_SECTIONS = [
    ("geometry", "Geometry", "mdi-vector-square"),
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
    ("MODEL", ("geometry", "materials", "physics", "boundaries", "sources")),
    ("ANALYSIS", ("mesh", "studies", "solver")),
    ("RESULTS", ("runs",)),
)

_FREQUENCY_RESULT_SUFFIX = re.compile(r"_\d+_([0-9.eE+-]+)Hz$", re.IGNORECASE)
_HIDDEN_RUN_HISTORY_FILE = ".mm_fem_hidden_run_history.json"
_CANVAS_GRID_SUBDIVISIONS = 5
_PREVIEW_VISIBILITY_ALPHA = {
    "opaque": 1.0,
    "translucent": 0.38,
    "hidden": 0.0,
}


def _run_history_path_key(path):
    """Return a stable key for a run output path across GUI restarts."""
    return os.path.normcase(str(Path(path).expanduser().resolve()))


def _load_hidden_run_history(root):
    try:
        data = json.loads((Path(root) / _HIDDEN_RUN_HISTORY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    paths = data.get("hidden_output_paths", []) if isinstance(data, dict) else []
    return {
        _run_history_path_key(path)
        for path in paths
        if isinstance(path, str) and path.strip()
    }


def _save_hidden_run_history(root, paths):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    destination = root / _HIDDEN_RUN_HISTORY_FILE
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"schema": 1, "hidden_output_paths": sorted(paths)}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)


def default_run_history_root():
    """Return the output root shared by run creation and run-history discovery."""
    configured_root = os.environ.get("COUPLED_SOLVER_OUTPUT_DIR", "").strip()
    if configured_root:
        return Path(configured_root).expanduser() / "gui_runs"
    return Path.home() / "NGSolve_results"


def _load_saved_run_history(root=None, limit=40):
    """Recover prior GUI runs from their manifests or legacy output folders."""
    root = Path(root) if root is not None else default_run_history_root()
    try:
        directories = [path for path in root.iterdir() if path.is_dir()]
        directories.sort(key=lambda path: path.stat().st_mtime)
    except OSError:
        return []

    hidden_paths = _load_hidden_run_history(root)
    runs = []
    for directory in directories[-max(1, int(limit)):]:
        if _run_history_path_key(directory) in hidden_paths:
            continue
        manifest_path = directory / "run_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            manifest = None
        if isinstance(manifest, dict) and manifest.get("schema") == "mm-fem.run":
            kind = str(manifest.get("kind", "study")).title()
            status = str(manifest.get("status", "complete")).title()
            if status == "Running":
                status = "Interrupted"
            name = str(manifest.get("run_name") or f"{kind}: {manifest.get('model_name', directory.name)}")
            finished = str(manifest.get("finished_utc") or manifest.get("created_utc") or "")
        else:
            has_project = any(directory.glob("*.ngsmodel"))
            fields = list((directory / "ngsolve_gui" / "fields").glob("*.pkl")) if (directory / "ngsolve_gui" / "fields").is_dir() else []
            has_mesh = (directory / "mesh.vol").is_file() or (directory / "mesh_preview.json").is_file()
            if not (has_project or fields or has_mesh):
                continue
            kind = "Study" if fields else "Mesh"
            status = "Complete" if fields or has_mesh else "Interrupted"
            name = f"{kind}: {directory.name.rsplit('_', 1)[0]}"
            finished = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(directory.stat().st_mtime))
        runs.append({
            "id": _run_history_path_key(directory),
            "name": name,
            "kind": kind,
            "status": status,
            "finished": finished,
            "output_path": str(directory),
        })
    return runs

_MATERIAL_COLORS = ["#6886ac", "#d58651", "#69a77b", "#ae8bb7", "#d1b44f", "#4cabb0"]


def _history_tracked(method):
    """Record one model snapshot around a top-level user edit."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        depth = getattr(self, "_history_transaction_depth", 0)
        if depth:
            self._history_transaction_depth = depth + 1
            try:
                return method(self, *args, **kwargs)
            finally:
                self._history_transaction_depth -= 1

        before = self._capture_history_state()
        self._history_transaction_depth = 1
        failed = False
        try:
            return method(self, *args, **kwargs)
        except Exception:
            failed = True
            raise
        finally:
            self._history_transaction_depth = 0
            if not failed:
                self._finish_history_transaction(before)

    return wrapped


def _round_sketch_m(value):
    """Round mouse-derived geometry to 0.01 mm, represented in metres."""
    return float(Decimal(str(value)).quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP))


def _mesh_signature(model):
    """Describe only model inputs that can change the generated mesh."""
    geometry = model.get("geometry", {}) if isinstance(model, dict) else {}
    regions = []
    for region in geometry.get("regions", []) if isinstance(geometry, dict) else []:
        if not isinstance(region, dict):
            regions.append(repr(region))
            continue
        shape = region.get("shape", {})
        if not isinstance(shape, dict):
            shape = {}
        shape_type = shape.get("type")
        dimension_keys = {
            "rectangle": ("r_min", "z_min", "width", "height"),
            "circle": ("r_center", "z_center", "radius"),
        }.get(shape_type, ())
        regions.append({
            "id": region.get("id"),
            "parent_id": region.get("parent_id"),
            "vertices": region.get("vertices", []),
            "shape": {
                "type": shape_type,
                **{key: shape.get(key) for key in dimension_keys},
            },
        })

    mesh = model.get("mesh", {}) if isinstance(model, dict) else {}
    if not isinstance(mesh, dict):
        mesh = {}
    parameters = {
        item.get("name"): item
        for item in model.get("parameters", [])
        if isinstance(item, dict) and item.get("name")
    } if isinstance(model, dict) else {}
    try:
        element_size = evaluate_expression(mesh.get("element_size", "0.01"), parameters)
    except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        # While a mesh field is invalid or being edited, treat its raw input as
        # part of the signature so a preview cannot appear to match it.
        element_size = {
            "expression": repr(mesh.get("element_size", "0.01")),
            "parameters": sorted(
                (str(name), repr(item.get("expression")))
                for name, item in parameters.items()
            ),
        }
    try:
        region_element_sizes = {
            str(region_id): evaluate_expression(expression, parameters)
            for region_id, expression in mesh.get("region_element_sizes", {}).items()
        }
    except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError):
        region_element_sizes = {
            "expressions": repr(mesh.get("region_element_sizes", {})),
            "parameters": sorted(
                (str(name), repr(item.get("expression")))
                for name, item in parameters.items()
            ),
        }
    payload = {
        "regions": regions,
        "element_size": element_size,
        "polynomial_order": mesh.get("polynomial_order", 3),
        "curve_order": mesh.get("curve_order", mesh.get("polynomial_order", 3)),
        "region_element_sizes": region_element_sizes,
        "hp_layers": mesh.get("hp_layers", 0),
        "hp_grading_factor": mesh.get("hp_grading_factor", 0.3),
        "hp_region_ids": sorted(mesh.get("hp_region_ids", [])),
        "hp_edge_ids": sorted(mesh.get("hp_edge_ids", [])),
    }
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return repr(payload)


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



def _icon_button(icon, tooltip, callback, *, color=None):
    button = QBtn(
        QTooltip(tooltip),
        ui_icon=icon,
        ui_flat=True,
        ui_dense=True,
        ui_no_caps=True,
        ui_color=color,
        ui_size="sm",
        ui_style="flex:0 0 30px; width:30px; min-width:30px; height:30px;",
    )
    button._props["aria-label"] = tooltip
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


def _update_svg_props(component, **props):
    """Patch changed SVG attributes on a mounted component in one update."""
    changed = {}
    for key, value in props.items():
        prop_name = key.replace("_", "-")
        if component._props.get(prop_name) != value:
            changed[prop_name] = value
    if changed:
        component._props.update(changed)
        component._update_frontend({"props": changed})


def _section_title(title, subtitle=None):
    return Div(
        Div(title, ui_style="font-size:12px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;"),
        Div(subtitle, ui_style="font-size:11px; color:var(--fg-muted); margin-top:3px;") if subtitle else Div(),
        ui_style="padding:14px 14px 10px; border-bottom:1px solid var(--border);",
    )


class SolveWorkspace(Div):
    """Editable axisymmetric r-z model with a selectable sketch viewport."""

    def __init__(self, on_log=None, on_run=None, on_mesh=None, on_open_file=None, on_history_change=None, on_cancel=None, material_library=None):
        self.model = new_model()
        self.model.setdefault("region_groups", [])
        self._material_library = material_library or MaterialLibrary()
        self._user_materials = self._material_library.load()
        self.studies = new_studies()
        self._saved_model = copy.deepcopy(self.model)
        self._saved_studies = copy.deepcopy(self.studies)
        self.layout = {"schema_version": 1, "active_section": "geometry", "camera": "fit"}
        self.on_log = on_log
        self.on_run = on_run
        self.on_mesh = on_mesh
        self.on_open_file = on_open_file
        self.on_history_change = on_history_change
        self.on_cancel = on_cancel
        self._job_active = False
        self._undo_history = []
        self._redo_history = []
        self._history_transaction_depth = 0
        self._history_limit = 100
        self.active_section = "geometry"
        self.selected_region_id = None
        self.selected_region_ids = []
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self.message = "Choose a sketch tool in the viewport toolbar to begin. Dimensions use SI metres (m)."
        self.message_is_error = False
        self._log_visible = False
        self._log_messages = ["Axisymmetric model editor ready."]
        self._run_history_root = default_run_history_root()
        self.runs = _load_saved_run_history(self._run_history_root)
        self.mesh_preview_edges = []
        self.mesh_preview_visible = False
        self._mesh_preview_signature = None
        self._canvas_background_visible = True
        self._canvas_geometry_visible = True
        self.selected_material_id = None
        self.selected_boundary_id = None
        self.selected_source_id = None
        self.selected_parameter_id = None
        self._selected_condition_id = None
        self._bulk_material_choice = "__keep__"
        self._bulk_mechanical_choice = "keep"
        self._tree_panel_width = 240
        self._properties_panel_width = 340
        self.snap_to_grid = False
        self._view_center = None
        self._view_world_width = None
        self._canvas_width = 900
        self._canvas_height = 640
        self._inspector_view = None
        self._geometry_inspector_ready = False
        self._tree_entry_buttons = {}
        self._tree_entry_containers = {}
        self._tree_entry_titles = {}
        self._tree_entry_lists = {}
        self._tree_entry_order = {}
        self._tree_structure_initialized = False
        self._messages_visible = False
        self._status_flash_variant = 0
        self.validation_issues = []
        self.sketch_tool = "select"
        self._sketch_tool_variant = {"rectangle": "corner", "circle": "center-radius", "arc": "three-point"}
        self._pending_circle_points = []
        self._canvas_drag = None
        self._screen_to_svg = None
        self._canvas_view_commit_version = 0
        self._canvas_view_render_version = 0
        self._canvas_grid_render_version = 0
        self._canvas_zoom_timer = None
        self._canvas_zoom_timer_lock = Lock()
        self._canvas_zoom_generation = 0
        self._preview_3d_active = False
        self._preview_3d_component = None
        self._preview_3d_scene = None
        self._preview_3d_renderers = {}
        self._preview_region_visibility = {}
        self._preview_visibility_controls = {}
        self._editor_drawer = None
        self.hp_target_selection_active = False

        self._canvas_background = _svg(
            "rect", x=0, y=0, width=self._canvas_width, height=self._canvas_height,
            fill="var(--canvas-bg, #f4f6f8)",
        )
        self._canvas_grid = Component("g")
        self._canvas_live_grid = Component("g")
        self._canvas_grid_layer = Component("g")
        self._canvas_grid_layer.ui_children = [self._canvas_grid, self._canvas_live_grid]
        self._canvas_scene = Component("g")
        self._canvas_mesh_path = _svg(
            "path",
            fill="none",
            stroke="#203c57",
            stroke_width="0.9",
            stroke_opacity="0.82",
            vector_effect="non-scaling-stroke",
            style="pointer-events:none;",
        )
        self._canvas_dimensions = Component("g")
        self._canvas_grid_signature = None
        self._canvas_scene_component_ids = None
        self._canvas_region_nodes = {}
        self._canvas_context_menu = None
        self._canvas_edge_nodes = {}
        self._pending_arc_points = []
        self._pending_line_points = []
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
            _svg(
                "path", id="solve-sketch-drag-spline", d="", display="none",
                fill="none", stroke="#0877b9", stroke_width="2",
                stroke_dasharray="6 4", style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-line-chain-halo", d="",
                fill="none", stroke="#ffffff", stroke_width="7",
                stroke_linecap="round", stroke_linejoin="round",
                style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-line-chain", d="",
                fill="none", stroke="#0877b9", stroke_width="3.2",
                stroke_linecap="round", stroke_linejoin="round",
                style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-drag-line-halo", d="", display="none",
                fill="none", stroke="#ffffff", stroke_width="7",
                stroke_linecap="round", stroke_linejoin="round",
                style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-drag-line", d="", display="none",
                fill="none", stroke="#0877b9", stroke_width="3.2",
                stroke_dasharray="7 4", stroke_linecap="round",
                style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-drag-arc-halo", d="", display="none",
                fill="none", stroke="#ffffff", stroke_width="7",
                stroke_linecap="round", stroke_linejoin="round",
                style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-drag-arc", d="", display="none",
                fill="none", stroke="#0877b9", stroke_width="3.2",
                stroke_dasharray="7 4", stroke_linecap="round",
                stroke_linejoin="round", style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-construction-halo", d="",
                fill="none", stroke="#ffffff", stroke_width="6",
                stroke_linecap="round", style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-construction-points", d="",
                fill="none", stroke="#d04a2b", stroke_width="2.3",
                stroke_linecap="round", style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-drag-trim", d="", display="none",
                fill="none", stroke="#737b85", stroke_width="1.5",
                stroke_linecap="round", stroke_linejoin="round",
                style="pointer-events:none;",
            ),
        )
        self._canvas_inference = Component(
            "g",
            _svg(
                "path", id="solve-sketch-inference-halo", d="", display="none",
                fill="none", stroke="#ffffff", stroke_width="6",
                stroke_linecap="round", stroke_linejoin="round",
                style="pointer-events:none;",
            ),
            _svg(
                "path", id="solve-sketch-inference-marker", d="", display="none",
                fill="none", stroke="#16814a", stroke_width="2.4",
                stroke_linecap="round", stroke_linejoin="round",
                style="pointer-events:none;",
            ),
            ui_style="pointer-events:none;",
        )
        for component, element_id in (
            (self._canvas_grid, "solve-sketch-grid"),
            (self._canvas_live_grid, "solve-sketch-live-grid"),
            (self._canvas_scene, "solve-sketch-scene"),
            (self._canvas_dimensions, "solve-sketch-dimensions"),
            (self._canvas_preview, "solve-sketch-preview"),
            (self._canvas_inference, "solve-sketch-inference"),
        ):
            component._props["id"] = element_id
        self._canvas_live_grid._props.update({
            "display": "none",
            "style": "pointer-events:none;",
        })

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
            self._canvas_grid_layer,
            self._canvas_scene,
            self._canvas_dimensions,
            self._canvas_preview,
            self._canvas_inference,
            ui_class="solve-sketch-canvas",
            ui_style="display:block; width:100%; height:100%; min-height:0; background:var(--canvas-bg, #f4f6f8); cursor:default; user-select:none; -webkit-user-select:none; touch-action:none; overscroll-behavior:contain;",
        )
        self._canvas._props.update({
            "viewBox": "0 0 900 640",
            "preserveAspectRatio": "xMidYMid meet",
            "role": "img",
            "aria-label": "Axisymmetric radial-axial sketch",
            "data-sketch-render-version": "0",
        })
        self._canvas.on_mounted(self._install_canvas_pointer_capture)
        self._canvas.on("mousedown", self._on_canvas_mouse_down)
        self._canvas.on("mouseup", self._on_canvas_mouse_up)
        self._canvas.on("keydown", self._on_canvas_keydown)
        self._canvas._props["tabindex"] = "0"
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
        self._messages_close_button = _button(
            "", "mdi-close", self.hide_messages,
            tooltip="Minimise messages",
            style="margin-left:auto;",
        )
        self._messages_header = Div(
            Div("Messages", ui_style="font-size:12px; font-weight:600;"),
            self._messages_close_button,
            ui_style="display:flex; align-items:center; gap:8px; flex:none; min-height:34px; padding:0 8px 0 14px; border-bottom:1px solid var(--border);",
        )
        self._messages_body = Div(
            ui_style="flex:1 1 auto; min-height:0; overflow:auto; padding:6px 14px 10px;",
        )
        self._messages_panel = Div(
            self._messages_header,
            self._messages_body,
            ui_hidden=True,
            ui_style="display:flex; flex-direction:column; max-height:180px; flex:none; min-height:0; overflow:hidden; border-top:1px solid var(--border); background:var(--surface);",
        )
        self._canvas_resize_observer = QResizeObserver(ui_debounce=80)
        self._canvas_resize_observer.on_resize(self._on_canvas_resize)
        # Mount one context menu on the canvas host so right-clicking a selected
        # region set opens the actions at the pointer, regardless of SVG nesting.
        self._canvas_context_menu = self._region_context_menu()
        self._canvas_host = Div(
            self._canvas,
            self._canvas_resize_observer,
            self._canvas_context_menu,
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
            tooltip="Drag regions to move them; drag empty space to select edges; middle-drag to pan. Hold Ctrl or Shift to select multiple regions, then right-click for group, ungroup, or delete.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        def sketch_tool_group(label, icon, tool, options, tooltip):
            main = _button(
                label, icon,
                lambda *a, active_tool=tool, default_variant=options[0][0]:
                    self.set_sketch_tool(active_tool, variant=default_variant),
                tooltip=tooltip,
                style="flex:0 0 auto; white-space:nowrap; border-top-right-radius:0; border-bottom-right-radius:0;",
            )
            menu_items = []
            for variant, option_label in options:
                option_icons = {
                    "rectangle": {"corner": "mdi-rectangle-outline", "center": "mdi-crosshairs"},
                    "circle": {"center-radius": "mdi-circle-outline", "three-point": "mdi-triangle-outline"},
                    "arc": {
                        "three-point": "mdi-vector-arc",
                        "centerpoint": "mdi-target",
                        "tangent": "mdi-vector-curve",
                        "spline3": "mdi-sine-wave",
                    },
                }
                option_icon = option_icons.get(tool, {}).get(variant, icon)
                item = QItem(
                    QItemSection(QIcon(ui_name=option_icon, ui_size="xs"), ui_avatar=True),
                    QItemSection(option_label),
                    ui_clickable=True, ui_dense=True,
                )

                def choose_variant(event=None, active_tool=tool, selected_variant=variant):
                    # Hide before refreshing the toolbar; refresh can replace the
                    # menu component and leave its old popup visible.
                    menu.ui_hide()
                    self.set_sketch_tool(active_tool, variant=selected_variant)

                item.on_click(choose_variant)
                menu_items.append(item)
            menu = QMenu(
                QList(*menu_items, ui_dense=True),
                ui_anchor="bottom left",
                ui_self="top left",
                ui_auto_close=True,
            )
            arrow = QBtn(
                QTooltip(f"More {label.lower()} options"), menu,
                ui_icon="mdi-menu-down", ui_flat=True, ui_dense=True, ui_no_caps=True,
                ui_class="mmfem-sketch-tool-menu",
                ui_style="min-width:25px; width:25px; padding:0; border-top-left-radius:0; border-bottom-left-radius:0;",
            )
            group = Div(main, arrow, ui_style="display:flex; flex:none; align-items:stretch;")
            return main, group

        rectangle_button, rectangle_group = sketch_tool_group(
            "Rectangle", "mdi-rectangle-outline", "rectangle",
            (("corner", "Corner rectangle"), ("center", "Center rectangle")),
            "Choose corner or center rectangle, then drag to size it.",
        )
        circle_button, circle_group = sketch_tool_group(
            "Circle", "mdi-circle-outline", "circle",
            (("center-radius", "Centerpoint circle"), ("three-point", "3-point circle")),
            "Choose a centerpoint circle by dragging or a 3-point circle by clicking three circumference points.",
        )
        line_button = _button(
            "Line", "mdi-vector-line",
            lambda *a: self.set_sketch_tool("line"),
            tooltip="Draw a straight sketch edge by dragging between endpoints.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        arc_button, arc_group = sketch_tool_group(
            "Arc", "mdi-vector-curve", "arc",
            (("three-point", "3-point arc"), ("centerpoint", "Centerpoint arc"), ("tangent", "Tangent arc"), ("spline3", "Spline")),
            "Choose a 3-point, centerpoint, tangent, or spline arc.",
        )
        trim_button = _button(
            "Trim", "mdi-content-cut",
            lambda *a: self.set_sketch_tool("trim"),
            tooltip="Trim an open line at its intersections with other sketch curves.",
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
        self._canvas_background_button = _button(
            "Hide background", "mdi-grid", self.toggle_canvas_background,
            tooltip="Toggle grid lines, axes, and coordinate labels.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        self._canvas_geometry_button = _button(
            "Hide geometry", "mdi-eye", self.toggle_canvas_geometry,
            tooltip="Toggle region fills, sketch edges, and dimensions. Mesh preview stays independent.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        self._mesh_preview_button = _button(
            "Mesh preview", "mdi-vector-polygon", self.toggle_mesh_preview,
            disable=True,
            tooltip="Generate a mesh before previewing it.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        self._preview_3d_button = _button(
            "Preview in 3D", "mdi-cube-scan", self.toggle_3d_preview,
            tooltip="Preview the axisymmetric regions revolved into 3D.",
            style="flex:0 0 auto; white-space:nowrap;",
        )
        self._preview_3d_tooltip = self._preview_3d_button.ui_slots["default"][0]
        toolbar = Div(
            select_button,
            snap_button,
            rectangle_group,
            circle_group,
            line_button,
            arc_group,
            trim_button,
            sketch_toolbar_separator,
            fit_view_button,
            zoom_out_button,
            zoom_in_button,
            self._canvas_background_button,
            self._canvas_geometry_button,
            self._mesh_preview_button,
            self._preview_3d_button,
            ui_style="display:flex; flex-wrap:wrap; align-items:center; justify-content:flex-start; gap:8px; flex:none; width:100%; min-width:0; min-height:46px; padding:5px 12px; border-bottom:1px solid var(--border); background:var(--surface);",
        )
        self._sketch_toolbar = toolbar
        self._sync_canvas_visibility_buttons()
        self._tool_buttons = {
            "select": select_button,
            "rectangle": rectangle_button,
            "circle": circle_button,
            "line": line_button,
            "arc": arc_button,
            "trim": trim_button,
        }
        self._snap_button = snap_button
        self._sketch_view_controls = [
            select_button, snap_button, rectangle_group, circle_group, line_button, arc_group,
            trim_button, sketch_toolbar_separator, fit_view_button, zoom_out_button,
            zoom_in_button, self._canvas_background_button, self._canvas_geometry_button,
            self._mesh_preview_button,
        ]

        self._preview_3d_viewport = Div(
            ui_class="mmfem-3d-preview-viewport",
            ui_style="display:flex; flex:1 1 auto; width:0; min-width:0; min-height:0; overflow:hidden; background:var(--canvas-bg, #f4f6f8);",
        )
        self._preview_3d_region_rows = Div(
            ui_style="display:flex; flex-direction:column; gap:6px; min-height:0; overflow:auto; padding:8px 12px 14px;",
        )
        region_legend = Div(
            Div(QIcon(ui_name="mdi-eye", ui_color="primary"), "Opaque", ui_style="display:flex; align-items:center; gap:4px;"),
            Div(QIcon(ui_name="mdi-circle-opacity", ui_color="primary"), "Translucent", ui_style="display:flex; align-items:center; gap:4px;"),
            Div(QIcon(ui_name="mdi-eye-off", ui_color="primary"), "Hidden", ui_style="display:flex; align-items:center; gap:4px;"),
            ui_style="display:flex; flex-wrap:wrap; gap:8px 12px; padding:10px 12px; border-bottom:1px solid var(--border); font-size:11px; color:var(--fg-muted);",
        )
        region_panel_header = Div(
            Div("REGIONS", ui_style="font-size:11px; font-weight:700; letter-spacing:.08em; color:var(--fg-muted);"),
            Div("Visibility", ui_style="font-size:11px; font-weight:600; color:var(--fg-muted); text-align:right;"),
            ui_style="display:grid; grid-template-columns:minmax(0, 1fr) max-content; align-items:center; gap:8px; padding:14px 12px 10px; border-bottom:1px solid var(--border);",
        )
        self._preview_3d_region_panel = Div(
            region_panel_header,
            region_legend,
            self._preview_3d_region_rows,
            ui_style="display:flex; flex-direction:column; flex:0 0 310px; width:310px; min-width:260px; min-height:0; overflow:hidden; border-left:1px solid var(--border); background:var(--surface);",
        )
        self._preview_3d_exit_button = QBtn(
            QTooltip("Close the 3D preview and return to the sketch"),
            ui_label="Exit 3D Preview",
            ui_icon="mdi-close",
            ui_color="primary",
            ui_outline=True,
            ui_dense=True,
            ui_no_caps=True,
        )
        self._preview_3d_exit_button.on_click(self.toggle_3d_preview)
        preview_header = Div(
            Div(
                Div("3D Geometry Preview", ui_style="font-size:17px; font-weight:650;"),
                Div("Rotate, pan, and zoom the revolved axisymmetric geometry.", ui_style="font-size:12px; color:var(--fg-muted); margin-top:2px;"),
                ui_style="display:flex; flex-direction:column; min-width:0;",
            ),
            Div(ui_style="flex:1;"),
            self._preview_3d_exit_button,
            ui_style="display:flex; align-items:center; gap:16px; flex:none; min-height:66px; padding:8px 14px 8px 20px; border-bottom:1px solid var(--border); background:var(--surface);",
        )
        preview_body = Div(
            self._preview_3d_viewport,
            self._preview_3d_region_panel,
            ui_style="display:flex; flex:1 1 auto; min-height:0; min-width:0; overflow:hidden;",
        )
        preview_card = QCard(
            preview_header,
            preview_body,
            ui_class="mmfem-3d-preview-window",
            ui_style=(
                "display:flex; flex-direction:column; width:min(1600px, calc(100vw - 48px)); "
                "height:min(920px, calc(100vh - 72px)); max-width:calc(100vw - 48px); "
                "max-height:calc(100vh - 72px); min-width:min(680px, calc(100vw - 24px)); "
                "min-height:min(480px, calc(100vh - 24px)); margin:auto; overflow:hidden; "
                "border:1px solid var(--border); border-radius:14px; "
                "box-shadow:0 24px 90px rgba(10, 22, 38, .32);"
            ),
        )
        self._preview_3d_dialog = QDialog(
            preview_card,
            ui_model_value=False,
            ui_persistent=True,
            ui_maximized=True,
            ui_transition_duration=160,
            ui_class="mmfem-3d-preview-dialog",
        )
        self._canvas_panel = Div(
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
            ui_input_style="font-size:14px; font-weight:600; padding:0; min-width:0; cursor:pointer;",
            ui_style="flex:1 1 auto; width:0; min-width:180px; min-height:30px;",
        )
        self._model_title._props["title"] = "Model name. Double-click to edit or use Rename."
        self._model_title.on("dblclick", self._begin_model_rename)
        self._model_title.on_update_model_value(self._update_model_rename_draft)
        self._model_title.on_blur(self._commit_model_rename)
        self._model_title.on("keydown", self._on_model_rename_keydown)
        self._model_title_input = self._model_title
        self._model_title_rename_button = QBtn(
            QTooltip("Rename this model"),
            ui_label="Rename",
            ui_icon="mdi-pencil-outline",
            ui_color="primary",
            ui_outline=True,
            ui_dense=True,
            ui_no_caps=True,
            ui_size="sm",
        )
        self._model_title_rename_button.on_click(self._begin_model_rename)
        self._model_title_control_style = (
            "display:flex; align-items:center; gap:8px; flex:0 1 520px; "
            "width:min(520px, 100%); min-width:0; min-height:36px; "
            "padding:2px 5px 2px 10px; border:1px solid var(--border); "
            "border-radius:7px; background:var(--surface); "
            "transition:border-color 120ms ease, box-shadow 120ms ease;"
        )
        self._model_title_control_editing_style = (
            "display:flex; align-items:center; gap:8px; flex:0 1 520px; "
            "width:min(520px, 100%); min-width:0; min-height:36px; "
            "padding:2px 5px 2px 10px; border:1px solid var(--primary); "
            "border-radius:7px; background:var(--surface); "
            "box-shadow:0 0 0 2px color-mix(in srgb, var(--primary) 16%, transparent);"
        )
        self._model_title_control = Div(
            self._model_title,
            self._model_title_rename_button,
            ui_class="mmfem-model-title-control",
            ui_style=self._model_title_control_style,
        )
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
        self._cancel_button = _button(
            "Cancel run", "mdi-stop-circle-outline", self._cancel_solver_job,
            color="negative", tooltip="Stop the active mesh or solver job.",
        )
        self._cancel_button.ui_hidden = True

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
            self._model_title_control,
            Div(ui_style="flex:1;"),
            self._validate_button,
            self._mesh_button,
            self._run_button,
            self._cancel_button,
            ui_style="display:flex; align-items:center; gap:8px; flex:none; min-height:40px; padding:4px 12px; border-bottom:1px solid var(--border); background:var(--surface);",
        )
        self._content = Div(
            ui_style="position:relative; display:flex; flex:1 1 auto; min-height:0; min-width:0; overflow:hidden;",
        )
        self._tree_reopen_tab = QBtn(
            QTooltip("Show the model tree"),
            ui_label="Model Tree",
            ui_icon="mdi-chevron-right",
            ui_dense=True,
            ui_no_caps=True,
            ui_flat=True,
            ui_class="mmfem-panel-reopen-tab mmfem-panel-reopen-tab-left",
            ui_style=(
                "position:absolute; top:50%; left:0; z-index:20; transform:translateY(-50%); "
                "width:36px; min-width:36px; height:136px; padding:8px 4px; "
                "writing-mode:vertical-rl; text-orientation:mixed; "
                "border:1px solid var(--border); border-left:0; "
                "border-radius:0 8px 8px 0; background:var(--surface); color:var(--fg); "
                "box-shadow:0 2px 8px rgba(18, 32, 48, .12);"
            ),
        )
        self._tree_reopen_tab._props["aria-label"] = "Show model tree"
        self._tree_reopen_tab.on_click(self._toggle_tree_panel)
        self._tree_reopen_tab.ui_hidden = True
        self._properties_reopen_tab = QBtn(
            QTooltip("Show properties"),
            ui_label="Properties",
            ui_icon="mdi-chevron-left",
            ui_dense=True,
            ui_no_caps=True,
            ui_flat=True,
            ui_class="mmfem-panel-reopen-tab mmfem-panel-reopen-tab-right",
            ui_style=(
                "position:absolute; top:50%; right:0; z-index:20; transform:translateY(-50%); "
                "width:36px; min-width:36px; height:136px; padding:8px 4px; "
                "writing-mode:vertical-rl; text-orientation:mixed; "
                "border:1px solid var(--border); border-right:0; "
                "border-radius:8px 0 0 8px; background:var(--surface); color:var(--fg); "
                "box-shadow:0 2px 8px rgba(18, 32, 48, .12);"
            ),
        )
        self._properties_reopen_tab._props["aria-label"] = "Show properties"
        self._properties_reopen_tab.on_click(self._toggle_properties_panel)
        self._properties_reopen_tab.ui_hidden = True
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
        self._content.ui_children = [
            self._tree_splitter,
            self._tree_reopen_tab,
            self._properties_reopen_tab,
        ]
        super().__init__(
            top,
            toolbar,
            self._content,
            self._messages_panel,
            self._log_panel,
            self._bottom_bar,
            self._rectangle_dialog,
            self._circle_dialog,
            self._preview_3d_dialog,
            ui_style="display:flex; flex-direction:column; width:100%; height:100%; min-height:0; overflow:hidden; background:var(--app-bg, #f7f8fa); color:var(--fg, #202631);",
        )
        self._refresh_model_tree()
        self._render_inspector()
        self._refresh_sketch_tool_buttons()
        self.render_canvas()

    @property
    def dirty(self):
        return (
            self.model != self._saved_model
            or self.studies != self._saved_studies
        )

    def mark_saved(self):
        """Record the current problem definition as the last saved state."""
        self._saved_model = copy.deepcopy(self.model)
        self._saved_studies = copy.deepcopy(self.studies)

    @property
    def can_undo(self):
        return bool(self._undo_history)

    @property
    def can_redo(self):
        return bool(self._redo_history)

    def _capture_history_state(self):
        return {
            "model": copy.deepcopy(self.model),
            "studies": copy.deepcopy(self.studies),
            "selection": {
                "region_id": self.selected_region_id,
                "region_ids": list(getattr(self, "selected_region_ids", [])),
                "edge_id": self.selected_edge_id,
                "edge_ids": list(self.selected_edge_ids),
                "parameter_id": self.selected_parameter_id,
                "material_id": self.selected_material_id,
                "boundary_id": self.selected_boundary_id,
                "active_section": self.active_section,
            },
        }

    def _finish_history_transaction(self, before):
        if before["model"] == self.model and before["studies"] == self.studies:
            return
        if (
            self._mesh_preview_signature is not None
            and _mesh_signature(self.model) != self._mesh_preview_signature
        ):
            self._clear_mesh_preview()
        self._undo_history.append(before)
        del self._undo_history[:-self._history_limit]
        self._redo_history.clear()
        self._notify_history_changed()

    def _notify_history_changed(self):
        if self.on_history_change:
            self.on_history_change()

    def _restore_history_state(self, state):
        self._preserve_canvas_view()
        mesh_preview_signature = self._mesh_preview_signature
        self.model = copy.deepcopy(state["model"])
        self.model.setdefault("region_groups", [])
        self._prune_region_groups()
        self.studies = copy.deepcopy(state["studies"])
        if (
            mesh_preview_signature is not None
            and _mesh_signature(self.model) != mesh_preview_signature
        ):
            self._clear_mesh_preview()
        selection = state["selection"]
        region_ids = {item["id"] for item in self.model["geometry"].get("regions", [])}
        edge_ids = {item["id"] for item in self.model["geometry"].get("edges", [])}
        self.selected_region_id = selection.get("region_id") if selection.get("region_id") in region_ids else None
        self.selected_region_ids = [
            region_id for region_id in selection.get("region_ids", [])
            if region_id in region_ids
        ]
        if self.selected_region_id and self.selected_region_id not in self.selected_region_ids:
            self.selected_region_ids.append(self.selected_region_id)
        self.selected_region_id = self.selected_region_ids[0] if len(self.selected_region_ids) == 1 else None
        self.selected_edge_id = selection.get("edge_id") if selection.get("edge_id") in edge_ids else None
        self.selected_edge_ids = [item for item in selection["edge_ids"] if item in edge_ids]
        self.selected_parameter_id = selection["parameter_id"]
        self.selected_material_id = selection["material_id"]
        self.selected_boundary_id = selection["boundary_id"]
        self._selected_condition_id = self.selected_boundary_id if selection["active_section"] == "boundaries" else None
        if selection["active_section"] in {key for key, _, _ in _SECTIONS}:
            self.active_section = selection["active_section"]
            self.layout["active_section"] = self.active_section
        self._recompute_parent_links()
        with self._batch_frontend_updates():
            self._refresh_model_tree()
            self._render_inspector()
            self.render_canvas()

    def undo(self):
        if not self.can_undo:
            return False
        self._redo_history.append(self._capture_history_state())
        state = self._undo_history.pop()
        self._restore_history_state(state)
        self._message("Undid the last model change.")
        self._notify_history_changed()
        return True

    def redo(self):
        if not self.can_redo:
            return False
        self._undo_history.append(self._capture_history_state())
        del self._undo_history[:-self._history_limit]
        state = self._redo_history.pop()
        self._restore_history_state(state)
        self._message("Redid the model change.")
        self._notify_history_changed()
        return True

    def set_model(self, model, studies=None, layout=None):
        if self._job_active:
            raise RuntimeError("Cannot replace the problem while a mesh or solver job is running.")
        self._cancel_canvas_zoom_render()
        self._clear_mesh_preview()
        model = migrate_legacy_model(model)
        model = migrate_regional_loads(model)
        model = copy.deepcopy(model)
        _sync_axis_boundary_conditions(model)
        _sync_mechanical_boundary_conditions(model)
        studies = migrate_legacy_studies(studies or new_studies())
        errors = validate_model(model, scope="draft")
        if errors:
            raise ValueError("Invalid model document: " + " ".join(errors))
        studies = copy.deepcopy(studies)
        parameter_map = {
            item.get("name"): item
            for item in model.get("parameters", [])
            if isinstance(item, dict) and item.get("name")
        }
        study_errors = validate_studies(studies, parameter_map, physics=model.get("physics"), for_solve=False)
        if study_errors:
            raise ValueError("Invalid studies: " + " ".join(study_errors))
        self._cancel_model_rename()
        self.model = model
        self.model.setdefault("region_groups", [])
        self._prune_region_groups()
        self.studies = studies
        edge_by_id = {
            edge.get("id"): edge
            for edge in self.model.get("geometry", {}).get("edges", [])
            if isinstance(edge, dict) and isinstance(edge.get("id"), str)
        }
        needs_circle_normalization = any(
            isinstance(region, dict)
            and isinstance(region.get("shape"), dict)
            and region["shape"].get("type") == "circle"
            and not (
                isinstance(region.get("edge_ids"), list)
                and len(region["edge_ids"]) == 1
                and isinstance(edge_by_id.get(region["edge_ids"][0]), dict)
                and isinstance(edge_by_id[region["edge_ids"][0]].get("curve"), dict)
                and edge_by_id[region["edge_ids"][0]]["curve"].get("type") == "circle"
            )
            for region in self.model.get("geometry", {}).get("regions", [])
        )
        if needs_circle_normalization:
            self._rebuild_edges()
            normalized_errors = validate_model(self.model, scope="draft")
            if normalized_errors:
                raise ValueError("Invalid normalized model document: " + " ".join(normalized_errors))
        self.mark_saved()
        self._undo_history.clear()
        self._redo_history.clear()
        self._notify_history_changed()
        self.layout = copy.deepcopy(layout or {"schema_version": 1, "active_section": "geometry", "camera": "fit"})
        self._load_canvas_view_from_layout()
        self._editor_drawer = None
        self.hp_target_selection_active = False
        self.selected_material_id = None
        self.selected_boundary_id = None
        self.selected_source_id = None
        self.selected_parameter_id = None
        self._selected_condition_id = None
        requested_section = self.layout.get("active_section", "geometry")
        self.active_section = requested_section if requested_section in {key for key, _, _ in _SECTIONS} else "geometry"
        self.selected_region_id = None
        self.selected_region_ids = []
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
        if section == "sources":
            self.set_sketch_tool("select", announce=False)
        if section != "mesh" and self.hp_target_selection_active:
            self.hp_target_selection_active = False
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
        self._model_title_rename_button.ui_hidden = True
        self._model_title_control.ui_style = self._model_title_control_editing_style
        if get_environment().type == EnvironmentType.LOCAL_APP:
            self._model_title.ui_focus()
            self._model_title.ui_select()

    def _update_model_rename_draft(self, event):
        self._model_title_draft = str(getattr(event, "value", "") or "")

    def _on_model_rename_keydown(self, event):
        payload = getattr(event, "value", None)
        key = payload.get("key") if isinstance(payload, dict) else getattr(payload, "key", None)
        if not key:
            key = getattr(event, "key", None)
        if not key and isinstance(payload, str):
            key = payload
        key = str(key or "").lower()
        if key in {"enter", "numpadenter"}:
            self._commit_model_rename()
        elif key == "escape":
            self._cancel_model_rename()

    @_history_tracked
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
            self._model_title_rename_button.ui_hidden = False
            self._model_title_control.ui_style = self._model_title_control_style

    def _tree_selected_entry(self):
        if self.active_section == "geometry" and self.selected_region_id:
            return f"geometry:{self.selected_region_id}"
        if self.active_section == "sources" and self.selected_source_id:
            return f"sources:{self.selected_source_id}"
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
        self._sync_panel_reopen_tabs()

    def _on_properties_width_change(self, event):
        value = float(event.value)
        if value > 30:
            self._properties_panel_width = value
        self._sync_panel_reopen_tabs()

    def _sync_panel_reopen_tabs(self):
        """Show edge tabs only while their corresponding panel is collapsed."""
        tree_tab = getattr(self, "_tree_reopen_tab", None)
        properties_tab = getattr(self, "_properties_reopen_tab", None)
        if tree_tab is not None:
            tree_tab.ui_hidden = float(self._tree_splitter.ui_model_value) > 30
        if properties_tab is not None:
            properties_tab.ui_hidden = float(self._properties_splitter.ui_model_value) > 30

    def _toggle_tree_panel(self, *_):
        if self._tree_splitter.ui_model_value > 30:
            self._tree_panel_width = float(self._tree_splitter.ui_model_value)
            self._tree_splitter.ui_limits = [0, 420]
            self._tree_splitter.ui_model_value = 0
        else:
            self._tree_splitter.ui_model_value = self._tree_panel_width
            self._tree_splitter.ui_limits = [180, 420]
        self._sync_panel_reopen_tabs()

    def _toggle_properties_panel(self, *_):
        if self._properties_splitter.ui_model_value > 30:
            self._properties_panel_width = float(self._properties_splitter.ui_model_value)
            self._properties_splitter.ui_limits = [0, 500]
            self._properties_splitter.ui_model_value = 0
        else:
            self._properties_splitter.ui_model_value = self._properties_panel_width
            self._properties_splitter.ui_limits = [240, 500]
        self._sync_panel_reopen_tabs()

    def set_sketch_tool(self, tool, *, announce=True, variant=None):
        if tool not in {"select", "rectangle", "circle", "line", "arc", "spline", "trim"}:
            return
        if not hasattr(self, "_sketch_tool_variant"):
            self._sketch_tool_variant = {"rectangle": "corner", "circle": "center-radius", "arc": "three-point"}
        if tool != "arc":
            self._pending_arc_points = []
        if tool != "line":
            self._pending_line_points = []
        if tool != "circle":
            self._pending_circle_points = []
        if variant is not None:
            next_variant = str(variant)
            if self._sketch_tool_variant.get(tool) != next_variant:
                if tool == "arc":
                    self._pending_arc_points = []
                if tool == "circle":
                    self._pending_circle_points = []
            self._sketch_tool_variant[tool] = next_variant
        self.sketch_tool = tool
        self._canvas_drag = None
        self._screen_to_svg = None
        try:
            self.js.eval(
                "if (window.__ngsolveCancelSketchGesture) window.__ngsolveCancelSketchGesture(); "
                f"window.__ngsolveSketchTool = '{tool}'; "
                f"window.__ngsolveSketchToolVariant = '{self._sketch_tool_variant.get(tool, '')}'; "
                "if (window.__ngsolveClearSketchInferenceMarker) window.__ngsolveClearSketchInferenceMarker();"
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
        region_cursor = "pointer" if tool == "select" else "crosshair"
        for shape in self._canvas_region_nodes.values():
            _update_svg_props(shape, style=f"cursor:{region_cursor};")
        for edge_nodes in self._canvas_edge_nodes.values():
            hit_target = edge_nodes[-1]
            _update_svg_props(
                hit_target,
                style=f"cursor:{region_cursor}; pointer-events:stroke;",
            )
        self._refresh_sketch_tool_buttons()
        self._sync_canvas_interaction_settings()
        self._sync_pending_construction_preview()
        if announce:
            variant = self._sketch_tool_variant.get(tool, "")
            help_text = {
                "select": "Drag regions to move them; drag empty space to select edges; middle-drag to pan. Hold Shift to add to the selection.",
                "rectangle": "Drag to draw a center rectangle." if variant == "center" else "Drag between opposite corners to draw a rectangle.",
                "circle": "Click three points on the circle." if variant == "three-point" else "Drag from the circle centre to its radius.",
                "line": "Drag to draw one line and return to Select; click points to chain lines, then click an edge or press Enter/Esc to finish.",
                "arc": {
                    "three-point": "Click the start, click the end, then place a point on the live arc preview.",
                    "centerpoint": "Click the center and start point, then move clockwise or counter-clockwise to preview the fixed-radius arc before placing its end.",
                    "tangent": "Click an existing curve endpoint, then place the end on the live tangent preview.",
                    "spline3": "Click the start and end points, then place the spline control point on the live preview.",
                }.get(variant, "Choose an arc construction method."),
                "spline": "Drag to draw a smooth spline. Closed curve loops become regions automatically.",
                "trim": "Drag the trim pencil across sketch edges to remove the touched curve segments.",
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
                "stepPx": self._canvas_snap_step() * pixels_per_world,
            }
            self.js.eval(f"window.__ngsolveSketchSnap = {json.dumps(grid, allow_nan=False)}")
            live_grid = {
                "width": plot[2] - plot[0],
                "height": plot[3] - plot[1],
                "originX": origin[0],
                "originY": origin[1],
                "pixelsPerWorld": pixels_per_world,
                "majorStep": self._canvas_grid_step(),
                # Keep a generous negative-r gutter so the symmetry axis does
                # not run through the z-axis tick labels when panning to the
                # left limit.
                "negativeRBufferPx": min(96.0, max(72.0, (plot[2] - plot[0]) * 0.08)),
            }
            self.js.eval(
                f"window.__ngsolveSketchLiveGrid = {json.dumps(live_grid, allow_nan=False)}"
            )
            self._sync_pending_construction_preview()
            move_step = 0.00001 * pixels_per_world  # 0.01 mm, represented as 1e-5 m.
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

    def _canvas_plot_bounds(self):
        # The sketch, grid, and axes use the complete SVG viewport. Coordinate
        # labels are drawn just inside its borders instead of reserving a
        # separate inset plot area.
        return (0.0, 0.0, float(self._canvas_width), float(self._canvas_height))

    def _on_canvas_resize(self, event):
        """Reproject the sketch after the available viewport size changes."""
        value = getattr(event, "value", None)
        if not isinstance(value, dict):
            return
        try:
            width = int(round(float(value["width"])))
            height = int(round(float(value["height"])))
        except (KeyError, TypeError, ValueError, OverflowError):
            return
        if width < 100 or height < 100:
            return
        if (width, height) == (self._canvas_width, self._canvas_height):
            return
        self._canvas_width = width
        self._canvas_height = height
        self.render_canvas()

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
        # Start with a narrow negative-r gutter so the rotation axis is not
        # clipped against the viewport edge. Preserve the fitted positive-r
        # extent when adding that gutter.
        plot_width = plot[2] - plot[0]
        buffer_px = min(96.0, max(72.0, plot_width * 0.08))
        buffer_fraction = min(0.2, buffer_px / plot_width)
        fitted_width = width / (1.0 - buffer_fraction)
        center = (fitted_width * (0.5 - buffer_fraction), 0.0)
        return center, fitted_width

    def _current_canvas_world_view(self):
        plot = self._canvas_plot_bounds()
        target_ratio = (plot[2] - plot[0]) / (plot[3] - plot[1])
        if self._view_center is None or self._view_world_width is None:
            center, width = self._fit_canvas_world_view()
        else:
            center, width = self._view_center, self._view_world_width
        return center, width, width / target_ratio

    def _minimum_radial_canvas_center(self, world_width):
        """Keep r=0 just inside the left edge when panning toward negative r."""
        plot = self._canvas_plot_bounds()
        plot_width = plot[2] - plot[0]
        buffer_px = min(96.0, max(72.0, plot_width * 0.08))
        return world_width * (0.5 - buffer_px / plot_width)

    def _preserve_canvas_view(self):
        """Keep geometry edits from changing the camera and rebuilding the grid."""
        if self._view_center is not None and self._view_world_width is not None:
            return
        self._view_center, self._view_world_width, _ = self._current_canvas_world_view()
        self._save_canvas_view_to_layout()

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
        center = (
            max(center[0], self._minimum_radial_canvas_center(new_width)),
            center[1],
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
            max(
                center[0] - dx / pixels_per_world,
                self._minimum_radial_canvas_center(width),
            ),
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

    def _commit_browser_canvas_view(self, view=None, *, render=True, clamp_negative_r=False):
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
        if clamp_negative_r:
            center = (max(center[0], self._minimum_radial_canvas_center(width)), center[1])
        self._prepare_canvas_view_commit_snapshot()
        self._view_center = center
        self._view_world_width = width
        self._save_canvas_view_to_layout()
        if render:
            self.render_canvas()
        return True

    def _on_canvas_wheel(self, event):
        """Commit one settled browser-side wheel gesture, never each tick."""
        view = self._read_browser_canvas_view()
        if view is not None and view[3] > self._canvas_view_commit_version:
            changed = self._commit_browser_canvas_view(
                view, render=False, clamp_negative_r=True
            )
            if changed:
                self._render_canvas_after_interaction()
            else:
                self._schedule_canvas_interaction_reset()
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
  if (window.__ngsolveSketchPointerCaptureVersion === 16) return;
  window.__ngsolveSketchPointerCaptureVersion = 16;
  let active = false;
  let activePointerId = null;
  let dragStart = null;
  let dragStartScreen = null;
  let dragBaseScale = 1;
  let dragBaseTranslateX = 0;
  let dragBaseTranslateY = 0;
  let dragRegionId = null;
  let dragEdgeId = null;
  let dragMode = null;
  let gestureScreenPoints = [];
  let gestureNumber = 0;
  let suppressCanvasClickUntil = 0;
  let previewFrame = 0;
  let pendingPreview = null;
  let wheelCommitTimer = 0;
  let visualScale = 1;
  let visualTranslateX = 0;
  let visualTranslateY = 0;
  let viewVersion = 0;
  let pendingViewResetToken = 0;
  const committedRegionOffsets = new Map();
  window.__ngsolveSketchViewTransform = [1, 0, 0, viewVersion];
  window.__ngsolveSketchCompletedGesture = null;
  const pointerEvents = window.__ngsolveSketchPointerEvents = [];
  const forwardedEvents = new WeakSet();
  const selector = 'svg.solve-sketch-canvas';
  const groupIds = [
    'solve-sketch-scene', 'solve-sketch-dimensions', 'solve-sketch-preview',
  ];
  const pointerSupported = typeof window.PointerEvent === 'function';
  const currentSvg = () => document.querySelector(selector);
  const targetIsSvg = (event, svg) => Boolean(
    svg && event.target && event.target.closest
      && event.target.closest(selector) === svg
  );
  const hideSketchContextMenu = () => {
    const menu = document.querySelector('.solve-sketch-context-menu');
    if (menu) menu.style.display = 'none';
  };
  const openSketchContextMenu = (event) => {
    const svg = currentSvg();
    const menu = document.querySelector('.solve-sketch-context-menu');
    if (!menu || !targetIsSvg(event, svg)) return;
    if (!svg.querySelector('[data-sketch-region-selected="true"]')) return;
    event.preventDefault();
    const host = menu.parentElement;
    if (!host) return;
    menu.style.display = 'block';
    const bounds = host.getBoundingClientRect();
    const maxLeft = Math.max(0, host.clientWidth - menu.offsetWidth);
    const maxTop = Math.max(0, host.clientHeight - menu.offsetHeight);
    menu.style.left = Math.max(0, Math.min(event.clientX - bounds.left, maxLeft)) + 'px';
    menu.style.top = Math.max(0, Math.min(event.clientY - bounds.top, maxTop)) + 'px';
  };
  document.addEventListener('contextmenu', openSketchContextMenu, true);
  document.addEventListener('pointerdown', (event) => {
    if (event.button === 2) {
      openSketchContextMenu(event);
      return;
    }
    const menu = document.querySelector('.solve-sketch-context-menu');
    if (menu && menu.style.display !== 'none' && !menu.contains(event.target)) hideSketchContextMenu();
  }, true);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hideSketchContextMenu();
    const svg = currentSvg();
    const state = window.__ngsolveSketchConstruction || {};
    const target = event.target;
    const editingText = Boolean(target && target.closest
      && target.closest('input, textarea, select, [contenteditable="true"]'));
    const finishLine = window.__ngsolveSketchTool === 'line'
      && (event.key === 'Enter' || event.key === 'Escape')
      && Array.isArray(state.linePoints) && state.linePoints.length;
    const cancelArc = window.__ngsolveSketchTool === 'arc' && event.key === 'Escape';
    if (!svg || editingText || (!finishLine && !cancelArc)) return;
    // Route these shortcuts through the same component event bridge as pointer
    // release. Synthetic KeyboardEvents do not reliably cross the WebView
    // component boundary on Windows Remote Desktop.
    const pointer = window.__ngsolveSketchPointer || [0, 0];
    window.__ngsolveSketchCompletedGesture = [
      finishLine ? 'line_finish' : 'arc_cancel',
      finishLine ? 'line' : 'arc',
      0, 0, 0, 0, '', false, false,
      visualScale, visualTranslateX, visualTranslateY, viewVersion,
      '', [], window.__ngsolveSketchToolVariant || '', event.key.toLowerCase(),
    ];
    svg.dispatchEvent(new MouseEvent('mouseup', {
      bubbles: true, cancelable: true, button: 0,
      clientX: pointer[0], clientY: pointer[1],
    }));
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);
  document.addEventListener('click', (event) => {
    const menu = document.querySelector('.solve-sketch-context-menu');
    if (menu && menu.contains(event.target)) window.setTimeout(hideSketchContextMenu, 0);
  });
  const gridCacheFor = (liveGrid) => {
    let cache = liveGrid.__ngsolveSketchGridCache;
    if (cache && cache.host === liveGrid && cache.nodes.every(node => node.isConnected)) return cache;
    liveGrid.replaceChildren();
    const ns = 'http://www.w3.org/2000/svg';
    const path = (stroke, width, dash = null) => {
      const node = document.createElementNS(ns, 'path');
      node.setAttribute('fill', 'none');
      node.setAttribute('stroke', stroke);
      node.setAttribute('stroke-width', String(width));
      node.setAttribute('pointer-events', 'none');
      if (dash) node.setAttribute('stroke-dasharray', dash);
      liveGrid.appendChild(node);
      return node;
    };
    const text = (size, anchor = null, fill = 'var(--fg-muted, #697586)', weight = null) => {
      const node = document.createElementNS(ns, 'text');
      node.setAttribute('font-size', String(size));
      node.setAttribute('fill', fill);
      node.setAttribute('pointer-events', 'none');
      node.setAttribute('style', 'user-select:none; -webkit-user-select:none;');
      if (anchor) node.setAttribute('text-anchor', anchor);
      if (weight) node.setAttribute('font-weight', weight);
      liveGrid.appendChild(node);
      return node;
    };
    cache = {
      host: liveGrid,
      minor: path('var(--grid-minor, #e8edf2)', 0.75),
      major: path('var(--border, #d9dfe7)', 1),
      axisX: path('#557187', 2, '6 4'),
      axisY: path('#557187', 1.5),
      radialLabels: [],
      axialLabels: [],
      rotationLabel: text(12, null, '#405e75', 600),
      zeroLabel: text(12, 'end', '#405e75'),
      radialUnit: text(12, 'end'),
      axialUnit: text(12),
      nodes: [],
    };
    cache.nodes = [
      cache.minor, cache.major, cache.axisX, cache.axisY,
      cache.rotationLabel, cache.zeroLabel, cache.radialUnit, cache.axialUnit,
    ];
    liveGrid.__ngsolveSketchGridCache = cache;
    return cache;
  };
  const setSvgValue = (node, name, value) => {
    const text = String(value);
    if (node.getAttribute(name) !== text) node.setAttribute(name, text);
  };
  const setGridLabel = (node, x, y, value, visible = true) => {
    setSvgValue(node, 'x', Number(x.toFixed(2)));
    setSvgValue(node, 'y', Number(y.toFixed(2)));
    if (node.textContent !== value) node.textContent = value;
    const display = visible ? 'inline' : 'none';
    if (node.getAttribute('display') !== display) node.setAttribute('display', display);
  };
  const pooledLabel = (cache, pool, index, anchor) => {
    while (pool.length <= index) {
      const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      label.setAttribute('font-size', '16');
      label.setAttribute('fill', 'var(--fg-muted, #697586)');
      label.setAttribute('pointer-events', 'none');
      label.setAttribute('style', 'user-select:none; -webkit-user-select:none;');
      label.setAttribute('text-anchor', anchor);
      cache.host.appendChild(label);
      pool.push(label);
      cache.nodes.push(label);
    }
    return pool[index];
  };
  const drawLiveGrid = (svg, scale, tx, ty) => {
    const settings = window.__ngsolveSketchLiveGrid;
    const staticGrid = svg && svg.querySelector('#solve-sketch-grid');
    const liveGrid = svg && svg.querySelector('#solve-sketch-live-grid');
    if (!settings || !staticGrid || !liveGrid) return;
    if (staticGrid.getAttribute('visibility') !== 'hidden') staticGrid.setAttribute('visibility', 'hidden');
    if (liveGrid.getAttribute('display') !== 'inline') liveGrid.setAttribute('display', 'inline');

    const width = settings.width;
    const height = settings.height;
    const pixelsPerWorld = settings.pixelsPerWorld * scale;
    if (!(width > 0 && height > 0 && pixelsPerWorld > 0)
        || !Number.isFinite(pixelsPerWorld)) return;
    const rawStep = 80 / pixelsPerWorld;
    const power = 10 ** Math.floor(Math.log10(rawStep));
    const fraction = rawStep / power;
    const majorStep = (fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10) * power;
    const minorStep = majorStep / 5;
    const minorPixels = minorStep * pixelsPerWorld;
    if (!(minorPixels > 0) || !Number.isFinite(minorPixels)) return;
    const axisX = settings.originX * scale + tx;
    const axisY = settings.originY * scale + ty;
    const cache = gridCacheFor(liveGrid);
    const format = value => Number(value.toPrecision(5)).toString();
    const minorSegments = [];
    const majorSegments = [];

    const radialMin = Math.max(0, Math.ceil((-axisX / minorPixels) - 1e-9));
    const radialMax = Math.min(500, Math.floor(((width - axisX) / minorPixels) + 1e-9));
    let radialLabelIndex = 0;
    for (let index = radialMin; index <= radialMax; index += 1) {
      const x = axisX + index * minorPixels;
      if (index === 0) continue;
      if (index % 5 === 0) {
        majorSegments.push('M' + x.toFixed(2) + ' 0V' + height);
        const radius = (index / 5) * majorStep;
        const label = pooledLabel(cache, cache.radialLabels, radialLabelIndex, 'middle');
        setGridLabel(label, Math.min(Math.max(x, 28), width - 28), height - 5, format(radius));
        radialLabelIndex += 1;
      } else {
        minorSegments.push('M' + x.toFixed(2) + ' 0V' + height);
      }
    }
    for (let index = radialLabelIndex; index < cache.radialLabels.length; index += 1) {
      setGridLabel(cache.radialLabels[index], 0, 0, '', false);
    }

    const axialMin = Math.max(-500, Math.ceil(((axisY - height) / minorPixels) - 1e-9));
    const axialMax = Math.min(500, Math.floor((axisY / minorPixels) + 1e-9));
    let axialLabelIndex = 0;
    for (let index = axialMin; index <= axialMax; index += 1) {
      const y = axisY - index * minorPixels;
      if (index === 0) continue;
      if (index % 5 === 0) {
        majorSegments.push('M0 ' + y.toFixed(2) + 'H' + width);
        const axial = (index / 5) * majorStep;
        const label = pooledLabel(cache, cache.axialLabels, axialLabelIndex, 'start');
        setGridLabel(label, 6, Math.min(Math.max(y + 4, 12), height - 3), format(axial));
        axialLabelIndex += 1;
      } else {
        minorSegments.push('M0 ' + y.toFixed(2) + 'H' + width);
      }
    }
    for (let index = axialLabelIndex; index < cache.axialLabels.length; index += 1) {
      setGridLabel(cache.axialLabels[index], 0, 0, '', false);
    }

    setSvgValue(cache.minor, 'd', minorSegments.join(''));
    setSvgValue(cache.major, 'd', majorSegments.join(''));
    setSvgValue(cache.axisX, 'd', axisX >= 0 && axisX <= width
      ? 'M' + axisX.toFixed(2) + ' 0V' + height : '');
    setSvgValue(cache.axisY, 'd', axisY >= 0 && axisY <= height
      ? 'M0 ' + axisY.toFixed(2) + 'H' + width : '');
    setGridLabel(
      cache.rotationLabel, axisX + 7, 15, 'Axis of rotation  -  r = 0',
      axisX >= 0 && axisX <= width,
    );
    setGridLabel(
      cache.zeroLabel, width - 4, axisY - 5, 'z = 0',
      axisY >= 0 && axisY <= height,
    );
    setGridLabel(cache.radialUnit, width - 6, height - 8, 'r  [m]');
    setGridLabel(cache.axialUnit, 8, 15, 'z  [m]');
  };
  const applyCanvasTransform = (scale, tx, ty, svg = currentSvg()) => {
    if (!svg) return;
    clearInferenceMarker(svg);
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
    drawLiveGrid(svg, scale, tx, ty);
  };
  const resetCanvasTransform = (svg = currentSvg()) => {
    if (!svg) return;
    clearInferenceMarker(svg);
    visualScale = 1;
    visualTranslateX = 0;
    visualTranslateY = 0;
    window.__ngsolveSketchViewTransform = [1, 0, 0, viewVersion];
    for (const id of groupIds) {
      const group = svg.querySelector('#' + id);
      if (group) group.removeAttribute('transform');
    }
    const staticGrid = svg.querySelector('#solve-sketch-grid');
    const liveGrid = svg.querySelector('#solve-sketch-live-grid');
    if (staticGrid) staticGrid.removeAttribute('visibility');
    if (liveGrid) liveGrid.setAttribute('display', 'none');
    for (const regionId of committedRegionOffsets.keys()) {
      for (const shape of svg.querySelectorAll('[data-sketch-region-id]')) {
        if (shape.getAttribute('data-sketch-region-id') === regionId) {
          shape.removeAttribute('transform');
        }
      }
    }
    committedRegionOffsets.clear();
  };
  const removeViewCommitSnapshot = (svg = currentSvg()) => {
    const snapshot = svg && svg.querySelector('[data-sketch-view-commit-snapshot="true"]');
    if (!snapshot) return;
    if (snapshot.__ngsolveLayerObserver) snapshot.__ngsolveLayerObserver.disconnect();
    for (const entry of snapshot.__ngsolveHiddenLayers || []) {
      // The renderer may replace a layer while the snapshot is covering the
      // canvas. Restore the current DOM node by id, not a detached old node.
      const layer = svg.querySelector('#' + entry.id);
      if (!layer) continue;
      if (entry.visibility === null) layer.removeAttribute('visibility');
      else layer.setAttribute('visibility', entry.visibility);
    }
    snapshot.remove();
  };
  const beginViewCommitSnapshot = () => {
    const svg = currentSvg();
    if (!svg || svg.querySelector('[data-sketch-view-commit-snapshot="true"]')) return false;
    if (Math.abs(visualScale - 1) < 1e-12
        && Math.abs(visualTranslateX) < 1e-9
        && Math.abs(visualTranslateY) < 1e-9) return false;
    const snapshot = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    snapshot.setAttribute('data-sketch-view-commit-snapshot', 'true');
    snapshot.setAttribute('pointer-events', 'none');
    snapshot.__ngsolveHiddenLayers = [];
    for (const id of [
      'solve-sketch-grid', 'solve-sketch-live-grid',
      'solve-sketch-scene', 'solve-sketch-dimensions', 'solve-sketch-preview',
    ]) {
      const layer = svg.querySelector('#' + id);
      if (!layer) continue;
      const visibility = layer.getAttribute('visibility');
      const copy = layer.cloneNode(true);
      copy.removeAttribute('id');
      copy.querySelectorAll('[id]').forEach(node => node.removeAttribute('id'));
      snapshot.appendChild(copy);
      snapshot.__ngsolveHiddenLayers.push({id, layer, visibility});
      layer.setAttribute('visibility', 'hidden');
    }
    // A framework patch can replace one of the SVG groups during the redraw.
    // Keep any replacement hidden so it cannot show through beside the frozen
    // frame, and restore the current nodes atomically when the commit is ready.
    snapshot.__ngsolveLayerObserver = new MutationObserver(() => {
      if (!snapshot.isConnected) return;
      for (const entry of snapshot.__ngsolveHiddenLayers) {
        const layer = svg.querySelector('#' + entry.id);
        if (layer && layer.getAttribute('visibility') !== 'hidden') {
          layer.setAttribute('visibility', 'hidden');
        }
      }
    });
    snapshot.__ngsolveLayerObserver.observe(svg, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['visibility'],
    });
    svg.appendChild(snapshot);
    return true;
  };
  window.__ngsolveBeginSketchViewCommit = beginViewCommitSnapshot;

  const remember = (event) => {
    window.__ngsolveSketchPointer = [event.clientX, event.clientY];
    const targetRegion = event.target && event.target.closest
      ? (event.target.closest('[data-sketch-region-id]') || {}).getAttribute('data-sketch-region-id')
      : null;
    const targetEdge = event.target && event.target.closest
      ? (event.target.closest('[data-sketch-edge-id]') || {}).getAttribute('data-sketch-edge-id')
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
      edgeId: targetEdge || null,
      regionKnown,
    });
    if (event.type === 'mousedown') {
      window.__ngsolveSketchRegionForNextMouseDown = null;
      window.__ngsolveSketchRegionForNextMouseDownKnown = false;
    }
    if (pointerEvents.length > 256) pointerEvents.splice(0, pointerEvents.length - 256);
  };
  const updateHover = (event, svg) => {
    if (!svg || !window.__ngsolveSketchBoundarySelection || (window.__ngsolveSketchTool || 'select') !== 'select') return;
    const target = document.elementFromPoint(event.clientX, event.clientY);
    const edgeHit = target && target.closest ? target.closest('[data-sketch-edge-role="hit"]') : null;
    const regionHit = !edgeHit && target && target.closest ? target.closest('[data-sketch-region-role="shape"]') : null;
    const edgeId = edgeHit ? edgeHit.getAttribute('data-sketch-edge-id') : null;
    const regionId = regionHit ? regionHit.getAttribute('data-sketch-region-id') : null;
    if (window.__ngsolveSketchHoveredEdgeId === edgeId && window.__ngsolveSketchHoveredRegionId === regionId) return;
    window.__ngsolveSketchHoveredEdgeId = edgeId;
    window.__ngsolveSketchHoveredRegionId = regionId;
    svg.querySelectorAll('[data-sketch-edge-role="visible"]').forEach((line) => {
      const selected = line.getAttribute('data-sketch-selected') === 'true';
      const highlighted = line.getAttribute('data-sketch-condition-highlight') === 'true';
      const hovered = line.getAttribute('data-sketch-edge-id') === edgeId;
      line.setAttribute('stroke', selected ? '#0877b9' : (hovered ? '#e29a12' : (highlighted ? '#c66f16' : '#263746')));
      line.setAttribute('stroke-width', selected ? '4.6' : (hovered ? '4.4' : (highlighted ? '4.2' : '1.6')));
    });
    svg.querySelectorAll('[data-sketch-region-role="shape"]').forEach((shape) => {
      const selected = shape.getAttribute('data-sketch-selected') === 'true';
      const hovered = shape.getAttribute('data-sketch-region-id') === regionId;
      shape.setAttribute('fill-opacity', selected ? '0.48' : (hovered ? '0.40' : '0.33'));
      shape.setAttribute('stroke', selected ? '#1b73ae' : (hovered ? '#17824b' : '#344658'));
      shape.setAttribute('stroke-width', selected ? '2.6' : (hovered ? '2.8' : '1.6'));
    });
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
  const regionElements = (svg, regionId) => {
    if (!svg || !regionId) return [];
    const escaped = window.CSS && window.CSS.escape
      ? window.CSS.escape(String(regionId))
      : String(regionId).replace(/[\"\\]/g, '\\$&');
    return svg.querySelectorAll('[data-sketch-region-id=\"' + escaped + '\"]');
  };
  const setRegionOffset = (svg, regionId, offset) => {
    if (!svg || !regionId) return;
    for (const shape of regionElements(svg, regionId)) {
      // The filled region path is refreshed from the model after each move.
      // Only its boundary paths still need the accumulated DOM offset.
      if (shape.getAttribute('data-sketch-region-role') === 'shape') {
        shape.removeAttribute('transform');
        continue;
      }
      if (offset && (offset[0] !== 0 || offset[1] !== 0)) {
        shape.setAttribute('transform', 'translate(' + offset[0] + ' ' + offset[1] + ')');
      } else {
        shape.removeAttribute('transform');
      }
    }
  };
  const clearPreview = (svg = currentSvg(), resetView = true, preserveCommitted = false) => {
    if (!svg) return;
    if (dragRegionId && window.__ngsolveSketchSelectedRegionId === dragRegionId) {
      const dimensions = svg.querySelector('#solve-sketch-dimensions');
      if (dimensions) dimensions.removeAttribute('transform');
    }
    for (const id of ['solve-sketch-drag-rect', 'solve-sketch-drag-circle', 'solve-sketch-drag-spline', 'solve-sketch-drag-line', 'solve-sketch-drag-line-halo', 'solve-sketch-drag-arc', 'solve-sketch-drag-arc-halo', 'solve-sketch-drag-trim']) {
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
    dragEdgeId = null;
    dragMode = null;
    gestureScreenPoints = [];
  };
  const snapPoint = (point) => {
    const grid = window.__ngsolveSketchSnap;
    if (!point || !grid || !grid.enabled || !Number.isFinite(grid.stepPx) || grid.stepPx <= 0) return point;
    const snappedX = grid.originX + Math.round((point[0] - grid.originX) / grid.stepPx) * grid.stepPx;
    const snappedY = grid.originY + Math.round((point[1] - grid.originY) / grid.stepPx) * grid.stepPx;
    return [Math.max(grid.originX, snappedX), snappedY];
  };
  const toScenePoint = (point) => [
    (point[0] - visualTranslateX) / visualScale,
    (point[1] - visualTranslateY) / visualScale,
  ];
  const toViewPoint = (point) => [
    visualScale * point[0] + visualTranslateX,
    visualScale * point[1] + visualTranslateY,
  ];
  let inferenceSnapCache = null;
  const clearInferenceMarker = (svg = currentSvg()) => {
    if (!svg) return;
    const halo = svg.querySelector("#solve-sketch-inference-halo");
    const marker = svg.querySelector("#solve-sketch-inference-marker");
    if (halo) halo.setAttribute("display", "none");
    if (marker) marker.setAttribute("display", "none");
  };
  window.__ngsolveClearSketchInferenceMarker = () => clearInferenceMarker(currentSvg());
  const inferenceMarkerPath = (point, kind) => {
    const x = point[0], y = point[1];
    const r = kind === "endpoint" ? 5.5 : 5;
    if (kind === "endpoint") {
      return "M" + (x-r) + "," + (y-r) + " H" + (x+r) + " V" + (y+r) + " H" + (x-r) + " Z"
        + " M" + (x-2) + "," + y + " H" + (x+2) + " M" + x + "," + (y-2) + " V" + (y+2);
    }
    if (kind === "midpoint") {
      return "M" + x + "," + (y-r) + " L" + (x+r) + "," + y + " L" + x + "," + (y+r) + " L" + (x-r) + "," + y + " Z"
        + " M" + (x-2) + "," + y + " H" + (x+2);
    }
    if (kind === "center") {
      return "M" + (x-r) + "," + y + " A" + r + "," + r + " 0 1 0 " + (x+r) + "," + y
        + " A" + r + "," + r + " 0 1 0 " + (x-r) + "," + y
        + " M" + (x-8) + "," + y + " H" + (x+8) + " M" + x + "," + (y-8) + " V" + (y+8);
    }
    return "M" + (x-r) + "," + y + " A" + r + "," + r + " 0 1 0 " + (x+r) + "," + y
      + " A" + r + "," + r + " 0 1 0 " + (x-r) + "," + y
      + " M" + (x-2) + "," + y + " H" + (x+2) + " M" + x + "," + (y-2) + " V" + (y+2);
  };
  const showInferenceMarker = (svg, candidate) => {
    if (!svg || !candidate) return clearInferenceMarker(svg);
    const halo = svg.querySelector("#solve-sketch-inference-halo");
    const marker = svg.querySelector("#solve-sketch-inference-marker");
    if (!halo || !marker) return;
    const d = inferenceMarkerPath(candidate.point, candidate.kind);
    halo.setAttribute("d", d);
    halo.setAttribute("display", "inline");
    marker.setAttribute("d", d);
    marker.setAttribute("stroke", ({endpoint:"#16814a", midpoint:"#d18a0a", center:"#7a43b6", edge:"#1176b8"})[candidate.kind] || "#1176b8");
    marker.setAttribute("display", "inline");
  };
  const pathPointInCanvas = (svg, path, distance) => {
    try {
      const matrix = path.getCTM();
      if (!matrix) return null;
      const local = path.getPointAtLength(distance);
      const point = svg.createSVGPoint();
      point.x = local.x;
      point.y = local.y;
      const canvasPoint = point.matrixTransform(matrix);
      return [canvasPoint.x, canvasPoint.y];
    } catch (_) {
      return null;
    }
  };
  const inferenceTargetsFor = (svg) => {
    const key = String(svg.getAttribute("data-sketch-render-version") || "0") + ":" + viewVersion;
    if (inferenceSnapCache && inferenceSnapCache.svg === svg && inferenceSnapCache.key === key) return inferenceSnapCache;
    const points = [];
    const buckets = new Map();
    const cellSize = 32;
    const addPoint = (kind, point, keyValue) => {
      if (!point || !point.every(Number.isFinite)) return;
      if (points.some(item => item.kind === kind && Math.hypot(item.point[0]-point[0], item.point[1]-point[1]) < 0.75)) return;
      points.push({kind, point, key:keyValue, priority:kind === "endpoint" ? 0 : (kind === "center" ? 1 : 2)});
    };
    let segmentId = 0;
    const addSegment = (a, b) => {
      if (!a || !b || Math.hypot(a[0]-b[0], a[1]-b[1]) < 1e-6) return;
      const segment = {id:segmentId++, a, b};
      const minX = Math.floor(Math.min(a[0],b[0]) / cellSize);
      const maxX = Math.floor(Math.max(a[0],b[0]) / cellSize);
      const minY = Math.floor(Math.min(a[1],b[1]) / cellSize);
      const maxY = Math.floor(Math.max(a[1],b[1]) / cellSize);
      for (let bx=minX; bx<=maxX; bx += 1) for (let by=minY; by<=maxY; by += 1) {
        const bucketKey = bx + "," + by;
        if (!buckets.has(bucketKey)) buckets.set(bucketKey, []);
        buckets.get(bucketKey).push(segment);
      }
    };
    const paths = svg.querySelectorAll('[data-sketch-edge-role="visible"]');
    for (const path of paths) {
      if (path.getAttribute("display") === "none" || getComputedStyle(path).display === "none") continue;
      let length;
      try { length = path.getTotalLength(); } catch (_) { continue; }
      if (!Number.isFinite(length) || length <= 0.5) continue;
      const type = (path.getAttribute("data-sketch-curve-type") || "line").toLowerCase();
      const edgeId = path.getAttribute("data-sketch-edge-id") || String(segmentId);
      const first = pathPointInCanvas(svg, path, 0);
      const last = pathPointInCanvas(svg, path, length);
      if (type !== "circle") {
        addPoint("endpoint", first, edgeId + ":start");
        addPoint("endpoint", last, edgeId + ":end");
      }
      if (type === "line" || type === "arc") {
        addPoint("midpoint", pathPointInCanvas(svg, path, length * 0.5), edgeId + ":mid");
      }
      if (type === "arc" || type === "circle") {
        const cxValue = path.getAttribute("data-sketch-curve-center-x");
        const cyValue = path.getAttribute("data-sketch-curve-center-y");
        const cx = Number(cxValue), cy = Number(cyValue);
        const matrix = path.getCTM();
        if (cxValue !== null && cxValue !== "" && cyValue !== null && cyValue !== ""
          && Number.isFinite(cx) && Number.isFinite(cy) && matrix) {
          const local = svg.createSVGPoint();
          local.x = cx;
          local.y = cy;
          const center = local.matrixTransform(matrix);
          addPoint("center", [center.x, center.y], edgeId + ":center");
        }
      }
      const sampleCount = Math.min(512, Math.max(2, Math.ceil(length / 6)));
      let previous = pathPointInCanvas(svg, path, 0);
      for (let index=1; index<=sampleCount; index += 1) {
        const current = pathPointInCanvas(svg, path, length * index / sampleCount);
        addSegment(previous, current);
        previous = current;
      }
    }
    inferenceSnapCache = {svg, key, points, buckets, cellSize};
    return inferenceSnapCache;
  };
  const closestEdgeCandidate = (cache, point, limit) => {
    const cell = cache.cellSize;
    const bx = Math.floor(point[0] / cell), by = Math.floor(point[1] / cell);
    let best = null;
    const visited = new Set();
    for (let ox=-1; ox<=1; ox += 1) for (let oy=-1; oy<=1; oy += 1) {
      const segments = cache.buckets.get((bx+ox) + "," + (by+oy)) || [];
      for (const segment of segments) {
        if (visited.has(segment.id)) continue;
        visited.add(segment.id);
        const dx = segment.b[0]-segment.a[0], dy = segment.b[1]-segment.a[1];
        const lengthSquared = dx*dx + dy*dy;
        const fraction = lengthSquared <= 1e-12 ? 0 : Math.max(0, Math.min(1,
          ((point[0]-segment.a[0])*dx + (point[1]-segment.a[1])*dy) / lengthSquared));
        const nearest = [segment.a[0] + fraction*dx, segment.a[1] + fraction*dy];
        const distance = Math.hypot(point[0]-nearest[0], point[1]-nearest[1]);
        if (distance <= limit && (!best || distance < best.distance)) best = {kind:"edge", point:nearest, distance};
      }
    }
    return best;
  };
  const findInferenceSnap = (scenePoint, svg) => {
    const tool = window.__ngsolveSketchTool || "select";
    if (!svg || !["line", "arc"].includes(tool)) {
      clearInferenceMarker(svg);
      return null;
    }
    const pointer = toViewPoint(scenePoint);
    const cache = inferenceTargetsFor(svg);
    const pointCandidates = cache.points.map(item => ({
      kind:item.kind, point:item.point, key:item.key, priority:item.priority,
      distance:Math.hypot(pointer[0]-item.point[0], pointer[1]-item.point[1]),
    })).filter(item => item.distance <= 10);
    pointCandidates.sort((a,b) => a.distance-b.distance || a.priority-b.priority);
    const candidate = pointCandidates[0] || closestEdgeCandidate(cache, pointer, 10);
    if (!candidate) {
      clearInferenceMarker(svg);
      return null;
    }
    showInferenceMarker(svg, candidate);
    return toScenePoint(candidate.point);
  };
  const sketchPoint = (point, svg = currentSvg()) => {
    const tool = window.__ngsolveSketchTool || "select";
    if (tool === "select") {
      clearInferenceMarker(svg);
      return point;
    }
    const inferred = findInferenceSnap(point, svg);
    if (inferred) return inferred;
    const grid = window.__ngsolveSketchSnap;
    if ((tool === "line" || tool === "arc") && grid && grid.enabled) {
      const gridPoint = snapPoint(point);
      const gridInference = findInferenceSnap(gridPoint, svg);
      if (gridInference) return gridInference;
      return gridPoint;
    }
    return snapPoint(point);
  };
  const setPreviewPath = (svg, id, d, visible = true) => {
    const node = svg && svg.querySelector("#" + id);
    if (!node) return;
    node.setAttribute("d", d || "");
    node.setAttribute("display", visible && d ? "inline" : "none");
  };
  const arcPathThroughThreePoints = (start, through, end) => {
    const ax = start[0], ay = start[1], bx = through[0], by = through[1], cx = end[0], cy = end[1];
    const denominator = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by));
    if (Math.abs(denominator) < 1e-9) return "";
    const aa = ax * ax + ay * ay, bb = bx * bx + by * by, cc = cx * cx + cy * cy;
    const center = [
      (aa * (by - cy) + bb * (cy - ay) + cc * (ay - by)) / denominator,
      (aa * (cx - bx) + bb * (ax - cx) + cc * (bx - ax)) / denominator,
    ];
    const radius = Math.hypot(start[0] - center[0], start[1] - center[1]);
    if (!(radius > 0) || !Number.isFinite(radius)) return "";
    const a0 = Math.atan2(ay - center[1], ax - center[0]);
    const am = Math.atan2(by - center[1], bx - center[0]);
    const a1 = Math.atan2(cy - center[1], cx - center[0]);
    const positive = (a1 - a0 + 2 * Math.PI) % (2 * Math.PI);
    const throughSweep = (am - a0 + 2 * Math.PI) % (2 * Math.PI);
    const sweep = throughSweep > positive ? positive - 2 * Math.PI : positive;
    return "M" + ax + "," + ay + " A" + radius + "," + radius + " 0 "
      + (Math.abs(sweep) > Math.PI ? "1" : "0") + " " + (sweep >= 0 ? "1" : "0")
      + " " + cx + "," + cy;
  };
  let centerArcTrace = null;
  const resetCenterArcTrace = () => {
    centerArcTrace = null;
    window.__ngsolveSketchCenterArcClockwise = false;
  };
  const arcPathFromCenter = (center, start, cursor) => {
    const radius = Math.hypot(start[0] - center[0], start[1] - center[1]);
    const cursorRadius = Math.hypot(cursor[0] - center[0], cursor[1] - center[1]);
    if (!(radius > 0) || !(cursorRadius > 1e-9)) return "";
    const a0 = Math.atan2(start[1] - center[1], start[0] - center[0]);
    const a1 = Math.atan2(cursor[1] - center[1], cursor[0] - center[0]);
    const key = center.join(",") + "|" + start.join(",");
    if (!centerArcTrace || centerArcTrace.key !== key) {
      centerArcTrace = {key, lastAngle: a0, direction: 0, travel: 0};
    }
    // The viewport's y axis points down, so increasing screen angle is
    // clockwise to the user. Follow angular pointer movement for both the
    // preview and the final sweep instead of always taking one orientation.
    const delta = Math.atan2(Math.sin(a1 - centerArcTrace.lastAngle), Math.cos(a1 - centerArcTrace.lastAngle));
    centerArcTrace.travel += delta;
    if (Math.abs(centerArcTrace.travel) > 0.035) centerArcTrace.direction = Math.sign(centerArcTrace.travel);
    centerArcTrace.lastAngle = a1;
    let direction = centerArcTrace.direction;
    if (!direction) {
      const shortest = Math.atan2(Math.sin(a1 - a0), Math.cos(a1 - a0));
      direction = Math.abs(shortest) > 1e-9 ? Math.sign(shortest) : -1;
    }
    const positiveSweep = (a1 - a0 + 2 * Math.PI) % (2 * Math.PI);
    const sweep = direction > 0 ? positiveSweep : positiveSweep - 2 * Math.PI;
    window.__ngsolveSketchCenterArcClockwise = direction > 0;
    const end = [center[0] + radius * Math.cos(a1), center[1] + radius * Math.sin(a1)];
    if (Math.hypot(end[0] - start[0], end[1] - start[1]) < 1e-7) return "";
    return "M" + start[0] + "," + start[1] + " A" + radius + "," + radius + " 0 "
      + (Math.abs(sweep) > Math.PI ? "1" : "0") + " " + (sweep >= 0 ? "1" : "0")
      + " " + end[0] + "," + end[1];
  };
  const spline3Path = (start, control, end) => {
    const chord = Math.hypot(end[0] - start[0], end[1] - start[1]);
    const firstLeg = Math.hypot(control[0] - start[0], control[1] - start[1]);
    const secondLeg = Math.hypot(end[0] - control[0], end[1] - control[1]);
    const controlWeight = Math.sqrt(0.5 * (firstLeg * firstLeg + secondLeg * secondLeg));
    if (!(chord > 1e-12) || !(controlWeight > 1e-12)) return "";
    const weight = chord / controlWeight;
    const pieces = [];
    for (let index = 0; index <= 64; index += 1) {
      const t = index / 64;
      const b0 = (1 - t) * (1 - t), b1 = weight * t * (1 - t), b2 = t * t;
      const denominator = b0 + b1 + b2;
      const x = (b0 * start[0] + b1 * control[0] + b2 * end[0]) / denominator;
      const y = (b0 * start[1] + b1 * control[1] + b2 * end[1]) / denominator;
      pieces.push((index ? "L" : "M") + x + "," + y);
    }
    return pieces.join(" ");
  };
  const arcPathTangent = (start, tangent, end) => {
    const dx = end[0] - start[0], dy = end[1] - start[1];
    const chordSquared = dx * dx + dy * dy;
    const normal = [-tangent[1], tangent[0]];
    const denominator = 2 * (dx * normal[0] + dy * normal[1]);
    if (chordSquared < 1e-9 || Math.abs(denominator) < 1e-9) return "";
    const offset = chordSquared / denominator;
    const center = [start[0] + offset * normal[0], start[1] + offset * normal[1]];
    const radius = Math.hypot(start[0] - center[0], start[1] - center[1]);
    if (!(radius > 0) || !Number.isFinite(radius)) return "";
    const a0 = Math.atan2(start[1] - center[1], start[0] - center[0]);
    const a1 = Math.atan2(end[1] - center[1], end[0] - center[0]);
    const positive = (a1 - a0 + 2 * Math.PI) % (2 * Math.PI);
    const ccw = [-Math.sin(a0), Math.cos(a0)];
    const sweep = ccw[0] * tangent[0] + ccw[1] * tangent[1] >= 0 ? positive : positive - 2 * Math.PI;
    if (Math.abs(sweep) < 1e-9) return "";
    return "M" + start[0] + "," + start[1] + " A" + radius + "," + radius + " 0 "
      + (Math.abs(sweep) > Math.PI ? "1" : "0") + " " + (sweep >= 0 ? "1" : "0")
      + " " + end[0] + "," + end[1];
  };
  const constructionCrosses = (points) => points.map((point) => {
    const x = point[0], y = point[1], r = 6;
    return "M" + (x - r) + "," + y + " L" + (x + r) + "," + y
      + " M" + x + "," + (y - r) + " L" + x + "," + (y + r)
      + " M" + (x - 3.5) + "," + y + " a3.5,3.5 0 1,0 7,0 a3.5,3.5 0 1,0 -7,0";
  }).join(" ");
  const updateConstructionPreview = (svg, point) => {
    const state = window.__ngsolveSketchConstruction || {};
    const tool = window.__ngsolveSketchTool || "select";
    const linePoints = Array.isArray(state.linePoints) ? state.linePoints : [];
    const arcPoints = Array.isArray(state.arcPoints) ? state.arcPoints : [];
    const circlePoints = Array.isArray(state.circlePoints) ? state.circlePoints : [];
    const centerpointArcPending = tool === "arc" && (state.arcVariant || "three-point") === "centerpoint" && arcPoints.length >= 2;
    if (!centerpointArcPending) resetCenterArcTrace();
    else {
      const center = arcPoints[0], start = arcPoints[1];
      const key = center.join(",") + "|" + start.join(",");
      if (!centerArcTrace || centerArcTrace.key !== key) {
        centerArcTrace = {key, lastAngle: Math.atan2(start[1] - center[1], start[0] - center[0]), direction: 0, travel: 0};
        window.__ngsolveSketchCenterArcClockwise = false;
      }
    }
    const markers = tool === "line" ? linePoints.slice()
      : (tool === "arc" ? arcPoints.slice()
        : (tool === "circle" ? circlePoints.slice() : []));
    if (active && dragMode === "draw" && dragStart && (tool === "line" || tool === "arc")) markers.push(dragStart);
    const fixedLine = linePoints.map((point, index) => (index ? "L" : "M") + point[0] + "," + point[1]).join(" ");
    setPreviewPath(svg, "solve-sketch-line-chain-halo", fixedLine, tool === "line");
    setPreviewPath(svg, "solve-sketch-line-chain", fixedLine, tool === "line");
    let lineTail = "";
    if (tool === "line" && point) {
      const anchor = linePoints.length ? linePoints[linePoints.length - 1] : (active && dragMode === "draw" ? dragStart : null);
      if (anchor && Math.hypot(point[0] - anchor[0], point[1] - anchor[1]) > 1) {
        lineTail = "M" + anchor[0] + "," + anchor[1] + " L" + point[0] + "," + point[1];
      }
    }
    setPreviewPath(svg, "solve-sketch-drag-line-halo", lineTail, tool === "line");
    setPreviewPath(svg, "solve-sketch-drag-line", lineTail, tool === "line");
    let arc = "";
    if (tool === "arc" && point) {
      const variant = state.arcVariant || "three-point";
      if (variant === "three-point" && arcPoints.length >= 2) arc = arcPathThroughThreePoints(arcPoints[0], point, arcPoints[1]);
      else if (variant === "spline3" && arcPoints.length >= 2) arc = spline3Path(arcPoints[0], point, arcPoints[1]);
      else if (variant === "centerpoint" && arcPoints.length >= 2) arc = arcPathFromCenter(arcPoints[0], arcPoints[1], point);
      else if (variant === "tangent" && arcPoints.length >= 1 && Array.isArray(state.arcTangent)) arc = arcPathTangent(arcPoints[0], state.arcTangent, point);
    }
    setPreviewPath(svg, "solve-sketch-drag-arc-halo", arc, tool === "arc");
    setPreviewPath(svg, "solve-sketch-drag-arc", arc, tool === "arc");
    const previewCircle = svg && svg.querySelector("#solve-sketch-drag-circle");
    const circleVariant = state.circleVariant || "center-radius";
    if (previewCircle && tool === "circle" && circleVariant === "three-point" && circlePoints.length >= 2 && point) {
      const first = circlePoints[0], second = circlePoints[1], third = point;
      const denominator = 2 * (first[0] * (second[1] - third[1])
        + second[0] * (third[1] - first[1]) + third[0] * (first[1] - second[1]));
      if (Math.abs(denominator) > 1e-9) {
        const aa = first[0] * first[0] + first[1] * first[1];
        const bb = second[0] * second[0] + second[1] * second[1];
        const cc = third[0] * third[0] + third[1] * third[1];
        const cx = (aa * (second[1] - third[1]) + bb * (third[1] - first[1])
          + cc * (first[1] - second[1])) / denominator;
        const cy = (aa * (third[0] - second[0]) + bb * (first[0] - third[0])
          + cc * (second[0] - first[0])) / denominator;
        const radius = Math.hypot(first[0] - cx, first[1] - cy);
        if (Number.isFinite(radius) && radius > 0) {
          previewCircle.setAttribute("cx", String(cx));
          previewCircle.setAttribute("cy", String(cy));
          previewCircle.setAttribute("r", String(radius));
          previewCircle.setAttribute("fill", "none");
          previewCircle.setAttribute("stroke-dasharray", "7 4");
          previewCircle.setAttribute("display", "inline");
        } else previewCircle.setAttribute("display", "none");
      } else previewCircle.setAttribute("display", "none");
    } else if (previewCircle && tool === "circle" && circleVariant === "three-point") {
      previewCircle.setAttribute("display", "none");
    }
    const cross = constructionCrosses(markers);
    setPreviewPath(svg, "solve-sketch-construction-halo", cross, true);
    setPreviewPath(svg, "solve-sketch-construction-points", cross, true);
  };
  window.__ngsolveRefreshConstructionPreview = () => updateConstructionPreview(currentSvg(), null);

  const updatePreview = (svg, point, screenPoint = null) => {
    if (!active || !dragStart || !point) return;
    if ((dragMode === 'region' || dragMode === 'edge') && dragRegionId) {
      const grid = window.__ngsolveSketchSnap;
      const start = grid && grid.enabled ? snapPoint(dragStart) : dragStart;
      const end = grid && grid.enabled ? snapPoint(point) : point;
      const step = Math.max(1e-12, Number(window.__ngsolveSketchMoveStep) || 0);
      const quantize = (value) => Math.sign(value) * Math.round(Math.abs(value) / step) * step;
      const dx = grid && grid.enabled ? end[0] - start[0] : quantize(end[0] - start[0]);
      const dy = grid && grid.enabled ? end[1] - start[1] : quantize(end[1] - start[1]);
      const baseOffset = committedRegionOffsets.get(dragRegionId) || [0, 0];
      for (const shape of regionElements(svg, dragRegionId)) {
        const isFilledRegion = shape.getAttribute('data-sketch-region-role') === 'shape';
        const totalX = (isFilledRegion ? 0 : baseOffset[0]) + dx;
        const totalY = (isFilledRegion ? 0 : baseOffset[1]) + dy;
        if (totalX === 0 && totalY === 0) shape.removeAttribute('transform');
        else shape.setAttribute('transform', `translate(${totalX} ${totalY})`);
      }
      if (window.__ngsolveSketchSelectedRegionId === dragRegionId) {
        const dimensions = svg.querySelector('#solve-sketch-dimensions');
        if (dimensions) {
          if (dx === 0 && dy === 0) dimensions.removeAttribute('transform');
          else dimensions.setAttribute('transform', `translate(${dx} ${dy})`);
        }
      }
      return;
    }
    if (dragMode === 'pan') {
      const screenStart = dragStartScreen || toViewPoint(dragStart);
      const screenEnd = screenPoint || toViewPoint(point);
      const grid = window.__ngsolveSketchLiveGrid;
      const maxAxisX = grid ? Number(grid.negativeRBufferPx) : 72;
      let translateX = dragBaseTranslateX + screenEnd[0] - screenStart[0];
      if (grid && Number.isFinite(maxAxisX)) {
        translateX = Math.min(translateX, maxAxisX - dragBaseScale * grid.originX);
      }
      applyCanvasTransform(
        dragBaseScale,
        translateX,
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
    const spline = svg.querySelector('#solve-sketch-drag-spline');
    const line = svg.querySelector('#solve-sketch-drag-line');
    const trim = svg.querySelector('#solve-sketch-drag-trim');
    const hide = (node) => { if (node) node.setAttribute('display', 'none'); };
    if (tool === 'line') {
      hide(rect); hide(circle); hide(spline); hide(trim);
      updateConstructionPreview(svg, point);
      return;
    }
    if (tool === 'trim') {
      hide(rect); hide(circle); hide(spline); hide(line);
      if (!trim) return;
      const points = gestureScreenPoints.map(toScenePoint);
      const current = point;
      if (!points.length || Math.hypot(points[points.length - 1][0] - current[0], points[points.length - 1][1] - current[1]) > 0.5) points.push(current);
      if (points.length < 2) { hide(trim); return; }
      trim.setAttribute('display', 'inline');
      trim.setAttribute('d', points.map((item, index) => (index ? 'L' : 'M') + item[0] + ',' + item[1]).join(' '));
      return;
    }
    if (tool === 'spline') {
      hide(rect); hide(circle); hide(line); hide(trim);
      if (!spline) return;
      const points = gestureScreenPoints.map(toScenePoint);
      const current = point;
      if (!points.length || Math.hypot(points[points.length - 1][0] - current[0], points[points.length - 1][1] - current[1]) > 0.5) points.push(current);
      if (points.length < 2) { hide(spline); return; }
      spline.setAttribute('display', 'inline');
      spline.setAttribute('d', points.map((item, index) => (index ? 'L' : 'M') + item[0] + ',' + item[1]).join(' '));
      return;
    }
    if (tool === 'arc') {
      hide(rect); hide(circle); hide(spline); hide(line); hide(trim);
      updateConstructionPreview(svg, point);
      return;
    }
    hide(spline); hide(line); hide(trim);
    if (Math.hypot(dx, dy) < 4) {
      if (rect) rect.setAttribute('display', 'none');
      if (circle) circle.setAttribute('display', 'none');
      return;
    }
    if (tool === 'circle') {
      if ((window.__ngsolveSketchToolVariant || 'center-radius') === 'three-point') { hide(circle); return; }
      if (rect) rect.setAttribute('display', 'none');
      if (!circle) return;
      circle.setAttribute('display', 'inline');
      circle.setAttribute('fill', '#2385bd20');
      circle.setAttribute('stroke-dasharray', '6 4');
      const variant = window.__ngsolveSketchToolVariant || 'center-radius';
      circle.setAttribute('cx', variant === 'center-radius' ? dragStart[0] : (dragStart[0] + point[0]) / 2);
      circle.setAttribute('cy', variant === 'center-radius' ? dragStart[1] : (dragStart[1] + point[1]) / 2);
      circle.setAttribute('r', Math.hypot(dx, dy) / (variant === 'diameter' ? 2 : 1));
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
    const centerRectangle = tool === 'rectangle' && (window.__ngsolveSketchToolVariant || '') === 'center';
    rect.setAttribute('x', centerRectangle ? dragStart[0] - Math.abs(dx) : Math.min(dragStart[0], point[0]));
    rect.setAttribute('y', centerRectangle ? dragStart[1] - Math.abs(dy) : Math.min(dragStart[1], point[1]));
    rect.setAttribute('width', centerRectangle ? 2 * Math.abs(dx) : Math.abs(dx));
    rect.setAttribute('height', centerRectangle ? 2 * Math.abs(dy) : Math.abs(dy));
    rect.setAttribute('fill', fill);
    rect.setAttribute('stroke', stroke);
    rect.setAttribute('stroke-width', isSelection ? '1.5' : '2');
  };
  const beginGesture = (event, svg) => {
    if (!targetIsSvg(event, svg) || ![0, 1].includes(event.button) || event.isPrimary === false) return;
    // The line-chain tool completes with Enter/Escape. SVG canvases are not
    // reliably focused by pointer capture alone, so focus it explicitly on
    // line-tool gestures to ensure the keyboard event reaches its handler.
    if (window.__ngsolveSketchTool === 'line') {
      try { svg.focus({preventScroll: true}); }
      catch (_) { try { svg.focus(); } catch (_) { /* non-focusable SVG */ } }
    }
    const middlePan = event.button === 1;
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
    dragStart = middlePan ? toScenePoint(screenStart) : sketchPoint(toScenePoint(screenStart));
    gestureScreenPoints = screenStart ? [[screenStart[0], screenStart[1]]] : [];
    const region = event.target && event.target.closest
      ? event.target.closest('[data-sketch-region-id]')
      : null;
    const edge = event.target && event.target.closest
      ? event.target.closest('[data-sketch-edge-id]')
      : null;
    const tool = window.__ngsolveSketchTool || 'select';
    if (tool === 'arc' && (window.__ngsolveSketchToolVariant || '') === 'centerpoint') {
      const state = window.__ngsolveSketchConstruction || {};
      if (!Array.isArray(state.arcPoints) || state.arcPoints.length < 2) resetCenterArcTrace();
    }
    const selectionEnabled = ['select', 'trim'].includes(tool);
    dragRegionId = selectionEnabled && region
      ? region.getAttribute('data-sketch-region-id')
      : null;
    dragEdgeId = (selectionEnabled || tool === 'line') && edge
      ? edge.getAttribute('data-sketch-edge-id')
      : null;
    window.__ngsolveSketchLastRegionId = dragRegionId;
    dragMode = middlePan
      ? 'pan'
      : tool === 'trim'
        ? 'trim'
        : tool !== 'select'
          ? 'draw'
          : window.__ngsolveSketchBoundarySelection
            ? (dragEdgeId ? 'edge' : (dragRegionId ? 'region' : 'marquee'))
            : (event.shiftKey || event.ctrlKey)
              ? (dragRegionId ? 'region' : (dragEdgeId ? 'edge' : 'marquee'))
              : dragEdgeId
                ? 'edge'
                : dragRegionId
                  ? 'region'
                  : 'marquee';
    window.__ngsolveSketchGestureActive = true;
    if (dragMode === 'pan') svg.style.cursor = 'grabbing';
    if (tool === 'select' && !middlePan) {
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
      if (screenPoint) {
        const last = gestureScreenPoints[gestureScreenPoints.length - 1];
        if (!last || Math.hypot(screenPoint[0] - last[0], screenPoint[1] - last[1]) >= 2) {
          if (gestureScreenPoints.length < 256) gestureScreenPoints.push([screenPoint[0], screenPoint[1]]);
        }
        updatePreview(pending.svg, toScenePoint(screenPoint), screenPoint);
      }
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
    const lastGesturePoint = gestureScreenPoints[gestureScreenPoints.length - 1];
    if (!cancelled && screenPoint && (!lastGesturePoint || Math.hypot(screenPoint[0] - lastGesturePoint[0], screenPoint[1] - lastGesturePoint[1]) > 0.5)) {
      if (gestureScreenPoints.length < 256) gestureScreenPoints.push([screenPoint[0], screenPoint[1]]);
    }
    const moved = Boolean(!cancelled && dragStartScreen && screenPoint
      && Math.hypot(screenPoint[0] - dragStartScreen[0], screenPoint[1] - dragStartScreen[1]) >= 4);
    const hasCompletedPointerGesture = Boolean(!cancelled && dragStartScreen && screenPoint);
    const finishedGesture = gestureNumber;
    const finishedRegionId = ['region', 'edge'].includes(dragMode) ? dragRegionId : null;
    const finishedEdgeId = dragEdgeId;
    const finishedSketchPoints = gestureScreenPoints.slice(0, 256);
    const preserveVisual = moved && ['draw', 'pan', 'region', 'edge'].includes(dragMode);
    const shouldForward = active && !targetIsSvg(event, svg) && event.type !== 'pointercancel';
    if (!cancelled && dragStartScreen && screenPoint) {
      const operation = dragMode;
      const tool = window.__ngsolveSketchTool || 'select';
      window.__ngsolveSketchCompletedGesture = [
        operation, tool,
        dragStartScreen[0], dragStartScreen[1], screenPoint[0], screenPoint[1],
        dragRegionId || '', Boolean(event.shiftKey || event.ctrlKey), moved,
        visualScale, visualTranslateX, visualTranslateY, viewVersion,
        finishedEdgeId || '', finishedSketchPoints,
        window.__ngsolveSketchToolVariant || '', '',
        Boolean(window.__ngsolveSketchCenterArcClockwise),
      ];
      if (wheelCommitTimer) window.clearTimeout(wheelCommitTimer);
      wheelCommitTimer = 0;
    }
    if (hasCompletedPointerGesture) {
      // Selection is resolved from this completed gesture because pointer
      // capture retargets the browser's following click to the SVG root.
      // Suppress that click to avoid selecting the region below its endpoint.
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
  window.__ngsolveCommitSketchRegionMove = (regionId, fillUpdates = [], edgeUpdates = []) => {
    if (!regionId) return false;
    const svg = currentSvg();
    if (!svg) return false;

    const shapesById = new Map(
      Array.from(svg.querySelectorAll('[data-sketch-region-role="shape"]'))
        .map((node) => [node.getAttribute('data-sketch-region-id'), node])
    );
    const edgesById = new Map();
    for (const node of svg.querySelectorAll('[data-sketch-edge-id]')) {
      const id = node.getAttribute('data-sketch-edge-id');
      if (!edgesById.has(id)) edgesById.set(id, []);
      edgesById.get(id).push(node);
    }
    const validFills = (fillUpdates || []).filter(
      (update) => update && update.id && typeof update.d === 'string'
    );
    const validEdges = (edgeUpdates || []).filter(
      (update) => update && update.id && typeof update.d === 'string'
    );

    // Check all targets before mutating the live canvas. A partial commit
    // could leave visible and clickable geometry at different coordinates.
    if (validFills.some((update) => !shapesById.has(String(update.id)))
        || validEdges.some((update) => !edgesById.has(String(update.id)))) {
      return false;
    }
    for (const update of validFills) {
      const shape = shapesById.get(String(update.id));
      shape.setAttribute('d', update.d);
      shape.removeAttribute('transform');
    }
    for (const update of validEdges) {
      for (const node of edgesById.get(String(update.id))) {
        node.setAttribute('d', update.d);
        node.removeAttribute('transform');
      }
    }

    // Every edge path now contains its model coordinates. Clear any accumulated
    // drag transforms only after both visible and hit-test paths are committed.
    for (const node of svg.querySelectorAll('[data-sketch-edge-id]')) {
      node.removeAttribute('transform');
    }
    for (const node of svg.querySelectorAll('[data-sketch-region-role="shape"]')) {
      node.removeAttribute('transform');
    }
    committedRegionOffsets.clear();
    const dimensions = svg.querySelector('#solve-sketch-dimensions');
    if (dimensions) dimensions.removeAttribute('transform');
    return true;
  };
  window.__ngsolveSyncSketchDimensionOffset = (regionId) => {
    const svg = currentSvg();
    const dimensions = svg && svg.querySelector('#solve-sketch-dimensions');
    if (!dimensions) return;
    window.__ngsolveSketchSelectedRegionId = regionId || null;
    // Dimension markup is regenerated from the model's current geometry, so it
    // already includes every committed region move. Only active drag preview
    // deltas should temporarily translate it.
    dimensions.removeAttribute('transform');
  };
  window.__ngsolveRollbackSketchRegionMove = (regionId) => {
    const svg = currentSvg();
    setRegionOffset(svg, regionId, committedRegionOffsets.get(regionId) || null);
    const dimensions = svg && svg.querySelector('#solve-sketch-dimensions');
    if (dimensions) dimensions.removeAttribute('transform');
  };
  window.__ngsolveResetSketchPreview = (expectedRenderVersion) => {
    const token = ++pendingViewResetToken;
    const expected = Number(expectedRenderVersion);
    const hasExpected = Number.isFinite(expected);
    const ready = (svg) => {
      if (!hasExpected) return true;
      if (Number(svg.getAttribute('data-sketch-render-version') || 0) < expected) return false;
      const layers = [
        ['solve-sketch-grid', 'data-sketch-grid-revision'],
        ['solve-sketch-scene', 'data-sketch-scene-revision'],
        ['solve-sketch-dimensions', 'data-sketch-dimensions-revision'],
      ];
      return layers.every(([id, revisionAttribute]) => {
        const node = svg.querySelector('#' + id);
        const revision = Number(svg.getAttribute(revisionAttribute) || 0);
        return node && Number(node.getAttribute('data-sketch-render-version') || 0) >= revision;
      });
    };
    const waitForRenderedView = () => {
      if (token !== pendingViewResetToken) return;
      if (active) {
        requestAnimationFrame(waitForRenderedView);
        return;
      }
      const svg = currentSvg();
      if (!svg) return;
      if (!ready(svg)) {
        requestAnimationFrame(waitForRenderedView);
        return;
      }
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (token !== pendingViewResetToken) return;
        if (active) {
          requestAnimationFrame(waitForRenderedView);
          return;
        }
        const current = currentSvg();
        if (!current) return;
        if (!ready(current)) {
          requestAnimationFrame(waitForRenderedView);
          return;
        }
        removeViewCommitSnapshot(current);
        clearPreview(current);
      }));
    };
    waitForRenderedView();
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
    const nextScale = visualScale * factor;
    let tx = point[0] + factor * (visualTranslateX - point[0]);
    const grid = window.__ngsolveSketchLiveGrid;
    const maxAxisX = grid ? Number(grid.negativeRBufferPx) : NaN;
    if (grid && Number.isFinite(maxAxisX) && Number.isFinite(grid.originX)) {
      tx = Math.min(tx, maxAxisX - nextScale * grid.originX);
    }
    const ty = point[1] + factor * (visualTranslateY - point[1]);
    applyCanvasTransform(nextScale, tx, ty, svg);
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
      if (event.type === 'mousedown' && targetIsCanvas && [0, 1].includes(event.button)) {
        remember(event);
        // Avoid the browser's middle-click autoscroll while preserving the
        // compatibility mouse events used by the canvas callbacks.
        if (event.button === 1) event.preventDefault();
      }
      else if (event.type === 'mouseup' && (targetIsCanvas || active)) remember(event);
      return;
    }
    if (event.type === 'mousedown') {
      beginGesture(event, svg);
      return;
    }
    if (!active) return;
    if (event.type === 'mousemove') {
      updateHover(event, svg);
      moveGesture(event, svg);
    }
    if (event.type === 'mouseup') finishGesture(event, svg);
  };
  if (pointerSupported) {
    document.addEventListener('pointerdown', event => beginGesture(event, currentSvg()), true);
    document.addEventListener('pointermove', event => {
      const svg = currentSvg();
      updateHover(event, svg);
      moveGesture(event, svg);
      if (!active && svg) {
        if (!targetIsSvg(event, svg)) {
          clearInferenceMarker(svg);
          return;
        }
        const screen = svgPoint(svg, event);
        if (screen) {
          const point = sketchPoint(toScenePoint(screen), svg);
          const state = window.__ngsolveSketchConstruction || {};
          const tool = window.__ngsolveSketchTool || 'select';
          const hasPending = tool === 'line'
            ? (Array.isArray(state.linePoints) && state.linePoints.length > 0)
            : (tool === 'arc'
              ? (Array.isArray(state.arcPoints) && state.arcPoints.length > 0)
              : (tool === 'circle' && state.circleVariant === 'three-point'
                && Array.isArray(state.circlePoints) && state.circlePoints.length > 0));
          if (hasPending) updateConstructionPreview(svg, point);
        }
      }
    }, true);
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
            # The initial render can run before the browser JS bridge is ready.
            # Re-send settings after the mounted handler confirms that bridge.
            self._sync_canvas_interaction_settings()
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

    def _read_canvas_edge_id(self, value):
        """Return the sketch edge hit by a native pointer event, if any."""
        if not isinstance(value, dict):
            return None
        event_type = value.get("type")
        timestamp = value.get("timeStamp")
        if event_type not in {None, "mousedown"}:
            return value.get("edge_id") or value.get("edgeId")
        try:
            timestamp_js = "null" if timestamp is None else repr(float(timestamp))
            edge_id = self.js.eval(
                "((timestamp) => { "
                "const events = window.__ngsolveSketchPointerEvents || []; "
                "const event = (timestamp === null ? null : events.find(item => item.type === 'mousedown' "
                "&& Math.abs(item.timeStamp - timestamp) < 1)) "
                "|| [...events].reverse().find(item => item.type === 'mousedown'); "
                "return event ? event.edgeId || null : null; })"
                f"({timestamp_js})"
            )
            return str(edge_id) if edge_id is not None else None
        except Exception:
            return value.get("edge_id") or value.get("edgeId")

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
        if not isinstance(value, dict):
            return
        try:
            button = int(value.get("button", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return
        if button not in {0, 1}:
            return
        tool = self.sketch_tool
        middle_pan = button == 1
        editor = self._editor_drawer or {}
        editor_region_pick = bool(editor.get("select_region_in_viewport"))
        hp_selection = bool(self.hp_target_selection_active)
        # Resolve the hit target before _canvas_event_point consumes the
        # matching browser pointer record to obtain clientX/clientY.
        hit_tool = tool in {"select", "trim"}
        region_id = self._read_canvas_region_id(value) if hit_tool and not middle_pan else None
        edge_id = self._read_canvas_edge_id(value) if hit_tool and not middle_pan else None
        browser_hit_is_known = region_id == ""
        if browser_hit_is_known:
            region_id = None
        point = self._canvas_event_point(event, refresh_transform=True)
        if point is None:
            return
        if not middle_pan:
            if tool not in {"select", "trim"}:
                point = self._snap_canvas_point(point)
            else:
                if self.active_section == "boundaries" or hp_selection or tool == "trim":
                    edge_id = edge_id or self._edge_at_canvas_point(point)
                if not region_id and not browser_hit_is_known:
                    region_id = self._region_at_canvas_point(point)
        if middle_pan:
            operation = "pan"
        elif tool == "trim":
            operation = "trim"
        elif tool != "select":
            operation = "draw"
        elif hp_selection:
            operation = "hp_select"
        elif editor_region_pick:
            operation = "editor_region_pick"
        elif self.active_section == "boundaries":
            operation = "edge" if edge_id else ("region" if region_id else "marquee")
        elif value.get("shiftKey") or value.get("ctrlKey"):
            operation = "marquee"
        elif edge_id:
            operation = "edge"
        elif region_id:
            operation = "region"
        else:
            operation = "marquee"
        self._canvas_drag = {
            "tool": tool,
            "operation": operation,
            "start": point,
            "current": point,
            "region_id": region_id,
            "edge_id": edge_id,
            "additive": bool(value.get("shiftKey") or value.get("ctrlKey")),
            "moved": False,
        }
        # Activate a region as soon as the user presses it. The browser-side
        # pointer capture keeps the gesture alive for dragging, but it can
        # retarget the eventual click away from the SVG region element. Doing
        # selection here makes a simple viewport press reliable and exposes
        # that region's properties before the pointer is released.
        if operation == "region" and region_id and self.active_section not in {"boundaries", "sources"}:
            self.select_region(region_id)

    def _sync_pending_construction_preview(self):
        if not hasattr(self, "_pending_line_points"):
            return
        project, _ = self._canvas_projection()
        payload = {
            "linePoints": [list(project(point)) for point in self._pending_line_points],
            "arcPoints": [],
            "arcTangent": None,
            "arcVariant": self._sketch_tool_variant.get("arc", "three-point"),
            "circlePoints": [],
            "circleVariant": self._sketch_tool_variant.get("circle", "center-radius"),
        }
        if self.sketch_tool == "circle" and self._pending_circle_points:
            payload["circlePoints"] = [list(project(point)) for point in self._pending_circle_points]
        if self.sketch_tool == "arc" and self._pending_arc_points:
            points = self._pending_arc_points
            if self._sketch_tool_variant.get("arc") == "tangent" and len(points) >= 2:
                endpoint, tangent = points[0], points[1]
                base = project(endpoint)
                adjacent = project((endpoint[0] + tangent[0], endpoint[1] + tangent[1]))
                payload["arcPoints"] = [list(base)]
                payload["arcTangent"] = [adjacent[0] - base[0], adjacent[1] - base[1]]
            else:
                payload["arcPoints"] = [list(project(point)) for point in points]
        try:
            self.js.eval(
                "window.__ngsolveSketchConstruction = "
                + json.dumps(payload, allow_nan=False)
                + "; if (window.__ngsolveRefreshConstructionPreview) "
                  "window.__ngsolveRefreshConstructionPreview();"
            )
        except Exception:
            pass

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

    def _edge_at_canvas_point(self, point, tolerance=7.0):
        """Hit-test edges in viewport pixels before falling back to regions."""
        project, _ = self._canvas_projection()
        x, y = float(point[0]), float(point[1])
        candidates = []
        for order, edge in enumerate(self.model["geometry"].get("edges", [])):
            curve = edge.get("curve", {})
            vertices = curve.get("sampled_points") if isinstance(curve, dict) else None
            vertices = vertices if isinstance(vertices, list) and len(vertices) >= 2 else edge.get("vertices", [])
            for first, second in zip(vertices, vertices[1:]):
                x1, y1 = project(first)
                x2, y2 = project(second)
                dx, dy = x2 - x1, y2 - y1
                length_squared = dx * dx + dy * dy
                if length_squared <= 1e-18:
                    distance_squared = (x - x1) ** 2 + (y - y1) ** 2
                else:
                    fraction = min(1.0, max(0.0, ((x - x1) * dx + (y - y1) * dy) / length_squared))
                    nearest_x, nearest_y = x1 + fraction * dx, y1 + fraction * dy
                    distance_squared = (x - nearest_x) ** 2 + (y - nearest_y) ** 2
                if distance_squared <= tolerance * tolerance:
                    candidates.append((distance_squared, order, edge["id"]))
        return min(candidates)[2] if candidates else None

    def _on_canvas_mouse_move(self, event):
        if self._canvas_drag is None:
            return
        point = self._canvas_event_point(event)
        if point is None:
            return
        if self._canvas_drag["operation"] == "draw":
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
                moved = bool(completed[8]) or math.hypot(end[0] - start[0], end[1] - start[1]) >= 4.0
                view = tuple(float(completed[index]) for index in (9, 10, 11)) + (int(completed[12]),)
                edge_id = (str(completed[13]) or None) if len(completed) >= 14 else None
                sketch_screen_points = completed[14] if len(completed) >= 15 and isinstance(completed[14], (list, tuple)) else []
                tool_variant = str(completed[15]) if len(completed) >= 16 else self._sketch_tool_variant.get(tool, "")
                centerpoint_clockwise = bool(completed[17]) if len(completed) >= 18 else None
            except (TypeError, ValueError, OverflowError):
                completed = None
            else:
                pending_drag = self._canvas_drag or {}
                pending_operation = pending_drag.get("operation")
                special_command = str(completed[16]).lower() if len(completed) >= 17 else ""
                if operation == "line_finish":
                    self._canvas_drag = None
                    self._screen_to_svg = None
                    self._finish_line_sketch_chain(select_tool=(special_command == "escape"))
                    return
                if operation == "arc_cancel":
                    self._canvas_drag = None
                    self._screen_to_svg = None
                    self._pending_arc_points = []
                    self.set_sketch_tool("select", announce=False)
                    self._message("Arc cancelled.")
                    return
                if operation == "trim":
                    self._canvas_drag = None
                    self._screen_to_svg = None
                    view_changed = self._commit_browser_canvas_view(view, render=False, clamp_negative_r=True)
                    if moved:
                        stroke = sketch_screen_points or [start, end]
                        world_stroke = self._sketch_screen_points_to_world(stroke, snap=False)
                        # If the pencil starts on a visible edge, keep that edge
                        # as its target. This avoids trimming a neighboring curve
                        # merely because it is close to the drawn stroke; strokes
                        # that start in blank space still trim every crossed edge.
                        trim_options = {"target_edge_id": edge_id} if edge_id else {}
                        changed = self._trim_sketch_entities_along_stroke(
                            world_stroke, **trim_options
                        )
                    elif edge_id:
                        changed = self._trim_sketch_edge_at_canvas_point(edge_id, end)
                    else:
                        self._message("Drag the trim pencil across sketch edges to remove them.")
                        changed = False
                    if view_changed and not changed:
                        self._render_canvas_after_interaction()
                    return
                if operation == "draw" and (tool in {"line", "arc", "spline"} or (tool == "circle" and tool_variant == "three-point")):
                    self._canvas_drag = None
                    self._screen_to_svg = None
                    view_changed = self._commit_browser_canvas_view(view, render=False, clamp_negative_r=True)
                    if tool == "line":
                        if moved:
                            points = self._sketch_screen_points_to_world([start, end], snap=True)
                            if self._pending_line_points:
                                if math.dist(self._pending_line_points[-1], points[0]) > 1e-12:
                                    self._pending_line_points.append(points[0])
                                self._pending_line_points.append(points[1])
                            else:
                                self._pending_line_points = points
                            self._finish_line_sketch_chain(select_tool=True)
                        else:
                            world_point = self._sketch_screen_points_to_world([start], snap=True)[0]
                            on_edge = bool(edge_id or (
                                self._pending_line_points
                                and self._edge_at_canvas_point(start) is not None
                            ))
                            self._accept_line_sketch_point(world_point, edge_termination=on_edge)
                            if view_changed:
                                self._render_canvas_after_interaction()
                    elif tool == "arc" and not moved:
                        world_point = self._sketch_screen_points_to_world([start], snap=True)[0]
                        self._accept_arc_sketch_point(
                            world_point, variant=tool_variant,
                            centerpoint_clockwise=centerpoint_clockwise,
                        )
                        if view_changed:
                            self._render_canvas_after_interaction()
                    elif tool == "circle" and tool_variant == "three-point" and not moved:
                        world_point = self._sketch_screen_points_to_world([start], snap=True)[0]
                        self._accept_circle_sketch_point(world_point)
                        if view_changed:
                            self._render_canvas_after_interaction()
                    elif moved and tool == "spline":
                        raw_points = sketch_screen_points or [start, end]
                        points = self._sketch_screen_points_to_world(raw_points, snap=False)
                        self._add_sketch_entity("spline", points)
                    elif view_changed:
                        self._render_canvas_after_interaction()
                    return
                if pending_operation == "editor_region_pick":
                    self._canvas_drag = None
                    self._screen_to_svg = None
                    view_changed = self._commit_browser_canvas_view(view, render=False)
                    if not moved and region_id:
                        self._accept_editor_viewport_region(region_id)
                    elif view_changed:
                        self._render_canvas_after_interaction()
                    return
                if pending_operation == "hp_select" and self.hp_target_selection_active:
                    self._canvas_drag = None
                    self._screen_to_svg = None
                    if operation == "pan" and moved:
                        view_changed = self._commit_browser_canvas_view(view, render=False, clamp_negative_r=True)
                        if view_changed:
                            self._render_canvas_after_interaction()
                        else:
                            self._schedule_canvas_interaction_reset()
                    else:
                        view_changed = self._commit_browser_canvas_view(view, render=False)
                        if moved:
                            self._select_hp_targets_in_canvas_box(start, end, additive=additive)
                        elif edge_id:
                            self._toggle_hp_edge_target(edge_id)
                        elif region_id:
                            self._toggle_hp_region_target(region_id)
                        if view_changed:
                            self._render_canvas_after_interaction()
                    return
                if self.active_section == "boundaries":
                    region_id = region_id or pending_drag.get("region_id")
                    edge_id = edge_id or pending_drag.get("edge_id")
                    self._canvas_drag = None
                    self._screen_to_svg = None
                    view_changed = self._commit_browser_canvas_view(
                        view, render=False, clamp_negative_r=True
                    )
                    if moved and operation == "draw":
                        before = len(self.model["geometry"].get("regions", []))
                        self._create_region_from_canvas_drag(tool, start, end)
                        if len(self.model["geometry"].get("regions", [])) == before and view_changed:
                            self._render_canvas_after_interaction()
                    elif moved and operation == "marquee":
                        self._select_edges_in_canvas_box(start, end, additive=additive, boundary_stage=True)
                    elif moved and operation == "pan":
                        if view_changed:
                            self._render_canvas_after_interaction()
                        else:
                            self._schedule_canvas_interaction_reset()
                    elif not moved:
                        if edge_id:
                            self.toggle_boundary_stage_edge(edge_id)
                        elif region_id:
                            self.toggle_boundary_stage_region(region_id)
                        elif operation != "pan":
                            self._clear_canvas_selection()
                    if view_changed and operation not in {"pan", "draw"}:
                        self._render_canvas_after_interaction()
                    return
                if self.active_section == "sources":
                    self._canvas_drag = None
                    self._screen_to_svg = None
                    view_changed = self._commit_browser_canvas_view(view, render=False, clamp_negative_r=True)
                    if not moved and operation == "region" and region_id:
                        self._toggle_active_source_region(region_id)
                    elif not moved and operation != "pan":
                        self._clear_canvas_selection()
                    elif moved and operation == "pan":
                        if view_changed:
                            self._render_canvas_after_interaction()
                        else:
                            self._schedule_canvas_interaction_reset()
                    elif view_changed:
                        self._render_canvas_after_interaction()
                    return
                self._canvas_drag = None
                self._screen_to_svg = None
                if moved and operation in {"region", "edge"} and region_id:
                    self._move_region_from_canvas_drag(region_id, start, end, preserve_canvas=True)
                    # Interpret the pointer coordinates in the view that was
                    # visible during the drag. If a wheel transform was still
                    # pending, commit it after the region move and redraw once.
                    if self._commit_browser_canvas_view(view, render=False):
                        self._render_canvas_after_interaction()
                elif moved and operation == "marquee":
                    view_changed = self._commit_browser_canvas_view(view, render=False)
                    self._select_edges_in_canvas_box(start, end, additive=additive)
                    if view_changed:
                        self._render_canvas_after_interaction()
                elif moved and operation == "pan":
                    view_changed = self._commit_browser_canvas_view(
                        view, render=False, clamp_negative_r=True
                    )
                    if view_changed:
                        self._render_canvas_after_interaction()
                    else:
                        self._schedule_canvas_interaction_reset()
                elif moved and operation == "draw":
                    view_changed = self._commit_browser_canvas_view(view, render=False)
                    region_count = len(self.model["geometry"].get("regions", []))
                    self._create_region_from_canvas_drag(tool, start, end)
                    if len(self.model["geometry"].get("regions", [])) == region_count:
                        if view_changed:
                            self._render_canvas_after_interaction()
                        else:
                            self._schedule_canvas_interaction_reset()
                elif edge_id:
                    self.select_edge(edge_id, additive=additive)
                elif operation == "region" and region_id:
                    self.select_region(region_id, additive=additive)
                elif not moved and operation != "pan":
                    view_changed = self._commit_browser_canvas_view(view, render=False)
                    self._clear_canvas_selection()
                    if view_changed:
                        self._render_canvas_after_interaction()
                elif self._commit_browser_canvas_view(view, render=False):
                    self._render_canvas_after_interaction()
                return
        drag = self._canvas_drag
        if drag is None:
            self._screen_to_svg = None
            return
        point = self._canvas_event_point(event)
        if point is not None:
            if drag["operation"] == "draw":
                point = self._snap_canvas_point(point)
            drag["current"] = point
        start, end = drag["start"], drag["current"]
        moved = math.hypot(end[0] - start[0], end[1] - start[1]) >= 4.0
        self._canvas_drag = None
        self._screen_to_svg = None
        if drag.get("operation") == "editor_region_pick":
            if not moved and drag.get("region_id"):
                self._accept_editor_viewport_region(drag["region_id"])
            return
        if drag.get("operation") == "hp_select" and self.hp_target_selection_active:
            if moved:
                self._select_hp_targets_in_canvas_box(start, end, additive=drag.get("additive", False))
            elif drag.get("edge_id"):
                self._toggle_hp_edge_target(drag["edge_id"])
            elif drag.get("region_id"):
                self._toggle_hp_region_target(drag["region_id"])
            return
        if self.active_section == "boundaries":
            if moved and drag["operation"] == "draw":
                self._create_region_from_canvas_drag(drag["tool"], start, end)
            elif moved and drag["operation"] == "marquee":
                self._select_edges_in_canvas_box(start, end, additive=drag["additive"], boundary_stage=True)
            elif not moved:
                if drag.get("edge_id"):
                    self.toggle_boundary_stage_edge(drag["edge_id"])
                elif drag.get("region_id"):
                    self.toggle_boundary_stage_region(drag["region_id"])
                elif drag.get("operation") != "pan":
                    self._clear_canvas_selection()
            elif moved and drag["operation"] == "pan":
                self._pan_canvas_by_pixels(start, end)
            return
        if self.active_section == "sources":
            if moved and drag.get("operation") == "pan":
                self._pan_canvas_by_pixels(start, end)
            elif not moved and drag.get("operation") == "region" and drag.get("region_id"):
                self._toggle_active_source_region(drag["region_id"])
            elif not moved and drag.get("operation") != "pan":
                self._clear_canvas_selection()
            return
        if moved and drag["operation"] in {"region", "edge"} and drag.get("region_id"):
            self._move_region_from_canvas_drag(drag["region_id"], start, end)
        elif moved and drag["operation"] == "marquee":
            self._select_edges_in_canvas_box(start, end, additive=drag["additive"])
        elif moved and drag["operation"] == "pan":
            self._pan_canvas_by_pixels(start, end)
        elif moved and drag["operation"] == "draw":
            self._create_region_from_canvas_drag(drag["tool"], start, end)
        elif drag.get("edge_id"):
            self.select_edge(drag["edge_id"], additive=drag["additive"])
        elif drag["operation"] == "region" and drag.get("region_id"):
            self.select_region(drag["region_id"], additive=drag.get("additive", False))
        elif not moved and drag.get("operation") != "pan":
            self._clear_canvas_selection()

    def _accept_line_sketch_point(self, point, *, edge_termination=False):
        point = tuple(_round_sketch_m(float(value)) for value in self._snap_sketch_endpoint(point))
        if not self._pending_line_points:
            self._pending_line_points = [point]
            self._sync_pending_construction_preview()
            self._message("Line chain: choose the next point. Click another edge or press Enter/Esc to finish.")
            return True
        point = tuple(self._snap_sketch_endpoint(point))
        if math.dist(point, self._pending_line_points[-1]) > 1e-12:
            self._pending_line_points.append(point)
        if edge_termination:
            self._finish_line_sketch_chain()
            return True
        self._sync_pending_construction_preview()
        self._message("Line chain: choose another point, or click an edge / press Enter/Esc to finish.")
        return True

    def _finish_line_sketch_chain(self, *, select_tool=False):
        points = [tuple(point) for point in self._pending_line_points]
        self._pending_line_points = []
        self._sync_pending_construction_preview()
        segments = [
            (first, second)
            for first, second in zip(points, points[1:])
            if math.dist(first, second) > 1e-12
        ]
        added = 0
        for first, second in segments:
            if self._add_sketch_entity("line", [first, second]):
                added += 1
        if select_tool:
            self.set_sketch_tool("select", announce=False)
        elif added == 0:
            self._sync_pending_construction_preview()
        if added:
            self._message(f"Finished line chain ({added} segment{'s' if added != 1 else ''}).")
        else:
            self._message("No line segment was created; choose two distinct points.", error=True)
        return added > 0

    def _on_canvas_keydown(self, event):
        payload = getattr(event, "value", None)
        key = payload.get("key") if isinstance(payload, dict) else getattr(payload, "key", None)
        if not key:
            key = getattr(event, "key", None)
        if not key and isinstance(payload, str):
            key = payload
        key = str(key or "").lower()
        if self.sketch_tool == "arc" and key == "escape":
            self._pending_arc_points = []
            self.set_sketch_tool("select", announce=False)
            self._message("Arc cancelled.")
            return
        if self.sketch_tool != "line" or not self._pending_line_points:
            return
        if key in {"enter", "numpadenter", "escape"}:
            self._finish_line_sketch_chain(select_tool=(key == "escape"))

    def _on_canvas_pointer_cancel(self, event=None):
        """Discard an incomplete sketch gesture after a cancelled pointer."""
        self._canvas_drag = None
        self._screen_to_svg = None

    @contextmanager
    def _batch_frontend_updates(self):
        """Coalesce duplicate component updates and flush them in order.

        Browser-backed components share one frontend event loop. Dispatching
        their patches from a thread pool can race slot updates and briefly
        unmount visible panels, so keep the final flush on the caller thread.
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
            for component, data, method, blocking in queued.values():
                send_update(component, data, method, blocking=blocking)

    def _select_edges_in_canvas_box(self, start, end, *, additive=False, boundary_stage=False):
        project, unproject = self._canvas_projection()
        box = (min(start[0], end[0]), max(start[0], end[0]), min(start[1], end[1]), max(start[1], end[1]))
        window_selection = end[0] >= start[0]
        selected = []
        selected_regions = []
        box_corners = [unproject((x, y)) for x in (box[0], box[1]) for y in (box[2], box[3])]
        for region in self.model["geometry"].get("regions", []):
            vertices = region.get("vertices", [])
            points = [project(point) for point in vertices]
            if len(points) < 3:
                continue
            if window_selection:
                matches = all(_point_in_box(point, box) for point in points)
            else:
                matches = (
                    any(_point_in_box(point, box) for point in points)
                    or any(_segment_intersects_box(points[i], points[(i + 1) % len(points)], box) for i in range(len(points)))
                    or any(_point_in_or_on_polygon(corner, vertices) for corner in box_corners)
                )
            if matches:
                selected_regions.append(region["id"])
                selected.extend(region.get("edge_ids", []))
        for edge in self.model["geometry"].get("edges", []):
            points = [project(point) for point in edge.get("vertices", [])]
            if len(points) != 2:
                continue
            matches = (
                all(_point_in_box(point, box) for point in points)
                if window_selection
                else _segment_intersects_box(points[0], points[1], box)
            )
            if matches:
                selected.append(edge["id"])
        selected_regions = self._expand_region_group_ids(selected_regions)
        regions_by_id = {
            region.get("id"): region
            for region in self.model.get("geometry", {}).get("regions", [])
        }
        for selected_region_id in selected_regions:
            selected.extend(regions_by_id.get(selected_region_id, {}).get("edge_ids", []))
        selected = list(dict.fromkeys(selected))
        if additive:
            selected = list(dict.fromkeys([*self.selected_edge_ids, *selected]))
            selected_regions = self._expand_region_group_ids([
                *getattr(self, "selected_region_ids", []), *selected_regions
            ])
        self.selected_edge_ids = selected
        self.selected_edge_id = selected[-1] if selected else None
        self.selected_region_ids = selected_regions
        self.selected_region_id = selected_regions[0] if len(selected_regions) == 1 else None
        self._selected_condition_id = self.selected_boundary_id if boundary_stage else None
        self.active_section = "boundaries" if boundary_stage else "geometry"
        self.layout["active_section"] = self.active_section
        kind = "enclosed" if window_selection else "crossed"
        self._message(f"Selected {len(selected_regions)} region(s) and {len(selected)} unique edge(s) ({kind} selection).")
        self._update_model_tree_selection()
        if boundary_stage:
            self._render_inspector()
            self._sync_boundary_selection_controls()
            self._assign_active_boundary_to_edges(selected)
        else:
            self._update_geometry_inspector()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()
        return selected

    def _create_region_from_canvas_drag(self, tool, start, end):
        if self.snap_to_grid:
            start = self._snap_canvas_point(start)
            end = self._snap_canvas_point(end)
        _, unproject = self._canvas_projection()
        # Mouse placement is intentionally limited to 0.01 mm (1e-5 m). The
        # property editor remains full precision for direct SI input.
        first = tuple(_round_sketch_m(coordinate) for coordinate in unproject(start))
        second = tuple(_round_sketch_m(coordinate) for coordinate in unproject(end))
        before = len(self.model["geometry"]["regions"])
        if tool == "rectangle":
            if self._sketch_tool_variant.get("rectangle", "corner") == "center":
                second = (2 * first[0] - second[0], 2 * first[1] - second[1])
            r_min, r_max = sorted((first[0], second[0]))
            z_min, z_max = sorted((first[1], second[1]))
            values = {
                "r_min": _round_sketch_m(r_min),
                "z_min": _round_sketch_m(z_min),
                "width": _round_sketch_m(r_max - r_min),
                "height": _round_sketch_m(z_max - z_min),
            }
        else:
            if self._sketch_tool_variant.get("circle", "center-radius") == "diameter":
                center = ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)
                radius = math.hypot(second[0] - first[0], second[1] - first[1]) / 2.0
            else:
                center = first
                radius = math.hypot(second[0] - first[0], second[1] - first[1])
            values = {
                "r_center": _round_sketch_m(center[0]),
                "z_center": _round_sketch_m(center[1]),
                "radius": _round_sketch_m(radius),
            }
        for key, value in values.items():
            self._primitive_values[(tool, key)] = value
        self._add_primitive(tool)
        if len(self.model["geometry"]["regions"]) > before:
            self.set_sketch_tool("select", announce=False)

    def _sketch_screen_points_to_world(self, points, *, snap=False):
        _, unproject = self._canvas_projection()
        result = []
        for raw in points:
            screen_point = (float(raw[0]), float(raw[1]))
            world = tuple(float(value) for value in unproject(screen_point))
            if snap:
                inferred, matched = self._snap_sketch_endpoint(world, return_match=True)
                if matched:
                    world = inferred
                else:
                    grid_screen_point = self._snap_canvas_point(screen_point)
                    grid_world = tuple(float(value) for value in unproject(grid_screen_point))
                    inferred, matched = self._snap_sketch_endpoint(grid_world, return_match=True)
                    world = inferred if matched else grid_world
            result.append((_round_sketch_m(float(world[0])), _round_sketch_m(float(world[1]))))
        return result

    def _snap_sketch_endpoint(self, point, *, return_match=False):
        """Return the nearest CAD inference target within ten screen pixels.

        Endpoint snaps take priority, followed by curve centers, line/arc
        midpoints, and then the nearest point on any visible sketch edge.
        Splines expose endpoint and edge snaps only.
        """
        project, _ = self._canvas_projection()
        target = project(point)
        tolerance = 10.0
        endpoint_candidates = []
        center_candidates = []
        midpoint_candidates = []
        edge_candidates = []
        sources = []
        sketch_endpoint_sources = []
        analytic_curves = []

        def add_candidate(collection, candidate):
            try:
                world = (float(candidate[0]), float(candidate[1]))
                screen = project(world)
                distance = math.hypot(target[0] - screen[0], target[1] - screen[1])
            except (TypeError, ValueError, IndexError, OverflowError):
                return
            if math.isfinite(distance) and distance <= tolerance:
                collection.append((distance, world))

        def sample_midpoint(samples):
            if len(samples) < 2:
                return None
            lengths = [math.dist(first, second) for first, second in zip(samples, samples[1:])]
            total = sum(lengths)
            if not math.isfinite(total) or total <= 1e-15:
                return None
            remaining = total / 2.0
            for index, (first, second, length) in enumerate(zip(samples, samples[1:], lengths)):
                if remaining <= length or index == len(lengths) - 1:
                    fraction = 0.0 if length <= 1e-15 else min(1.0, max(0.0, remaining / length))
                    return (
                        float(first[0]) + fraction * (float(second[0]) - float(first[0])),
                        float(first[1]) + fraction * (float(second[1]) - float(first[1])),
                    )
                remaining -= length
            return tuple(float(value) for value in samples[-1])

        def add_analytic_curve(kind, curve, endpoints):
            if kind not in {"arc", "circle"} or not isinstance(curve, dict):
                return
            center = curve.get("center")
            radius = curve.get("radius")
            if not isinstance(center, (list, tuple)) or len(center) < 2:
                return
            try:
                center = (float(center[0]), float(center[1]))
                radius = float(radius)
            except (TypeError, ValueError, OverflowError):
                return
            if not math.isfinite(radius) or radius <= 0:
                return
            add_candidate(center_candidates, center)
            endpoints = endpoints if isinstance(endpoints, (list, tuple)) else []
            start_point = endpoints[0] if endpoints else center
            end_point = endpoints[-1] if endpoints else center
            through = endpoints[1] if kind == "arc" and len(endpoints) > 2 else None
            analytic_curves.append((
                kind, center, radius, tuple(start_point),
                tuple(through) if through is not None else None,
                tuple(end_point), bool(curve.get("clockwise", False)),
            ))

        for entity in self.model["geometry"].get("sketch_entities", []):
            samples = entity.get("sampled_points") or entity.get("points") or []
            curve_type = str(entity.get("type", "line")).lower()
            if len(samples) >= 2:
                samples = [tuple(float(value) for value in sample[:2]) for sample in samples]
                sources.append(samples)
                if curve_type != "circle":
                    sketch_endpoint_sources.append((samples, curve_type))
                if curve_type in {"line", "arc"}:
                    midpoint = sample_midpoint(samples)
                    if midpoint is not None:
                        add_candidate(midpoint_candidates, midpoint)
                add_analytic_curve(curve_type, entity, entity.get("points"))

        represented = {
            edge.get("sketch_entity_id")
            for edge in self.model["geometry"].get("edges", [])
            if edge.get("sketch_entity_id")
        }
        for edge in self.model["geometry"].get("edges", []):
            if edge.get("sketch_entity_id") in represented:
                continue
            curve = edge.get("curve", {})
            curve = curve if isinstance(curve, dict) else {}
            curve_type = str(curve.get("type", "line")).lower()
            samples = curve.get("sampled_points")
            samples = samples if isinstance(samples, list) and len(samples) >= 2 else edge.get("vertices", [])
            if len(samples) >= 2:
                samples = [tuple(float(value) for value in sample[:2]) for sample in samples]
                sources.append(samples)
                if curve_type != "circle":
                    sketch_endpoint_sources.append((samples, curve_type))
                if curve_type in {"line", "arc"}:
                    midpoint = sample_midpoint(samples)
                    if midpoint is not None:
                        add_candidate(midpoint_candidates, midpoint)
                add_analytic_curve(curve_type, curve, edge.get("vertices", []))

        for samples, curve_type in sketch_endpoint_sources:
            if curve_type == "circle":
                continue
            for endpoint in (samples[0], samples[-1]):
                add_candidate(endpoint_candidates, endpoint)

        for kind, center, radius, start_point, through, end_point, clockwise in analytic_curves:
            dx, dy = float(point[0]) - center[0], float(point[1]) - center[1]
            if dx * dx + dy * dy <= 1e-24:
                continue
            angle = math.atan2(dy, dx)
            if kind == "arc":
                start_angle = math.atan2(start_point[1] - center[1], start_point[0] - center[0])
                end_angle = math.atan2(end_point[1] - center[1], end_point[0] - center[0])
                tau = 2.0 * math.pi
                if clockwise:
                    total = (start_angle - end_angle) % tau
                    offset = (start_angle - angle) % tau
                else:
                    total = (end_angle - start_angle) % tau
                    offset = (angle - start_angle) % tau
                if through is not None:
                    through_angle = math.atan2(through[1] - center[1], through[0] - center[0])
                    through_offset = (
                        (start_angle - through_angle) % tau
                        if clockwise else (through_angle - start_angle) % tau
                    )
                    if through_offset > total + 1e-10:
                        clockwise = not clockwise
                        total = (
                            (start_angle - end_angle) % tau
                            if clockwise else (end_angle - start_angle) % tau
                        )
                        offset = (
                            (start_angle - angle) % tau
                            if clockwise else (angle - start_angle) % tau
                        )
                if offset > total + 1e-10:
                    continue
            world_nearest = (
                center[0] + radius * math.cos(angle),
                center[1] + radius * math.sin(angle),
            )
            add_candidate(edge_candidates, world_nearest)

        for samples in sources:
            projected = [project(candidate) for candidate in samples]
            for first, second, screen_first, screen_second in zip(samples, samples[1:], projected, projected[1:]):
                dx, dy = screen_second[0] - screen_first[0], screen_second[1] - screen_first[1]
                length_squared = dx * dx + dy * dy
                fraction = 0.0 if length_squared <= 1e-16 else min(
                    1.0,
                    max(0.0, ((target[0] - screen_first[0]) * dx + (target[1] - screen_first[1]) * dy) / length_squared),
                )
                screen_nearest = (screen_first[0] + fraction * dx, screen_first[1] + fraction * dy)
                distance = math.hypot(target[0] - screen_nearest[0], target[1] - screen_nearest[1])
                if distance <= tolerance:
                    world_nearest = (
                        first[0] + fraction * (second[0] - first[0]),
                        first[1] + fraction * (second[1] - first[1]),
                    )
                    edge_candidates.append((distance, tuple(world_nearest)))

        point_candidates = [
            (distance, priority, world)
            for priority, collection in enumerate(
                (endpoint_candidates, center_candidates, midpoint_candidates)
            )
            for distance, world in collection
        ]
        if point_candidates:
            result = min(point_candidates, key=lambda item: (item[0], item[1]))[2]
            return (result, True) if return_match else result
        if edge_candidates:
            result = min(edge_candidates, key=lambda item: item[0])[1]
            return (result, True) if return_match else result
        result = tuple(point)
        return (result, False) if return_match else result

    @staticmethod
    def _arc_from_three_points(start, through, end):
        ax, ay = start
        bx, by = through
        cx, cy = end
        denominator = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(denominator) <= 1e-20:
            raise ValueError("Arc points must not be collinear.")
        aa, bb, cc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
        center = (
            (aa * (by - cy) + bb * (cy - ay) + cc * (ay - by)) / denominator,
            (aa * (cx - bx) + bb * (ax - cx) + cc * (bx - ax)) / denominator,
        )
        radius = math.hypot(ax - center[0], ay - center[1])
        a0 = math.atan2(ay - center[1], ax - center[0])
        am = math.atan2(by - center[1], bx - center[0])
        a1 = math.atan2(cy - center[1], cx - center[0])
        positive_sweep = (a1 - a0) % (2.0 * math.pi)
        through_sweep = (am - a0) % (2.0 * math.pi)
        clockwise = through_sweep > positive_sweep
        sweep = positive_sweep - 2.0 * math.pi if clockwise else positive_sweep
        count = min(256, max(12, int(math.ceil(abs(sweep) / (math.pi / 48.0)))))
        samples = [
            [center[0] + radius * math.cos(a0 + sweep * index / count),
             center[1] + radius * math.sin(a0 + sweep * index / count)]
            for index in range(count + 1)
        ]
        return center, radius, clockwise, samples

    @staticmethod
    def _sample_spline(control_points):
        points = [tuple(point) for point in control_points]
        if len(points) < 2:
            raise ValueError("Draw a spline with at least two distinct points.")
        sampled = [list(points[0])]
        subdivisions = 5
        for index in range(len(points) - 1):
            p0 = points[max(0, index - 1)]
            p1 = points[index]
            p2 = points[index + 1]
            p3 = points[min(len(points) - 1, index + 2)]
            for step in range(1, subdivisions + 1):
                t = step / subdivisions
                t2, t3 = t * t, t * t * t
                value = []
                for axis in range(2):
                    value.append(0.5 * (
                        (2 * p1[axis])
                        + (-p0[axis] + p2[axis]) * t
                        + (2 * p0[axis] - 5 * p1[axis] + 4 * p2[axis] - p3[axis]) * t2
                        + (-p0[axis] + 3 * p1[axis] - 3 * p2[axis] + p3[axis]) * t3
                    ))
                sampled.append(value)
        return sampled

    @staticmethod
    def _sample_spline3(start, control, end, subdivisions=64):
        """Sample Netgen's rational quadratic spline3 for display and topology."""
        start = tuple(float(value) for value in start)
        control = tuple(float(value) for value in control)
        end = tuple(float(value) for value in end)
        chord = math.dist(start, end)
        control_weight = math.sqrt(0.5 * (
            math.dist(start, control) ** 2 + math.dist(control, end) ** 2
        ))
        if chord <= 1e-14 or control_weight <= 1e-14:
            raise ValueError("Spline endpoints must differ and its control geometry must be finite.")
        weight = chord / control_weight
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError("Spline3 requires a positive finite rational weight.")
        sampled = []
        for step in range(subdivisions + 1):
            t = step / subdivisions
            b0 = (1.0 - t) ** 2
            b1 = weight * t * (1.0 - t)
            b2 = t * t
            denominator = b0 + b1 + b2
            sampled.append([
                (b0 * start[axis] + b1 * control[axis] + b2 * end[axis]) / denominator
                for axis in range(2)
            ])
        return sampled

    @_history_tracked
    def _add_sketch_entity(self, curve_type, points, *, arc_geometry=None):
        if curve_type not in {"line", "arc", "spline", "spline3"}:
            return False
        cleaned = []
        for point in points:
            value = (_round_sketch_m(float(point[0])), _round_sketch_m(float(point[1])))
            if curve_type == "spline3" or not cleaned or math.dist(value, cleaned[-1]) > 1e-12:
                cleaned.append(value)
        if curve_type == "spline3" and len(cleaned) != 3:
            self._message("A Spline3 needs a start point, control point, and end point.", error=True)
            return False
        if len(cleaned) < 2:
            self._message("Sketch curves need two distinct points.", error=True)
            return False
        if curve_type == "arc" and arc_geometry:
            cleaned[0] = tuple(arc_geometry["start"])
            cleaned[1] = tuple(arc_geometry["through"])
            cleaned[-1] = tuple(arc_geometry["end"])
        else:
            cleaned[0] = self._snap_sketch_endpoint(cleaned[0])
            cleaned[-1] = self._snap_sketch_endpoint(cleaned[-1])
        if any(point[0] < -1e-12 for point in cleaned):
            self._message("Sketch geometry must remain at r = 0 or to its right.", error=True)
            return False
        entity = {"id": new_id("curve"), "type": curve_type, "points": [list(p) for p in cleaned]}
        if curve_type == "line":
            if math.dist(cleaned[0], cleaned[-1]) <= 1e-12:
                self._message("Line endpoints must be different.", error=True)
                return False
            entity["sampled_points"] = [list(cleaned[0]), list(cleaned[-1])]
        elif curve_type == "arc":
            if arc_geometry:
                center = tuple(float(value) for value in arc_geometry["center"])
                radius = float(arc_geometry["radius"])
                clockwise = bool(arc_geometry["clockwise"])
                samples = copy.deepcopy(arc_geometry["samples"])
                if not math.isfinite(radius) or radius <= 0 or len(samples) < 2:
                    raise ValueError("Arc radius must be positive and finite.")
            else:
                center, radius, clockwise, samples = self._arc_from_three_points(*cleaned[:3])
            entity.update({
                "center": list(center), "radius": radius, "clockwise": clockwise,
                "points": [list(cleaned[0]), list(cleaned[1]), list(cleaned[-1])],
                "sampled_points": samples,
            })
        elif curve_type == "spline3":
            if math.dist(cleaned[0], cleaned[-1]) <= 1e-12:
                self._message("Spline start and end points must be different.", error=True)
                return False
            try:
                entity["control_point"] = list(cleaned[1])
                entity["sampled_points"] = self._sample_spline3(*cleaned)
            except ValueError as exc:
                self._message(str(exc), error=True)
                return False
        else:
            # Thin freehand noise before smoothing; retain enough points to keep
            # corners and the original endpoints stable.
            reduced = [cleaned[0]]
            for point in cleaned[1:-1]:
                if math.dist(point, reduced[-1]) >= 1e-5:
                    reduced.append(point)
            if math.dist(cleaned[-1], reduced[-1]) > 1e-12:
                reduced.append(cleaned[-1])
            entity["points"] = [list(p) for p in reduced]
            entity["sampled_points"] = self._sample_spline(reduced)
        samples = entity["sampled_points"]
        if len(samples) < 2 or any(not all(math.isfinite(float(v)) for v in point) for point in samples):
            self._message("The sketch curve is not finite.", error=True)
            return False
        self._preserve_canvas_view()
        geometry = self.model["geometry"]
        split_templates = []
        if curve_type == "line":
            for region in geometry.get("regions", []):
                shape = region.get("shape", {})
                if not isinstance(shape, dict) or shape.get("type") not in {"rectangle", "circle"}:
                    continue
                intersections, enters_interior = _polyline_polygon_intersections(
                    samples, region.get("vertices", [])
                )
                if len(intersections) >= 2 and enters_interior:
                    split_templates.append(copy.deepcopy(region))

        if split_templates:
            geometry_snapshot = copy.deepcopy(geometry)
            try:
                templates_by_id = {item.get("id"): item for item in split_templates}
                for region in list(geometry.get("regions", [])):
                    if region.get("id") in templates_by_id:
                        self._materialize_primitive_as_sketch(region)
                geometry["regions"] = [
                    region for region in geometry.get("regions", [])
                    if region.get("id") not in templates_by_id
                ]
                # Primitive outlines become true sketch curves here. Re-snap
                # the new line endpoints to those curves so its endpoints stay
                # topologically connected even when the old circle was shown
                # with a coarser polygonal approximation.
                for endpoint_index in (0, -1):
                    endpoint = self._snap_sketch_endpoint(samples[endpoint_index])
                    samples[endpoint_index] = list(endpoint)
                    entity["points"][endpoint_index] = list(endpoint)
            except (KeyError, TypeError, ValueError, OverflowError) as error:
                geometry.clear()
                geometry.update(geometry_snapshot)
                self._message(f"Could not split the intersected region: {error}", error=True)
                return False

        entities = geometry.setdefault("sketch_entities", [])
        entities.append(entity)
        edges = geometry.setdefault("edges", [])
        curve = {"type": "line"}
        if curve_type == "arc":
            curve = {
                "type": "arc", "center": list(entity["center"]),
                "radius": entity["radius"], "clockwise": entity["clockwise"],
                "sampled_points": copy.deepcopy(samples),
            }
        elif curve_type == "spline":
            curve = {
                "type": "spline", "control_points": copy.deepcopy(entity["points"]),
                "sampled_points": copy.deepcopy(samples),
            }
        elif curve_type == "spline3":
            curve = {
                "type": "spline3", "control_point": copy.deepcopy(entity["control_point"]),
                "sampled_points": copy.deepcopy(samples),
            }
        edges.append({
            "id": new_id("edge"),
            "name": f"{curve_type.title()} edge {1 + sum(1 for item in edges if item.get('sketch_entity_id'))}",
            "vertices": [list(samples[0]), list(samples[-1])],
            "curve": curve,
            "sketch_entity_id": entity["id"],
            "boundary_condition_ids": [],
        })
        self._refresh_sketch_generated_regions(source_templates=split_templates or None)
        self.selected_edge_ids = [
            edge["id"] for edge in geometry.get("edges", [])
            if edge.get("sketch_entity_id") == entity["id"]
        ]
        self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None
        self.selected_region_id = None
        self.active_section = "geometry"
        self.layout["active_section"] = self.active_section
        self._refresh_model_tree()
        self._render_inspector()
        self.render_canvas()
        kind = "spline" if curve_type == "spline" else ("arc" if curve_type == "arc" else "line")
        self._message(f"Added {kind}; closed sketch loops are detected as regions.")
        return True

    def _tangent_at_sketch_endpoint(self, point, tolerance_px=12.0):
        project, _ = self._canvas_projection()
        target = project(point)
        candidates = []
        sources = []
        for entity in self.model["geometry"].get("sketch_entities", []):
            samples = entity.get("sampled_points") or entity.get("points") or []
            if len(samples) >= 2:
                sources.append(samples)
        represented = {
            edge.get("sketch_entity_id")
            for edge in self.model["geometry"].get("edges", [])
            if edge.get("sketch_entity_id")
        }
        for edge in self.model["geometry"].get("edges", []):
            if edge.get("sketch_entity_id") in represented:
                continue
            curve = edge.get("curve", {})
            samples = curve.get("sampled_points") if isinstance(curve, dict) else None
            samples = samples if isinstance(samples, list) and len(samples) >= 2 else edge.get("vertices", [])
            if len(samples) >= 2:
                sources.append(samples)
        for samples in sources:
            # Tangent arcs continue outward from whichever endpoint the user
            # chooses. At the first sample this points opposite the curve's
            # parameter direction; at the last sample it follows it.
            for endpoint, neighbor in (
                (samples[0], samples[1]),
                (samples[-1], samples[-2]),
            ):
                screen = project(endpoint)
                distance = math.hypot(target[0] - screen[0], target[1] - screen[1])
                tangent = (
                    endpoint[0] - neighbor[0],
                    endpoint[1] - neighbor[1],
                )
                norm = math.hypot(*tangent)
                if distance <= tolerance_px and norm > 1e-15:
                    candidates.append((distance, tuple(endpoint), (tangent[0] / norm, tangent[1] / norm)))
        if not candidates:
            return None
        _, endpoint, tangent = min(candidates, key=lambda item: item[0])
        return endpoint, tangent

    def _accept_arc_sketch_point(self, point, *, variant=None, centerpoint_clockwise=None):
        variant = variant or self._sketch_tool_variant.get("arc", "three-point")
        point = tuple(float(value) for value in point)
        pending = self._pending_arc_points

        if variant == "tangent":
            if not pending:
                tangent_start = self._tangent_at_sketch_endpoint(point)
                if tangent_start is None:
                    self._message("Tangent arc must start at an existing line or curve endpoint.", error=True)
                    return False
                endpoint, tangent = tangent_start
                self._pending_arc_points = [tuple(endpoint), tuple(tangent)]
                self._sync_pending_construction_preview()
                self._message("Tangent arc: move the pointer to preview the tangent, then click its end point.")
                return True
            start, tangent = pending[:2]
            end = tuple(self._snap_sketch_endpoint(point))
            chord = (end[0] - start[0], end[1] - start[1])
            chord_squared = chord[0] ** 2 + chord[1] ** 2
            normal = (-tangent[1], tangent[0])
            denominator = 2.0 * (chord[0] * normal[0] + chord[1] * normal[1])
            if chord_squared <= 1e-24 or abs(denominator) <= 1e-18:
                self._message("Choose a distinct end point that makes a finite curved arc.", error=True)
                return False
            offset = chord_squared / denominator
            center = (start[0] + offset * normal[0], start[1] + offset * normal[1])
            radius = math.dist(start, center)
            if not math.isfinite(radius) or radius <= 0:
                self._message("The tangent arc radius must be positive.", error=True)
                return False
            start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
            end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
            positive_sweep = (end_angle - start_angle) % (2.0 * math.pi)
            ccw_tangent = (-math.sin(start_angle), math.cos(start_angle))
            sweep = positive_sweep if ccw_tangent[0] * tangent[0] + ccw_tangent[1] * tangent[1] >= 0 else positive_sweep - 2.0 * math.pi
            if abs(sweep) <= 1e-12 or abs(sweep) >= 2.0 * math.pi - 1e-12:
                self._message("The tangent arc must have a nonzero angular span.", error=True)
                return False
            through_angle = start_angle + sweep / 2.0
            through = (
                center[0] + radius * math.cos(through_angle),
                center[1] + radius * math.sin(through_angle),
            )
            count = min(512, max(12, int(math.ceil(abs(sweep) / (math.pi / 48.0)))))
            samples = [
                [center[0] + radius * math.cos(start_angle + sweep * index / count),
                 center[1] + radius * math.sin(start_angle + sweep * index / count)]
                for index in range(count + 1)
            ]
            self._pending_arc_points = []
            self._sync_pending_construction_preview()
            try:
                return self._add_sketch_entity(
                    "arc", [start, through, end],
                    arc_geometry={
                        "start": start, "through": through, "end": end,
                        "center": center, "radius": radius, "clockwise": sweep < 0,
                        "samples": samples,
                    },
                )
            except ValueError as exc:
                self._message(str(exc), error=True)
                return False

        if variant in {"three-point", "spline3"}:
            point = tuple(self._snap_sketch_endpoint(point))
            label = "Spline" if variant == "spline3" else "3-point arc"
            if not pending:
                self._pending_arc_points = [point]
                self._sync_pending_construction_preview()
                self._message(f"{label}: click the start point; next click the end point.")
                return True
            if len(pending) == 1:
                self._pending_arc_points.append(point)
                self._sync_pending_construction_preview()
                preview_point = "control point" if variant == "spline3" else "through-point C"
                self._message(f"{label}: move the pointer to preview the {preview_point}, then click.")
                return True
            start, end = pending[:2]
            self._pending_arc_points = []
            self._sync_pending_construction_preview()
            if variant == "spline3":
                return self._add_sketch_entity("spline3", [start, point, end])
            try:
                self._arc_from_three_points(start, point, end)
                return self._add_sketch_entity("arc", [start, point, end])
            except ValueError as exc:
                self._message(str(exc), error=True)
                return False

        if variant == "centerpoint":
            point = tuple(self._snap_sketch_endpoint(point))
            if not pending:
                self._pending_arc_points = [point]
                self._sync_pending_construction_preview()
                self._message("Centerpoint arc: click the center; next click the start point.")
                return True
            if len(pending) == 1:
                start = point
                radius = math.dist(pending[0], start)
                if radius <= 1e-12:
                    self._message("The arc center and start point must be different.", error=True)
                    return False
                self._pending_arc_points.append(start)
                self._sync_pending_construction_preview()
                self._message("Centerpoint arc: move clockwise or counter-clockwise from the start to preview the fixed-radius arc, then click its end.")
                return True
            center, start = (tuple(pending[0]), tuple(pending[1]))
            radius = math.dist(center, start)

            # If the end click snapped to an authored curve endpoint, keep that
            # exact topological vertex. Projecting it back onto the originally
            # clicked circle can move it by a few micrometres, leaving an arc
            # that looks closed but does not share the chord endpoint.
            snapped_end = None
            for entity in self.model["geometry"].get("sketch_entities", []):
                if entity.get("type") == "circle":
                    continue
                entity_points = entity.get("points") or entity.get("sampled_points") or []
                if len(entity_points) < 2:
                    continue
                try:
                    first_endpoint = tuple(float(value) for value in entity_points[0][:2])
                    last_endpoint = tuple(float(value) for value in entity_points[-1][:2])
                except (TypeError, ValueError, IndexError, OverflowError):
                    continue
                if math.dist(first_endpoint, last_endpoint) <= 1e-12:
                    continue
                for endpoint in (first_endpoint, last_endpoint):
                    if math.dist(point, endpoint) <= 1e-12:
                        snapped_end = endpoint
                        break
                if snapped_end is not None:
                    break

            if snapped_end is not None:
                end = snapped_end
                chord = (end[0] - start[0], end[1] - start[1])
                chord_length = math.hypot(*chord)
                if chord_length <= 1e-12:
                    self._message("The arc start and end must be different points.", error=True)
                    return False
                midpoint = ((start[0] + end[0]) * 0.5, (start[1] + end[1]) * 0.5)
                normal = (-chord[1] / chord_length, chord[0] / chord_length)
                center_offset = (center[0] - midpoint[0]) * normal[0] + (center[1] - midpoint[1]) * normal[1]
                adjusted_center = (
                    midpoint[0] + center_offset * normal[0],
                    midpoint[1] + center_offset * normal[1],
                )
                project, _ = self._canvas_projection()
                if math.dist(project(center), project(adjusted_center)) > 16.0:
                    self._message(
                        "The selected arc center is too far from the circle through the snapped endpoints. "
                        "Choose a center closer to their perpendicular bisector.",
                        error=True,
                    )
                    return False
                center = adjusted_center
                radius = math.dist(center, start)
                cursor = end
            else:
                cursor = point
            cursor_radius = math.dist(center, cursor)
            if radius <= 0 or cursor_radius <= 1e-12:
                self._message("The arc radius must be positive and the endpoint must differ from its center.", error=True)
                return False
            end_angle = math.atan2(cursor[1] - center[1], cursor[0] - center[0])
            start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
            if snapped_end is None:
                end = (center[0] + radius * math.cos(end_angle), center[1] + radius * math.sin(end_angle))
            if math.dist(start, end) <= 1e-12:
                self._message("The arc start and end must define a nonzero angular span.", error=True)
                return False
            positive_sweep = (end_angle - start_angle) % (2.0 * math.pi)
            clockwise = bool(centerpoint_clockwise) if centerpoint_clockwise is not None else False
            sweep = positive_sweep - 2.0 * math.pi if clockwise else positive_sweep
            through_angle = start_angle + sweep / 2.0
            through = (
                center[0] + radius * math.cos(through_angle),
                center[1] + radius * math.sin(through_angle),
            )
            count = min(512, max(12, int(math.ceil(abs(sweep) / (math.pi / 48.0)))))
            samples = [
                [center[0] + radius * math.cos(start_angle + sweep * index / count),
                 center[1] + radius * math.sin(start_angle + sweep * index / count)]
                for index in range(count + 1)
            ]
            self._pending_arc_points = []
            self._sync_pending_construction_preview()
            try:
                return self._add_sketch_entity(
                    "arc", [start, through, end],
                    arc_geometry={
                        "start": start, "through": through, "end": end,
                        "center": center, "radius": radius, "clockwise": sweep < 0,
                        "samples": samples,
                    },
                )
            except ValueError as exc:
                self._message(str(exc), error=True)
                return False

        self._message("Choose a supported arc construction method.", error=True)
        return False

    @_history_tracked
    def _set_selected_arc_radius(self, value):
        edge = self._selected_edge()
        if edge is None or len(self.selected_edge_ids) != 1:
            return False
        try:
            radius = float(value)
        except (TypeError, ValueError, OverflowError):
            self._message("Enter a positive arc radius in metres.", error=True)
            return False
        if not math.isfinite(radius) or radius <= 0:
            self._message("Arc radius must be positive.", error=True)
            return False
        curve = edge.get("curve", {})
        if not isinstance(curve, dict) or curve.get("type") != "arc":
            self._message("Select one sketch arc to edit its radius.", error=True)
            return False
        entity = next(
            (item for item in self.model["geometry"].get("sketch_entities", [])
             if item.get("id") == edge.get("sketch_entity_id")),
            None,
        )
        if entity is None or len(entity.get("points", [])) < 3:
            self._message("This arc is not an editable sketch arc.", error=True)
            return False
        start = tuple(float(value) for value in entity["points"][0])
        end = tuple(float(value) for value in entity["points"][-1])
        dx, dy = end[0] - start[0], end[1] - start[1]
        chord = math.hypot(dx, dy)
        if chord <= 1e-15 or radius < chord / 2.0 - 1e-12:
            self._message("The radius must be at least half the distance between the fixed endpoints.", error=True)
            return False
        midpoint = ((start[0] + end[0]) / 2.0, (start[1] + end[1]) / 2.0)
        height = math.sqrt(max(0.0, radius * radius - chord * chord / 4.0))
        normal = (-dy / chord, dx / chord)
        centers = (
            (midpoint[0] + height * normal[0], midpoint[1] + height * normal[1]),
            (midpoint[0] - height * normal[0], midpoint[1] - height * normal[1]),
        )
        previous_center = entity.get("center", curve.get("center", midpoint))
        center = min(centers, key=lambda candidate: math.dist(candidate, previous_center))
        clockwise = bool(entity.get("clockwise", curve.get("clockwise", False)))
        start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
        end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
        positive_sweep = (end_angle - start_angle) % (2.0 * math.pi)
        sweep = positive_sweep - 2.0 * math.pi if clockwise else positive_sweep
        if abs(sweep) <= 1e-12:
            self._message("Arc endpoints must define a nonzero angular span.", error=True)
            return False
        count = min(512, max(12, int(math.ceil(abs(sweep) / (math.pi / 48.0)))))
        samples = [
            [center[0] + radius * math.cos(start_angle + sweep * index / count),
             center[1] + radius * math.sin(start_angle + sweep * index / count)]
            for index in range(count + 1)
        ]
        through_angle = start_angle + sweep / 2.0
        through = (
            center[0] + radius * math.cos(through_angle),
            center[1] + radius * math.sin(through_angle),
        )
        entity["center"] = list(center)
        entity["radius"] = radius
        entity["clockwise"] = clockwise
        entity["points"] = [list(start), list(through), list(end)]
        entity["sampled_points"] = samples
        edge["curve"] = {
            **curve, "type": "arc", "center": list(center), "radius": radius,
            "clockwise": clockwise, "sampled_points": copy.deepcopy(samples),
        }
        self._preserve_canvas_view()
        self._refresh_sketch_generated_regions()
        self._refresh_model_tree()
        self._render_inspector()
        self.render_canvas()
        self._message(f"Arc radius set to {radius:.6g} m.")
        return True

    def _accept_circle_sketch_point(self, point):
        self._pending_circle_points.append(tuple(point))
        if len(self._pending_circle_points) < 3:
            self._sync_pending_construction_preview()
            if len(self._pending_circle_points) == 1:
                self._message("Circle: click a second point on the circumference.")
            else:
                self._message("Circle: move the pointer to preview the circle, then click the third circumference point.")
            return
        first, second, third = self._pending_circle_points[:3]
        self._pending_circle_points = []
        self._sync_pending_construction_preview()
        ax, ay = first
        bx, by = second
        cx, cy = third
        denominator = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
        if abs(denominator) <= 1e-20:
            self._message("Circle points must not be collinear.", error=True)
            return
        aa, bb, cc = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
        center = (
            (aa * (by - cy) + bb * (cy - ay) + cc * (ay - by)) / denominator,
            (aa * (cx - bx) + bb * (ax - cx) + cc * (bx - ax)) / denominator,
        )
        radius = math.dist(first, center)
        self._primitive_values[("circle", "r_center")] = _round_sketch_m(center[0])
        self._primitive_values[("circle", "z_center")] = _round_sketch_m(center[1])
        self._primitive_values[("circle", "radius")] = _round_sketch_m(radius)
        self._add_primitive("circle")
        self.set_sketch_tool("select", announce=False)

    @staticmethod
    def _merge_sketch_parameter_intervals(intervals, *, tolerance=1e-8):
        cleaned = []
        for interval in intervals or []:
            if not isinstance(interval, (list, tuple)) or len(interval) != 2:
                continue
            try:
                first, second = (float(value) for value in interval)
            except (TypeError, ValueError, OverflowError):
                continue
            if not math.isfinite(first) or not math.isfinite(second):
                continue
            low = max(0.0, min(1.0, min(first, second)))
            high = max(0.0, min(1.0, max(first, second)))
            if high - low > tolerance:
                cleaned.append((low, high))
        merged = []
        for low, high in sorted(cleaned):
            if merged and low <= merged[-1][1] + tolerance:
                merged[-1] = (merged[-1][0], max(merged[-1][1], high))
            else:
                merged.append((low, high))
        return merged

    def _sketch_entity_active_intervals(self, entity, geometry=None):
        """Return the ranges of a source curve that are present in the sketch."""
        if not isinstance(entity, dict):
            return []
        if isinstance(entity.get("active_intervals"), (list, tuple)):
            return self._merge_sketch_parameter_intervals(entity["active_intervals"])
        geometry = geometry or self.model.get("geometry", {})
        entity_id = entity.get("id")
        raw_source = entity.get("sampled_points") or entity.get("points") or []
        try:
            source = [(float(point[0]), float(point[1])) for point in raw_source if len(point) >= 2]
        except (TypeError, ValueError, IndexError, OverflowError):
            source = []
        if len(source) < 2:
            return []
        edge_points = [
            (float(point[0]), float(point[1]))
            for edge in geometry.get("edges", []) if isinstance(edge, dict)
            for point in edge.get("vertices", [])
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        span = max(
            max((point[0] for point in edge_points), default=0.0) - min((point[0] for point in edge_points), default=0.0),
            max((point[1] for point in edge_points), default=0.0) - min((point[1] for point in edge_points), default=0.0),
            1e-12,
        )
        match_tolerance = max(1e-12, min(1e-7, span * 2.5e-5))

        def nearest_parameter(point):
            best_distance, best_parameter = float("inf"), 0.0
            count = len(source) - 1
            for index, (first, second) in enumerate(zip(source, source[1:])):
                dx, dy = second[0] - first[0], second[1] - first[1]
                length2 = dx * dx + dy * dy
                fraction = 0.0 if length2 <= 1e-24 else min(
                    1.0, max(0.0, ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / length2)
                )
                nearest = (first[0] + fraction * dx, first[1] + fraction * dy)
                distance = math.dist(point, nearest)
                if distance < best_distance:
                    best_distance = distance
                    best_parameter = (index + fraction) / count
            return best_distance, best_parameter

        edge_ranges = []
        for edge in geometry.get("edges", []):
            if not isinstance(edge, dict) or edge.get("sketch_entity_id") != entity_id:
                continue
            curve = edge.get("curve", {})
            raw_edge = curve.get("sampled_points") if isinstance(curve, dict) else None
            raw_edge = raw_edge or edge.get("vertices", [])
            try:
                points = [(float(point[0]), float(point[1])) for point in raw_edge if len(point) >= 2]
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
            if len(points) < 2:
                continue
            matches = [nearest_parameter(point) for point in points]
            if max(distance for distance, _ in matches) <= match_tolerance:
                values = [parameter for _, parameter in matches]
                edge_ranges.append((min(values), max(values)))
        # Face references are authoritative only when there is a corresponding
        # cached visible edge. Without an edge record this may be a freshly
        # created open overhang, which must remain editable.
        ranges = list(edge_ranges) if edge_ranges else []
        for region in geometry.get("regions", []) if edge_ranges else []:
            shape = region.get("shape", {})
            if not isinstance(shape, dict) or shape.get("type") != "sketch":
                continue
            for boundary in shape.get("boundary", []):
                if isinstance(boundary, dict) and boundary.get("entity_id") == entity_id:
                    ranges.append((boundary.get("start_t", 0.0), boundary.get("end_t", 1.0)))
        return self._merge_sketch_parameter_intervals(ranges) or [(0.0, 1.0)]

    @_history_tracked
    def _trim_sketch_entities_along_stroke(
        self, world_stroke, *, target_entity_ids=None, target_edge_id=None,
        target_intervals=None,
    ):
        """Power-trim curve sections crossed by the trim trace, up to intersections."""
        if not isinstance(world_stroke, (list, tuple)) or len(world_stroke) < 2:
            self._message("Drag the trim pencil across a sketch curve.", error=True)
            return False

        project, _ = self._canvas_projection()
        stroke = [project(point) for point in world_stroke]
        geometry = self.model["geometry"]
        # Trimming changes an existing arrangement; it must not turn unrelated
        # loose curves into new faces just because they nearly close after one
        # edge is removed. Rebuild faces from entities that already define
        # regions, extending the set for fragments created by this trim.
        refresh_entity_ids = set()
        for region in geometry.get("regions", []):
            shape = region.get("shape", {})
            if not isinstance(shape, dict) or shape.get("type") != "sketch":
                continue
            refresh_entity_ids.update(
                entity_id for entity_id in shape.get("entity_ids", [])
                if isinstance(entity_id, str)
            )
            refresh_entity_ids.update(
                boundary.get("entity_id") for boundary in shape.get("boundary", [])
                if isinstance(boundary, dict) and isinstance(boundary.get("entity_id"), str)
            )
        target_entity_ids = set(target_entity_ids or ())
        target_intervals = target_intervals or {}
        primitive_snapshot = None
        source_templates = []
        # A trim stroke must actually cross an edge. Keep only a small
        # pixel allowance for sampling/rendering error; the larger pick radius
        # is reserved for a deliberate zero-length edge click.
        tolerance_px = 2.5
        is_point_trim = all(
            math.dist(stroke[index], stroke[index + 1]) <= 1e-9
            for index in range(len(stroke) - 1)
        )
        nearest_hit_tolerance_px = 6.0 if is_point_trim else tolerance_px
        parameter_epsilon = 1e-7

        def cross(a, b):
            return a[0] * b[1] - a[1] * b[0]

        def segment_hit_parameter(a, b, c, d):
            """Return the parameter on c-d where the two finite segments meet."""
            r = (b[0] - a[0], b[1] - a[1])
            s = (d[0] - c[0], d[1] - c[1])
            denominator = cross(r, s)
            if abs(denominator) <= 1e-12:
                return None
            offset = (c[0] - a[0], c[1] - a[1])
            along_stroke = cross(offset, s) / denominator
            along_curve = cross(offset, r) / denominator
            if -1e-9 <= along_stroke <= 1.0 + 1e-9 and -1e-9 <= along_curve <= 1.0 + 1e-9:
                return max(0.0, min(1.0, along_curve))
            return None

        def point_segment_distance(point, first, second):
            dx, dy = second[0] - first[0], second[1] - first[1]
            length2 = dx * dx + dy * dy
            if length2 <= 1e-16:
                return math.hypot(point[0] - first[0], point[1] - first[1]), 0.0
            fraction = min(1.0, max(0.0, ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / length2))
            closest = (first[0] + fraction * dx, first[1] + fraction * dy)
            return math.hypot(point[0] - closest[0], point[1] - closest[1]), fraction

        def curve_samples(entity):
            return entity.get("sampled_points") or entity.get("points") or []

        def exact_arc_line_parameters(first_entity, second_entity):
            """Return exact parameters where one authored line meets an arc."""
            first_type = first_entity.get("type")
            second_type = second_entity.get("type")
            if first_type == "arc" and second_type == "line":
                arc_entity, line_entity, parameter_on_first = first_entity, second_entity, False
            elif first_type == "line" and second_type == "arc":
                arc_entity, line_entity, parameter_on_first = second_entity, first_entity, True
            else:
                return None
            line_points = line_entity.get("points", [])
            try:
                if len(line_points) != 2:
                    return None
                line_start = tuple(float(value) for value in line_points[0][:2])
                line_end = tuple(float(value) for value in line_points[1][:2])
                center = tuple(float(value) for value in arc_entity.get("center", []))
                radius = float(arc_entity.get("radius"))
                arc_points = arc_entity.get("points", [])
                arc_start = tuple(float(value) for value in arc_points[0][:2])
                arc_end = tuple(float(value) for value in arc_points[-1][:2])
            except (TypeError, ValueError, IndexError, OverflowError):
                return None
            if len(center) != 2 or radius <= 0 or not math.isfinite(radius):
                return None

            dx, dy = line_end[0] - line_start[0], line_end[1] - line_start[1]
            fx, fy = line_start[0] - center[0], line_start[1] - center[1]
            quadratic_a = dx * dx + dy * dy
            if quadratic_a <= 1e-24:
                return []
            quadratic_b = 2.0 * (fx * dx + fy * dy)
            quadratic_c = fx * fx + fy * fy - radius * radius
            discriminant = quadratic_b * quadratic_b - 4.0 * quadratic_a * quadratic_c
            discriminant_tolerance = max(
                1e-30,
                1e-12 * (quadratic_b * quadratic_b + abs(4.0 * quadratic_a * quadratic_c)),
            )
            if discriminant < -discriminant_tolerance:
                return []
            root = math.sqrt(max(0.0, discriminant))
            line_tolerance = max(1e-10, 1e-12 / math.sqrt(quadratic_a))
            start_angle = math.atan2(arc_start[1] - center[1], arc_start[0] - center[0])
            end_angle = math.atan2(arc_end[1] - center[1], arc_end[0] - center[0])
            clockwise = bool(arc_entity.get("clockwise", False))
            total_sweep = (
                (start_angle - end_angle) % (2.0 * math.pi)
                if clockwise else (end_angle - start_angle) % (2.0 * math.pi)
            )
            if total_sweep <= 1e-14:
                return []

            parameters = []
            for line_fraction in sorted({
                (-quadratic_b - root) / (2.0 * quadratic_a),
                (-quadratic_b + root) / (2.0 * quadratic_a),
            }):
                if not -line_tolerance <= line_fraction <= 1.0 + line_tolerance:
                    continue
                line_fraction = min(1.0, max(0.0, line_fraction))
                hit = (line_start[0] + line_fraction * dx, line_start[1] + line_fraction * dy)
                angle = math.atan2(hit[1] - center[1], hit[0] - center[0])
                offset = (
                    (start_angle - angle) % (2.0 * math.pi)
                    if clockwise else (angle - start_angle) % (2.0 * math.pi)
                )
                if offset > total_sweep + 1e-9:
                    continue
                arc_fraction = min(1.0, max(0.0, offset / total_sweep))
                parameters.append(line_fraction if parameter_on_first else arc_fraction)
            return parameters

        def exact_spline_line_parameters(first_entity, second_entity):
            """Refine spline/line crossings so trimmed endpoints stay connected."""
            curve_types = {"spline", "spline3"}
            if first_entity.get("type") == "line" and second_entity.get("type") in curve_types:
                line_entity, curve_entity, parameter_on_first = first_entity, second_entity, True
            elif first_entity.get("type") in curve_types and second_entity.get("type") == "line":
                line_entity, curve_entity, parameter_on_first = second_entity, first_entity, False
            else:
                return None
            try:
                line_points = line_entity.get("points", [])
                if len(line_points) != 2:
                    return None
                line_start = tuple(float(value) for value in line_points[0][:2])
                line_end = tuple(float(value) for value in line_points[1][:2])
            except (TypeError, ValueError, IndexError, OverflowError):
                return None
            dx, dy = line_end[0] - line_start[0], line_end[1] - line_start[1]
            line_length_squared = dx * dx + dy * dy
            if line_length_squared <= 1e-24:
                return []
            samples = curve_samples(curve_entity)
            if len(samples) < 2:
                return None
            residual_tolerance = max(1e-24, line_length_squared * 1e-12)

            def signed_distance(parameter):
                point = evaluate_sketch_entity(curve_entity, parameter)
                if point is None:
                    return None
                return cross((dx, dy), (point[0] - line_start[0], point[1] - line_start[1]))

            roots = []
            sample_count = len(samples) - 1
            for index in range(sample_count):
                low, high = index / sample_count, (index + 1) / sample_count
                value_low, value_high = signed_distance(low), signed_distance(high)
                if value_low is None or value_high is None:
                    return None
                candidates = []
                if abs(value_low) <= residual_tolerance:
                    candidates.append(low)
                if (value_low < 0) != (value_high < 0):
                    left, right, left_value = low, high, value_low
                    for _ in range(48):
                        middle = 0.5 * (left + right)
                        middle_value = signed_distance(middle)
                        if middle_value is None:
                            break
                        if abs(middle_value) <= residual_tolerance or right - left <= 1e-13:
                            left = right = middle
                            break
                        if (left_value < 0) != (middle_value < 0):
                            right = middle
                        else:
                            left, left_value = middle, middle_value
                    candidates.append(0.5 * (left + right))
                if abs(value_high) <= residual_tolerance:
                    candidates.append(high)
                for parameter in candidates:
                    point = evaluate_sketch_entity(curve_entity, parameter)
                    if point is None:
                        continue
                    line_fraction = (
                        (point[0] - line_start[0]) * dx + (point[1] - line_start[1]) * dy
                    ) / line_length_squared
                    if not -1e-10 <= line_fraction <= 1.0 + 1e-10:
                        continue
                    roots.append(line_fraction if parameter_on_first else parameter)

            ordered = []
            for value in sorted(max(0.0, min(1.0, float(item))) for item in roots):
                if not ordered or value - ordered[-1] > 1e-9:
                    ordered.append(value)
            return ordered

        def curve_intersections(first_entity, second_entity, first_samples, second_samples):
            exact = exact_arc_line_parameters(first_entity, second_entity)
            if exact is None:
                exact = exact_spline_line_parameters(first_entity, second_entity)
            if exact is not None:
                return exact
            values = []
            first_count = max(1, len(first_samples) - 1)
            for first_index, (a, b) in enumerate(zip(first_samples, first_samples[1:])):
                for c, d in zip(second_samples, second_samples[1:]):
                    local = segment_hit_parameter(c, d, a, b)
                    if local is not None:
                        values.append((first_index + local) / first_count)
            return values

        def sample_at(samples, parameter):
            position = max(0.0, min(1.0, float(parameter))) * (len(samples) - 1)
            index = min(len(samples) - 2, max(0, int(math.floor(position))))
            fraction = position - index
            first, second = samples[index], samples[index + 1]
            return (
                first[0] + fraction * (second[0] - first[0]),
                first[1] + fraction * (second[1] - first[1]),
            )

        def nearest_parameter(point, samples):
            best = (float("inf"), 0.0)
            segment_count = len(samples) - 1
            for index, (first, second) in enumerate(zip(samples, samples[1:])):
                distance, fraction = point_segment_distance(point, first, second)
                if distance < best[0]:
                    best = (distance, (index + fraction) / segment_count)
            return best

        def sample_tangent(samples, parameter):
            step = min(0.04, max(0.002, 1.0 / max(2, len(samples) - 1)))
            first = sample_at(samples, max(0.0, parameter - step))
            second = sample_at(samples, min(1.0, parameter + step))
            dx, dy = second[0] - first[0], second[1] - first[1]
            magnitude = math.hypot(dx, dy)
            return (dx / magnitude, dy / magnitude) if magnitude > 1e-12 else None

        def is_overlapping_curve_intersection(parameter, first_samples, second_samples):
            """Ignore tessellation crossings between curves that follow the same edge."""
            if min(len(first_samples), len(second_samples)) < 3:
                return False
            point = sample_at(first_samples, parameter)
            distance, other_parameter = nearest_parameter(point, second_samples)
            if distance > 1.5:
                return False
            step = min(0.08, max(0.02, 2.0 / max(len(first_samples), len(second_samples))))
            probes = [
                value for value in (parameter - step, parameter + step)
                if 0.0 <= value <= 1.0
            ]
            if not probes:
                return False
            for probe in probes:
                probe_point = sample_at(first_samples, probe)
                probe_distance, probe_other_parameter = nearest_parameter(probe_point, second_samples)
                if probe_distance > 1.5:
                    return False
                first_tangent = sample_tangent(first_samples, probe)
                second_tangent = sample_tangent(second_samples, probe_other_parameter)
                if first_tangent is None or second_tangent is None:
                    return False
                tangent_alignment = abs(
                    first_tangent[0] * second_tangent[0]
                    + first_tangent[1] * second_tangent[1]
                )
                if tangent_alignment < 0.97:
                    return False
            return True

        def stroke_touches_curve(samples):
            if len(samples) < 2:
                return False
            screen_samples = [project(point) for point in samples]
            nearest_distance = None
            for stroke_a, stroke_b in zip(stroke, stroke[1:]):
                for curve_a, curve_b in zip(screen_samples, screen_samples[1:]):
                    if segment_hit_parameter(stroke_a, stroke_b, curve_a, curve_b) is not None:
                        return True
                    for point in (stroke_a, stroke_b):
                        distance, _ = point_segment_distance(point, curve_a, curve_b)
                        nearest_distance = distance if nearest_distance is None else min(nearest_distance, distance)
                    for point in (curve_a, curve_b):
                        distance, _ = point_segment_distance(point, stroke_a, stroke_b)
                        nearest_distance = distance if nearest_distance is None else min(nearest_distance, distance)
            return nearest_distance is not None and nearest_distance <= tolerance_px

        # Primitive rectangles are displayed as regions rather than sketch
        # entities. Convert only the rectangle whose outline was crossed so
        # Power Trim can cut its individual sides without disturbing other
        # primitive regions in the model.
        edges_by_id = {
            edge.get("id"): edge for edge in geometry.get("edges", [])
            if isinstance(edge, dict) and edge.get("id")
        }
        primitive_regions = []
        for region in geometry.get("regions", []):
            shape = region.get("shape", {})
            if not isinstance(shape, dict) or shape.get("type") != "rectangle":
                continue
            region_edge_ids = set(region.get("edge_ids", []))
            if target_edge_id and target_edge_id in region_edge_ids:
                primitive_regions.append(region)
                continue
            if target_edge_id:
                continue
            if any(
                edge_id in edges_by_id
                and stroke_touches_curve(
                    (edges_by_id[edge_id].get("curve", {}).get("sampled_points")
                     if isinstance(edges_by_id[edge_id].get("curve"), dict) else None)
                    or edges_by_id[edge_id].get("vertices", [])
                )
                for edge_id in region_edge_ids
            ):
                primitive_regions.append(region)

        if primitive_regions:
            primitive_snapshot = copy.deepcopy(geometry)
            source_templates = [copy.deepcopy(region) for region in primitive_regions]
            primitive_ids = {region.get("id") for region in primitive_regions}
            for region in primitive_regions:
                self._materialize_primitive_as_sketch(region)
                shape = region.get("shape", {})
                if isinstance(shape, dict) and shape.get("type") == "sketch":
                    refresh_entity_ids.update(
                        entity_id for entity_id in shape.get("entity_ids", [])
                        if isinstance(entity_id, str)
                    )
                    refresh_entity_ids.update(
                        boundary.get("entity_id") for boundary in shape.get("boundary", [])
                        if isinstance(boundary, dict) and isinstance(boundary.get("entity_id"), str)
                    )
            geometry["regions"] = [
                region for region in geometry.get("regions", [])
                if region.get("id") not in primitive_ids
            ]
            if target_edge_id and not target_entity_ids:
                target_edge = next((edge for edge in geometry.get("edges", [])
                                   if edge.get("id") == target_edge_id), None)
                entity_id = target_edge.get("sketch_entity_id") if target_edge else None
                if entity_id:
                    target_entity_ids = {entity_id}
        # Recover visible edge fragments left by older/imported models when
        # their stale entity ID now points to a source curve that no longer
        # covers them. This prevents trim cleanup from deleting those fragments.
        reconciliation_snapshot = None
        entities = geometry.get("sketch_entities", [])
        edge_points = [
            (float(point[0]), float(point[1]))
            for edge in geometry.get("edges", []) if isinstance(edge, dict)
            for point in edge.get("vertices", [])
            if isinstance(point, (list, tuple)) and len(point) >= 2
        ]
        geometry_span = max(
            max((point[0] for point in edge_points), default=0.0)
            - min((point[0] for point in edge_points), default=0.0),
            max((point[1] for point in edge_points), default=0.0)
            - min((point[1] for point in edge_points), default=0.0),
        )
        match_tolerance = max(1e-12, min(1e-7, geometry_span * 2.5e-5))

        def distance_to_curve(point, samples):
            result = float("inf")
            for first, second in zip(samples, samples[1:]):
                dx, dy = second[0] - first[0], second[1] - first[1]
                length2 = dx * dx + dy * dy
                fraction = 0.0 if length2 <= 1e-24 else min(
                    1.0, max(0.0, ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / length2)
                )
                result = min(result, math.hypot(
                    point[0] - first[0] - fraction * dx,
                    point[1] - first[1] - fraction * dy,
                ))
            return result

        for edge in geometry.get("edges", []):
            if not isinstance(edge, dict) or not edge.get("sketch_entity_id"):
                continue
            edge_curve = edge.get("curve")
            raw_samples = (
                edge_curve.get("sampled_points") if isinstance(edge_curve, dict) else None
            ) or edge.get("vertices", [])
            try:
                edge_samples = [(float(p[0]), float(p[1])) for p in raw_samples if len(p) >= 2]
            except (TypeError, ValueError, OverflowError):
                continue
            if len(edge_samples) < 2:
                continue
            represented = False
            for entity in entities:
                try:
                    samples = [(float(p[0]), float(p[1])) for p in curve_samples(entity)]
                except (TypeError, ValueError, OverflowError):
                    continue
                if len(samples) >= 2 and all(
                    distance_to_curve(point, samples) <= match_tolerance for point in edge_samples
                ):
                    represented = True
                    break
            if represented:
                continue

            curve_type = edge_curve.get("type") if isinstance(edge_curve, dict) else None
            recovered_type = "line" if curve_type == "line" else "spline"
            recovered_points = (
                [list(edge_samples[0]), list(edge_samples[-1])]
                if recovered_type == "line" else [list(p) for p in edge_samples]
            )
            if reconciliation_snapshot is None:
                reconciliation_snapshot = copy.deepcopy(geometry)
            recovered_id = new_id("curve")
            recovered_samples = copy.deepcopy(recovered_points)
            entities.append({
                "id": recovered_id, "type": recovered_type,
                "points": recovered_points, "sampled_points": recovered_samples,
            })
            # This curve was already visible as an edge before the trim. Keep
            # it eligible to complete its existing region when repairing stale
            # legacy/imported entity references.
            refresh_entity_ids.add(recovered_id)
            edge["sketch_entity_id"] = recovered_id
            edge["sketch_t0"], edge["sketch_t1"] = 0.0, 1.0
            if isinstance(edge_curve, dict):
                edge_curve["type"] = recovered_type
                edge_curve["sampled_points"] = copy.deepcopy(recovered_samples)

        if target_edge_id and not target_entity_ids:
            target_edge = next(
                (edge for edge in geometry.get("edges", []) if edge.get("id") == target_edge_id),
                None,
            )
            entity_id = target_edge.get("sketch_entity_id") if target_edge else None
            if entity_id:
                target_entity_ids = {entity_id}
                if not target_intervals:
                    try:
                        first = float(target_edge.get("sketch_t0", 0.0))
                        second = float(target_edge.get("sketch_t1", 1.0))
                        if math.isfinite(first) and math.isfinite(second):
                            target_intervals = {entity_id: (first, second)}
                    except (TypeError, ValueError, OverflowError):
                        pass
        if target_edge_id and not target_entity_ids:
            rollback_snapshot = primitive_snapshot if primitive_snapshot is not None else reconciliation_snapshot
            if rollback_snapshot is not None:
                geometry.clear()
                geometry.update(rollback_snapshot)
            self._message("That edge cannot be trimmed as an editable sketch curve.", error=True)
            return False

        active_intervals_by_id = {
            entity.get("id"): self._sketch_entity_active_intervals(entity, geometry)
            for entity in entities if isinstance(entity, dict) and isinstance(entity.get("id"), str)
        }

        # Imported and previously edited geometry can contain multiple sketch
        # entities tracing the same visible edge. Treat those as one trim target
        # so clipping one source does not leave a duplicate curve on screen.
        if target_edge_id and target_entity_ids:
            entities_by_id = {
                item.get("id"): item for item in entities
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
            selected_id = next(iter(target_entity_ids))
            selected_entity = entities_by_id.get(selected_id)
            selected_samples = (
                [project(point) for point in curve_samples(selected_entity)]
                if selected_entity is not None else []
            )
            selected_interval = target_intervals.get(selected_id, (0.0, 1.0))
            try:
                interval_start, interval_end = sorted(float(value) for value in selected_interval)
            except (TypeError, ValueError, OverflowError):
                interval_start, interval_end = 0.0, 1.0
            interval_start = max(0.0, min(1.0, interval_start))
            interval_end = max(0.0, min(1.0, interval_end))
            if len(selected_samples) >= 3 and interval_end - interval_start > parameter_epsilon:
                for other in entities:
                    other_id = other.get("id") if isinstance(other, dict) else None
                    if not other_id or other_id in target_entity_ids:
                        continue
                    other_samples = [project(point) for point in curve_samples(other)]
                    if len(other_samples) < 3:
                        continue
                    corresponding_parameters = []
                    close_and_parallel = True
                    for fraction in (0.0, 0.25, 0.5, 0.75, 1.0):
                        selected_parameter = interval_start + fraction * (interval_end - interval_start)
                        point = sample_at(selected_samples, selected_parameter)
                        distance, other_parameter = nearest_parameter(point, other_samples)
                        first_tangent = sample_tangent(selected_samples, selected_parameter)
                        second_tangent = sample_tangent(other_samples, other_parameter)
                        other_active = active_intervals_by_id.get(other_id, [(0.0, 1.0)])
                        if (
                            distance > 2.5
                            or not any(lo - parameter_epsilon <= other_parameter <= hi + parameter_epsilon for lo, hi in other_active)
                            or first_tangent is None
                            or second_tangent is None
                            or abs(first_tangent[0] * second_tangent[0]
                                   + first_tangent[1] * second_tangent[1]) < 0.90
                        ):
                            close_and_parallel = False
                            break
                        corresponding_parameters.append(other_parameter)
                    if not close_and_parallel or len(corresponding_parameters) != 5:
                        continue
                    increasing = all(
                        corresponding_parameters[index] <= corresponding_parameters[index + 1] + 0.03
                        for index in range(4)
                    )
                    decreasing = all(
                        corresponding_parameters[index] >= corresponding_parameters[index + 1] - 0.03
                        for index in range(4)
                    )
                    if (increasing or decreasing) and abs(
                        corresponding_parameters[-1] - corresponding_parameters[0]
                    ) > 0.1:
                        target_entity_ids.add(other_id)
                        target_intervals[other_id] = (
                            min(corresponding_parameters[0], corresponding_parameters[-1]),
                            max(corresponding_parameters[0], corresponding_parameters[-1]),
                        )

        def unique_parameters(values):
            result = []
            for value in sorted(max(0.0, min(1.0, float(item))) for item in values):
                if not result or value - result[-1] > parameter_epsilon:
                    result.append(value)
            return result

        def sample_interval(entity, samples, start_t, end_t):
            count = len(samples) - 1
            if count <= 0:
                return [list(samples[0])] if samples else []
            start_t = max(0.0, min(1.0, start_t))
            end_t = max(0.0, min(1.0, end_t))

            def at(fraction):
                exact = evaluate_sketch_entity(entity, fraction)
                if exact is not None:
                    return [float(exact[0]), float(exact[1])]
                position = fraction * count
                index = min(count - 1, max(0, int(math.floor(position))))
                local = position - index
                a, b = samples[index], samples[index + 1]
                return [float(a[axis]) + local * (float(b[axis]) - float(a[axis])) for axis in range(2)]

            result = [at(start_t)]
            for index in range(1, count):
                fraction = index / count
                if start_t + 1e-10 < fraction < end_t - 1e-10:
                    result.append([float(value) for value in samples[index]])
            result.append(at(end_t))
            return result

        def entity_fragment(entity, samples, start_t, end_t, fragment_id):
            fragment_samples = sample_interval(entity, samples, start_t, end_t)
            if len(fragment_samples) < 2 or math.dist(fragment_samples[0], fragment_samples[-1]) <= 1e-12:
                return None
            fragment = copy.deepcopy(entity)
            fragment["id"] = fragment_id
            curve_type = entity.get("type")
            if curve_type == "line":
                fragment["points"] = [fragment_samples[0], fragment_samples[-1]]
                fragment["sampled_points"] = [fragment_samples[0], fragment_samples[-1]]
            elif curve_type == "arc" and entity.get("center") and entity.get("radius"):
                center = [float(value) for value in entity["center"]]
                radius = float(entity["radius"])
                first_angle = math.atan2(samples[0][1] - center[1], samples[0][0] - center[0])
                last_angle = math.atan2(samples[-1][1] - center[1], samples[-1][0] - center[0])
                clockwise = bool(entity.get("clockwise"))
                sweep = -((first_angle - last_angle) % (2.0 * math.pi)) if clockwise else ((last_angle - first_angle) % (2.0 * math.pi))
                angle_a = first_angle + start_t * sweep
                angle_b = first_angle + end_t * sweep
                middle_angle = 0.5 * (angle_a + angle_b)
                start = [center[0] + radius * math.cos(angle_a), center[1] + radius * math.sin(angle_a)]
                through = [center[0] + radius * math.cos(middle_angle), center[1] + radius * math.sin(middle_angle)]
                end = [center[0] + radius * math.cos(angle_b), center[1] + radius * math.sin(angle_b)]
                steps = max(2, int(math.ceil((end_t - start_t) * max(2, len(samples) - 1))))
                fragment["points"] = [start, through, end]
                fragment["sampled_points"] = [
                    [center[0] + radius * math.cos(angle_a + (angle_b - angle_a) * index / steps),
                     center[1] + radius * math.sin(angle_a + (angle_b - angle_a) * index / steps)]
                    for index in range(steps + 1)
                ]
            else:
                # The original control points no longer describe this clipped spline.
                fragment["type"] = "spline"
                fragment["points"] = copy.deepcopy(fragment_samples)
                fragment["sampled_points"] = copy.deepcopy(fragment_samples)
                fragment.pop("center", None)
                fragment.pop("radius", None)
                fragment.pop("clockwise", None)
                fragment.pop("control_point", None)
            return fragment

        replacement_entities = []
        removed_sections = 0
        changed_curves = 0
        touched_curves = 0
        changed_entity_ids = set()
        for entity in entities:
            if target_entity_ids and entity.get("id") not in target_entity_ids:
                replacement_entities.append(entity)
                continue
            target_interval = target_intervals.get(entity.get("id"))
            if isinstance(target_interval, (list, tuple)) and len(target_interval) == 2:
                try:
                    interval_start, interval_end = sorted(float(value) for value in target_interval)
                    if not all(math.isfinite(value) for value in (interval_start, interval_end)):
                        target_interval = None
                    else:
                        target_interval = (
                            max(0.0, min(1.0, interval_start)),
                            max(0.0, min(1.0, interval_end)),
                        )
                except (TypeError, ValueError, OverflowError):
                    target_interval = None
            else:
                target_interval = None

            def hit_is_in_target_interval(parameter):
                return (
                    target_interval is None
                    or target_interval[0] - parameter_epsilon <= parameter <= target_interval[1] + parameter_epsilon
                )

            samples_world = curve_samples(entity)
            if len(samples_world) < 2:
                replacement_entities.append(entity)
                continue
            samples_screen = [project(point) for point in samples_world]
            active_ranges = active_intervals_by_id.get(entity.get("id"), [(0.0, 1.0)])
            hit_parameters = []
            nearest = None
            for stroke_a, stroke_b in zip(stroke, stroke[1:]):
                for index, (curve_a, curve_b) in enumerate(zip(samples_screen, samples_screen[1:])):
                    local = segment_hit_parameter(stroke_a, stroke_b, curve_a, curve_b)
                    if local is not None:
                        parameter = (index + local) / (len(samples_screen) - 1)
                        if hit_is_in_target_interval(parameter) and any(
                            lo - parameter_epsilon <= parameter <= hi + parameter_epsilon for lo, hi in active_ranges
                        ):
                            hit_parameters.append(parameter)
                        continue
                    for point in (stroke_a, stroke_b):
                        distance, local = point_segment_distance(point, curve_a, curve_b)
                        parameter = (index + local) / (len(samples_screen) - 1)
                        if not hit_is_in_target_interval(parameter) or not any(
                            lo - parameter_epsilon <= parameter <= hi + parameter_epsilon for lo, hi in active_ranges
                        ):
                            continue
                        candidate = (distance, parameter)
                        if nearest is None or candidate[0] < nearest[0]:
                            nearest = candidate
                    for point, parameter in ((curve_a, index / (len(samples_screen) - 1)),
                                             (curve_b, (index + 1) / (len(samples_screen) - 1))):
                        if not hit_is_in_target_interval(parameter) or not any(
                            lo - parameter_epsilon <= parameter <= hi + parameter_epsilon for lo, hi in active_ranges
                        ):
                            continue
                        distance, _ = point_segment_distance(point, stroke_a, stroke_b)
                        candidate = (distance, parameter)
                        if nearest is None or candidate[0] < nearest[0]:
                            nearest = candidate
            if not hit_parameters and nearest is not None and nearest[0] <= nearest_hit_tolerance_px:
                hit_parameters.append(nearest[1])
            hit_parameters = unique_parameters(hit_parameters)
            if not hit_parameters:
                replacement_entities.append(entity)
                continue
            touched_curves += 1

            intersections = []
            for other in entities:
                if other is entity:
                    continue
                other_samples = curve_samples(other)
                if len(other_samples) >= 2:
                    other_samples_screen = [project(point) for point in other_samples]
                    candidate_intersections = curve_intersections(
                        entity, other, samples_screen, other_samples_screen
                    )
                    other_active = active_intervals_by_id.get(other.get("id"), [(0.0, 1.0)])
                    for parameter in candidate_intersections:
                        if not any(lo - parameter_epsilon <= parameter <= hi + parameter_epsilon for lo, hi in active_ranges):
                            continue
                        if is_overlapping_curve_intersection(parameter, samples_screen, other_samples_screen):
                            continue
                        point = sample_at(samples_screen, parameter)
                        distance, other_parameter = nearest_parameter(point, other_samples_screen)
                        if distance <= 2.5 and any(
                            lo - parameter_epsilon <= other_parameter <= hi + parameter_epsilon
                            for lo, hi in other_active
                        ):
                            intersections.append(parameter)
            intersections = unique_parameters(intersections)
            if target_interval is not None:
                intersections = [
                    parameter for parameter in intersections
                    if target_interval[0] - parameter_epsilon <= parameter
                    <= target_interval[1] + parameter_epsilon
                ]
            # Region boundary intervals are the topology actually shown to the
            # user. Keep their endpoints as cuts as well as computed curve
            # intersections, so a slightly approximate imported or snapped
            # junction still trims to the visible edge endpoint.
            topology_parameters = []
            entity_id = entity.get("id")
            if entity_id:
                for region in geometry.get("regions", []):
                    shape = region.get("shape", {})
                    if not isinstance(shape, dict) or shape.get("type") != "sketch":
                        continue
                    for boundary in shape.get("boundary", []):
                        if not isinstance(boundary, dict) or boundary.get("entity_id") != entity_id:
                            continue
                        try:
                            topology_parameters.extend((
                                float(boundary.get("start_t", 0.0)),
                                float(boundary.get("end_t", 1.0)),
                            ))
                        except (TypeError, ValueError, OverflowError):
                            continue
                if target_interval is not None:
                    topology_parameters.extend(target_interval)
            topology_parameters = unique_parameters(topology_parameters)

            # A free-standing entity has no trim boundary. SolidWorks Power
            # Trim leaves it untouched; whole-entity deletion is a separate
            # operation in MM-FEM as well. Region boundary endpoints count as
            # real trim boundaries even when a source curve intersection is
            # only approximate.
            if not intersections and not topology_parameters:
                replacement_entities.append(entity)
                continue

            sections = []
            for active_start, active_end in active_ranges:
                local_cuts = [active_start, active_end]
                local_cuts.extend(value for value in intersections if active_start + parameter_epsilon < value < active_end - parameter_epsilon)
                local_cuts.extend(value for value in topology_parameters if active_start + parameter_epsilon < value < active_end - parameter_epsilon)
                cuts = unique_parameters(local_cuts)
                sections.extend((lo, hi) for lo, hi in zip(cuts, cuts[1:]) if hi - lo > parameter_epsilon)
            removed = set()
            for hit in hit_parameters:
                # A hit exactly on an interior intersection does not identify
                # which adjacent segment the user intended to remove.
                if any(
                    active_start + parameter_epsilon < boundary < active_end - parameter_epsilon
                    and abs(hit - boundary) <= parameter_epsilon
                    for active_start, active_end in active_ranges for boundary in intersections
                ):
                    continue
                for index, (lo, hi) in enumerate(sections):
                    if lo - parameter_epsilon <= hit <= hi + parameter_epsilon:
                        if abs(hit - lo) <= parameter_epsilon and index > 0:
                            continue
                        if abs(hit - hi) <= parameter_epsilon and index < len(sections) - 1:
                            continue
                        removed.add(index)
                        break
            if not removed:
                replacement_entities.append(entity)
                continue

            removed_sections += len(removed)
            changed_curves += 1
            if entity.get("id"):
                changed_entity_ids.add(entity.get("id"))
            retained = [(lo, hi) for index, (lo, hi) in enumerate(sections)
                        if index not in removed and hi - lo > parameter_epsilon]
            for fragment_index, (lo, hi) in enumerate(retained):
                fragment_id = entity.get("id") if fragment_index == 0 else new_id("curve")
                fragment = entity_fragment(entity, samples_world, lo, hi, fragment_id)
                if fragment is not None:
                    if entity.get("id") in refresh_entity_ids:
                        refresh_entity_ids.add(fragment_id)
                    replacement_entities.append(fragment)

        if not removed_sections:
            rollback_snapshot = primitive_snapshot if primitive_snapshot is not None else reconciliation_snapshot
            if rollback_snapshot is not None:
                geometry.clear()
                geometry.update(rollback_snapshot)
            if not touched_curves:
                self._message("The trim trace did not touch a sketch curve.", error=True)
            else:
                self._message(
                    "Power Trim needs an intersecting curve to stop at. "
                    "Drag across a section between intersections; use Delete to remove a whole isolated curve.",
                    error=True,
                )
            return False

        geometry = self.model["geometry"]
        geometry["sketch_entities"] = replacement_entities
        retained_entity_ids = {item.get("id") for item in replacement_entities}
        geometry["edges"] = [
            item for item in geometry.get("edges", [])
            if (
                not item.get("sketch_entity_id")
                or (
                    item.get("sketch_entity_id") in retained_entity_ids
                    and item.get("sketch_entity_id") not in changed_entity_ids
                )
            )
        ]
        partial_intervals = {
            entity_id: ranges
            for entity_id, ranges in active_intervals_by_id.items()
            if entity_id not in changed_entity_ids
            and not (len(ranges) == 1 and abs(ranges[0][0]) <= 1e-12 and abs(ranges[0][1] - 1.0) <= 1e-12)
        }
        self._refresh_sketch_generated_regions(
            source_templates=source_templates or None,
            entity_ids=refresh_entity_ids,
            active_intervals_by_id=partial_intervals,
        )
        self.selected_edge_ids = []
        self.selected_edge_id = None
        self.selected_region_id = None
        self._refresh_model_tree()
        self._render_inspector()
        self.render_canvas()
        self._message(f"Power Trim removed {removed_sections} section(s) from {changed_curves} curve(s).")
        return True

    @staticmethod
    def _sketch_region_signature(entity_ids, boundary=None):
        intervals = []
        for reference in boundary or []:
            if not isinstance(reference, dict):
                continue
            entity_id = reference.get("entity_id")
            if not isinstance(entity_id, str):
                continue
            start = float(reference.get("start_t", 0.0))
            end = float(reference.get("end_t", 1.0))
            intervals.append((entity_id, round(min(start, end), 9), round(max(start, end), 9)))
        return tuple(sorted(intervals)) if intervals else tuple(sorted(set(entity_ids)))

    def _refresh_sketch_generated_regions(self, *, source_templates=None, entity_ids=None, active_intervals_by_id=None):
        geometry = self.model["geometry"]
        old_generated = [
            region for region in geometry.get("regions", [])
            if isinstance(region.get("shape"), dict) and region["shape"].get("type") == "sketch"
        ]
        generated_by_entities = {
            self._sketch_region_signature(
                region.get("shape", {}).get("entity_ids", []),
                region.get("shape", {}).get("boundary", []),
            ): region
            for region in old_generated
        }
        unused_generated = list(old_generated)
        source_templates = [
            copy.deepcopy(item) for item in (source_templates or [])
            if isinstance(item, dict) and isinstance(item.get("vertices"), list)
        ]
        template_uses = {item.get("id"): 1 for item in old_generated if item.get("id")}
        geometry["regions"] = [
            region for region in geometry.get("regions", [])
            if not (isinstance(region.get("shape"), dict) and region["shape"].get("type") == "sketch")
        ]
        # Ignore sketch faces already materialized from this same planar
        # arrangement: adjacent faces share vertices and edges, but are not
        # overlapping source regions.
        source_regions = list(geometry.get("regions", []))
        sketch_entities = geometry.get("sketch_entities", [])
        if entity_ids is not None:
            allowed_entity_ids = set(entity_ids)
            sketch_entities = [item for item in sketch_entities if item.get("id") in allowed_entity_ids]
        interval_views = []
        interval_view_sources = {}
        for entity in sketch_entities:
            entity_id = entity.get("id")
            ranges = (active_intervals_by_id or {}).get(entity_id)
            if not ranges:
                interval_views.append(entity)
                continue
            if len(ranges) == 1 and abs(ranges[0][0]) <= 1e-12 and abs(ranges[0][1] - 1.0) <= 1e-12:
                interval_views.append(entity)
                continue
            source_samples = entity.get("sampled_points") or entity.get("points") or []
            for interval_index, (start_t, end_t) in enumerate(ranges):
                if end_t - start_t <= 1e-9:
                    continue
                view = copy.deepcopy(entity)
                view_id = f"__active_interval__{entity_id}__{interval_index}"
                view["id"] = view_id
                view["_parameter_source_entity"] = entity
                view["_parameter_source_interval"] = [start_t, end_t]
                sample_count = max(2, int(math.ceil(max(1, len(source_samples) - 1) * (end_t - start_t))) + 1)
                view_samples = []
                for sample_index in range(sample_count):
                    parameter = start_t + (end_t - start_t) * sample_index / (sample_count - 1)
                    point = evaluate_sketch_entity(entity, parameter)
                    if point is not None:
                        view_samples.append([float(point[0]), float(point[1])])
                if len(view_samples) < 2:
                    continue
                view["sampled_points"] = view_samples
                interval_views.append(view)
                interval_view_sources[view_id] = (entity_id, start_t, end_t)
        loops = find_closed_sketch_loops(interval_views)
        for loop in loops:
            mapped_entity_ids = []
            mapped_boundary = []
            for view_id in loop.get("entity_ids", []):
                source = interval_view_sources.get(view_id)
                entity_id = source[0] if source else view_id
                if entity_id not in mapped_entity_ids:
                    mapped_entity_ids.append(entity_id)
            for reference in loop.get("boundary", []):
                source = interval_view_sources.get(reference.get("entity_id"))
                if source is None:
                    mapped_boundary.append(reference)
                    continue
                entity_id, start_t, end_t = source
                mapped = copy.deepcopy(reference)
                mapped["entity_id"] = entity_id
                mapped["start_t"] = start_t + float(reference.get("start_t", 0.0)) * (end_t - start_t)
                mapped["end_t"] = start_t + float(reference.get("end_t", 1.0)) * (end_t - start_t)
                mapped_boundary.append(mapped)
            if interval_view_sources:
                if len(mapped_boundary) < 3:
                    continue
                loop["entity_ids"] = mapped_entity_ids
                loop["boundary"] = mapped_boundary
            vertices = loop["vertices"]
            parent_id = self._find_containing_region(vertices)
            if self._has_unsupported_overlap(vertices, parent_id, regions=source_regions):
                continue
            signature = self._sketch_region_signature(loop["entity_ids"], loop["boundary"])
            if any(
                void.get("parent_id") == parent_id
                and self._sketch_region_signature(
                    void.get("shape", {}).get("entity_ids", []),
                    void.get("shape", {}).get("boundary", []),
                ) == signature
                for void in geometry.get("voids", [])
                if isinstance(void.get("shape"), dict)
            ):
                continue
            matched = generated_by_entities.pop(signature, None)
            if matched is None:
                loop_entities = set(loop["entity_ids"])
                candidates = []
                for order, old_region in enumerate(unused_generated):
                    old_shape = old_region.get("shape", {})
                    shared_entities = len(loop_entities.intersection(old_shape.get("entity_ids", [])))
                    if shared_entities <= 0:
                        continue
                    old_area = abs(_polygon_area(old_region.get("vertices", [])))
                    new_area = abs(_polygon_area(vertices))
                    candidates.append((shared_entities, -abs(old_area - new_area), -order, old_region))
                if candidates:
                    matched = max(candidates, key=lambda item: item[:3])[3]
            if matched in unused_generated:
                unused_generated.remove(matched)
            if matched is not None:
                template_uses[matched.get("id")] = max(1, template_uses.get(matched.get("id"), 0))
                generated_by_entities = {
                    key: value for key, value in generated_by_entities.items() if value is not matched
                }
            region = copy.deepcopy(matched or {})
            if not region:
                probe = self._sketch_loop_interior_point(vertices)
                owners = [
                    item for item in source_templates
                    if _point_in_or_on_polygon(probe, item.get("vertices", []))
                ]
                template = owners[-1] if owners else None
                if template is not None:
                    region = copy.deepcopy(template)
                    template_id = template.get("id")
                    template_uses[template_id] = template_uses.get(template_id, 0) + 1
                    part_number = template_uses[template_id]
                    if part_number > 1:
                        region["id"] = new_id("region")
                        region["name"] = f"{template.get('name', 'Region')} part {part_number}"
                else:
                    region = {
                        "id": new_id("region"),
                        "name": f"Region {1 + sum(1 for item in geometry['regions'] if item.get('name', '').startswith('Region '))}",
                        "material_id": None,
                        "mechanical": False,
                        "sources": {
                            "dc_current_density": "0",
                            "ac_current_density_real": "0",
                            "ac_current_density_imaginary": "0",
                            "mechanical_body_force": {"r": "0", "z": "0"},
                        },
                        "constraints": [],
                    }
            region["vertices"] = copy.deepcopy(vertices)
            region["shape"] = {
                "type": "sketch",
                "entity_ids": list(loop["entity_ids"]),
                "boundary": copy.deepcopy(loop["boundary"]),
            }
            region["parent_id"] = parent_id
            region.setdefault("edge_ids", [])
            geometry["regions"].append(region)
        self._recompute_parent_links()
        self._rebuild_edges()
        self._prune_region_groups()

    @staticmethod
    def _sketch_loop_interior_point(vertices):
        """Choose an interior probe for assigning split faces to source regions."""
        if len(vertices) < 3:
            return tuple(vertices[0]) if vertices else (0.0, 0.0)
        signed_twice_area = sum(
            vertices[index][0] * vertices[(index + 1) % len(vertices)][1]
            - vertices[(index + 1) % len(vertices)][0] * vertices[index][1]
            for index in range(len(vertices))
        )
        if abs(signed_twice_area) > 1e-24:
            cx = sum(
                (vertices[index][0] + vertices[(index + 1) % len(vertices)][0])
                * (vertices[index][0] * vertices[(index + 1) % len(vertices)][1]
                   - vertices[(index + 1) % len(vertices)][0] * vertices[index][1])
                for index in range(len(vertices))
            ) / (3.0 * signed_twice_area)
            cy = sum(
                (vertices[index][1] + vertices[(index + 1) % len(vertices)][1])
                * (vertices[index][0] * vertices[(index + 1) % len(vertices)][1]
                   - vertices[(index + 1) % len(vertices)][0] * vertices[index][1])
                for index in range(len(vertices))
            ) / (3.0 * signed_twice_area)
            centroid = (cx, cy)
            if _point_in_or_on_polygon(centroid, vertices):
                return centroid
        span = max(
            max(point[0] for point in vertices) - min(point[0] for point in vertices),
            max(point[1] for point in vertices) - min(point[1] for point in vertices),
            1e-12,
        )
        orientation = 1.0 if signed_twice_area >= 0 else -1.0
        for index, start in enumerate(vertices):
            end = vertices[(index + 1) % len(vertices)]
            dx, dy = end[0] - start[0], end[1] - start[1]
            length = math.hypot(dx, dy)
            if length <= 1e-15:
                continue
            for fraction in (1e-5, 1e-7, 1e-9):
                offset = span * fraction
                probe = (
                    0.5 * (start[0] + end[0]) - orientation * dy / length * offset,
                    0.5 * (start[1] + end[1]) + orientation * dx / length * offset,
                )
                if _point_in_or_on_polygon(probe, vertices):
                    return probe
        return tuple(vertices[0])

    @_history_tracked
    def delete_selected_sketch_edges(self):
        """Delete selected edges, leaving unselected straight outline segments editable."""
        geometry = self.model["geometry"]
        geometry.setdefault("voids", [])
        requested_ids = set(
            self.selected_edge_ids
            or ([self.selected_edge_id] if self.selected_edge_id else [])
        )
        edges_by_id = {
            edge.get("id"): edge for edge in geometry.get("edges", [])
            if isinstance(edge, dict) and isinstance(edge.get("id"), str)
        }
        delete_ids = requested_ids.intersection(edges_by_id)
        if not delete_ids:
            self._message("Select one or more edges to delete.", error=True)
            return False

        entity_ids = {
            edges_by_id[edge_id].get("sketch_entity_id")
            for edge_id in delete_ids
            if edges_by_id[edge_id].get("sketch_entity_id")
        }
        # A selected segment belongs to its original sketch curve. Remove the
        # whole curve so its other cached edge segments cannot come back.
        delete_ids.update(
            edge.get("id")
            for edge in geometry.get("edges", [])
            if edge.get("sketch_entity_id") in entity_ids
        )
        affected_regions = {
            region.get("id")
            for region in geometry.get("regions", [])
            if set(region.get("edge_ids", [])).intersection(delete_ids)
        }
        # Sketch regions are derived faces of the editable curve network. Let
        # the topology rebuild remove/recreate those faces after a curve is
        # deleted; deleting the cached faces directly also removes their
        # surviving outline curves and destroys the original region.
        generated_sketch_regions = {
            region.get("id")
            for region in geometry.get("regions", [])
            if region.get("id") in affected_regions
            and isinstance(region.get("shape"), dict)
            and region["shape"].get("type") == "sketch"
        }
        regions_to_delete = affected_regions - generated_sketch_regions
        affected_voids = [
            void for void in geometry.get("voids", [])
            if set(void.get("edge_ids", [])).intersection(delete_ids)
        ]

        def preserve_as_sketch(edge):
            if not isinstance(edge, dict) or edge.get("sketch_entity_id"):
                return
            vertices = edge.get("vertices", [])
            if len(vertices) != 2:
                return
            curve = edge.get("curve", {})
            if isinstance(curve, dict) and curve.get("type") not in {None, "line"}:
                return
            entity_id = new_id("curve")
            points = copy.deepcopy(vertices)
            entity = {
                "id": entity_id,
                "type": "line",
                "points": points,
                "sampled_points": points,
                "curve": {"type": "line"},
            }
            geometry.setdefault("sketch_entities", []).append(entity)
            edge["sketch_entity_id"] = entity_id

        for region in geometry.get("regions", []):
            if region.get("id") not in regions_to_delete:
                continue
            for edge_id in region.get("edge_ids", []):
                if edge_id in delete_ids:
                    continue
                edge = edges_by_id.get(edge_id)
                if edge is None:
                    continue
                used_elsewhere = any(
                    other.get("id") not in regions_to_delete
                    and edge_id in other.get("edge_ids", [])
                    for other in geometry.get("regions", [])
                )
                if not used_elsewhere:
                    preserve_as_sketch(edge)

        for void in affected_voids:
            for edge_id in void.get("edge_ids", []):
                if edge_id not in delete_ids:
                    preserve_as_sketch(edges_by_id.get(edge_id))

        geometry["sketch_entities"] = [
            entity for entity in geometry.get("sketch_entities", [])
            if entity.get("id") not in entity_ids
        ]
        geometry["edges"] = [
            edge for edge in geometry.get("edges", [])
            if edge.get("id") not in delete_ids
        ]
        geometry["voids"] = [
            void for void in geometry.get("voids", [])
            if not set(void.get("edge_ids", [])).intersection(delete_ids)
        ]

        if regions_to_delete:
            self._delete_regions(
                regions_to_delete,
                create_holes=False,
                notify=False,
            )
        self._refresh_sketch_generated_regions()
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self.selected_region_id = None
        self.selected_region_ids = []
        self._refresh_model_tree()
        self._render_inspector()
        self.render_canvas()
        self._message(f"Deleted {len(delete_ids)} selected edge(s).")
        return True

    def _trim_sketch_edge_at_canvas_point(self, edge_id, canvas_point):
        edge = next((item for item in self.model["geometry"].get("edges", []) if item.get("id") == edge_id), None)
        entity_id = edge.get("sketch_entity_id") if edge else None
        if not edge:
            self._message("Select a sketch curve to trim.", error=True)
            return False
        _, unproject = self._canvas_projection()
        click = unproject(canvas_point)
        if entity_id:
            interval = None
            try:
                first = float(edge.get("sketch_t0", 0.0))
                second = float(edge.get("sketch_t1", 1.0))
                if math.isfinite(first) and math.isfinite(second):
                    interval = (first, second)
            except (TypeError, ValueError, OverflowError):
                pass
            return self._trim_sketch_entities_along_stroke(
                [click, click],
                target_entity_ids={entity_id},
                target_intervals={entity_id: interval} if interval is not None else None,
            )
        return self._trim_sketch_entities_along_stroke(
            [click, click], target_edge_id=edge_id
        )

    @staticmethod
    def _dimension_literal_m(value_m):
        """Format a stored metre coordinate as a stable SI expression."""
        return format(value_m, ".12g")

    @_history_tracked
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
        dr = _round_sketch_m(end_world[0] - start_world[0])
        dz = _round_sketch_m(end_world[1] - start_world[1])
        if dr == 0 and dz == 0:
            if preserve_canvas:
                self._rollback_canvas_region_move(region_id)
            else:
                self.render_canvas()
            return False

        self._preserve_canvas_view()
        geometry = self.model["geometry"]
        old_geometry_points = copy.deepcopy(geometry.get("points", []))
        old_boundary_loops = {
            item["id"]: copy.deepcopy(item.get("boundary_loops", []))
            for item in geometry.get("regions", [])
        }
        old_shape = copy.deepcopy(region["shape"])
        old_vertices = copy.deepcopy(region["vertices"])
        old_parents = {item["id"]: item.get("parent_id") for item in geometry["regions"]}
        old_region_edge_ids = {
            item["id"]: copy.deepcopy(item.get("edge_ids"))
            for item in geometry.get("regions", [])
        }
        old_edges = copy.deepcopy(geometry.get("edges", []))
        had_sketch_entities = "sketch_entities" in geometry
        old_sketch_entities = copy.deepcopy(geometry.get("sketch_entities", []))
        had_voids = "voids" in geometry
        old_voids = copy.deepcopy(geometry.get("voids", []))
        had_boundary_conditions = "boundary_conditions" in self.model
        old_boundary_conditions = copy.deepcopy(self.model.get("boundary_conditions", []))
        try:
            shape = region["shape"]
            imported_edge_ids = set()
            if shape["type"] == "rectangle":
                expressions = shape.setdefault("dimension_expressions", {})
                shape["r_min"] += dr
                shape["z_min"] += dz
                if shape["r_min"] < -1e-12:
                    raise ValueError("A region cannot be moved left of the r = 0 axis.")
                shape["r_min"] = max(0.0, shape["r_min"])
                expressions["r_min"] = self._dimension_literal_m(shape["r_min"])
                expressions["z_min"] = self._dimension_literal_m(shape["z_min"])
                r0, z0 = shape["r_min"], shape["z_min"]
                width, height = shape["width"], shape["height"]
                region["vertices"] = [[r0, z0], [r0 + width, z0], [r0 + width, z0 + height], [r0, z0 + height]]
            elif shape.get("type") == "sketch":
                shared = [
                    other for other in self.model["geometry"].get("regions", [])
                    if other["id"] != region_id
                    and isinstance(other.get("shape"), dict)
                    and other["shape"].get("type") == "sketch"
                    and set(other["shape"].get("entity_ids", [])) & set(shape.get("entity_ids", []))
                ]
                if shared:
                    raise ValueError("This sketch shares curves with another region; move its curves together instead.")
                region["vertices"] = [[point[0] + dr, point[1] + dz] for point in region["vertices"]]
                for entity in self.model["geometry"].get("sketch_entities", []):
                    if entity.get("id") not in shape.get("entity_ids", []):
                        continue
                    for key in ("points", "sampled_points"):
                        entity[key] = [[point[0] + dr, point[1] + dz] for point in entity.get(key, [])]
                    if isinstance(entity.get("control_point"), (list, tuple)):
                        entity["control_point"] = [entity["control_point"][0] + dr, entity["control_point"][1] + dz]
                    if isinstance(entity.get("center"), list):
                        entity["center"] = [entity["center"][0] + dr, entity["center"][1] + dz]
            elif shape.get("type") == "imported_contour":
                # _move_imported_contour translates these model edges itself.
                # Remember their IDs so the shared edge pass below does not
                # apply the same displacement a second time.
                imported_edge_ids = set(region.get("edge_ids", []))
                self._move_imported_contour(region, dr, dz)
                self._sync_imported_contour_dimension_expressions(region)
            elif shape.get("type") == "circle":
                expressions = shape.setdefault("dimension_expressions", {})
                shape["r_center"] += dr
                shape["z_center"] += dz
                if shape["r_center"] < shape["radius"] - 1e-12:
                    raise ValueError("The circle must remain at r ≥ 0.")
                shape["r_center"] = max(shape["radius"], shape["r_center"])
                expressions["r_center"] = self._dimension_literal_m(shape["r_center"])
                expressions["z_center"] = self._dimension_literal_m(shape["z_center"])
                radius = shape["radius"]
                rcenter, zcenter = shape["r_center"], shape["z_center"]
                segments = int(shape.get("segments", 48))
                region["vertices"] = [
                    [rcenter + radius * math.cos(2 * math.pi * index / segments),
                     zcenter + radius * math.sin(2 * math.pi * index / segments)]
                    for index in range(segments)
                ]
            else:
                raise ValueError(f"Dragging is not supported for {shape.get('type', 'unknown')} regions.")

            # Move cutouts with their containing region so a hole stays in place
            # relative to the material boundary.
            edge_ids = set(region.get("edge_ids", []))
            for void in self.model["geometry"].get("voids", []):
                if void.get("parent_id") != region_id:
                    continue
                void["vertices"] = [
                    [point[0] + dr, point[1] + dz]
                    for point in void.get("vertices", [])
                ]
                edge_ids.update(void.get("edge_ids", []))
                void_shape = void.get("shape", {})
                if isinstance(void_shape, dict):
                    if void_shape.get("type") == "rectangle":
                        void_shape["r_min"] = float(void_shape.get("r_min", 0.0)) + dr
                        void_shape["z_min"] = float(void_shape.get("z_min", 0.0)) + dz
                    elif void_shape.get("type") == "circle":
                        void_shape["r_center"] = float(void_shape.get("r_center", 0.0)) + dr
                        void_shape["z_center"] = float(void_shape.get("z_center", 0.0)) + dz
                    elif void_shape.get("type") == "sketch":
                        entity_ids = set(void_shape.get("entity_ids", []))
                        for entity in self.model["geometry"].get("sketch_entities", []):
                            if entity.get("id") not in entity_ids:
                                continue
                            for key in ("points", "sampled_points"):
                                entity[key] = [
                                    [point[0] + dr, point[1] + dz]
                                    for point in entity.get(key, [])
                                ]
                            if isinstance(entity.get("center"), list):
                                entity["center"] = [entity["center"][0] + dr, entity["center"][1] + dz]
            # Moving existing edges before rebuilding preserves their IDs,
            # names, and electromagnetic/mechanical boundary assignments.
            for edge in self.model["geometry"].get("edges", []):
                if edge["id"] in edge_ids and edge["id"] not in imported_edge_ids:
                    edge["vertices"] = [[point[0] + dr, point[1] + dz] for point in edge["vertices"]]
                    curve = edge.get("curve")
                    if isinstance(curve, dict) and isinstance(curve.get("sampled_points"), list):
                        curve["sampled_points"] = [
                            [point[0] + dr, point[1] + dz]
                            for point in curve["sampled_points"]
                        ]
            self._recompute_parent_links()
            if shape.get("type") == "imported_contour":
                self._rebuild_imported_parent_loops()
                _sync_axis_boundary_conditions(self.model)
                _sync_mechanical_boundary_conditions(self.model)
            else:
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
                    affected_fills = [
                        region_id,
                        old_parents.get(region_id),
                        region.get("parent_id"),
                    ]
                    self._commit_canvas_region_move(region_id, dr, dz, affected_fills)
                else:
                    self.render_canvas()
            self._message(f"Moved {region['name']} by {dr:g} m radially and {dz:g} m axially.")
            return True
        except (KeyError, ValueError, TypeError, OverflowError) as error:
            geometry = self.model["geometry"]
            region["shape"] = old_shape
            region["vertices"] = old_vertices
            geometry["edges"] = old_edges
            geometry["points"] = old_geometry_points
            if had_sketch_entities:
                geometry["sketch_entities"] = old_sketch_entities
            else:
                geometry.pop("sketch_entities", None)
            if had_voids:
                geometry["voids"] = old_voids
            else:
                geometry.pop("voids", None)
            for item in geometry["regions"]:
                item["parent_id"] = old_parents[item["id"]]
                item["boundary_loops"] = old_boundary_loops[item["id"]]
                saved_edge_ids = old_region_edge_ids[item["id"]]
                if saved_edge_ids is None:
                    item.pop("edge_ids", None)
                else:
                    item["edge_ids"] = saved_edge_ids
            if had_boundary_conditions:
                self.model["boundary_conditions"] = old_boundary_conditions
            else:
                self.model.pop("boundary_conditions", None)
            self._message(str(error), error=True)
            if preserve_canvas:
                self._rollback_canvas_region_move(region_id)
            else:
                self.render_canvas()
            return False

    def _is_imported_legacy_edge(self, edge):
        entity_id = edge.get("sketch_entity_id")
        if not isinstance(entity_id, str):
            return False
        entity = next((
            item for item in self.model.get("geometry", {}).get("sketch_entities", [])
            if item.get("id") == entity_id
        ), None)
        return isinstance(entity, dict) and entity.get("type") in {"line", "spline3"}

    @staticmethod
    def _sample_imported_spline3(start, control, end, count=33):
        # Keep this compatibility helper on the same rational definition used
        # by native sketch topology and the Netgen Spline3 solver adapter.
        return sample_sketch_entity(
            {"type": "spline3", "points": [list(start), list(control), list(end)]},
            count=count,
        )

    def _sync_imported_contour_dimension_expressions(self, region):
        if region.get("shape", {}).get("type") != "imported_contour" or not region.get("vertices"):
            return
        radial = [float(point[0]) for point in region["vertices"]]
        axial = [float(point[1]) for point in region["vertices"]]
        r_min, z_min = min(radial), min(axial)
        region["shape"].setdefault("dimension_expressions", {}).update({
            "r_min": self._dimension_literal_m(r_min),
            "z_min": self._dimension_literal_m(z_min),
            "width": self._dimension_literal_m(max(radial) - r_min),
            "height": self._dimension_literal_m(max(axial) - z_min),
        })

    def _refresh_imported_region_vertices(self):
        """Rebuild imported region polygons from their exact edge-loop samples."""
        edge_by_id = {
            edge.get("id"): edge
            for edge in self.model.get("geometry", {}).get("edges", [])
            if edge.get("id")
        }
        for region in self.model.get("geometry", {}).get("regions", []):
            if region.get("shape", {}).get("type") != "imported_contour":
                continue
            outer = next((
                loop for loop in region.get("boundary_loops", [])
                if isinstance(loop, dict) and loop.get("role") == "outer"
            ), None)
            if not isinstance(outer, dict):
                continue
            vertices = []
            for reference in outer.get("edges", []):
                edge = edge_by_id.get(reference.get("edge_id"))
                if not edge:
                    continue
                curve = edge.get("curve", {})
                samples = curve.get("sampled_points") if isinstance(curve, dict) else None
                samples = samples or edge.get("vertices", [])
                if not reference.get("forward", True):
                    samples = list(reversed(samples))
                for point in samples:
                    point = [float(point[0]), float(point[1])]
                    if not vertices or math.dist(vertices[-1], point) > 1e-12:
                        vertices.append(point)
            if len(vertices) > 1 and math.dist(vertices[0], vertices[-1]) <= 1e-12:
                vertices.pop()
            if len(vertices) >= 3:
                region["vertices"] = vertices
                self._sync_imported_contour_dimension_expressions(region)

    def _transform_sketch_contour_bounds(self, region, target_bounds):
        """Resize a sketch contour by an affine transform while keeping shared points connected."""
        shape = region.get("shape", {})
        entity_ids = set(shape.get("entity_ids", [])) if isinstance(shape, dict) else set()
        if not entity_ids:
            raise ValueError("This sketch contour has no editable boundary curves.")
        regions = self.model.get("geometry", {}).get("regions", [])
        for other in regions:
            if other.get("id") == region.get("id"):
                continue
            other_shape = other.get("shape", {})
            if isinstance(other_shape, dict) and entity_ids.intersection(other_shape.get("entity_ids", [])):
                raise ValueError("This contour shares a curve with another region. Edit its shared edge or endpoint instead of resizing one side.")
        vertices = region.get("vertices", [])
        if len(vertices) < 3:
            raise ValueError("This sketch contour has no closed outline.")
        old_r = [float(point[0]) for point in vertices]
        old_z = [float(point[1]) for point in vertices]
        old_r_min, old_r_max = min(old_r), max(old_r)
        old_z_min, old_z_max = min(old_z), max(old_z)
        old_width, old_height = old_r_max - old_r_min, old_z_max - old_z_min
        new_r_min, new_r_max, new_z_min, new_z_max = (float(value) for value in target_bounds)
        if old_width <= 1e-15 or old_height <= 1e-15:
            raise ValueError("This sketch contour must have nonzero radial and axial dimensions.")
        if new_r_min < -1e-12 or new_r_max <= new_r_min or new_z_max <= new_z_min:
            raise ValueError("The sketch contour needs r ≥ 0 and positive dimensions.")
        radial_scale = (new_r_max - new_r_min) / old_width
        axial_scale = (new_z_max - new_z_min) / old_height

        def transform(point):
            radial = new_r_min + (float(point[0]) - old_r_min) * radial_scale
            if radial < -1e-12:
                raise ValueError("The edit would place part of the contour at r < 0.")
            return [0.0 if abs(radial) <= 1e-12 else radial,
                    new_z_min + (float(point[1]) - old_z_min) * axial_scale]

        geometry = self.model["geometry"]
        entities = geometry.get("sketch_entities", [])
        entities_by_id = {
            entity.get("id"): entity for entity in entities
            if isinstance(entity, dict) and isinstance(entity.get("id"), str)
        }
        if not entity_ids.issubset(entities_by_id):
            raise ValueError("This sketch contour refers to a missing curve.")
        point_updates = {}
        for entity_id in entity_ids:
            entity = entities_by_id[entity_id]
            points = entity.get("points", [])
            point_ids = entity.get("point_ids", [])
            if len(points) != len(point_ids):
                raise ValueError("A sketch curve is missing persistent point references.")
            for point_id, point in zip(point_ids, points):
                candidate = transform(point)
                previous = point_updates.get(point_id)
                if previous is not None and math.dist(previous, candidate) > 1e-10:
                    raise ValueError("Connected sketch curves disagree about a shared point.")
                point_updates[point_id] = candidate

        affected = set(entity_ids)
        for entity in entities:
            entity_points = entity.get("points", [])
            point_ids = entity.get("point_ids", [])
            if entity.get("id") in entity_ids:
                entity["points"] = [transform(point) for point in entity_points]
                if isinstance(entity.get("control_point"), (list, tuple)):
                    entity["control_point"] = transform(entity["control_point"])
                if isinstance(entity.get("center"), (list, tuple)):
                    entity["center"] = transform(entity["center"])
                affected.add(entity.get("id"))
            elif len(entity_points) == len(point_ids) and any(point_id in point_updates for point_id in point_ids):
                entity["points"] = [
                    list(point_updates[point_id]) if point_id in point_updates else list(point)
                    for point_id, point in zip(point_ids, entity_points)
                ]
                control_id = entity.get("control_point_id")
                if control_id in point_updates:
                    entity["control_point"] = list(point_updates[control_id])
                affected.add(entity.get("id"))
            if entity.get("id") in affected:
                sample_count = max(33, len(entity.get("sampled_points", [])) or 0)
                entity["sampled_points"] = sample_sketch_entity(entity, sample_count)

        point_records = {
            point.get("id"): point for point in geometry.get("points", [])
            if isinstance(point, dict) and point.get("id")
        }
        for point_id, coordinates in point_updates.items():
            record = point_records.get(point_id)
            if record is None:
                raise ValueError("A sketch point is missing from the point table.")
            record["coordinates"] = list(coordinates)

        templates = copy.deepcopy(regions)
        self._refresh_sketch_generated_regions(source_templates=templates)

    def _transform_imported_contour_bounds(self, region, target_bounds):
        """Resize/reposition one legacy contour while retaining curve and edge IDs."""
        vertices = region.get("vertices", [])
        if len(vertices) < 3:
            raise ValueError("This imported contour has no editable closed outline.")
        old_r = [float(point[0]) for point in vertices]
        old_z = [float(point[1]) for point in vertices]
        old_r_min, old_r_max = min(old_r), max(old_r)
        old_z_min, old_z_max = min(old_z), max(old_z)
        old_width, old_height = old_r_max - old_r_min, old_z_max - old_z_min
        new_r_min, new_r_max, new_z_min, new_z_max = target_bounds
        if old_width <= 1e-15 or old_height <= 1e-15:
            raise ValueError("This imported contour must have non-zero radial and axial dimensions.")
        if new_r_min < -1e-12 or new_r_max <= new_r_min or new_z_max <= new_z_min:
            raise ValueError("The imported contour needs r ≥ 0 and positive dimensions.")
        radial_scale = (new_r_max - new_r_min) / old_width
        axial_scale = (new_z_max - new_z_min) / old_height

        def transform(point):
            radial = new_r_min + (float(point[0]) - old_r_min) * radial_scale
            if radial < -1e-12:
                raise ValueError("The edit would place part of the contour at r < 0.")
            return [0.0 if abs(radial) <= 1e-12 else radial,
                    new_z_min + (float(point[1]) - old_z_min) * axial_scale]

        geometry = self.model["geometry"]
        edge_ids = set(region.get("edge_ids", []))
        selected_edges = [edge for edge in geometry.get("edges", []) if edge.get("id") in edge_ids]
        if not selected_edges or len(selected_edges) != len(edge_ids):
            raise ValueError("This imported contour references missing edges and cannot be resized safely.")
        point_records = {
            point.get("id"): point for point in geometry.get("points", [])
            if isinstance(point, dict) and point.get("id")
        }
        source_coordinates = {}
        for edge in selected_edges:
            for point_id, point in zip(edge.get("point_ids", []), edge.get("vertices", [])):
                if point_id:
                    source_coordinates.setdefault(point_id, point)
            curve = edge.get("curve", {})
            if isinstance(curve, dict) and curve.get("control_point_id"):
                source_coordinates[curve["control_point_id"]] = curve.get("control_point")
        transformed = {}
        for point_id, point in source_coordinates.items():
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                point = point_records.get(point_id, {}).get("coordinates")
            if not isinstance(point, (list, tuple)) or len(point) < 2:
                raise ValueError("An imported geometry point is missing; the contour cannot be resized safely.")
            transformed[point_id] = transform(point)
        for point_id, point in transformed.items():
            if point_id in point_records:
                point_records[point_id]["coordinates"] = list(point)
            else:
                new_record = {"id": point_id, "coordinates": list(point)}
                geometry.setdefault("points", []).append(new_record)
                point_records[point_id] = new_record

        for edge in geometry.get("edges", []):
            curve = edge.get("curve", {})
            point_ids = edge.get("point_ids", [])
            endpoint_changed = False
            for index, point_id in enumerate(point_ids[:2]):
                if point_id in transformed and index < len(edge.get("vertices", [])):
                    edge["vertices"][index] = list(transformed[point_id])
                    endpoint_changed = True
            if edge.get("id") in edge_ids:
                if isinstance(curve, dict):
                    if isinstance(curve.get("sampled_points"), list):
                        curve["sampled_points"] = [transform(point) for point in curve["sampled_points"]]
                    if isinstance(curve.get("control_point"), (list, tuple)):
                        control_id = curve.get("control_point_id")
                        curve["control_point"] = list(transformed[control_id]) if control_id in transformed else transform(curve["control_point"])
                    if isinstance(curve.get("center"), (list, tuple)):
                        curve["center"] = transform(curve["center"])
            elif endpoint_changed and isinstance(curve, dict):
                if curve.get("type") == "spline3":
                    control_id = curve.get("control_point_id")
                    control = transformed.get(control_id, curve.get("control_point"))
                    if control is not None:
                        curve["control_point"] = list(control)
                        curve["sampled_points"] = self._sample_imported_spline3(
                            edge["vertices"][0], control, edge["vertices"][1]
                        )
                elif curve.get("type") == "line":
                    curve["sampled_points"] = copy.deepcopy(edge.get("vertices", []))
                elif isinstance(curve.get("sampled_points"), list) and curve["sampled_points"]:
                    curve["sampled_points"][0] = list(edge["vertices"][0])
                    curve["sampled_points"][-1] = list(edge["vertices"][1])
        self._refresh_imported_region_vertices()

    @_history_tracked
    def _set_imported_edge_coordinate(self, edge_id, point_role, axis, value):
        """Edit a native sketch curve point, including curves migrated from legacy Python."""
        edge = next((
            item for item in self.model.get("geometry", {}).get("edges", [])
            if item.get("id") == edge_id
        ), None)
        if edge is None or point_role not in {"start", "end", "control"} or axis not in {"r", "z"}:
            return
        entity_id = edge.get("sketch_entity_id")
        geometry = self.model.get("geometry", {})
        entity = next((
            item for item in geometry.get("sketch_entities", [])
            if isinstance(item, dict) and item.get("id") == entity_id
        ), None)
        if not isinstance(entity, dict) or entity.get("type") not in {"line", "spline3"}:
            return
        if point_role == "control" and entity.get("type") != "spline3":
            return
        old_geometry = copy.deepcopy(geometry)
        old_conditions = copy.deepcopy(self.model.get("boundary_conditions", []))
        old_mesh = copy.deepcopy(self.model.get("mesh", {}))
        source_templates = copy.deepcopy(geometry.get("regions", []))
        try:
            parameters = {item["name"]: item for item in self.model.get("parameters", [])}
            coordinate = evaluate_expression(str(value).strip(), parameters)
            if not math.isfinite(coordinate):
                raise ValueError("Coordinates must be finite SI lengths.")
            self._preserve_canvas_view()
            points = entity.get("points", [])
            point_ids = entity.get("point_ids", [])
            if len(points) < 2 or len(points) != len(point_ids):
                raise ValueError("This sketch curve is missing editable point IDs.")
            axis_index = 0 if axis == "r" else 1
            if point_role == "start":
                point_index = 0
            elif point_role == "end":
                point_index = len(points) - 1
            else:
                point_index = 1
            point_id = (
                entity.get("control_point_id") if point_role == "control"
                else point_ids[point_index]
            ) or point_ids[point_index]
            point_records = {item.get("id"): item for item in geometry.get("points", []) if item.get("id")}
            if point_id not in point_records:
                raise ValueError("This sketch point is missing from the point table.")
            updated = list(point_records[point_id].get("coordinates", points[point_index]))
            if len(updated) != 2:
                raise ValueError("This sketch point has invalid coordinates.")
            updated[axis_index] = coordinate
            if updated[0] < -1e-12:
                raise ValueError("Axisymmetric geometry must remain at r ≥ 0.")
            updated[0] = 0.0 if abs(updated[0]) <= 1e-12 else updated[0]
            point_records[point_id]["coordinates"] = list(updated)

            changed_entities = set()
            for candidate in geometry.get("sketch_entities", []):
                candidate_points = candidate.get("points", [])
                candidate_ids = candidate.get("point_ids", [])
                changed = False
                if len(candidate_points) == len(candidate_ids):
                    for index, candidate_id in enumerate(candidate_ids):
                        if candidate_id == point_id:
                            candidate_points[index] = list(updated)
                            changed = True
                if candidate.get("control_point_id") == point_id:
                    candidate["control_point"] = list(updated)
                    changed = True
                if changed:
                    sample_count = max(33, len(candidate.get("sampled_points", [])) or 0)
                    candidate["sampled_points"] = sample_sketch_entity(candidate, sample_count)
                    changed_entities.add(candidate.get("id"))

            if not changed_entities:
                raise ValueError("The selected sketch point is not connected to an editable curve.")
            self._refresh_sketch_generated_regions(source_templates=source_templates)
            _sync_axis_boundary_conditions(self.model)
            _sync_mechanical_boundary_conditions(self.model)
            self._refresh_model_tree()
            self._render_inspector()
            self._message("Updated sketch curve point.")
        except (ValueError, TypeError, OverflowError) as error:
            self.model["geometry"] = old_geometry
            self.model["boundary_conditions"] = old_conditions
            self.model["mesh"] = old_mesh
            self._refresh_model_tree()
            self._render_inspector()
            self._message(str(error), error=True)
        self.render_canvas()

    def _move_imported_contour(self, region, dr, dz):
        """Translate an imported contour without rebuilding its exact legacy curves."""
        geometry = self.model["geometry"]
        region_id = region["id"]
        edge_ids = set(region.get("edge_ids", []))
        if not edge_ids:
            raise ValueError("This imported region has no movable boundary edges.")
        if any(
            edge_ids.intersection(other.get("edge_ids", []))
            for other in geometry.get("regions", [])
            if other.get("id") != region_id
        ):
            raise ValueError("This imported region shares boundary edges with another region and cannot be moved independently.")

        edges = [edge for edge in geometry.get("edges", []) if edge.get("id") in edge_ids]
        if len(edges) != len(edge_ids):
            raise ValueError("The imported region references a missing boundary edge.")
        old_points = {
            point.get("id"): point for point in geometry.get("points", [])
            if isinstance(point, dict) and isinstance(point.get("id"), str)
        }
        externally_referenced_point_ids = set()
        for edge in geometry.get("edges", []):
            if edge.get("id") in edge_ids:
                continue
            externally_referenced_point_ids.update(
                point_id for point_id in edge.get("point_ids", [])
                if isinstance(point_id, str)
            )
            curve = edge.get("curve")
            if isinstance(curve, dict) and isinstance(curve.get("control_point_id"), str):
                externally_referenced_point_ids.add(curve["control_point_id"])
        coordinates_by_id = {}
        for edge in edges:
            vertices = edge.get("vertices", [])
            point_ids = edge.get("point_ids", [])
            if len(vertices) != 2 or len(point_ids) != 2:
                raise ValueError("The imported region has incomplete point topology and cannot be moved safely.")
            for point_id, point in zip(point_ids, vertices):
                if isinstance(point_id, str) and isinstance(point, (list, tuple)) and len(point) >= 2:
                    coordinates_by_id.setdefault(point_id, [float(point[0]), float(point[1])])
            curve = edge.get("curve")
            if isinstance(curve, dict):
                control_id = curve.get("control_point_id")
                control = curve.get("control_point")
                if isinstance(control_id, str) and isinstance(control, (list, tuple)) and len(control) >= 2:
                    coordinates_by_id[control_id] = [float(control[0]), float(control[1])]

        point_id_map = {}
        moved_points = []

        def moved_coordinate(point):
            radial = float(point[0]) + dr
            if radial < -1e-12:
                raise ValueError("A region cannot be moved left of the r = 0 axis.")
            return [0.0 if abs(radial) <= 1e-12 else radial, float(point[1]) + dz]

        def remap_point_id(point_id):
            if not isinstance(point_id, str) or not point_id:
                raise ValueError("The imported region has an invalid point reference.")
            if point_id not in point_id_map:
                point = coordinates_by_id.get(point_id)
                if point is None:
                    stored = old_points.get(point_id, {}).get("coordinates")
                    if isinstance(stored, (list, tuple)) and len(stored) == 2:
                        point = [float(stored[0]), float(stored[1])]
                if point is None:
                    raise ValueError("The imported region references a point that is missing from the geometry.")
                moved = moved_coordinate(point)
                if point_id in externally_referenced_point_ids:
                    new_point_id = new_id("point")
                    moved_points.append({"id": new_point_id, "coordinates": moved})
                else:
                    new_point_id = point_id
                    point_record = old_points.get(point_id)
                    if point_record is None:
                        moved_points.append({"id": new_point_id, "coordinates": moved})
                    else:
                        point_record["coordinates"] = moved
                point_id_map[point_id] = new_point_id
            return point_id_map[point_id]

        moved_vertices = [moved_coordinate(point) for point in region.get("vertices", [])]
        for edge in edges:
            edge["point_ids"] = [remap_point_id(point_id) for point_id in edge["point_ids"]]
            edge["vertices"] = [moved_coordinate(point) for point in edge["vertices"]]
            curve = edge.get("curve")
            if not isinstance(curve, dict):
                continue
            if isinstance(curve.get("sampled_points"), list):
                curve["sampled_points"] = [moved_coordinate(point) for point in curve["sampled_points"]]
            for key in ("control_point", "center"):
                if isinstance(curve.get(key), (list, tuple)) and len(curve[key]) >= 2:
                    curve[key] = moved_coordinate(curve[key])
            if curve.get("control_point_id"):
                curve["control_point_id"] = remap_point_id(curve["control_point_id"])
        region["vertices"] = moved_vertices
        geometry.setdefault("points", []).extend(moved_points)

    def _rebuild_imported_parent_loops(self):
        """Refresh nested-region hole references while retaining legacy edge IDs."""
        regions = self.model["geometry"].get("regions", [])
        regions_by_id = {item.get("id"): item for item in regions if item.get("id")}
        for parent in regions:
            loops = parent.get("boundary_loops")
            if not isinstance(loops, list):
                loops = []
            parent["boundary_loops"] = [
                loop for loop in loops
                if not (
                    isinstance(loop, dict)
                    and loop.get("role") == "hole"
                    and loop.get("region_id") in regions_by_id
                )
            ]
        for child in regions:
            parent = regions_by_id.get(child.get("parent_id"))
            if not isinstance(parent, dict):
                continue
            outer = next((
                loop for loop in child.get("boundary_loops", [])
                if isinstance(loop, dict) and loop.get("role") == "outer"
            ), None)
            if not isinstance(outer, dict):
                continue
            references = [
                item for item in outer.get("edges", [])
                if isinstance(item, dict) and item.get("edge_id")
            ]
            if references:
                parent.setdefault("boundary_loops", []).append({
                    "role": "hole",
                    "region_id": child.get("id"),
                    "edges": [
                        {"edge_id": item["edge_id"], "forward": not bool(item.get("forward", True))}
                        for item in reversed(references)
                    ],
                })

    def _commit_canvas_region_move(self, region_id, dr, dz, fill_region_ids=()):
        """Commit model coordinates to every live fill and edge before clearing drag transforms."""
        regions_by_id = {
            item.get("id"): item for item in self.model["geometry"].get("regions", [])
        }
        fill_updates = [
            {"id": affected_id, "d": self._canvas_region_path_data(regions_by_id[affected_id])}
            for affected_id in dict.fromkeys(fill_region_ids)
            if affected_id in regions_by_id
        ]
        project, _ = self._canvas_projection()
        edge_updates = []
        for edge in self.model["geometry"].get("edges", []):
            vertices = edge.get("vertices", [])
            curve = edge.get("curve", {})
            curve_points = curve.get("sampled_points") if isinstance(curve, dict) else None
            display_points = (
                curve_points
                if isinstance(curve_points, list) and len(curve_points) >= 2
                else vertices
            )
            projected = [project(point) for point in display_points]
            if len(projected) < 2:
                continue
            path_data = f"M{projected[0][0]:.3f},{projected[0][1]:.3f}"
            path_data += " ".join(
                f"L{x:.3f},{y:.3f}" for x, y in projected[1:]
            )
            edge_updates.append({"id": edge.get("id"), "d": path_data})

        try:
            committed = self.js.eval(
                "(() => { const commit = window.__ngsolveCommitSketchRegionMove; "
                f"if (!commit) return false; return commit({json.dumps(region_id)}, "
                f"{json.dumps(fill_updates)}, {json.dumps(edge_updates)}); "
                "})()"
            )
            if committed is False:
                # If the live nodes no longer match the model, replace the
                # canvas once so no old clickable paths remain behind.
                self.render_canvas()
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
        if self._messages_visible:
            self.hide_messages()
        else:
            self.show_messages()

    def show_messages(self, *args):
        self._messages_visible = True
        self._messages_panel.ui_hidden = False

    def hide_messages(self, *args):
        self._messages_visible = False
        self._messages_panel.ui_hidden = True

    @staticmethod
    def _setup_check_category(issue):
        """Group messages by the input area that can resolve them."""
        text = str(issue).casefold()
        if "young's modulus" in text or "material" in text:
            return "materials"
        if any(term in text for term in ("mechanical region", "mechanical regions", "robin support", "stiffnesses must be non-negative", "region to include in mechanics", "axial motion")):
            return "mechanics"
        if any(term in text for term in ("regional load", "source", "current density", "body force")):
            return "loads"
        if "magnetic-potential boundary" in text or "finite domain boundary" in text:
            return "em_reference"
        if "frequency" in text:
            return "harmonic_frequencies"
        if "study" in text:
            return "studies"
        if any(term in text for term in ("mesh", "hp layer", "hp refinement", "hp-refined")):
            return "mesh"
        if any(term in text for term in ("solver", "anderson", "relative tolerance", "linear solver", "iteration")):
            return "solver"
        if "physics" in text or "coupling" in text:
            return "physics"
        if any(term in text for term in ("sketch edge", "geometry", "region", "polygon", "coordinate", "r=0 axis")):
            return "geometry"
        if "boundary" in text or "edge" in text or "axis of symmetry" in text:
            return "boundaries"
        return "model"

    def _setup_check_results(self, errors, warnings=None):
        physics = self.model.get("physics", {})
        if not isinstance(physics, dict):
            physics = {}
        def enabled(name):
            section = physics.get(name)
            return isinstance(section, dict) and section.get("enabled") is True

        dc_enabled = enabled("dc_magnetic")
        harmonic_enabled = enabled("harmonic_electromagnetic")
        mechanics_enabled = enabled("mechanics")
        active = dc_enabled or harmonic_enabled or mechanics_enabled
        by_category = {}
        warning_by_category = {}
        for issue in errors:
            by_category.setdefault(self._setup_check_category(issue), []).append(str(issue))
        for issue in warnings or []:
            warning_by_category.setdefault(self._setup_check_category(issue), []).append(str(issue))
        definitions = [
            ("model", "Model structure, parameters and units", True),
            ("geometry", "Geometry and region topology", True),
            ("materials", "Active material properties and assignments", active),
            ("boundaries", "Boundary definitions and assignments", active),
            ("loads", "Sources and loads for selected physics", active),
            ("physics", "Physics selection and coupling requirements", True),
            ("em_reference", "Electromagnetic boundaries (natural boundaries are allowed)", dc_enabled or harmonic_enabled),
            ("mechanics", "Mechanical domains and properties", mechanics_enabled),
            ("studies", "Study definition", True),
            ("harmonic_frequencies", "Time-harmonic frequency inputs", harmonic_enabled or mechanics_enabled),
            ("mesh", "Mesh and refinement settings", True),
            ("solver", "Solver settings", True),
            ("numerical_operator", "Mesh-dependent singularity and quality checks", active),
        ]
        results = []
        for category, label, applicable in definitions:
            category_errors = by_category.get(category, [])
            category_warnings = warning_by_category.get(category, [])
            unsupported = any("currently runs one study at a time" in item for item in category_errors)
            if not applicable:
                status = "Not applicable"
            elif unsupported:
                status = "Unsupported"
            elif category_errors:
                status = "Needs attention"
            elif category_warnings:
                status = "Warning"
            elif category == "numerical_operator":
                status = "Pending"
            else:
                status = "Passed"
            issues = category_errors + category_warnings
            if category == "numerical_operator" and applicable:
                issues = ["Operator singularities, resonance, conditioning, and mesh quality are evaluated after meshing during the solve."]
            results.append({
                "id": category,
                "label": label,
                "applicable": applicable,
                "issues": issues,
                "status": status,
            })
        return results

    def _show_setup_check_results(self, errors, warnings=None):
        """Show checks, warnings, solver-pending work, and clickable errors."""
        warnings = list(warnings or [])
        self.validation_issues = list(errors)
        self.setup_validation_warnings = warnings
        total = len(errors) + len(warnings)
        self._messages_button.ui_label = f"Messages ({total})" if total else "Messages"
        self.setup_check_results = self._setup_check_results(errors, warnings)
        passed = sum(item["status"] == "Passed" for item in self.setup_check_results)
        failed = sum(item["status"] in {"Needs attention", "Unsupported"} for item in self.setup_check_results)
        warned = sum(item["status"] == "Warning" for item in self.setup_check_results)
        pending = sum(item["status"] == "Pending" for item in self.setup_check_results)
        skipped = sum(item["status"] == "Not applicable" for item in self.setup_check_results)
        rows = [
            Div("Setup checks performed", ui_style="font-size:13px; font-weight:600; padding:2px 0;"),
            Div(
                f"{passed} passed · {failed} need attention · {warned} warnings · {pending} pending · {skipped} not applicable",
                ui_style="font-size:11px; color:var(--fg-muted); padding:0 0 5px;",
            ),
        ]
        for item in self.setup_check_results:
            status = item["status"]
            icon = "✓" if status == "Passed" else "!" if status in {"Needs attention", "Unsupported"} else "…" if status == "Pending" else "–"
            color = (
                "var(--positive)" if status == "Passed"
                else "var(--negative)" if status in {"Needs attention", "Unsupported"}
                else "var(--warning)" if status == "Warning"
                else "var(--fg-muted)"
            )
            issue_count = len(item["issues"])
            detail = f" ({issue_count} item{'s' if issue_count != 1 else ''})" if issue_count else ""
            rows.append(Div(
                f"{icon}  {item['label']} — {status}{detail}",
                ui_style=f"font-size:11px; padding:3px 0; color:{color};",
            ))
        if errors:
            rows.append(Div(
                "Issues to fix — select a message for guidance",
                ui_style="font-size:11px; font-weight:600; padding:7px 0 3px; border-top:1px solid var(--border);",
            ))
            for issue in errors:
                rows.append(_button(
                    issue,
                    "mdi-alert-circle-outline",
                    lambda *a, selected_issue=issue: self._navigate_to_validation_issue(selected_issue),
                    style="width:100%; justify-content:flex-start; text-align:left; white-space:normal;",
                    align="left",
                ))
        if warnings:
            rows.append(Div(
                "Warnings — these do not prevent a solve",
                ui_style="font-size:11px; font-weight:600; padding:7px 0 3px; border-top:1px solid var(--border);",
            ))
            rows.extend(Div(
                warning,
                ui_style="font-size:11px; padding:3px 0; color:var(--warning); white-space:normal;",
            ) for warning in warnings)
        if not errors and not warnings:
            rows.append(Div(
                "Input checks passed. Mesh-dependent operator checks and convergence remain pending until the solve runs.",
                ui_style="font-size:11px; color:var(--fg-muted); padding:7px 0 0; border-top:1px solid var(--border);",
            ))
        self._messages_body.ui_children = rows
        self._messages_visible = True
        self._messages_panel.ui_hidden = False

    def _show_validation_issues(self, errors):
        self.validation_issues = list(errors)
        self._messages_button.ui_label = f"Messages ({len(errors)})" if errors else "Messages"
        if not errors:
            self._messages_body.ui_children = [Div(
                "Setup checks passed. Meshing and solving still need to complete successfully.",
                ui_style="font-size:12px; color:var(--positive); padding:5px 0;",
            )]
            self._messages_visible = True
            self._messages_panel.ui_hidden = False
            return
        rows = []
        for issue in errors:
            rows.append(_button(
                issue,
                "mdi-alert-circle-outline",
                lambda *a, selected_issue=issue: self._navigate_to_validation_issue(selected_issue),
                style="width:100%; justify-content:flex-start; text-align:left; white-space:normal;",
                align="left",
            ))
        self._messages_body.ui_children = rows
        self._messages_visible = True
        self._messages_panel.ui_hidden = False

    def _navigate_to_validation_issue(self, issue):
        """Open the relevant inspector item when a setup message is clicked."""
        text = str(issue)
        section = self._validation_issue_section(text)
        matchers = (
            ("parameter", r"Parameter '([^']+)'"),
            ("materials", r"Material '([^']+)'"),
            ("boundaries", r"Boundary '([^']+)'"),
            ("sources", r"Regional load '([^']+)'"),
            ("boundaries", r"(?:Geometry )?Edge '([^']+)'"),
            ("region", r"Region '([^']+)'"),
        )
        target_kind = None
        target_name = None
        for kind, pattern in matchers:
            found = re.search(pattern, text, re.IGNORECASE)
            if found:
                target_kind, target_name = kind, found.group(1)
                break

        if target_name is not None:
            if target_kind == "parameter":
                matches = [item for item in self.model.get("parameters", []) if item.get("name") == target_name]
                if len(matches) == 1:
                    self.select_parameter(matches[0]["id"])
                    return
            elif target_kind == "materials":
                matches = [item for item in self.model.get("materials", []) if item.get("name") == target_name]
                if len(matches) == 1:
                    assigned = [region for region in self.model.get("geometry", {}).get("regions", []) if region.get("material_id") == matches[0].get("id")]
                    if assigned:
                        self._open_region_material_editor(assigned[0]["id"])
                    else:
                        self.select_section("materials")
                    return
            elif target_kind == "sources":
                matches = [item for item in self.model.get("regional_loads", []) if item.get("name") == target_name]
                if len(matches) == 1:
                    self.select_regional_load(matches[0]["id"])
                    self._open_sources_editor(matches[0]["id"])
                    return
            elif target_kind == "boundaries":
                matches = [item for item in self.model.get("boundary_conditions", []) if item.get("name") == target_name]
                if len(matches) == 1:
                    self.select_boundary(matches[0]["id"])
                    return
                edge_matches = [item for item in self.model.get("geometry", {}).get("edges", []) if item.get("name") == target_name or item.get("id") == target_name]
                if len(edge_matches) == 1:
                    self.select_edge(edge_matches[0]["id"])
                    return
            elif target_kind == "region":
                matches = [item for item in self.model.get("geometry", {}).get("regions", []) if item.get("name") == target_name or item.get("id") == target_name]
                if len(matches) == 1:
                    if section == "sources":
                        self.select_source_region(matches[0]["id"])
                    else:
                        self.select_region(matches[0]["id"])
                    return
        self.select_section(section)

    @staticmethod
    def _validation_issue_section(issue):
        text = issue.lower().replace("_", " ")
        if "parameter" in text:
            return "geometry"
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
                ("r_min", "Inner radius", "0", "m"),
                ("z_min", "Bottom", "0", "m"),
                ("width", "Radial width", "0.02", "m"),
                ("height", "Axial height", "0.02", "m"),
            ]
            if rectangle
            else [
                ("r_center", "Centre radius", "0.015", "m"),
                ("z_center", "Centre height", "0.01", "m"),
                ("radius", "Radius", "0.005", "m"),
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

    def _materialize_primitive_as_sketch(self, region):
        """Represent a primitive outline as editable line/arc entities."""
        geometry = self.model["geometry"]
        shape = region.get("shape", {})
        shape_type = shape.get("type") if isinstance(shape, dict) else None
        if shape_type not in {"rectangle", "circle"}:
            raise ValueError(f"Cannot split the outline of {region.get('name', 'this region')} automatically.")
        region_edge_ids = set(region.get("edge_ids", []))
        edge_records = geometry.setdefault("edges", [])
        old_edges = {
            edge.get("id"): edge for edge in edge_records
            if isinstance(edge, dict) and edge.get("id") in region_edge_ids
        }
        entities = geometry.setdefault("sketch_entities", [])
        made = []

        def add_entity(curve_type, start, end, sampled, *, center=None, radius=None, through=None, source=None):
            entity_id = new_id("curve")
            start = [float(value) for value in start]
            end = [float(value) for value in end]
            sampled = [[float(value) for value in point] for point in sampled]
            entity = {
                "id": entity_id,
                "type": curve_type,
                "points": [start, end] if curve_type == "line" else [start, list(through), end],
                "sampled_points": sampled,
            }
            curve = {"type": curve_type, "sampled_points": copy.deepcopy(sampled)}
            if curve_type == "arc":
                entity.update({"center": list(center), "radius": float(radius), "clockwise": False})
                curve.update({"center": list(center), "radius": float(radius), "clockwise": False})
            entities.append(entity)
            made.append(entity_id)
            edge = copy.deepcopy(source) if source else {
                "id": new_id("edge"), "name": f"{curve_type.title()} edge",
                "boundary_condition_ids": [],
            }
            edge["vertices"] = [start, end]
            edge["sketch_entity_id"] = entity_id
            edge["curve"] = curve
            if source:
                for index, old in enumerate(edge_records):
                    if old.get("id") == source.get("id"):
                        edge_records[index] = edge
                        break
                else:
                    edge_records.append(edge)
            else:
                edge_records.append(edge)
            return edge

        if shape_type == "rectangle":
            vertices = region.get("vertices", [])
            if len(vertices) < 4:
                raise ValueError(f"{region.get('name', 'Rectangle')} has an invalid outline.")
            region_edges = [old_edges[item] for item in region.get("edge_ids", []) if item in old_edges]
            for index, side_start in enumerate(vertices):
                side_end = vertices[(index + 1) % len(vertices)]
                side = [edge for edge in region_edges if len(edge.get("vertices", [])) == 2
                        and all(_point_on_segment(point, side_start, side_end, 1e-10)
                                for point in edge["vertices"])]
                if not side:
                    add_entity("line", side_start, side_end, [side_start, side_end])
                    continue
                dx, dy = side_end[0] - side_start[0], side_end[1] - side_start[1]
                denominator = dx * dx + dy * dy
                side.sort(key=lambda edge: sum(
                    ((point[0] - side_start[0]) * dx + (point[1] - side_start[1]) * dy) / max(denominator, 1e-30)
                    for point in edge["vertices"]
                ))
                for edge in side:
                    a, b = edge["vertices"]
                    add_entity("line", a, b, [a, b], source=edge)
        else:
            center = [float(shape["r_center"]), float(shape["z_center"])]
            radius = float(shape["radius"])
            if radius <= 0:
                raise ValueError(f"{region.get('name', 'Circle')} has an invalid radius.")
            source = next((old_edges[item] for item in region.get("edge_ids", [])
                           if item in old_edges), None)
            for quadrant in range(4):
                angle_a = quadrant * math.pi / 2.0
                angle_b = (quadrant + 1) * math.pi / 2.0
                middle = 0.5 * (angle_a + angle_b)
                start = [center[0] + radius * math.cos(angle_a), center[1] + radius * math.sin(angle_a)]
                end = [center[0] + radius * math.cos(angle_b), center[1] + radius * math.sin(angle_b)]
                through = [center[0] + radius * math.cos(middle), center[1] + radius * math.sin(middle)]
                sampled = [
                    [center[0] + radius * math.cos(angle_a + (angle_b - angle_a) * step / 32),
                     center[1] + radius * math.sin(angle_a + (angle_b - angle_a) * step / 32)]
                    for step in range(33)
                ]
                old = copy.deepcopy(source) if source else None
                if old is not None and quadrant > 0:
                    old["id"] = new_id("edge")
                    old["name"] = f"{region.get('name', 'Circle')} edge {quadrant + 1}"
                add_entity("arc", start, end, sampled, center=center, radius=radius,
                           through=through, source=old)
        return made

    @_history_tracked
    def _add_primitive(self, kind):
        try:
            vals = {key: value for (primitive, key), value in self._primitive_values.items() if primitive == kind}
            if not all(math.isfinite(value) for value in vals.values()):
                raise ValueError("Enter a number in each dimension.")
            if kind == "rectangle":
                r0 = vals["r_min"]
                z0 = vals["z_min"]
                width = vals["width"]
                height = vals["height"]
                if r0 < 0 or width <= 0 or height <= 0:
                    raise ValueError("Radius must be non-negative and both dimensions must be positive.")
                vertices = [[r0, z0], [r0 + width, z0], [r0 + width, z0 + height], [r0, z0 + height]]
                primitive_data = {
                    "type": "rectangle", "r_min": r0, "z_min": z0, "width": width, "height": height,
                    "dimension_expressions": {key: str(vals[key]) for key in ("r_min", "z_min", "width", "height")},
                }
                label = "square"
            else:
                radius = vals["radius"]
                rcenter = vals["r_center"]
                zcenter = vals["z_center"]
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
            overlapping = []
            candidate_area = abs(_polygon_area(vertices))
            for existing in self.model["geometry"]["regions"]:
                existing_vertices = existing.get("vertices", [])
                if len(existing_vertices) < 3 or not _polygons_overlap_interior(vertices, existing_vertices):
                    continue
                existing_area = abs(_polygon_area(existing_vertices))
                nested = (
                    existing_area > candidate_area + _area_tolerance(existing_vertices, vertices)
                    and _contains_or_touches(existing_vertices, vertices)
                ) or (
                    candidate_area > existing_area + _area_tolerance(existing_vertices, vertices)
                    and _contains_or_touches(vertices, existing_vertices)
                )
                if not nested:
                    overlapping.append(existing)
            used_indices = []
            for existing in self.model["geometry"]["regions"]:
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
            self._preserve_canvas_view()
            with self._batch_frontend_updates():
                geometry = self.model["geometry"]
                if overlapping:
                    allowed_types = {"rectangle", "circle", "sketch"}
                    unsupported = [item for item in overlapping
                                   if item.get("shape", {}).get("type") not in allowed_types]
                    if unsupported:
                        raise ValueError("This overlap involves imported geometry that cannot yet be split safely.")
                    geometry_snapshot = copy.deepcopy(geometry)
                    templates = [copy.deepcopy(item) for item in overlapping] + [copy.deepcopy(region)]
                    try:
                        for existing in overlapping:
                            if existing.get("shape", {}).get("type") in {"rectangle", "circle"}:
                                self._materialize_primitive_as_sketch(existing)
                        self._materialize_primitive_as_sketch(region)
                        overlapping_ids = {item["id"] for item in overlapping}
                        geometry["regions"] = [item for item in geometry.get("regions", [])
                                                if item.get("id") not in overlapping_ids]
                        self._refresh_sketch_generated_regions(source_templates=templates)
                        if not any(item.get("id") == region["id"] for item in geometry.get("regions", [])):
                            raise ValueError("Overlapping outlines did not produce a bounded region.")
                    except Exception:
                        geometry.clear()
                        geometry.update(geometry_snapshot)
                        raise
                else:
                    geometry["regions"].append(region)
                    try:
                        self._recompute_parent_links()
                    except ValueError:
                        geometry["regions"].pop()
                        raise
                    self._rebuild_edges()
                self.selected_region_id = region["id"]
                self.selected_edge_id = None
                self.selected_edge_ids = []
                self._selected_condition_id = None
                self.active_section = "geometry"
                self.layout["active_section"] = self.active_section
                self._close_dialog(kind)
                if overlapping:
                    self._message(f"Created {region['name']}; intersecting outlines are split into editable subregions.")
                else:
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
        child_area = abs(_polygon_area(vertices))
        for region in self.model["geometry"]["regions"]:
            polygon = region["vertices"]
            outer_area = abs(_polygon_area(polygon))
            if outer_area > child_area + _area_tolerance(polygon, vertices) and _contains_or_touches(polygon, vertices):
                candidates.append((outer_area, region["id"]))
        return min(candidates)[1] if candidates else None

    def _has_unsupported_overlap(self, vertices, parent_id, *, regions=None):
        candidate_area = abs(_polygon_area(vertices))
        for region in (self.model["geometry"]["regions"] if regions is None else regions):
            existing = region["vertices"]
            existing_area = abs(_polygon_area(existing))
            area_tolerance = _area_tolerance(existing, vertices)
            strictly_larger_existing = existing_area > candidate_area + area_tolerance
            strictly_larger_candidate = candidate_area > existing_area + area_tolerance
            if strictly_larger_existing and _contains_or_touches(existing, vertices):
                continue
            if strictly_larger_candidate and _contains_or_touches(vertices, existing):
                continue
            if _polygons_overlap_interior(vertices, existing):
                return True
        return False

    def _recompute_parent_links(self):
        regions = self.model["geometry"]["regions"]
        for index, first in enumerate(regions):
            for second in regions[index + 1:]:
                a, b = first["vertices"], second["vertices"]
                area_a, area_b = abs(_polygon_area(a)), abs(_polygon_area(b))
                area_tolerance = _area_tolerance(a, b)
                nested = (
                    area_a > area_b + area_tolerance and _contains_or_touches(a, b)
                ) or (
                    area_b > area_a + area_tolerance and _contains_or_touches(b, a)
                )
                if nested:
                    continue
                if _polygons_overlap_interior(a, b):
                    raise ValueError(
                        "Regions may touch, but their interiors cannot overlap."
                    )
        parents = {}
        for child in regions:
            child_area = abs(_polygon_area(child["vertices"]))
            containers = [
                (abs(_polygon_area(outer["vertices"])), outer["id"])
                for outer in regions
                if outer["id"] != child["id"]
                and abs(_polygon_area(outer["vertices"]))
                    > child_area + _area_tolerance(outer["vertices"], child["vertices"])
                and _contains_or_touches(outer["vertices"], child["vertices"])
            ]
            parents[child["id"]] = min(containers)[1] if containers else None
        for region in regions:
            region["parent_id"] = parents[region["id"]]

    def _rebuild_edges(self):
        geometry = self.model["geometry"]
        old_edges = geometry.get("edges", [])
        old_points_by_coordinate = {
            tuple(round(float(value), 12) for value in point.get("coordinates", [])): point.get("id")
            for point in geometry.get("points", [])
            if isinstance(point, dict)
            and isinstance(point.get("coordinates"), (list, tuple))
            and len(point.get("coordinates")) == 2
            and isinstance(point.get("id"), str)
        }
        # Entity coordinates are authoritative during edits; retain endpoint
        # and Spline3 control IDs even while the point table is being rebuilt.
        for entity in geometry.get("sketch_entities", []):
            points = entity.get("points", []) if isinstance(entity, dict) else []
            point_ids = entity.get("point_ids", []) if isinstance(entity, dict) else []
            for point_id, point in zip(point_ids, points):
                if isinstance(point_id, str) and isinstance(point, (list, tuple)) and len(point) >= 2:
                    key = tuple(round(float(value), 12) for value in point[:2])
                    old_points_by_coordinate.setdefault(key, point_id)
        geometry.setdefault("voids", [])
        old_edges_by_id = {
            edge.get("id"): edge for edge in old_edges
            if isinstance(edge, dict) and isinstance(edge.get("id"), str)
        }
        selected_before = set(getattr(self, "selected_edge_ids", []))
        old_selected = {
            edge["id"]: edge.get("vertices")
            for edge in old_edges
            if edge.get("id") in selected_before
        }
        all_points = []
        for region in geometry.get("regions", []):
            all_points.extend(
                tuple(float(value) for value in point)
                for point in region.get("vertices", [])
                if isinstance(point, (list, tuple)) and len(point) >= 2
            )
        for void in geometry.get("voids", []):
            all_points.extend(
                tuple(float(value) for value in point)
                for point in void.get("vertices", [])
                if isinstance(point, (list, tuple)) and len(point) >= 2
            )

        edges = []
        edges_by_key = {}
        used_old_ids = set()
        source_part_counts = {}
        collapsed_edge_id_map = {}
        selected_circle_edge_ids = set()
        condition_by_id = {
            item.get("id"): item
            for item in self.model.get("boundary_conditions", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

        def source_edges_for(start, end, key):
            matches = []
            for old in old_edges:
                old_curve = old.get("curve", {})
                if isinstance(old_curve, dict) and old_curve.get("type", "line") != "line":
                    continue
                old_points = old.get("vertices", [])
                if len(old_points) != 2:
                    continue
                if (
                    _point_on_segment(start, old_points[0], old_points[1], 1e-10)
                    and _point_on_segment(end, old_points[0], old_points[1], 1e-10)
                ):
                    matches.append(old)
            matches.sort(key=lambda old: (
                sketch_edge_key("line", old["vertices"]) != key,
                not bool(_edge_condition_ids(old)),
            ))
            return matches

        def merged_condition_ids(candidates):
            by_physics = {}
            other_ids = []
            for old in candidates:
                for condition_id in _edge_condition_ids(old):
                    condition = condition_by_id.get(condition_id, {})
                    kind = str(condition.get("type", ""))
                    physics = (
                        "mechanical"
                        if kind.startswith("mechanical_") or kind == "transmission_interface"
                        else "electromagnetic"
                        if kind in {"axis_of_symmetry", "magnetic_potential_zero", "natural"}
                        else "other"
                    )
                    if physics == "other":
                        other_ids.append(condition_id)
                    else:
                        by_physics[physics] = condition_id
            return list(dict.fromkeys([
                *by_physics.values(),
                *other_ids[-1:],
            ]))

        def sketch_edge_key(curve_type, samples):
            # Endpoint-only keys merge distinct curves that happen to share
            # endpoints (for example, a straight chord and a closing arc).
            # Keep identical shared curve fragments merged, but preserve
            # different curve geometry between the same nodes.
            kind = str(curve_type or "line")
            points = tuple(
                (round(float(point[0]), 12), round(float(point[1]), 12))
                for point in samples
            )
            if kind == "line":
                return kind, tuple(sorted((points[0], points[-1])))
            reverse = tuple(reversed(points))
            return kind, min(points, reverse)

        for region in geometry.get("regions", []):
            region_edge_ids = []
            region_loop_refs = []
            shape = region.get("shape", {})
            if isinstance(shape, dict) and shape.get("type") == "sketch":
                old_edges_by_entity = {}
                for old in old_edges:
                    if isinstance(old, dict) and old.get("sketch_entity_id"):
                        old_edges_by_entity.setdefault(old["sketch_entity_id"], []).append(old)
                entities_by_id = {
                    entity.get("id"): entity
                    for entity in geometry.get("sketch_entities", [])
                    if isinstance(entity, dict) and isinstance(entity.get("id"), str)
                }
                for reference in shape.get("boundary", []):
                    entity_id = reference.get("entity_id")
                    entity = entities_by_id.get(entity_id)
                    if not entity:
                        continue
                    samples = entity.get("sampled_points") or entity.get("points") or []
                    start_t = float(reference.get("start_t", 0.0))
                    end_t = float(reference.get("end_t", 1.0))
                    low_t, high_t = sorted((start_t, end_t))
                    source_candidates = [
                        old for old in old_edges_by_entity.get(entity_id, [])
                        if max(
                            min(float(old.get("sketch_t0", 0.0)), float(old.get("sketch_t1", 1.0))),
                            low_t,
                        ) < min(
                            max(float(old.get("sketch_t0", 0.0)), float(old.get("sketch_t1", 1.0))),
                            high_t,
                        ) + 1e-10
                    ]
                    source_candidates.sort(key=lambda old: (
                        -max(0.0, min(
                            max(float(old.get("sketch_t0", 0.0)), float(old.get("sketch_t1", 1.0))),
                            high_t,
                        ) - max(
                            min(float(old.get("sketch_t0", 0.0)), float(old.get("sketch_t1", 1.0))),
                            low_t,
                        )),
                        not bool(_edge_condition_ids(old)),
                        str(old.get("id", "")),
                    ))
                    source_edge = source_candidates[0] if source_candidates else None
                    if len(samples) < 2:
                        continue
                    path = [tuple(float(value) for value in sample[:2]) for sample in samples]
                    if len(path) < 2:
                        continue
                    def sample_at(parameter):
                        scaled = min(1.0, max(0.0, parameter)) * (len(path) - 1)
                        index = min(len(path) - 2, int(math.floor(scaled)))
                        fraction = scaled - index
                        if parameter >= 1.0:
                            return path[-1]
                        exact_point = evaluate_sketch_entity(entity, parameter)
                        if exact_point is not None:
                            return exact_point
                        return (
                            path[index][0] + fraction * (path[index + 1][0] - path[index][0]),
                            path[index][1] + fraction * (path[index + 1][1] - path[index][1]),
                        )

                    def sample_interval(first_t, last_t):
                        # Store geometry samples in source-parameter order.
                        # Boundary traversal direction stays in the loop ref.
                        low, high = sorted((first_t, last_t))
                        count = max(2, int(math.ceil(max(1, len(path) - 1) * (high - low))) + 1)
                        return sample_sketch_entity(entity, count=count, start=low, end=high)

                    interval_samples = sample_interval(start_t, end_t)
                    edge_start, edge_end = interval_samples[0], interval_samples[-1]
                    key = sketch_edge_key(entity.get("type"), interval_samples)
                    forward = bool(reference.get("forward", end_t >= start_t))
                    if key in edges_by_key:
                        edge = edges_by_key[key]
                        forward = tuple(round(float(value), 12) for value in edge["vertices"][0]) == tuple(round(float(value), 12) for value in (edge_start if forward else edge_end))
                    else:
                        edge = copy.deepcopy(source_edge) if source_edge else {}
                        old_id = source_edge.get("id") if source_edge else None
                        if old_id and old_id not in used_old_ids:
                            edge["id"] = old_id
                            used_old_ids.add(old_id)
                        else:
                            edge["id"] = new_id("edge")
                        edge.setdefault("name", f"Edge {len(edges) + 1}")
                        edge.setdefault("boundary_condition_ids", [])
                        edge["vertices"] = [edge_start, edge_end]
                        edge["sketch_entity_id"] = entity_id
                        curve = {"type": str(entity.get("type", "line"))}
                        for curve_key in ("control_point", "control_point_id", "center", "radius", "clockwise", "weight"):
                            if curve_key in entity:
                                curve[curve_key] = copy.deepcopy(entity[curve_key])
                        source_curve = source_edge.get("curve", {}) if isinstance(source_edge, dict) else {}
                        if curve.get("type") == "spline3" and "control_point" not in curve and isinstance(source_curve, dict):
                            for curve_key in ("control_point", "control_point_id"):
                                if curve_key in source_curve:
                                    curve[curve_key] = copy.deepcopy(source_curve[curve_key])
                        curve["sampled_points"] = copy.deepcopy(interval_samples)
                        edge["curve"] = curve
                        edge["sketch_t0"] = low_t
                        edge["sketch_t1"] = high_t
                        edges_by_key[key] = edge
                        edges.append(edge)
                    region_edge_ids.append(edge["id"])
                    region_loop_refs.append({"edge_id": edge["id"], "forward": forward})
                region["edge_ids"] = list(dict.fromkeys(region_edge_ids))
                region["boundary_loops"] = [{"role": "outer", "edges": region_loop_refs}] if region_loop_refs else []
                continue
            if isinstance(shape, dict) and shape.get("type") == "circle":
                center = [float(shape["r_center"]), float(shape["z_center"])]
                radius = float(shape["radius"])
                circle_samples = [
                    [float(point[0]), float(point[1])]
                    for point in region.get("vertices", [])
                    if isinstance(point, (list, tuple)) and len(point) >= 2
                ]
                if len(circle_samples) < 3 or radius <= 0:
                    continue
                if circle_samples[0] != circle_samples[-1]:
                    circle_samples.append(list(circle_samples[0]))
                else:
                    circle_samples[-1] = list(circle_samples[0])
                circle_key = (
                    "circle", round(center[0], 12), round(center[1], 12), round(radius, 12)
                )
                source_ids = set(region.get("edge_ids", []))
                candidates = [
                    old for old in old_edges
                    if isinstance(old, dict) and old.get("id") in source_ids
                ]
                if not candidates:
                    candidates = [
                        old for old in old_edges
                        if isinstance(old, dict)
                        and isinstance(old.get("curve"), dict)
                        and old["curve"].get("type") in {"arc", "circle"}
                        and old["curve"].get("center") == center
                        and math.isclose(float(old["curve"].get("radius", -1)), radius, rel_tol=1e-10, abs_tol=1e-12)
                    ]
                candidates.sort(key=lambda old: (
                    old.get("id") not in selected_before,
                    not bool(_edge_condition_ids(old)),
                    str(old.get("id", "")),
                ))
                source_edge = candidates[0] if candidates else None
                edge = edges_by_key.get(circle_key)
                if edge is None:
                    edge = copy.deepcopy(source_edge) if source_edge else {}
                    old_id = source_edge.get("id") if source_edge else None
                    if old_id and old_id not in used_old_ids:
                        edge_id = old_id
                        used_old_ids.add(old_id)
                    else:
                        edge_id = new_id("edge")
                    edge["id"] = edge_id
                    edge["name"] = f"{region.get('name', 'Circle')} boundary"
                    edge["vertices"] = [list(circle_samples[0]), list(circle_samples[0])]
                    edge["curve"] = {
                        "type": "circle",
                        "center": center,
                        "radius": radius,
                        "sampled_points": circle_samples,
                    }
                    edge["boundary_condition_ids"] = merged_condition_ids(candidates)
                    edge.pop("boundary_condition_id", None)
                    edge.pop("sketch_entity_id", None)
                    edge.pop("sketch_t0", None)
                    edge.pop("sketch_t1", None)
                    edges_by_key[circle_key] = edge
                    edges.append(edge)
                else:
                    edge["boundary_condition_ids"] = merged_condition_ids([edge, *candidates])
                for old in candidates:
                    old_id = old.get("id")
                    if isinstance(old_id, str) and old_id != edge["id"]:
                        collapsed_edge_id_map[old_id] = edge["id"]
                if any(old.get("id") in selected_before for old in candidates):
                    selected_circle_edge_ids.add(edge["id"])
                region_edge_ids.append(edge["id"])
                region_loop_refs.append({"edge_id": edge["id"], "forward": True})
                region["edge_ids"] = list(dict.fromkeys(region_edge_ids))
                region["boundary_loops"] = [{"role": "outer", "edges": region_loop_refs}]
                continue
            points = region.get("vertices", [])
            for index, raw_start in enumerate(points):
                raw_end = points[(index + 1) % len(points)]
                start = tuple(float(value) for value in raw_start)
                end = tuple(float(value) for value in raw_end)
                dx, dy = end[0] - start[0], end[1] - start[1]
                length_squared = dx * dx + dy * dy
                if length_squared <= 1e-30:
                    continue

                split_points = [(0.0, start), (1.0, end)]
                min_x, max_x = sorted((start[0], end[0]))
                min_y, max_y = sorted((start[1], end[1]))
                for point in all_points:
                    if (
                        point[0] < min_x - 1e-10 or point[0] > max_x + 1e-10
                        or point[1] < min_y - 1e-10 or point[1] > max_y + 1e-10
                        or not _point_on_segment(point, start, end, 1e-10)
                    ):
                        continue
                    t = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared
                    if -1e-10 <= t <= 1.0 + 1e-10:
                        split_points.append((min(1.0, max(0.0, t)), point))
                split_points.sort(key=lambda item: item[0])
                ordered = []
                seen_points = set()
                for _t, point in split_points:
                    point_key = tuple(round(value, 12) for value in point)
                    if point_key not in seen_points:
                        seen_points.add(point_key)
                        ordered.append(point)

                for first, second in zip(ordered, ordered[1:]):
                    key = sketch_edge_key("line", [first, second])
                    if key in edges_by_key:
                        edge = edges_by_key[key]
                        region_edge_ids.append(edge["id"])
                        forward = tuple(round(float(value), 12) for value in edge["vertices"][0]) == tuple(round(float(value), 12) for value in first)
                        region_loop_refs.append({"edge_id": edge["id"], "forward": forward})
                        continue

                    candidates = source_edges_for(first, second, key)
                    source_edge = candidates[0] if candidates else None
                    edge = copy.deepcopy(source_edge) if source_edge else {}
                    old_id = source_edge.get("id") if source_edge else None
                    if old_id and old_id not in used_old_ids:
                        edge_id = old_id
                        used_old_ids.add(old_id)
                    else:
                        edge_id = new_id("edge")
                    edge["id"] = edge_id
                    edge["vertices"] = [list(first), list(second)]
                    source_curve = source_edge.get("curve", {}) if source_edge else {}
                    source_samples = (
                        source_curve.get("sampled_points")
                        if isinstance(source_curve, dict) else None
                    ) or (source_edge.get("vertices", []) if source_edge else [])
                    source_curve_key = (
                        sketch_edge_key(source_curve.get("type", "line"), source_samples)
                        if source_edge else None
                    )
                    if source_edge and source_curve_key == key:
                        edge["curve"] = copy.deepcopy(source_edge["curve"])
                    else:
                        shape = region.get("shape", {})
                        if isinstance(shape, dict) and shape.get("type") == "circle":
                            center = [float(shape["r_center"]), float(shape["z_center"])]
                            cross = (
                                (float(first[0]) - center[0]) * (float(second[1]) - center[1])
                                - (float(first[1]) - center[1]) * (float(second[0]) - center[0])
                            )
                            edge["curve"] = {
                                "type": "arc",
                                "center": center,
                                "radius": float(shape["radius"]),
                                "clockwise": cross < 0.0,
                            }
                        else:
                            edge["curve"] = {"type": "line"}
                    if source_edge is None:
                        edge["name"] = f"Edge {len(edges) + 1}"
                        edge["boundary_condition_ids"] = []
                    else:
                        source_key = sketch_edge_key("line", source_edge["vertices"])
                        if source_key != key:
                            part = source_part_counts.get(old_id, 0) + 1
                            source_part_counts[old_id] = part
                            edge["name"] = f"{source_edge.get('name', 'Edge')} (part {part})"
                        edge["boundary_condition_ids"] = merged_condition_ids(candidates)
                        edge.pop("boundary_condition_id", None)
                    edges_by_key[key] = edge
                    edges.append(edge)
                    region_edge_ids.append(edge_id)
                    region_loop_refs.append({"edge_id": edge_id, "forward": True})
            region["edge_ids"] = list(dict.fromkeys(region_edge_ids))
            region["boundary_loops"] = [{"role": "outer", "edges": region_loop_refs}] if region_loop_refs else []

        regions_by_id = {
            region.get("id"): region for region in geometry.get("regions", [])
            if isinstance(region, dict) and isinstance(region.get("id"), str)
        }
        for child in geometry.get("regions", []):
            if not isinstance(child, dict):
                continue
            parent = regions_by_id.get(child.get("parent_id"))
            if not isinstance(parent, dict):
                continue
            outer = next((loop for loop in child.get("boundary_loops", []) if loop.get("role") == "outer"), None)
            if not isinstance(outer, dict):
                continue
            parent_loops = parent.setdefault("boundary_loops", [])
            parent_loops.append({
                "role": "hole",
                "region_id": child.get("id"),
                "edges": [
                    {"edge_id": item["edge_id"], "forward": not bool(item.get("forward", True))}
                    for item in reversed(outer.get("edges", []))
                ],
            })

        current_edge_ids = {edge.get("id") for edge in edges}
        for void in geometry.get("voids", []):
            if not isinstance(void, dict):
                continue
            parent = regions_by_id.get(void.get("parent_id"))
            if not isinstance(parent, dict):
                continue
            outer = next(
                (loop for loop in void.get("boundary_loops", []) if loop.get("role") == "outer"),
                None,
            )
            if not isinstance(outer, dict):
                continue
            outer_refs = []
            for reference in outer.get("edges", []):
                if not isinstance(reference, dict):
                    continue
                edge_id = reference.get("edge_id")
                source = old_edges_by_id.get(edge_id)
                if source is None:
                    continue
                if edge_id not in current_edge_ids:
                    edge = copy.deepcopy(source)
                    edges.append(edge)
                    current_edge_ids.add(edge_id)
                    edge_key = (
                        "circle",
                        *[round(float(value), 12) for value in edge.get("curve", {}).get("center", [])],
                        round(float(edge.get("curve", {}).get("radius")), 12),
                    ) if isinstance(edge.get("curve"), dict) and edge["curve"].get("type") == "circle" else sketch_edge_key(
                        edge.get("curve", {}).get("type", "line"),
                        edge.get("curve", {}).get("sampled_points") or edge.get("vertices", []),
                    )
                    edges_by_key.setdefault(edge_key, edge)
                outer_refs.append({
                    "edge_id": edge_id,
                    "forward": bool(reference.get("forward", True)),
                })
            if outer_refs:
                parent.setdefault("boundary_loops", []).append({
                    "role": "hole",
                    "void_id": void.get("id"),
                    "edges": [
                        {"edge_id": item["edge_id"], "forward": not item["forward"]}
                        for item in reversed(outer_refs)
                    ],
                })

        # Region boundaries only consume the portions of sketch curves that
        # form closed faces. Keep the remaining portions of intersecting line
        # entities as individual edges so open branches remain visible and
        # available to select/trim after a nearby face is created.
        sketch_entities = [
            entity for entity in geometry.get("sketch_entities", [])
            if isinstance(entity, dict) and isinstance(entity.get("id"), str)
        ]
        line_entities = [
            entity for entity in sketch_entities
            if entity.get("type") == "line"
            and len(entity.get("sampled_points") or entity.get("points") or []) == 2
        ]
        split_parameters = {entity["id"]: {0.0, 1.0} for entity in line_entities}

        def vector_cross(first, second):
            return first[0] * second[1] - first[1] * second[0]

        for entity in line_entities:
            entity_id = entity["id"]
            try:
                first, second = [
                    tuple(float(value) for value in point[:2])
                    for point in (entity.get("sampled_points") or entity.get("points"))
                ]
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
            dx, dy = second[0] - first[0], second[1] - first[1]
            length_squared = dx * dx + dy * dy
            length = math.sqrt(length_squared)
            if length_squared <= 1e-30:
                continue
            for other in sketch_entities:
                if other.get("id") == entity_id:
                    continue
                if other.get("type") == "arc":
                    try:
                        arc_points = other.get("points") or []
                        center = tuple(float(value) for value in other.get("center", []))
                        radius = float(other.get("radius"))
                        arc_start = tuple(float(value) for value in arc_points[0][:2])
                        arc_through = tuple(float(value) for value in arc_points[1][:2])
                        arc_end = tuple(float(value) for value in arc_points[-1][:2])
                    except (TypeError, ValueError, IndexError, OverflowError):
                        center = ()
                    if len(center) == 2 and math.isfinite(radius) and radius > 0:
                        quadratic_a = length_squared
                        fx, fy = first[0] - center[0], first[1] - center[1]
                        quadratic_b = 2.0 * (fx * dx + fy * dy)
                        quadratic_c = fx * fx + fy * fy - radius * radius
                        discriminant = quadratic_b * quadratic_b - 4.0 * quadratic_a * quadratic_c
                        discriminant_tolerance = max(
                            1e-30,
                            1e-12 * (quadratic_b * quadratic_b + abs(4.0 * quadratic_a * quadratic_c)),
                        )
                        if discriminant >= -discriminant_tolerance:
                            root = math.sqrt(max(0.0, discriminant))
                            clockwise = bool(other.get("clockwise", False))
                            start_angle = math.atan2(arc_start[1] - center[1], arc_start[0] - center[0])
                            through_angle = math.atan2(arc_through[1] - center[1], arc_through[0] - center[0])
                            end_angle = math.atan2(arc_end[1] - center[1], arc_end[0] - center[0])
                            tau = 2.0 * math.pi

                            def directed_offset(first_angle, second_angle, is_clockwise):
                                return (
                                    (first_angle - second_angle) % tau
                                    if is_clockwise else (second_angle - first_angle) % tau
                                )

                            sweep = directed_offset(start_angle, end_angle, clockwise)
                            through_offset = directed_offset(start_angle, through_angle, clockwise)
                            if through_offset > sweep + 1e-10:
                                clockwise = not clockwise
                                sweep = directed_offset(start_angle, end_angle, clockwise)
                            line_tolerance = max(1e-10, 1e-12 / max(length, 1e-12))
                            for t in sorted({
                                (-quadratic_b - root) / (2.0 * quadratic_a),
                                (-quadratic_b + root) / (2.0 * quadratic_a),
                            }):
                                if not -line_tolerance <= t <= 1.0 + line_tolerance:
                                    continue
                                t = min(1.0, max(0.0, t))
                                hit = (first[0] + t * dx, first[1] + t * dy)
                                hit_angle = math.atan2(hit[1] - center[1], hit[0] - center[0])
                                offset = directed_offset(start_angle, hit_angle, clockwise)
                                if offset <= sweep + 1e-10:
                                    split_parameters[entity_id].add(round(t, 12))
                        continue
                samples = other.get("sampled_points") or other.get("points") or []
                try:
                    path = [tuple(float(value) for value in point[:2]) for point in samples]
                except (TypeError, ValueError, IndexError, OverflowError):
                    continue
                for start, end in zip(path, path[1:]):
                    sx, sy = end[0] - start[0], end[1] - start[1]
                    other_length = math.hypot(sx, sy)
                    if other_length <= 1e-15:
                        continue
                    denominator = vector_cross((dx, dy), (sx, sy))
                    qx, qy = start[0] - first[0], start[1] - first[1]
                    area_tolerance = max(1e-12 * max(length, other_length), 1e-24)
                    parameter_tolerance = max(
                        1e-10,
                        1e-12 / max(length, 1e-12),
                        1e-12 / max(other_length, 1e-12),
                    )
                    if abs(denominator) > area_tolerance:
                        t = vector_cross((qx, qy), (sx, sy)) / denominator
                        u = vector_cross((qx, qy), (dx, dy)) / denominator
                        if (
                            -parameter_tolerance <= t <= 1.0 + parameter_tolerance
                            and -parameter_tolerance <= u <= 1.0 + parameter_tolerance
                        ):
                            split_parameters[entity_id].add(round(min(1.0, max(0.0, t)), 12))
                        continue

                    if abs(vector_cross((qx, qy), (dx, dy))) > area_tolerance:
                        continue
                    for point in (start, end):
                        if not _point_on_segment(point, first, second, 1e-10):
                            continue
                        t = ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / length_squared
                        split_parameters[entity_id].add(round(min(1.0, max(0.0, t)), 12))

        for entity in line_entities:
            entity_id = entity["id"]
            try:
                first, second = [
                    tuple(float(value) for value in point[:2])
                    for point in (entity.get("sampled_points") or entity.get("points"))
                ]
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
            dx, dy = second[0] - first[0], second[1] - first[1]
            parameters = sorted(split_parameters[entity_id])
            for start_t, end_t in zip(parameters, parameters[1:]):
                if end_t - start_t <= 1e-12:
                    continue
                start = (first[0] + start_t * dx, first[1] + start_t * dy)
                end = (first[0] + end_t * dx, first[1] + end_t * dy)
                key = sketch_edge_key("line", [start, end])
                if key in edges_by_key:
                    continue
                candidates = source_edges_for(start, end, key)
                source_edge = next(
                    (old for old in candidates if old.get("sketch_entity_id") == entity_id),
                    candidates[0] if candidates else None,
                )
                edge = copy.deepcopy(source_edge) if source_edge else {}
                old_id = source_edge.get("id") if source_edge else None
                if old_id and old_id not in used_old_ids:
                    edge_id = old_id
                    used_old_ids.add(old_id)
                else:
                    edge_id = new_id("edge")
                edge["id"] = edge_id
                edge["vertices"] = [list(start), list(end)]
                edge["sketch_entity_id"] = entity_id
                edge["sketch_t0"] = start_t
                edge["sketch_t1"] = end_t
                edge["curve"] = {"type": "line"}
                if source_edge is None:
                    edge["name"] = f"Edge {len(edges) + 1}"
                    edge["boundary_condition_ids"] = []
                else:
                    edge["boundary_condition_ids"] = merged_condition_ids(candidates)
                    if start_t > 1e-12 or end_t < 1.0 - 1e-12:
                        edge["name"] = f"{source_edge.get('name', 'Edge')} (part)"
                    edge.pop("boundary_condition_id", None)
                edges_by_key[key] = edge
                edges.append(edge)
                current_edge_ids.add(edge_id)

        # Edges on generated faces cover only the curve intervals that bound
        # those faces. Preserve the remaining authored arc/spline intervals as
        # open sketch edges, so they remain visible and trim-able. When a loaded
        # project already has edge records, those records define its visible
        # source ranges and prevent hidden portions from being resurrected.
        boundary_intervals_by_entity = {}
        for region in geometry.get("regions", []):
            shape = region.get("shape", {})
            if not isinstance(shape, dict) or shape.get("type") != "sketch":
                continue
            for reference in shape.get("boundary", []):
                if isinstance(reference, dict) and isinstance(reference.get("entity_id"), str):
                    boundary_intervals_by_entity.setdefault(reference["entity_id"], []).append((
                        reference.get("start_t", 0.0), reference.get("end_t", 1.0)
                    ))

        for entity in geometry.get("sketch_entities", []):
            if not isinstance(entity, dict) or entity.get("type") == "line":
                continue
            entity_id = entity.get("id")
            source_samples = entity.get("sampled_points") or entity.get("points") or []
            if not isinstance(entity_id, str) or len(source_samples) < 2:
                continue
            active_ranges = self._sketch_entity_active_intervals(entity, geometry)
            covered_ranges = self._merge_sketch_parameter_intervals(
                boundary_intervals_by_entity.get(entity_id, [])
            )
            for active_start, active_end in active_ranges:
                cuts = {active_start, active_end}
                for cover_start, cover_end in covered_ranges:
                    if active_start < cover_start < active_end:
                        cuts.add(cover_start)
                    if active_start < cover_end < active_end:
                        cuts.add(cover_end)
                ordered = sorted(cuts)
                for start_t, end_t in zip(ordered, ordered[1:]):
                    if end_t - start_t <= 1e-10:
                        continue
                    midpoint = 0.5 * (start_t + end_t)
                    if any(lo - 1e-10 <= midpoint <= hi + 1e-10 for lo, hi in covered_ranges):
                        continue
                    parameters = [start_t]
                    parameters.extend(
                        index / (len(source_samples) - 1)
                        for index in range(1, len(source_samples) - 1)
                        if start_t < index / (len(source_samples) - 1) < end_t
                    )
                    parameters.append(end_t)
                    path = []
                    for parameter in parameters:
                        point = evaluate_sketch_entity(entity, parameter)
                        if point is None:
                            scaled = parameter * (len(source_samples) - 1)
                            index = min(len(source_samples) - 2, max(0, int(math.floor(scaled))))
                            fraction = scaled - index
                            first, second = source_samples[index], source_samples[index + 1]
                            point = tuple(
                                float(first[axis]) + fraction * (float(second[axis]) - float(first[axis]))
                                for axis in range(2)
                            )
                        path.append([float(point[0]), float(point[1])])
                    if len(path) < 2 or math.dist(path[0], path[-1]) <= 1e-12:
                        continue
                    authored_points = entity.get("points") or []
                    if start_t <= 1e-12 and len(authored_points) >= 2:
                        path[0] = [float(value) for value in authored_points[0][:2]]
                    if end_t >= 1.0 - 1e-12 and len(authored_points) >= 2:
                        path[-1] = [float(value) for value in authored_points[-1][:2]]
                    if any(
                        edge.get("sketch_entity_id") == entity_id
                        and min(float(edge.get("sketch_t0", 0.0)), float(edge.get("sketch_t1", 1.0))) <= start_t + 1e-9
                        and max(float(edge.get("sketch_t0", 0.0)), float(edge.get("sketch_t1", 1.0))) >= end_t - 1e-9
                        for edge in edges
                    ):
                        continue
                    curve_type = str(entity.get("type", "spline"))
                    key = sketch_edge_key(curve_type, path)
                    if key in edges_by_key:
                        continue
                    source_edge = next((
                        old for old in old_edges
                        if old.get("sketch_entity_id") == entity_id
                        and len(old.get("vertices", [])) == 2
                        and max(min(float(old.get("sketch_t0", 0.0)), float(old.get("sketch_t1", 1.0))), start_t)
                            < min(max(float(old.get("sketch_t0", 0.0)), float(old.get("sketch_t1", 1.0))), end_t) + 1e-10
                    ), None)
                    edge = copy.deepcopy(source_edge) if source_edge else {}
                    old_id = source_edge.get("id") if source_edge else None
                    if old_id and old_id not in used_old_ids:
                        edge_id = old_id
                        used_old_ids.add(old_id)
                    else:
                        edge_id = new_id("edge")
                    edge["id"] = edge_id
                    edge.setdefault("name", entity.get("name", f"Edge {len(edges) + 1}"))
                    edge.setdefault("boundary_condition_ids", _edge_condition_ids(source_edge) if source_edge else [])
                    edge["vertices"] = [path[0], path[-1]]
                    edge["sketch_entity_id"] = entity_id
                    edge["sketch_t0"], edge["sketch_t1"] = start_t, end_t
                    edge["curve"] = copy.deepcopy(entity)
                    edge["curve"]["sampled_points"] = copy.deepcopy(path)
                    edges_by_key[key] = edge
                    edges.append(edge)
                    current_edge_ids.add(edge_id)

        live_sketch_entity_ids = {
            entity.get("id") for entity in geometry.get("sketch_entities", [])
            if isinstance(entity, dict) and isinstance(entity.get("id"), str)
        }
        for old in old_edges:
            if (
                not isinstance(old, dict)
                or not old.get("sketch_entity_id")
                or old.get("sketch_entity_id") not in live_sketch_entity_ids
                or old.get("id") in used_old_ids
                or old.get("id") in current_edge_ids
                or len(old.get("vertices", [])) != 2
            ):
                continue
            old_curve = old.get("curve", {})
            old_samples = (
                old_curve.get("sampled_points")
                if isinstance(old_curve, dict) else None
            ) or old["vertices"]
            key = sketch_edge_key(
                old_curve.get("type", "line") if isinstance(old_curve, dict) else "line",
                old_samples,
            )
            if key in edges_by_key:
                continue
            edge = copy.deepcopy(old)
            edges_by_key[key] = edge
            edges.append(edge)
            current_edge_ids.add(edge.get("id"))

        points_by_coordinate = dict(old_points_by_coordinate)
        topology_points_by_id = {}
        for edge in edges:
            point_ids = []
            for point in edge.get("vertices", []):
                key = tuple(round(float(value), 12) for value in point)
                point_id = points_by_coordinate.get(key)
                if not point_id:
                    point_id = new_id("point")
                    points_by_coordinate[key] = point_id
                topology_points_by_id.setdefault(
                    point_id, {"id": point_id, "coordinates": list(key)}
                )
                point_ids.append(point_id)
            edge["point_ids"] = point_ids
        for entity in geometry.get("sketch_entities", []):
            entity_points = entity.get("points", []) if isinstance(entity, dict) else []
            entity_point_ids = entity.get("point_ids", []) if isinstance(entity, dict) else []
            for point_id, point in zip(entity_point_ids, entity_points):
                if not isinstance(point_id, str) or not isinstance(point, (list, tuple)) or len(point) < 2:
                    continue
                coordinates = [float(point[0]), float(point[1])]
                topology_points_by_id[point_id] = {"id": point_id, "coordinates": coordinates}
                points_by_coordinate.setdefault(
                    tuple(round(value, 12) for value in coordinates), point_id
                )
        geometry["points"] = list(topology_points_by_id.values())
        geometry["topology_version"] = 1
        geometry["edges"] = edges

        # Keep a selected old edge selected across each of its split segments.
        if hasattr(self, "selected_edge_ids"):
            selected_ids = set(selected_circle_edge_ids)
            for edge in edges:
                if edge["id"] in old_selected:
                    selected_ids.add(edge["id"])
                    continue
                for old_vertices in old_selected.values():
                    if (
                        _point_on_segment(edge["vertices"][0], old_vertices[0], old_vertices[1], 1e-10)
                        and _point_on_segment(edge["vertices"][1], old_vertices[0], old_vertices[1], 1e-10)
                    ):
                        selected_ids.add(edge["id"])
                        break
            self.selected_edge_ids = [
                edge_id for edge_id in self.selected_edge_ids if edge_id in selected_ids
            ] + [
                edge["id"] for edge in edges
                if edge["id"] in selected_ids and edge["id"] not in self.selected_edge_ids
            ]
            if self.selected_edge_id not in selected_ids:
                self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None

        # Remove sizing and refinement assignments whose geometry was deleted.
        mesh = self.model.setdefault("mesh", {})
        if collapsed_edge_id_map:
            mesh["hp_edge_ids"] = list(dict.fromkeys(
                collapsed_edge_id_map.get(edge_id, edge_id)
                for edge_id in mesh.get("hp_edge_ids", [])
            ))
        live_region_ids = {region["id"] for region in geometry.get("regions", [])}
        live_edge_ids = {edge["id"] for edge in edges}
        region_sizes = mesh.get("region_element_sizes", {})
        if isinstance(region_sizes, dict):
            mesh["region_element_sizes"] = {
                region_id: value for region_id, value in region_sizes.items()
                if region_id in live_region_ids
            }
        mesh["hp_region_ids"] = [
            region_id for region_id in mesh.get("hp_region_ids", [])
            if region_id in live_region_ids
        ]
        mesh["hp_edge_ids"] = [
            edge_id for edge_id in mesh.get("hp_edge_ids", [])
            if edge_id in live_edge_ids
        ]
        self._rebuild_imported_parent_loops()
        _sync_axis_boundary_conditions(self.model)
        _sync_mechanical_boundary_conditions(self.model)
        if hasattr(self, "selected_edge_ids"):
            live_ids = {edge["id"] for edge in edges}
            self.selected_edge_ids = [
                edge_id for edge_id in self.selected_edge_ids if edge_id in live_ids
            ]
            if self.selected_edge_id not in live_ids:
                self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None

    def _refresh_model_tree(self):
        labels = {key: label for key, label, _ in _SECTIONS}
        for key, button in self._section_buttons.items():
            button.ui_label = labels[key]
            button.ui_color = "primary" if key == self.active_section else None
            button.ui_flat = key != self.active_section
        if not self._tree_structure_initialized:
            tree_groups = []
            for group_label, section_keys in _TREE_GROUPS:
                rows = []
                for key in section_keys:
                    title = Div(
                        "",
                        ui_style="font-size:10px; font-weight:700; letter-spacing:.06em; color:var(--fg-muted); padding:7px 8px 3px 10px;",
                    )
                    entries = Div(
                        ui_style="display:flex; flex-direction:column; gap:1px;",
                    )
                    self._tree_entry_titles[key] = title
                    self._tree_entry_lists[key] = entries
                    self._tree_entry_order[key] = ()
                    subsection = Div(
                        title,
                        entries,
                        ui_style="display:flex; flex-direction:column; gap:1px; margin:0 0 4px 12px; border-left:1px solid var(--border); padding-left:4px;",
                    )
                    self._tree_entry_containers[key] = subsection
                    rows.append(Div(
                        self._section_buttons[key],
                        subsection,
                        ui_style="display:flex; flex-direction:column; gap:1px;",
                    ))
                tree_groups.append(Div(
                    Div(group_label, ui_style="font-size:10px; font-weight:700; letter-spacing:.09em; color:var(--fg-muted); padding:7px 8px 5px;"),
                    *rows,
                    ui_style="display:flex; flex-direction:column; gap:2px;",
                ))
            self._left_items.ui_children = tree_groups
            self._tree_structure_initialized = True

        for key in self._section_buttons:
            subsection = self._tree_subsection(key)
            if not subsection:
                continue
            title, entries = subsection
            title_component = self._tree_entry_titles[key]
            if title_component.ui_children != [title]:
                title_component.ui_children = [title]

            old_order = self._tree_entry_order[key]
            entry_ids = tuple(entry_id for entry_id, *_ in entries)
            for entry_id in old_order:
                if entry_id not in entry_ids:
                    self._tree_entry_buttons.pop(f"{key}:{entry_id}", None)

            entry_buttons = []
            for entry_id, label, icon, callback, selected in entries:
                identity = f"{key}:{entry_id}"
                button = self._tree_entry_buttons.get(identity)
                if button is None:
                    button = self._make_tree_entry_button(
                        key, entry_id, label, icon, callback, selected
                    )
                else:
                    tree_label = getattr(button, "_mmfem_tree_label", None)
                    tree_icon = getattr(button, "_mmfem_tree_icon", None)
                    if tree_label is not None:
                        tree_label.ui_children = [label]
                    if tree_icon is not None:
                        tree_icon.ui_name = icon
                    button.ui_color = "primary" if selected else None
                    button.ui_flat = not selected
                entry_buttons.append(button)
            if entry_ids != old_order:
                self._tree_entry_lists[key].ui_children = entry_buttons
                self._tree_entry_order[key] = entry_ids

        self._update_model_tree_selection()
        if hasattr(self, "_bottom_count"):
            count_text = f"Regions: {len(self.model['geometry']['regions'])}  ·  Mesh order: {self.model['mesh']['polynomial_order']}"
            if self._bottom_count.ui_children != [count_text]:
                self._bottom_count.ui_children = [count_text]
        if hasattr(self, "_model_title") and not self._model_title_editing:
            self._model_title.ui_model_value = self.model.get("name", "Untitled axisymmetric model")

    def _make_tree_entry_button(self, section, entry_id, label, icon, callback, selected):
        icon_widget = QIcon(ui_name=icon, ui_style="align-self:center; justify-self:center;")
        label_widget = Div(
            label,
            ui_style=(
                "min-width:0; white-space:normal; overflow-wrap:anywhere; "
                "word-break:normal; line-height:1.25; text-align:left;"
            ),
        )
        content = Div(
            icon_widget,
            label_widget,
            ui_style=(
                "display:grid; grid-template-columns:20px minmax(0,1fr); "
                "align-items:center; gap:7px; width:100%; min-width:0;"
            ),
        )
        button = QBtn(
            QTooltip(label),
            content,
            ui_color="primary" if selected else None,
            ui_flat=not selected,
            ui_dense=True,
            ui_no_caps=True,
            ui_class="mmfem-tree-entry",
            ui_style=(
                "width:100%; min-width:0; min-height:30px; height:auto; "
                "justify-content:flex-start; text-align:left; padding:5px 8px;"
            ),
        )
        button._mmfem_tree_label = label_widget
        button._mmfem_tree_icon = icon_widget
        button._props["aria-label"] = label
        button.on_click(callback)
        if section == "runs":
            delete_item = QItem(
                QItemSection(QIcon(ui_name="mdi-delete", ui_size="xs"), ui_avatar=True),
                QItemSection("Delete"),
                ui_clickable=True,
                ui_dense=True,
            )
            delete_item.on_click(
                lambda e=None, run_id=entry_id: self.delete_run_history_entry(run_id)
            )
            button.ui_children = [*button.ui_children, QMenu(QList(delete_item, ui_dense=True), ui_context_menu=True)]
        self._tree_entry_buttons[f"{section}:{entry_id}"] = button
        return button

    def _assigned_regions_for_load(self, load_id):
        load = next((item for item in self.model.get("regional_loads", []) if item.get("id") == load_id), None)
        if load is None:
            return []
        valid_ids = {region.get("id") for region in self.model.get("geometry", {}).get("regions", [])}
        return [region_id for region_id in load.get("region_ids", []) if region_id in valid_ids]

    def _sync_region_sources_from_loads(self):
        """Materialize named regional loads into the legacy solver input fields."""
        loads = self.model.setdefault("regional_loads", [])
        for region in self.model.get("geometry", {}).get("regions", []):
            region_id = region.get("id")
            assigned = [
                item for item in loads
                if region_id in item.get("region_ids", [])
            ]
            source = {}
            for key in ("dc_current_density", "ac_current_density_real", "ac_current_density_imaginary"):
                values = [str(item.get(key, "0") or "0").strip() or "0" for item in assigned]
                values = [value for value in values if value not in {"0", "0.0", "0.00", "0e0", "0.0e0"}]
                source[key] = "0" if not values else values[0] if len(values) == 1 else " + ".join(f"({value})" for value in values)
            force = {}
            for component in ("r", "z"):
                values = [
                    str((item.get("mechanical_body_force") or {}).get(component, "0") or "0").strip() or "0"
                    for item in assigned
                ]
                values = [value for value in values if value not in {"0", "0.0", "0.00", "0e0", "0.0e0"}]
                force[component] = "0" if not values else values[0] if len(values) == 1 else " + ".join(f"({value})" for value in values)
            source["mechanical_body_force"] = force
            region["sources"] = source

    def select_regional_load(self, load_id):
        if not any(item.get("id") == load_id for item in self.model.get("regional_loads", [])):
            return
        self.selected_source_id = load_id
        self.set_sketch_tool("select", announce=False)
        assigned = self._assigned_regions_for_load(load_id)
        self.selected_region_ids = list(assigned)
        self.selected_region_id = assigned[-1] if assigned else None
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self._selected_condition_id = None
        self.active_section = "sources"
        self.layout["active_section"] = "sources"
        self._update_model_tree_selection()
        self._render_inspector()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()

    @_history_tracked
    def _toggle_active_source_region(self, region_id):
        if self.active_section != "sources" or not self.selected_source_id:
            self._message("Select a saved source/load before assigning it to regions.", error=True)
            return False
        load = next((item for item in self.model.get("regional_loads", []) if item.get("id") == self.selected_source_id), None)
        if load is None:
            self.selected_source_id = None
            return False
        if not any(region.get("id") == region_id for region in self.model.get("geometry", {}).get("regions", [])):
            return False
        target_ids = self._group_members_for_region(region_id)
        assigned_set = set(load.get("region_ids", []))
        if set(target_ids).issubset(assigned_set):
            assigned_set.difference_update(target_ids)
            action = "Removed"
        else:
            assigned_set.update(target_ids)
            action = "Applied"
        region_order = [item.get("id") for item in self.model.get("geometry", {}).get("regions", [])]
        assigned = [item for item in region_order if item in assigned_set]
        load["region_ids"] = assigned
        names = [
            item.get("name", "Region") for item in self.model.get("geometry", {}).get("regions", [])
            if item.get("id") in target_ids
        ]
        target_label = ", ".join(names)
        message = f"{action} {load.get('name', 'source/load')} {'to' if action == 'Applied' else 'from'} {target_label}."
        self._sync_region_sources_from_loads()
        self.selected_region_ids = list(assigned)
        self.selected_region_id = assigned[-1] if assigned else None
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self._refresh_model_tree()
        self._render_inspector()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()
        self._message(message)
        return True

    @_history_tracked
    def delete_regional_load(self, load_id):
        loads = self.model.get("regional_loads", [])
        index = next((i for i, item in enumerate(loads) if item.get("id") == load_id), None)
        if index is None:
            return False
        name = loads[index].get("name", "Source/load")
        del loads[index]
        self._sync_region_sources_from_loads()
        if self.selected_source_id == load_id:
            self.selected_source_id = None
            self.selected_region_id = None
            self.selected_region_ids = []
        self._refresh_model_tree()
        self._render_inspector()
        self._sync_canvas_selection()
        self._message(f"Deleted {name}.")
        return True

    def _tree_subsection(self, section):
        if section == "geometry":
            selected_regions = set(self._selected_region_ids_live())
            groups_by_region = {
                region_id: group.get("name", "Region group")
                for group in self.model.get("region_groups", [])
                for region_id in group.get("region_ids", [])
            }
            entries = [
                (
                    region["id"],
                    f"{region['name']}  ·  {groups_by_region[region['id']]}" if region["id"] in groups_by_region else region["name"],
                    "mdi-vector-square-outline",
                    lambda *a, rid=region["id"]: self.select_region(rid),
                    region["id"] in selected_regions,
                )
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
            materials = {item.get("id"): item for item in [*builtin_materials(), *self._user_materials, *self.model.get("materials", [])]}
            for region in self.model.get("geometry", {}).get("regions", []):
                material = materials.get(region.get("material_id"))
                status = material.get("name", "Assigned") if material else "Unassigned"
                entries.append((
                    region["id"],
                    f"{region.get('name', 'Region')} · {status}",
                    "mdi-cube-outline",
                    lambda *a, rid=region["id"]: self._open_region_material_editor(rid),
                    self.active_section == "materials" and region.get("id") in set(self._selected_region_ids_live()),
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
            entries = []
            for item in self.model.get("regional_loads", []):
                assigned = len(self._assigned_regions_for_load(item.get("id")))
                noun = "region" if assigned == 1 else "regions"
                entries.append((
                    item["id"],
                    f"{item.get('name', 'Source/load')} · {assigned} {noun}",
                    "mdi-flash-outline",
                    lambda *a, lid=item["id"]: self.select_regional_load(lid),
                    item.get("id") == self.selected_source_id,
                ))
            return f"REGIONAL LOADS · {len(entries)}", entries
        if section == "studies":
            entries = [
                (item.get("id", item.get("name", "Study")), item.get("name", "Study"), "mdi-play-box-outline", lambda *a: self.select_section("studies"), False)
                for item in self.studies.get("studies", [])
            ]
            return f"STUDY · {len(entries)}", entries
        if section == "runs":
            entries = [
                (item.get("id") or item.get("output_path") or item.get("name", "Run"), item.get("name", "Run"), "mdi-file-chart-outline", lambda *a: self.select_section("runs"), False)
                for item in reversed(self.runs)
            ]
            return f"RUNS · {len(entries)}", entries
        return None
    def delete_run_history_entry(self, run_id):
        """Hide one Run History entry without deleting its output files."""
        run = next(
            (
                item for item in self.runs
                if str(item.get("id") or item.get("output_path") or item.get("name", "Run")) == str(run_id)
            ),
            None,
        )
        if run is None:
            return False

        output_path = run.get("output_path")
        if output_path:
            hidden_paths = _load_hidden_run_history(self._run_history_root)
            hidden_paths.add(_run_history_path_key(output_path))
            try:
                _save_hidden_run_history(self._run_history_root, hidden_paths)
            except OSError as error:
                self._message(f"Could not save the Run History change: {error}", error=True)
                return False

        self.runs.remove(run)
        self._refresh_model_tree()
        if self.active_section == "runs":
            self._render_inspector()
        return True

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
        """Retain legacy parameter selection without exposing its retired panel."""
        self.selected_parameter_id = parameter_id
        self.select_section("geometry")

    def select_source_region(self, region_id):
        load = next((item for item in self.model.get("regional_loads", []) if region_id in item.get("region_ids", [])), None)
        if load is not None:
            self.select_regional_load(load["id"])
            self.selected_region_id = region_id
            self._sync_canvas_selection()
            return
        self.selected_source_id = None
        self.selected_region_id = region_id
        self.selected_region_ids = [region_id]
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self._selected_condition_id = None
        self.active_section = "sources"
        self.layout["active_section"] = "sources"
        self._update_model_tree_selection()
        self._render_inspector()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()
    def _selected_region_ids_live(self):
        live = {item.get("id") for item in self.model.get("geometry", {}).get("regions", [])}
        selected = [
            region_id for region_id in getattr(self, "selected_region_ids", [])
            if region_id in live
        ]
        if self.selected_region_id in live and self.selected_region_id not in selected:
            selected.append(self.selected_region_id)
        return list(dict.fromkeys(selected))

    def _group_members_for_region(self, region_id):
        live = {item.get("id") for item in self.model.get("geometry", {}).get("regions", [])}
        for group in self.model.get("region_groups", []):
            members = [item for item in group.get("region_ids", []) if item in live]
            if region_id in members:
                return members
        return [region_id] if region_id in live else []

    def _expand_region_group_ids(self, region_ids):
        live_order = [
            item.get("id") for item in self.model.get("geometry", {}).get("regions", [])
            if item.get("id")
        ]
        requested = set(region_ids)
        for region_id in list(requested):
            requested.update(self._group_members_for_region(region_id))
        return [region_id for region_id in live_order if region_id in requested]

    def _prune_region_groups(self):
        if not isinstance(getattr(self, "model", None), dict):
            return
        groups = self.model.get("region_groups", [])
        if not isinstance(groups, list):
            self.model["region_groups"] = []
            return
        live = {
            item.get("id") for item in self.model.get("geometry", {}).get("regions", [])
            if isinstance(item, dict) and item.get("id")
        }
        claimed = set()
        normalized = []
        for group in groups:
            if not isinstance(group, dict) or not group.get("id"):
                continue
            members = [
                region_id for region_id in group.get("region_ids", [])
                if region_id in live and region_id not in claimed
            ]
            if len(members) < 2:
                continue
            group["region_ids"] = members
            group.setdefault("name", f"Region group {len(normalized) + 1}")
            normalized.append(group)
            claimed.update(members)
        self.model["region_groups"] = normalized

    def _set_selected_regions(self, region_ids, *, additive=False):
        region_ids = self._expand_region_group_ids(region_ids)
        if additive:
            region_ids = self._expand_region_group_ids([
                *self._selected_region_ids_live(), *region_ids
            ])
        self.selected_region_ids = list(dict.fromkeys(region_ids))
        self.selected_region_id = self.selected_region_ids[0] if len(self.selected_region_ids) == 1 else None
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

    def select_region(self, region_id, additive=False):
        targets = set(self._group_members_for_region(region_id))
        if not targets:
            return
        if additive:
            current = self._selected_region_ids_live()
            if targets.issubset(set(current)):
                selected = [item for item in current if item not in targets]
            else:
                selected = [*current, *targets]
            self._set_selected_regions(selected)
            self._message(f"Selected {len(self.selected_region_ids)} region(s).")
            return
        self._set_selected_regions(list(targets))

    def _region_context_menu(self):
        delete_item = QItem(
            QItemSection(QIcon(ui_name="mdi-delete-outline", ui_size="xs"), ui_avatar=True),
            QItemSection("Delete selected regions"),
            ui_clickable=True, ui_dense=True,
        )
        delete_item.on_click(lambda *args: self._run_region_context_action(self.delete_selected_regions))
        group_item = QItem(
            QItemSection(QIcon(ui_name="mdi-folder-plus-outline", ui_size="xs"), ui_avatar=True),
            QItemSection("Group selected regions"),
            ui_clickable=True, ui_dense=True,
        )
        group_item.on_click(lambda *args: self._run_region_context_action(self.group_selected_regions))
        ungroup_item = QItem(
            QItemSection(QIcon(ui_name="mdi-folder-remove-outline", ui_size="xs"), ui_avatar=True),
            QItemSection("Ungroup selected regions"),
            ui_clickable=True, ui_dense=True,
        )
        ungroup_item.on_click(lambda *args: self._run_region_context_action(self.ungroup_selected_regions))
        menu = Div(
            delete_item, group_item, ungroup_item,
            id="solve-sketch-context-menu",
            ui_class="solve-sketch-context-menu",
            ui_style=(
                "display:none; position:absolute; z-index:10000; min-width:220px; "
                "padding:4px; border:1px solid var(--border, #d6dde5); "
                "border-radius:8px; background:var(--surface, #fff); "
                "box-shadow:0 8px 24px #14263830;"
            ),
        )
        menu.on("click", lambda *_: self._hide_region_context_menu())
        return menu

    def _run_region_context_action(self, action):
        try:
            return action()
        finally:
            self._hide_region_context_menu()

    def _hide_region_context_menu(self):
        try:
            self.js.eval(
                "(() => { const menu = document.querySelector('.solve-sketch-context-menu'); "
                "if (menu) menu.style.display = 'none'; })()"
            )
        except Exception:
            pass

    @_history_tracked
    def group_selected_regions(self, *args):
        self._prune_region_groups()
        region_ids = self._selected_region_ids_live()
        if len(region_ids) < 2:
            self._message("Select at least two regions before grouping them.", error=True)
            return False
        groups = self.model.setdefault("region_groups", [])
        for group in groups:
            group["region_ids"] = [
                region_id for region_id in group.get("region_ids", [])
                if region_id not in region_ids
            ]
        groups[:] = [group for group in groups if len(group.get("region_ids", [])) >= 2]
        indices = [
            int(match.group(1)) for group in groups
            if (match := re.fullmatch(r"Region group (\d+)", str(group.get("name", ""))))
        ]
        group = {
            "id": new_id("region-group"),
            "name": f"Region group {max(indices, default=0) + 1}",
            "region_ids": region_ids,
        }
        groups.append(group)
        self.selected_region_ids = region_ids
        self.selected_region_id = None
        self._refresh_model_tree()
        self._render_inspector()
        self._sync_canvas_selection()
        self._message(f"Grouped {len(region_ids)} regions as {group['name']}.")
        return True

    @_history_tracked
    def ungroup_selected_regions(self, *args):
        selected = set(self._selected_region_ids_live())
        groups = self.model.get("region_groups", [])
        removed = [group for group in groups if selected.intersection(group.get("region_ids", []))]
        if not removed:
            self._message("Select a grouped region before ungrouping.", error=True)
            return False
        self.model["region_groups"] = [group for group in groups if group not in removed]
        self._refresh_model_tree()
        self._render_inspector()
        self._sync_canvas_selection()
        self._message(f"Ungrouped {sum(len(group.get('region_ids', [])) for group in removed)} regions.")
        return True

    def delete_selected_regions(self, *args):
        return self.delete_regions(self._selected_region_ids_live())

    def delete_selected_geometry(self, *args):
        """Delete the active region selection, or selected edges when no regions are active."""
        region_ids = self._selected_region_ids_live()
        if region_ids:
            return self.delete_regions(region_ids)
        if self.selected_edge_ids or self.selected_edge_id:
            return self.delete_selected_sketch_edges()
        self._message("Select one or more regions or edges to delete.", error=True)
        return False

    @_history_tracked
    def delete_regions(self, region_ids):
        return self._delete_regions(region_ids, create_holes=True)

    def _delete_regions(self, region_ids, *, create_holes=True, notify=True):
        geometry = self.model.setdefault("geometry", {})
        geometry.setdefault("voids", [])
        regions = geometry.get("regions", [])
        region_by_id = {region.get("id"): region for region in regions}
        removed_ids = {region_id for region_id in region_ids if region_id in region_by_id}
        if not removed_ids:
            if notify:
                self._message("Select one or more regions to delete.", error=True)
            return False
        removed_names = [
            region.get("name", "Region") for region in regions
            if region.get("id") in removed_ids
        ]
        removed_sketch_entity_ids = {
            entity_id
            for region in regions
            if region.get("id") in removed_ids and isinstance(region.get("shape"), dict)
            and region["shape"].get("type") == "sketch"
            for entity_id in region["shape"].get("entity_ids", [])
            if isinstance(entity_id, str)
        }

        # A nested material region is a cutout in its surviving container once
        # deleted. Preserve its contour as a void rather than letting the parent
        # fill reclaim the area.
        if create_holes:
            for region in regions:
                if region.get("id") not in removed_ids:
                    continue
                parent_id = region.get("parent_id")
                parent = region_by_id.get(parent_id)
                if parent is None or parent_id in removed_ids:
                    continue
                parent_points = parent.get("vertices", [])
                child_points = region.get("vertices", [])
                if not parent_points or not child_points or not _contains_or_touches(parent_points, child_points):
                    continue
                touches_parent = any(
                    _point_on_segment(
                        tuple(point),
                        tuple(parent_points[index]),
                        tuple(parent_points[(index + 1) % len(parent_points)]),
                        1e-10,
                    )
                    for point in child_points
                    for index in range(len(parent_points))
                )
                if touches_parent:
                    continue
                outer = next(
                    (loop for loop in region.get("boundary_loops", []) if loop.get("role") == "outer"),
                    None,
                )
                if not isinstance(outer, dict) or not outer.get("edges"):
                    continue
                geometry["voids"].append({
                    "id": new_id("void"),
                    "name": f"{region.get('name', 'Region')} opening",
                    "source_region_id": region.get("id"),
                    "parent_id": parent_id,
                    "vertices": copy.deepcopy(child_points),
                    "shape": copy.deepcopy(region.get("shape", {})),
                    "edge_ids": list(region.get("edge_ids", [])),
                    "boundary_loops": [copy.deepcopy(outer)],
                })

        self._preserve_canvas_view()
        with self._batch_frontend_updates():
            def surviving_parent(parent_id):
                visited = set()
                while parent_id in removed_ids and parent_id not in visited:
                    visited.add(parent_id)
                    parent_id = region_by_id[parent_id].get("parent_id")
                return parent_id if parent_id in region_by_id and parent_id not in removed_ids else None

            for region in regions:
                if region.get("id") not in removed_ids and region.get("parent_id") in removed_ids:
                    region["parent_id"] = surviving_parent(region.get("parent_id"))
            self.model["geometry"]["regions"] = [
                region for region in regions if region.get("id") not in removed_ids
            ]
            geometry["voids"] = [
                void for void in geometry.get("voids", [])
                if void.get("parent_id") not in removed_ids
            ]
            retained_sketch_entity_ids = {
                entity_id
                for item in [*self.model["geometry"].get("regions", []), *geometry.get("voids", [])]
                if isinstance(item.get("shape"), dict) and item["shape"].get("type") == "sketch"
                for entity_id in item["shape"].get("entity_ids", [])
                if isinstance(entity_id, str)
            }
            orphaned_sketch_entity_ids = removed_sketch_entity_ids - retained_sketch_entity_ids
            if orphaned_sketch_entity_ids:
                geometry["sketch_entities"] = [
                    entity for entity in geometry.get("sketch_entities", [])
                    if entity.get("id") not in orphaned_sketch_entity_ids
                ]
            for load in self.model.get("regional_loads", []):
                load["region_ids"] = [
                    region_id for region_id in load.get("region_ids", [])
                    if region_id not in removed_ids
                ]
            mesh = self.model.get("mesh", {})
            sizes = mesh.get("region_element_sizes", {})
            if isinstance(sizes, dict):
                for region_id in removed_ids:
                    sizes.pop(region_id, None)
            mesh["hp_region_ids"] = [
                region_id for region_id in mesh.get("hp_region_ids", [])
                if region_id not in removed_ids
            ]
            self._prune_region_groups()
            self.selected_region_id = None
            self.selected_region_ids = []
            self.selected_edge_id = None
            self.selected_edge_ids = []
            self._recompute_parent_links()
            self._rebuild_edges()
            self._sync_region_sources_from_loads()
            if notify:
                self._message("Deleted " + ", ".join(removed_names) + ".")
            self._refresh_model_tree()
            self._render_inspector()
            self.render_canvas()
        return True

    def _on_canvas_region_click(self, region_id, event=None):
        if self.active_section in {"boundaries", "sources"}:
            # Boundary-stage selection is committed on pointer release; the
            # following native click must not apply the same action twice.
            return
        value = getattr(event, "value", None) or {}
        additive = isinstance(value, dict) and bool(value.get("shiftKey") or value.get("ctrlKey"))
        self.select_region(region_id, additive=additive)

    def _on_canvas_edge_click(self, edge_id, event=None):
        if self.active_section == "boundaries":
            # In this stage pointer release handles edge toggles. A second
            # native click would immediately undo the just-applied toggle.
            return
        value = getattr(event, "value", None) or {}
        additive = isinstance(value, dict) and bool(value.get("shiftKey") or value.get("ctrlKey"))
        self.select_edge(edge_id, additive=additive)

    def _edge_ids_for_sketch_entity(self, edge_id):
        """Return every rendered segment that belongs to one sketch curve."""
        edges = self.model.get("geometry", {}).get("edges", [])
        edge = next((item for item in edges if item.get("id") == edge_id), None)
        if edge is None or not edge.get("sketch_entity_id"):
            return [edge_id]
        entity_id = edge["sketch_entity_id"]
        return [
            item["id"] for item in edges
            if item.get("sketch_entity_id") == entity_id and item.get("id")
        ]

    def select_edge(self, edge_id, additive=False):
        entity_edge_ids = self._edge_ids_for_sketch_entity(edge_id)
        if additive:
            selected = list(self.selected_edge_ids)
            if set(selected).intersection(entity_edge_ids):
                selected = [item for item in selected if item not in entity_edge_ids]
            else:
                selected.extend(item for item in entity_edge_ids if item not in selected)
            self.selected_edge_ids = selected
        else:
            self.selected_edge_ids = entity_edge_ids
        self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None
        self.selected_region_id = None
        self.selected_region_ids = []
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

    def _set_boundary_stage_selection(self, edge_ids):
        live_edge_ids = {edge["id"] for edge in self.model.get("geometry", {}).get("edges", [])}
        self.selected_edge_ids = list(dict.fromkeys(edge_id for edge_id in edge_ids if edge_id in live_edge_ids))
        self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None
        self.selected_region_id = None
        self.selected_region_ids = []
        self._selected_condition_id = self.selected_boundary_id
        self.active_section = "boundaries"
        self.layout["active_section"] = self.active_section
        self._update_model_tree_selection()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()
        self._sync_boundary_selection_controls()

    def _clear_canvas_selection(self):
        """Clear selected regions and edges after a click on blank canvas."""
        has_selection = bool(
            self.selected_region_id
            or getattr(self, "selected_region_ids", [])
            or self.selected_edge_id
            or self.selected_edge_ids
        )
        if not has_selection:
            return
        if self.active_section == "boundaries":
            self._set_boundary_stage_selection([])
            return

        self.selected_region_id = None
        self.selected_region_ids = []
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self._selected_condition_id = None
        self._update_model_tree_selection()
        if self._inspector_view != "geometry":
            self._render_inspector()
        else:
            self._update_geometry_inspector()
        self._sync_canvas_selection()
        self._render_canvas_dimensions()

    def toggle_boundary_stage_region(self, region_id):
        """Toggle every unique edge belonging to a clicked region."""
        region = next((item for item in self.model.get("geometry", {}).get("regions", []) if item.get("id") == region_id), None)
        if region is None:
            return
        region_edges = list(dict.fromkeys(region.get("edge_ids", [])))
        current = list(self.selected_edge_ids)
        if region_edges and all(edge_id in current for edge_id in region_edges):
            blocked = set(region_edges)
            current = [edge_id for edge_id in current if edge_id not in blocked]
        else:
            current = list(dict.fromkeys([*current, *region_edges]))
        self._set_boundary_stage_selection(current)
        if any(edge_id in current for edge_id in region_edges):
            self._assign_active_boundary_to_edges(region_edges)

    def add_boundary_stage_region(self, region_id):
        """Backward-compatible alias for the region selection toggle."""
        self.toggle_boundary_stage_region(region_id)

    def toggle_boundary_stage_edge(self, edge_id):
        """Toggle one boundary independently of any region selection."""
        live_edge_ids = {edge["id"] for edge in self.model.get("geometry", {}).get("edges", [])}
        if edge_id not in live_edge_ids:
            return
        selected = list(self.selected_edge_ids)
        added = edge_id not in selected
        if added:
            selected.append(edge_id)
        else:
            selected.remove(edge_id)
        self._set_boundary_stage_selection(selected)
        if added:
            self._assign_active_boundary_to_edges([edge_id])

    def _assign_active_boundary_to_edges(self, edge_ids):
        condition = next((item for item in self.model.get("boundary_conditions", []) if item.get("id") == self.selected_boundary_id), None)
        if condition is None or not edge_ids or condition.get("automatic_for_axis") or condition.get("automatic_for_mechanics"):
            return
        kind = str(condition.get("type", ""))
        physics = "mechanical" if kind.startswith("mechanical_") or kind == "transmission_interface" else "electromagnetic"
        self._set_edge_conditions(edge_ids, physics, condition["id"])

    def _sync_boundary_selection_controls(self):
        summary = getattr(self, "_boundary_selection_summary", None)
        if summary is None:
            return
        selected_count = len(self.selected_edge_ids)
        condition = next((item for item in self.model.get("boundary_conditions", []) if item.get("id") == self.selected_boundary_id), None)
        if condition is None:
            detail = "Select a condition definition, then click a region or edge to assign it."
        elif condition.get("automatic_for_axis") or condition.get("automatic_for_mechanics"):
            detail = f"{condition.get('name', 'Condition')} is automatic and cannot be assigned manually."
        else:
            detail = f"Active condition: {condition.get('name', 'Boundary condition')} - {selected_count} unique edge(s) selected."
        summary.ui_children = [
            Div("Geometry selection", ui_style="font-size:10px; font-weight:700; letter-spacing:.06em; color:var(--fg-muted);"),
            Div("Click a region to toggle all its boundaries. Click an edge to toggle only that edge. The active condition is assigned on each new selection.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
            Div(detail, ui_style="font-size:12px; font-weight:600; line-height:1.4; padding-top:4px;"),
        ]

    def _sync_canvas_selection(self):
        """Update selected sketch styling without replacing canvas geometry."""
        edge_ids = json.dumps(self.selected_edge_ids)
        region_ids = json.dumps(getattr(self, "selected_region_ids", []) or ([self.selected_region_id] if self.selected_region_id else []))
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
  const selectedRegions = new Set({region_ids});
  const highlightedEdges = new Set({highlighted_ids});
  window.__ngsolveSketchBoundarySelection = {str(self.active_section == "boundaries").lower()};
  svg.querySelectorAll('[data-sketch-edge-role="visible"]').forEach((line) => {{
    const id = line.getAttribute('data-sketch-edge-id');
    const selected = selectedEdges.has(id);
    const highlighted = highlightedEdges.has(id);
    line.setAttribute('data-sketch-selected', String(selected));
    line.setAttribute('data-sketch-condition-highlight', String(highlighted));
    const hovered = window.__ngsolveSketchBoundarySelection && window.__ngsolveSketchHoveredEdgeId === id;
    line.setAttribute('stroke', selected ? '#0877b9' : (hovered ? '#e29a12' : (highlighted ? '#c66f16' : '#263746')));
    line.setAttribute('stroke-width', selected ? '4.6' : (hovered ? '4.4' : (highlighted ? '4.2' : '1.6')));
  }});
  svg.querySelectorAll('[data-sketch-region-role="shape"]').forEach((shape) => {{
    const selected = selectedRegions.has(shape.getAttribute('data-sketch-region-id'));
    const hovered = window.__ngsolveSketchBoundarySelection && window.__ngsolveSketchHoveredRegionId === shape.getAttribute('data-sketch-region-id');
    shape.setAttribute('data-sketch-selected', String(selected));
    shape.setAttribute('fill-opacity', selected ? '0.48' : (hovered ? '0.40' : '0.33'));
    shape.setAttribute('stroke', selected ? '#1b73ae' : (hovered ? '#17824b' : '#344658'));
    shape.setAttribute('stroke-width', selected ? '2.6' : (hovered ? '2.8' : '1.6'));
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
            width_m = float(shape_data.get("width", 0))
            height_m = float(shape_data.get("height", 0))
            dim_style = "stroke:#455f78; stroke-width:1; fill:none; pointer-events:none;"
            text_style = "fill:#263746; font-size:11px; font-weight:600; pointer-events:none;"
            offset = 18
            return [
                _svg("line", x1=p0[0], y1=p0[1] + offset, x2=p1[0], y2=p1[1] + offset, style=dim_style),
                _svg("line", x1=p0[0], y1=p0[1] + 4, x2=p0[0], y2=p0[1] + offset + 3, style=dim_style),
                _svg("line", x1=p1[0], y1=p1[1] + 4, x2=p1[0], y2=p1[1] + offset + 3, style=dim_style),
                _svg("text", x=(p0[0] + p1[0]) / 2, y=p0[1] + offset + 14, text_anchor="middle", style=text_style, children=f"W {width_m:.4g} m"),
                _svg("line", x1=p1[0] + offset, y1=p1[1], x2=p2[0] + offset, y2=p2[1], style=dim_style),
                _svg("line", x1=p1[0] + 4, y1=p1[1], x2=p1[0] + offset + 3, y2=p1[1], style=dim_style),
                _svg("line", x1=p2[0] + 4, y1=p2[1], x2=p2[0] + offset + 3, y2=p2[1], style=dim_style),
                _svg("text", x=p1[0] + offset + 5, y=(p1[1] + p2[1]) / 2, style=text_style, children=f"H {height_m:.4g} m"),
            ]
        if shape_data.get("type") == "circle":
            center = xy((shape_data["r_center"], shape_data["z_center"]))
            radial = xy((shape_data["r_center"] + shape_data["radius"], shape_data["z_center"]))
            radius_m = float(shape_data.get("radius", 0))
            return [
                _svg("line", x1=center[0], y1=center[1], x2=radial[0], y2=radial[1], stroke="#455f78", stroke_width="1", style="pointer-events:none;"),
                _svg("text", x=(center[0] + radial[0]) / 2, y=center[1] - 7, text_anchor="middle", fill="#263746", font_size="11", font_weight="600", style="pointer-events:none;", children=f"R {radius_m:.4g} m"),
            ]
        return []

    def _render_canvas_dimensions(self):
        """Update selected dimensions without replacing the full sketch scene."""
        region = self._selected_region()
        self._canvas_dimensions.ui_children = self._region_dimension_components(region)
        _update_svg_props(
            self._canvas_dimensions,
            display="inline" if self._canvas_geometry_visible else "none",
        )
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
            Div("Choose Rectangle, Circle, Line or Arc in the viewport toolbar. Select a region to edit its dimensions. Assign materials and mechanics under Materials.", ui_style="font-size:12px; line-height:1.5; color:var(--fg-muted);"),
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
            "imported_contour": (("r_min", "Inner radius"), ("z_min", "Bottom"), ("width", "Radial width"), ("height", "Axial height")),
            "sketch": (("r_min", "Inner radius"), ("z_min", "Bottom"), ("width", "Radial width"), ("height", "Axial height")),
        }.items():
            inputs = []
            for key, label in fields:
                inputs.append(_input(
                    label,
                    "",
                    lambda event, dimension=key: self._set_selected_region_dimension(dimension, event.value),
                    suffix="m",
                    hint="Enter lengths in SI metres.",
                ))
                self._region_dimension_groups.setdefault(shape_type, {})[key] = inputs[-1]
            self._region_dimension_groups[shape_type]["group"] = Div(
                *inputs,
                ui_hidden=True,
                ui_style="display:flex; flex-direction:column; gap:7px;",
            )

        self._region_constraints = Div(ui_style="display:flex; flex-direction:column; gap:2px;")
        self._region_panel = Div(
            self._region_title,
            Div("GEOMETRY", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted);"),
            self._region_name_input,
            Div("Driving dimensions", ui_style="font-size:11px; font-weight:700; letter-spacing:.05em; padding-top:4px;"),
            Div("Dimensions are in SI metres (m). Sketch contours can be moved or resized here; select an edge to edit its curve endpoints or Spline3 control point.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
            *[group["group"] for group in self._region_dimension_groups.values()],
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
        self._imported_edge_geometry_fields = {}
        imported_edge_fields = []
        for point_role, point_label in (("start", "Start"), ("end", "End"), ("control", "Spline control")):
            for axis, axis_label in (("r", "r"), ("z", "z")):
                field = _input(
                    f"{point_label} {axis_label}",
                    "",
                    lambda event, role=point_role, coordinate=axis: self._set_selected_imported_edge_coordinate(role, coordinate, event.value),
                    suffix="m",
                    hint="Edit in SI metres. Curves connected at the same sketch point update together.",
                )
                self._imported_edge_geometry_fields[(point_role, axis)] = field
                imported_edge_fields.append(field)
        self._imported_edge_geometry = Div(
            Div("EDIT SKETCH CURVE", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted);"),
            Div("Change a curve endpoint or its Spline3 control point. Connected curves follow shared sketch points.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
            *imported_edge_fields,
            ui_hidden=True,
            ui_style="display:flex; flex-direction:column; gap:7px; padding:8px 0;",
        )
        self._arc_radius_input = _input("Arc radius", "", lambda event: self._set_selected_arc_radius(event.value), number=True, suffix="m", hint="Keep the endpoints fixed; radius must be at least half their separation.")
        self._arc_radius_input.ui_hidden = True
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
        self._edge_boundary_note = Div(
            "An edge can have one EM and one mechanical condition. A manual mechanical condition replaces automatic transmission.",
            ui_style="font-size:11px; color:var(--fg-muted); line-height:1.45;",
        )
        self._edge_panel = Div(
            self._edge_title,
            self._edge_name_input,
            self._imported_edge_geometry,
            self._arc_radius_input,
            self._edge_em_select,
            self._edge_mech_select,
            self._edge_boundary_note,
            ui_hidden=True,
            ui_style="display:flex; flex-direction:column; gap:12px;",
        )
        self._multi_region_panel = Div(
            _section_title("Multiple regions selected", "Use the Materials section to apply a shared material to this selection."),
            Div("Right-click a selected region to group, ungroup, or delete the selection. Grouped regions stay separate for solving.", ui_style="font-size:11px; line-height:1.45; color:var(--fg-muted);"),
            _button("Assign material to selection", "mdi-cube-outline", lambda *args: self.select_section("materials"), color="primary"),
            ui_hidden=True,
            ui_style="display:flex; flex-direction:column; gap:12px;",
        )
        self._geometry_inspector_children = [self._geometry_empty_panel, self._multi_region_panel, self._region_panel, self._edge_panel]
        self._geometry_inspector_ready = True

    def _edge_condition_options(self, physics, assigned_ids=()):
        if physics == "mechanical":
            kinds = lambda item: (
                item.get("type", "").startswith("mechanical_")
                or item.get("type") == "transmission_interface"
            )
            empty = "No mechanical condition (default)"
        else:
            kinds = lambda item: item.get("type") in {"axis_of_symmetry", "magnetic_potential_zero", "natural"}
            empty = "Natural / material interface"
        assigned_ids = set(assigned_ids)
        options = [{"label": empty, "value": None}]
        for item in self.model.get("boundary_conditions", []):
            if not kinds(item):
                continue
            automatic = bool(item.get("automatic_for_mechanics") or item.get("automatic_for_axis"))
            if automatic and item.get("id") not in assigned_ids:
                continue
            label = item.get("name", "Boundary condition")
            option = {"label": label, "value": item["id"]}
            if automatic:
                option["disable"] = True
                option["label"] = label + " (automatic)"
            options.append(option)
        return options

    def _material_options(self):
        """Offer library presets and project materials without preloading them."""
        options = [{"label": "Unassigned", "value": None}]
        seen = set()
        for material in [
            *builtin_materials(),
            *self._user_materials,
            *self.model.get("materials", []),
        ]:
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

    def _set_selected_imported_edge_coordinate(self, point_role, axis, value):
        edge = self._selected_edge()
        if edge is not None and len(self.selected_edge_ids) == 1:
            self._set_imported_edge_coordinate(edge["id"], point_role, axis, value)

    def _update_geometry_inspector(self):
        if not self._geometry_inspector_ready or self.active_section != "geometry":
            return
        region = self._selected_region()
        edges = self._selected_edges()
        multiple_regions = len(self._selected_region_ids_live()) > 1
        show_region = region is not None and not edges and not multiple_regions
        show_edges = bool(edges) and not multiple_regions
        self._geometry_empty_panel.ui_hidden = show_region or show_edges or multiple_regions
        self._imported_edge_geometry.ui_hidden = True
        self._multi_region_panel.ui_hidden = not multiple_regions
        self._region_panel.ui_hidden = not show_region
        self._edge_panel.ui_hidden = not show_edges
        if show_region:
            shape = region.get("shape", {})
            shape_type = shape.get("type")
            parent = next((item["name"] for item in self.model["geometry"]["regions"] if item["id"] == region.get("parent_id")), "Exterior")
            self._region_subtitle.ui_children = [f"{shape_type.title() if shape_type else 'Region'} · parent: {parent}"]
            self._region_name_input.ui_model_value = region.get("name", "")
            expressions = shape.get("dimension_expressions", {})
            dimension_values = shape
            if shape_type in {"imported_contour", "sketch"}:
                points = region.get("vertices", [])
                if points:
                    radial = [float(point[0]) for point in points]
                    axial = [float(point[1]) for point in points]
                    r_min, z_min = min(radial), min(axial)
                    dimension_values = {
                        "r_min": r_min,
                        "z_min": z_min,
                        "width": max(radial) - r_min,
                        "height": max(axial) - z_min,
                    }
            for dimension, widget in self._region_dimension_groups.get(shape_type, {}).items():
                if dimension in dimension_values:
                    widget.ui_model_value = expressions.get(dimension, self._dimension_literal_m(float(dimension_values[dimension])))
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
                curve = edge.get("curve", {})
                is_editable_arc = isinstance(curve, dict) and curve.get("type") == "arc" and bool(edge.get("sketch_entity_id"))
                self._arc_radius_input.ui_hidden = not is_editable_arc
                if is_editable_arc:
                    self._arc_radius_input.ui_model_value = str(float(curve.get("radius", 0.0)))
                is_editable_sketch_curve = self._is_imported_legacy_edge(edge) and isinstance(curve, dict) and curve.get("type") in {"line", "spline3"}
                self._imported_edge_geometry.ui_hidden = not is_editable_sketch_curve
                if is_editable_sketch_curve:
                    entity = next((
                        item for item in self.model.get("geometry", {}).get("sketch_entities", [])
                        if item.get("id") == edge.get("sketch_entity_id")
                    ), {})
                    source_points = entity.get("points", [])
                    endpoints = [source_points[0], source_points[-1]] if len(source_points) >= 2 else edge.get("vertices", [])
                    for role, index in (("start", 0), ("end", 1)):
                        point = endpoints[index] if len(endpoints) > index else (0.0, 0.0)
                        for axis, coordinate in (("r", 0), ("z", 1)):
                            self._imported_edge_geometry_fields[(role, axis)].ui_model_value = self._dimension_literal_m(float(point[coordinate]))
                    control = entity.get("control_point") or (source_points[1] if len(source_points) >= 3 else [0.0, 0.0])
                    for axis, coordinate in (("r", 0), ("z", 1)):
                        widget = self._imported_edge_geometry_fields[("control", axis)]
                        widget.ui_hidden = entity.get("type") != "spline3"
                        if entity.get("type") == "spline3":
                            widget.ui_model_value = self._dimension_literal_m(float(control[coordinate]))
            else:
                self._imported_edge_geometry.ui_hidden = True
                self._arc_radius_input.ui_hidden = True
                self._edge_subtitle.ui_children = [f"{len(edges)} edges selected · assignments apply to all"]
                self._edge_name_input.ui_hidden = True
            conditions = {item["id"]: item for item in self.model.get("boundary_conditions", [])}
            assignments = [_edge_condition_ids(edge) for edge in edges]
            em_values = [next((cid for cid in ids if conditions.get(cid, {}).get("type") in {"axis_of_symmetry", "magnetic_potential_zero", "natural"}), None) for ids in assignments]
            mech_values = [next((cid for cid in ids if conditions.get(cid, {}).get("type", "").startswith("mechanical_") or conditions.get(cid, {}).get("type") == "transmission_interface"), None) for ids in assignments]
            assigned_ids = [condition_id for ids in assignments for condition_id in ids]
            self._edge_em_select.ui_options = self._edge_condition_options("electromagnetic", assigned_ids)
            self._edge_em_select.ui_model_value = em_values[0] if all(value == em_values[0] for value in em_values) else None
            self._edge_mech_select.ui_options = self._edge_condition_options("mechanical", assigned_ids)
            self._edge_mech_select.ui_model_value = mech_values[0] if all(value == mech_values[0] for value in mech_values) else None
            axis_count = sum(_edge_is_on_rotation_axis(edge) for edge in edges)
            locked_mechanical_count = sum(bool(edge.get("mechanical_boundary_locked")) for edge in edges)
            self._edge_em_select.ui_disable = axis_count == len(edges)
            self._edge_mech_select.ui_disable = axis_count + locked_mechanical_count == len(edges)
            if axis_count == len(edges):
                self._edge_boundary_note.ui_children = ["The axis of symmetry is required and locked; no other condition can be applied there."]
            elif axis_count or locked_mechanical_count:
                self._edge_boundary_note.ui_children = ["Protected edges in this selection will be skipped. Shared mechanical edges keep the default mechanical condition; the EM condition remains editable."]
            else:
                self._edge_boundary_note.ui_children = ["One EM and one mechanical condition are allowed. A user mechanical condition replaces automatic transmission."]

    def _render_inspector(self):
        if self._editor_drawer is not None:
            children = self._editor_drawer_children()
            self._inspector.ui_children = children
            self._inspector_view = f"editor:{self._editor_drawer['kind']}"
            return
        if self.active_section == "geometry":
            if not self._geometry_inspector_ready:
                self._build_geometry_inspector()
            self._update_geometry_inspector()
            if self._inspector_view != "geometry":
                self._inspector.ui_children = self._geometry_inspector_children
                self._inspector_view = "geometry"
            return
        section_methods = {
            "parameters": self._parameter_properties,
            "materials": self._material_properties,
            "boundaries": self._boundary_properties,
            "physics": self._physics_properties,
            "sources": self._sources_properties,
            "mesh": self._mesh_properties,
            "solver": self._solver_properties,
            "studies": self._study_properties,
        }
        children = section_methods.get(self.active_section, self._run_properties)()
        self._inspector.ui_children = children
        self._inspector_view = self.active_section

    def _run_properties(self):
        children = [_section_title("Run history", "Mesh and study outputs are saved in NGSolve_results.")]
        if not self.runs:
            children.append(Div("No mesh or solver runs yet.", ui_style="font-size:12px; color:var(--fg-muted);"))
            return children
        for run in reversed(self.runs):
            status = run.get("status", "Complete")
            status_color = (
                "var(--negative)" if status == "Failed"
                else "var(--warning)" if status in {"Cancelled", "Interrupted"}
                else "var(--positive)"
            )
            children.append(Div(
                Div(
                    Div(run["name"], ui_style="font-weight:600; font-size:12px; overflow-wrap:anywhere;"),
                    Div(status, ui_style=f"color:{status_color}; font-size:11px; font-weight:600;"),
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
            frequency_by_file = {}
            for field_file in field_files:
                match = _FREQUENCY_RESULT_SUFFIX.search(field_file.stem)
                if match:
                    frequency_by_file[field_file] = match.group(1)
            frequencies = sorted(
                set(frequency_by_file.values()),
                key=lambda value: (float(value), value),
            )
            selected_frequency = str(
                run.get("selected_frequency_hz")
                or (frequencies[0] if frequencies else "")
            )
            if frequencies:
                run["selected_frequency_hz"] = selected_frequency
            if len(frequencies) > 1:
                frequency_select = QSelect(
                    ui_label="Frequency",
                    ui_options=[
                        {"label": f"{frequency} Hz", "value": frequency}
                        for frequency in frequencies
                    ],
                    ui_option_label="label",
                    ui_option_value="value",
                    ui_model_value=selected_frequency,
                    ui_emit_value=True,
                    ui_map_options=True,
                    ui_dense=True,
                    ui_filled=True,
                )
                frequency_select.on_update_model_value(
                    lambda event, selected_run=run: self._set_run_frequency(
                        selected_run, event.value
                    )
                )
                children.append(frequency_select)
            for field_file in field_files:
                frequency = frequency_by_file.get(field_file)
                if frequency and len(frequencies) > 1 and frequency != selected_frequency:
                    continue
                if self.on_open_file:
                    children.append(Div(
                        Div(field_file.name, ui_style="font-size:11px; overflow-wrap:anywhere; flex:1;"),
                        _button("Open in Post Process", "mdi-open-in-app", lambda *a, path=str(field_file): self.on_open_file(path)),
                        ui_style="display:flex; align-items:center; gap:6px; padding:3px 6px 3px 12px;",
                    ))
        return children

    def _set_run_frequency(self, run, frequency):
        run["selected_frequency_hz"] = str(frequency)
        if self.active_section == "runs":
            self._render_inspector()

    def _geometry_properties(self):
        if len(self._selected_region_ids_live()) > 1:
            selected_ids = set(self._selected_region_ids_live())
            group = next((
                item for item in self.model.get("region_groups", [])
                if set(item.get("region_ids", [])) == selected_ids
            ), None)
            title = f"{group.get('name')} selected" if group else "Geometry selection"
            return [
                _section_title(title, "Multiple regions selected in the viewport."),
                Div(f"{len(selected_ids)} regions are selected.", ui_style="font-size:12px; line-height:1.5; color:var(--fg-muted);"),
                Div("Ctrl-click or Shift-click regions to add or remove them. Right-click a region to group, ungroup, or delete the selection.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
                _button("Assign material to selection", "mdi-cube-outline", lambda *args: self.select_section("materials"), color="primary"),
            ]
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
            has_auto_transmission = any(
                conditions.get(cid, {}).get("automatic_for_mechanics")
                and conditions.get(cid, {}).get("type") == "transmission_interface"
                for cid in selected_ids
            )
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
                *([Div("Transmission interface · automatic", ui_style="font-size:11px; color:var(--fg-muted);")] if has_auto_transmission else []),
                Div("Electromagnetic and mechanical conditions are assigned independently, so one edge can participate in both physics." + (" The axis is excluded from mechanical support assignments in this version." if on_axis else ""), ui_style="font-size:11px; color:var(--fg-muted); line-height:1.45;"),
            ]
        if region:
            name = _input("Region name", region["name"], lambda event, rid=region["id"]: self._set_region_value(rid, "name", event.value))
            shape_fields = []
            shape = region.get("shape", {})
            if shape.get("type") == "rectangle":
                expressions = shape.get("dimension_expressions", {})
                for key, label in (("r_min", "Inner radius"), ("z_min", "Bottom"), ("width", "Radial width"), ("height", "Axial height")):
                    value = expressions.get(key, str(float(shape[key])))
                    shape_fields.append(_input(label, value, lambda event, rid=region["id"], k=key: self._set_region_dimension(rid, k, event.value), suffix="m", hint="Enter lengths in SI metres."))
            elif shape.get("type") == "circle":
                expressions = shape.get("dimension_expressions", {})
                for key, label in (("r_center", "Centre radius"), ("z_center", "Centre height"), ("radius", "Radius")):
                    value = expressions.get(key, str(float(shape[key])))
                    shape_fields.append(_input(label, value, lambda event, rid=region["id"], k=key: self._set_region_dimension(rid, k, event.value), suffix="m", hint="Enter lengths in SI metres."))
            delete_button = _button("Delete region", "mdi-delete-outline", lambda *a, rid=region["id"]: self.delete_region(rid), color="negative")
            constraints = [Div(f"{constraint['type'].replace('_', ' ').title()}  ·  {'driving' if constraint.get('driving') else 'reference'}", ui_style="font-size:11px; padding:3px 0; color:var(--fg-muted);") for constraint in region.get("constraints", [])]
            parent = next((item["name"] for item in self.model["geometry"]["regions"] if item["id"] == region.get("parent_id")), "Exterior")
            return [
                _section_title("Region properties", f"{region.get('shape', {}).get('type', 'polygon').title()} · parent: {parent}"),
                Div("GEOMETRY", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted);"),
                name,
                Div("Driving dimensions", ui_style="font-size:11px; font-weight:700; letter-spacing:.05em; padding-top:4px;"),
                Div("Dimensions are in SI metres (m). Imported legacy contours can be moved and resized here; select one of their edges to edit its endpoints or spline control point.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
                *shape_fields,
                Div("SKETCH CONSTRAINTS", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:6px;"),
                Div("These are built-in relationships for the rectangle and circle tools. This version does not solve general user-defined sketch constraints.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
                *constraints,
                delete_button,
            ]
        return [
            _section_title("Geometry", "Click a region or edge to edit it."),
            Div("Choose Rectangle, Circle, Line or Arc in the viewport toolbar. Select a region to edit its dimensions. Assign materials and mechanics under Materials.", ui_style="font-size:12px; line-height:1.5; color:var(--fg-muted);"),
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
            unit_select = QSelect(
                ui_label="Dimension",
                ui_options=list(UNIT_OPTIONS),
                ui_option_label="label",
                ui_option_value="value",
                ui_model_value=parameter.get("unit", "m"),
                ui_emit_value=True,
                ui_map_options=True,
                ui_dense=True,
                ui_filled=True,
            )
            unit_select.on_update_model_value(
                lambda event, pid=parameter["id"]: self._set_parameter(pid, "unit", event.value)
            )
            children.append(Div(
                Div(
                    _input("Name", parameter["name"], lambda event, pid=parameter["id"]: self._set_parameter(pid, "name", event.value)),
                    unit_select,
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
                "Choose the parameter's physical dimension. Compatible expressions can reuse it; Check setup rejects using it in a field with a different SI unit. Bare numbers are interpreted in the destination field's unit.",
                ui_style="font-size:11px; line-height:1.45; color:var(--fg-muted);",
            ),
        ])
        return children

    def _open_editor(self, kind, *, item_id=None, draft=None, is_new=False):
        self._editor_drawer = {
            "kind": kind,
            "item_id": item_id,
            "draft": copy.deepcopy(draft or {}),
            "is_new": bool(is_new),
        }
        self._render_inspector()

    def _cancel_editor(self, *args):
        self._editor_drawer = None
        self._render_inspector()

    def _set_editor_draft(self, key, event):
        if self._editor_drawer is None:
            return
        value = getattr(event, "value", event)
        self._editor_drawer["draft"][key] = "" if value is None else str(value).strip()
        if key == "type" and self._editor_drawer.get("kind") == "boundary":
            # The boundary editor's value fields depend on the selected type.
            # Rebuild the drawer as soon as the user changes that selection.
            self._render_inspector()

    def _set_editor_source_force(self, component, event):
        if self._editor_drawer is None:
            return
        value = str(getattr(event, "value", event) or "").strip()
        force = self._editor_drawer["draft"].setdefault("mechanical_body_force", {})
        force[component] = value or "0"

    def _editor_drawer_children(self):
        editor = self._editor_drawer
        kind, draft = editor["kind"], editor["draft"]
        titles = {
            "material": ("Material details", "Set the material properties and save them to this problem."),
            "region_material": ("Region material", "Choose a preset or define a custom material for this region."),
            "boundary": ("Boundary condition", "Configure this condition. Select it afterwards to assign it in the viewport."),
            "regional_load": ("Source/load", "Define the values, save, then assign this source to regions from the main page."),
            "region_mesh_size": ("Region-wise element sizing", "Choose one region and its local target element size."),
        }
        title, subtitle = titles[kind]
        back = _button("Back", "mdi-arrow-left", self._cancel_editor, style="flex:0 0 auto;")
        header = Div(
            back,
            Div(title, Div(subtitle, ui_style="font-size:11px; color:var(--fg-muted); line-height:1.35; margin-top:2px;"), ui_style="min-width:0;"),
            ui_style="display:flex; align-items:center; gap:10px; padding:10px 0 12px; border-bottom:1px solid var(--border);",
        )
        body = []
        if kind == "material":
            props = draft.setdefault("properties", {})
            body.extend([
                _input("Material name", draft.get("name", ""), lambda event: self._set_editor_draft("name", event)),
                Div("ELECTROMAGNETIC", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:4px;"),
                _input("Relative permeability", props.get("relative_permeability", ""), lambda event: props.__setitem__("relative_permeability", str(getattr(event, "value", event) or "").strip())),
                _input("Electrical conductivity", props.get("electrical_conductivity", ""), lambda event: props.__setitem__("electrical_conductivity", str(getattr(event, "value", event) or "").strip()), suffix="S/m"),
                Div("MECHANICAL", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:4px;"),
                _input("Young's modulus", props.get("youngs_modulus", ""), lambda event: props.__setitem__("youngs_modulus", str(getattr(event, "value", event) or "").strip()), suffix="Pa"),
                _input("Poisson's ratio", props.get("poissons_ratio", ""), lambda event: props.__setitem__("poissons_ratio", str(getattr(event, "value", event) or "").strip())),
                _input("Density", props.get("density", ""), lambda event: props.__setitem__("density", str(getattr(event, "value", event) or "").strip()), suffix="kg/m³"),
            ])
        elif kind == "region_material":
            choice = draft.get("material_choice")
            options = list(self._material_options())
            options.append({"label": "Custom...", "value": "__custom__"})
            material_select = QSelect(
                ui_label="Material", ui_options=options,
                ui_option_label="label", ui_option_value="value",
                ui_model_value=choice, ui_emit_value=True, ui_map_options=True,
                ui_dense=True, ui_filled=True,
            )
            material_select.on_update_model_value(self._set_editor_material_choice)
            mechanical = QCheckbox(ui_label="Include in mechanics", ui_model_value=bool(draft.get("mechanical", False)), ui_dense=True)
            mechanical.on_update_model_value(lambda event: self._editor_drawer["draft"].__setitem__("mechanical", bool(event.value)))
            body.extend([material_select, mechanical])
            if choice == "__custom__":
                custom = draft.setdefault("custom_material", {})
                properties = custom.setdefault("properties", {})
                body.extend([
                    Div("CUSTOM MATERIAL", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:4px;"),
                    _input("Material name", custom.get("name", ""), lambda event: self._set_editor_custom_material_property("name", event)),
                    Div("ELECTROMAGNETIC", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:4px;"),
                    _input("Relative permeability", properties.get("relative_permeability", "1"), lambda event: self._set_editor_custom_material_property("relative_permeability", event)),
                    _input("Electrical conductivity", properties.get("electrical_conductivity", "0"), lambda event: self._set_editor_custom_material_property("electrical_conductivity", event), suffix="S/m"),
                    Div("MECHANICAL", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:4px;"),
                    _input("Young's modulus", properties.get("youngs_modulus", "0"), lambda event: self._set_editor_custom_material_property("youngs_modulus", event), suffix="Pa"),
                    _input("Poisson's ratio", properties.get("poissons_ratio", "0.3"), lambda event: self._set_editor_custom_material_property("poissons_ratio", event)),
                    _input("Density", properties.get("density", "0"), lambda event: self._set_editor_custom_material_property("density", event), suffix="kg/m³"),
                ])
        elif kind == "boundary":
            kinds = [
                ("axis_of_symmetry", "Axis of symmetry"),
                ("magnetic_potential_zero", "Magnetic potential = 0"),
                ("natural", "Natural / free boundary"),
                ("mechanical_fixed", "Fixed displacement"),
                ("mechanical_prescribed", "Prescribed displacement"),
                ("mechanical_traction", "Vector traction"),
                ("mechanical_robin", "Robin support"),
            ]
            select = QSelect(
                ui_label="Condition type",
                ui_options=[{"label": label, "value": value} for value, label in kinds],
                ui_option_label="label", ui_option_value="value",
                ui_model_value=draft.get("type", "natural"), ui_emit_value=True,
                ui_map_options=True, ui_dense=True, ui_filled=True,
            )
            select.on_update_model_value(lambda event: self._set_editor_draft("type", event))
            body.extend([
                _input("Condition name", draft.get("name", ""), lambda event: self._set_editor_draft("name", event)),
                select,
            ])
            typ = draft.get("type", "natural")
            type_help = {
                "axis_of_symmetry": "Enforces axisymmetric regularity on r = 0; no value is prescribed.",
                "magnetic_potential_zero": "Sets magnetic potential to zero.",
                "natural": "Uses the homogeneous natural (zero-flux / zero-traction) condition.",
                "mechanical_fixed": "Sets both radial and axial displacement to zero.",
                "mechanical_prescribed": "Enter the prescribed radial and axial displacements.",
                "mechanical_traction": "Enter the radial and axial traction components.",
                "mechanical_robin": "Enter the normal and tangential spring stiffnesses to ground.",
            }.get(typ)
            if type_help:
                body.append(Div(type_help, ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"))
            fields = {
                "mechanical_prescribed": (("displacement_r", "Radial displacement", "m"), ("displacement_z", "Axial displacement", "m")),
                "mechanical_traction": (("traction_r", "Radial traction", "N/m²"), ("traction_z", "Axial traction", "N/m²")),
                "mechanical_robin": (("stiffness_normal", "Normal stiffness", "N/m³"), ("stiffness_tangential", "Tangential stiffness", "N/m³")),
            }
            for key, label, suffix in fields.get(typ, ()):
                body.append(_input(label, draft.get(key, "0"), lambda event, field=key: self._set_editor_draft(field, event), suffix=suffix))
        elif kind == "regional_load":
            force = draft.get("mechanical_body_force", {})
            if not isinstance(force, dict):
                force = {}
            body.extend([
                _input("Source/load name", draft.get("name", ""), lambda event: self._set_editor_draft("name", event)),
                _input("DC current density", draft.get("dc_current_density", "0"), lambda event: self._set_editor_draft("dc_current_density", event), suffix="A/m²"),
                _input("AC current density (real)", draft.get("ac_current_density_real", "0"), lambda event: self._set_editor_draft("ac_current_density_real", event), suffix="A/m²"),
                _input("AC current density (imaginary)", draft.get("ac_current_density_imaginary", "0"), lambda event: self._set_editor_draft("ac_current_density_imaginary", event), suffix="A/m²"),
                Div("MECHANICAL BODY FORCE", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:4px;"),
                _input("Radial body force", force.get("r", "0"), lambda event: self._set_editor_source_force("r", event), suffix="N/m³"),
                _input("Axial body force", force.get("z", "0"), lambda event: self._set_editor_source_force("z", event), suffix="N/m³"),
            ])
        elif kind == "region_mesh_size":
            regions = self.model.get("geometry", {}).get("regions", [])
            select = QSelect(
                ui_label="Region",
                ui_options=[{"label": r.get("name", "Region"), "value": r["id"]} for r in regions],
                ui_option_label="label", ui_option_value="value",
                ui_model_value=draft.get("region_id"), ui_emit_value=True,
                ui_map_options=True, ui_dense=True, ui_filled=True,
            )
            select.on_update_model_value(lambda event: self._set_editor_draft("region_id", event))
            body.extend([
                select,
                _button("Select region in viewport", "mdi-cursor-default-click-outline", self._toggle_editor_region_pick, color="primary" if draft.get("select_region_in_viewport") else None),
                _input("Target element size", draft.get("value", ""), lambda event: self._set_editor_draft("value", event), suffix="m"),
                Div("Choose a region from the list or use the viewport selection button. Regions without an entry use the global target size.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
            ])
        cancel = _button("Cancel", "mdi-close", self._cancel_editor)
        if kind == "region_material":
            confirm = _button("Apply", "mdi-check", lambda *a: self._commit_region_material(False), color="primary")
            actions = [cancel, confirm]
            if draft.get("material_choice") == "__custom__":
                actions.append(_button("Save + apply", "mdi-content-save-check-outline", lambda *a: self._commit_region_material(True), color="positive"))
        else:
            confirm = _button("Save", "mdi-check", self._commit_editor, color="primary")
            actions = [cancel, confirm]
        return [header, Div(*body, ui_style="display:flex; flex-direction:column; gap:10px; padding:12px 0;"), Div(*actions, ui_style="display:flex; flex-wrap:wrap; justify-content:flex-end; gap:8px; padding:10px 0; border-top:1px solid var(--border);")]


    @_history_tracked
    def _commit_editor(self, *args):
        editor = self._editor_drawer
        if editor is None:
            return
        kind, item_id, draft = editor["kind"], editor.get("item_id"), copy.deepcopy(editor["draft"])
        if kind == "region_mesh_size":
            region_id = draft.get("region_id")
            expression = str(draft.get("value", "")).strip()
            if not region_id or not expression:
                self._message("Choose a region and enter a target size before saving.", error=True)
                return
            self._set_region_mesh_size(region_id, expression)
        elif kind == "region_material":
            region_id = draft.get("region_id")
            region = next((item for item in self.model.get("geometry", {}).get("regions", []) if item.get("id") == region_id), None)
            if region is None:
                self._message("Choose a region before applying its material.", error=True)
                return
            choice = draft.get("material_choice")
            material_id = None
            if choice == "__custom__":
                material = copy.deepcopy(draft.get("custom_material") or {})
                material["id"] = material.get("id") or new_id("material")
                material["name"] = str(material.get("name", "")).strip()
                if not material["name"]:
                    self._message("Enter a material name before applying it.", error=True)
                    return
                properties = material.setdefault("properties", {})
                for key, default in (("relative_permeability", "1"), ("electrical_conductivity", "0"), ("youngs_modulus", "0"), ("poissons_ratio", "0.3"), ("density", "0")):
                    properties.setdefault(key, default)
                existing = next((index for index, item in enumerate(self.model.get("materials", [])) if item.get("id") == material["id"]), None)
                if existing is None:
                    self.model.setdefault("materials", []).append(copy.deepcopy(material))
                else:
                    self.model["materials"][existing] = copy.deepcopy(material)
                if editor.get("save_to_list"):
                    library_index = next((index for index, item in enumerate(self._user_materials) if item.get("id") == material["id"]), None)
                    if library_index is None:
                        self._user_materials.append(copy.deepcopy(material))
                    else:
                        self._user_materials[library_index] = copy.deepcopy(material)
                    self._save_user_materials()
                material_id = material["id"]
            elif choice:
                material = self._find_material(choice)
                if material is None:
                    self._message("Choose a material from the available list.", error=True)
                    return
                if not any(item.get("id") == choice for item in self.model.get("materials", [])):
                    self.model.setdefault("materials", []).append(copy.deepcopy(material))
                material_id = choice
            region["material_id"] = material_id
            region["mechanical"] = bool(draft.get("mechanical", False))
            self.selected_region_id = region_id
            self.selected_region_ids = [region_id]
            self.selected_edge_id = None
            self.selected_edge_ids = []
            self._selected_condition_id = None
            self.active_section = "materials"
            self.layout["active_section"] = "materials"
            self._rebuild_edges()
        elif kind == "material":
            draft["name"] = str(draft.get("name", "")).strip() or "Material"
            draft.setdefault("properties", {})
            if editor.get("is_new"):
                self.model.setdefault("materials", []).append(copy.deepcopy(draft))
                self._user_materials.append(copy.deepcopy(draft))
                self.selected_material_id = draft["id"]
            else:
                for index, material in enumerate(self.model.get("materials", [])):
                    if material.get("id") == item_id:
                        self.model["materials"][index] = copy.deepcopy(draft)
                        break
                library_index = next((i for i, material in enumerate(self._user_materials) if material.get("id") == item_id), None)
                if library_index is not None:
                    self._user_materials[library_index] = copy.deepcopy(draft)
            self._save_user_materials()
            self._refresh_model_tree()
        elif kind == "boundary":
            draft["name"] = str(draft.get("name", "")).strip() or "Boundary condition"
            if editor.get("is_new"):
                self.model.setdefault("boundary_conditions", []).append(copy.deepcopy(draft))
            else:
                for index, condition in enumerate(self.model.get("boundary_conditions", [])):
                    if condition.get("id") == item_id:
                        self.model["boundary_conditions"][index] = copy.deepcopy(draft)
                        break
            self.selected_boundary_id = draft["id"]
            self._selected_condition_id = draft["id"]
            self.active_section = "boundaries"
            self.layout["active_section"] = self.active_section
            self._refresh_model_tree()
            self._sync_canvas_selection()
        elif kind == "regional_load":
            name = str(draft.get("name", "")).strip()
            if not name:
                self._message("Enter a name for this source/load before saving.", error=True)
                return
            if any(item.get("id") != item_id and str(item.get("name", "")).strip().casefold() == name.casefold() for item in self.model.get("regional_loads", [])):
                self._message("Each source/load needs a unique name.", error=True)
                return
            existing = next((item for item in self.model.get("regional_loads", []) if item.get("id") == item_id), None)
            load = {
                "id": item_id or new_id("load"),
                "name": name,
                "region_ids": list(existing.get("region_ids", [])) if existing else [],
                "dc_current_density": str(draft.get("dc_current_density", "0") or "0").strip() or "0",
                "ac_current_density_real": str(draft.get("ac_current_density_real", "0") or "0").strip() or "0",
                "ac_current_density_imaginary": str(draft.get("ac_current_density_imaginary", "0") or "0").strip() or "0",
                "mechanical_body_force": {
                    component: str((draft.get("mechanical_body_force") or {}).get(component, "0") or "0").strip() or "0"
                    for component in ("r", "z")
                },
            }
            if existing is None:
                self.model.setdefault("regional_loads", []).append(load)
            else:
                self.model["regional_loads"][self.model["regional_loads"].index(existing)] = load
            self._sync_region_sources_from_loads()
            self.selected_source_id = load["id"]
            self.active_section = "sources"
            self.layout["active_section"] = "sources"
            self.selected_region_ids = self._assigned_regions_for_load(load["id"])
            self.selected_region_id = self.selected_region_ids[-1] if self.selected_region_ids else None
        self._editor_drawer = None
        self._render_inspector()
        self._refresh_model_tree()
        self._message("Changes saved.")

    def _find_material(self, material_id):
        return next((item for item in [*builtin_materials(), *self._user_materials, *self.model.get("materials", [])] if item.get("id") == material_id), None)

    def _open_region_material_editor(self, region_id):
        region = next((item for item in self.model.get("geometry", {}).get("regions", []) if item.get("id") == region_id), None)
        if region is None:
            return
        assigned = self._find_material(region.get("material_id"))
        builtin_ids = {item["id"] for item in builtin_materials()}
        custom_choice = assigned is not None and assigned.get("id") not in builtin_ids
        template = copy.deepcopy(assigned) if custom_choice else copy.deepcopy(builtin_materials()[0])
        if not custom_choice:
            template.update({"id": new_id("material"), "name": f"{region.get('name', 'Region')} material"})
        template.setdefault("properties", {})
        defaults = {"relative_permeability": "1", "electrical_conductivity": "0", "youngs_modulus": "0", "poissons_ratio": "0.3", "density": "0"}
        for key, value in defaults.items():
            template["properties"].setdefault(key, value)
        self.selected_region_id = region_id
        self.selected_region_ids = [region_id]
        self.selected_edge_id = None
        self.selected_edge_ids = []
        self._selected_condition_id = None
        self.active_section = "materials"
        self.layout["active_section"] = "materials"
        self._open_editor("region_material", item_id=region_id, draft={
            "region_id": region_id,
            "material_choice": "__custom__" if custom_choice else region.get("material_id"),
            "mechanical": bool(region.get("mechanical", False)),
            "custom_material": template,
        })
        self._sync_canvas_selection()

    def _set_editor_material_choice(self, event):
        if self._editor_drawer is None or self._editor_drawer.get("kind") != "region_material":
            return
        self._editor_drawer["draft"]["material_choice"] = getattr(event, "value", event)
        self._render_inspector()

    def _set_editor_custom_material_property(self, key, event):
        if self._editor_drawer is None or self._editor_drawer.get("kind") != "region_material":
            return
        material = self._editor_drawer["draft"].setdefault("custom_material", {})
        value = str(getattr(event, "value", event) or "").strip()
        if key == "name":
            material["name"] = value
        else:
            material.setdefault("properties", {})[key] = value

    def _commit_region_material(self, save_to_list=False, *args):
        if self._editor_drawer is not None:
            self._editor_drawer["save_to_list"] = bool(save_to_list)
            self._commit_editor()

    def _open_material_editor(self, material_id=None):
        current = next((m for m in self.model.get("materials", []) if m.get("id") == material_id), None)
        if current is None:
            template = copy.deepcopy(builtin_materials()[0])
            template.update({"id": new_id("material"), "name": f"Material {len(self._user_materials) + 1}"})
            material_id, draft, is_new = template["id"], template, True
        else:
            draft, is_new = copy.deepcopy(current), False
        self._open_editor("material", item_id=material_id, draft=draft, is_new=is_new)

    def _open_boundary_editor(self, boundary_id=None):
        current = next((item for item in self.model.get("boundary_conditions", []) if item.get("id") == boundary_id), None)
        if current is None:
            index = len(self.model.get("boundary_conditions", [])) + 1
            draft = {"id": new_id("boundary"), "name": f"Boundary {index}", "type": "natural"}
            is_new = True
        else:
            draft, is_new = copy.deepcopy(current), False
        self._open_editor("boundary", item_id=draft["id"], draft=draft, is_new=is_new)

    def _open_sources_editor(self, load_id=None):
        self.active_section = "sources"
        self.layout["active_section"] = "sources"
        self.set_sketch_tool("select", announce=False)
        current = next((item for item in self.model.get("regional_loads", []) if item.get("id") == load_id), None)
        if current is None:
            index = len(self.model.get("regional_loads", [])) + 1
            draft = {
                "id": new_id("load"),
                "name": f"Source/load {index}",
                "region_ids": [],
                "dc_current_density": "0",
                "ac_current_density_real": "0",
                "ac_current_density_imaginary": "0",
                "mechanical_body_force": {"r": "0", "z": "0"},
            }
            self._open_editor("regional_load", item_id=draft["id"], draft=draft, is_new=True)
        else:
            self._open_editor("regional_load", item_id=load_id, draft=copy.deepcopy(current), is_new=False)
    def _open_region_mesh_size_editor(self, region_id=None):
        regions = self.model.get("geometry", {}).get("regions", [])
        if not regions:
            self._message("Add a geometry region before defining region-wise sizing.", error=True)
            return
        region = next((r for r in regions if r.get("id") == region_id), None)
        value = self.model.get("mesh", {}).get("region_element_sizes", {}).get(region_id, "") if region else ""
        self._open_editor(
            "region_mesh_size",
            item_id=region_id,
            draft={"region_id": region_id, "value": value},
            is_new=region_id is None,
        )

    def _toggle_editor_region_pick(self, *args):
        if self._editor_drawer is None or self._editor_drawer.get("kind") != "region_mesh_size":
            return
        enabled = not bool(self._editor_drawer.get("select_region_in_viewport"))
        self._editor_drawer["select_region_in_viewport"] = enabled
        if enabled:
            self.set_sketch_tool("select", announce=False)
            self._message("Click a region in the viewport to choose its local mesh size.")
        else:
            self._message("Region selection finished.")
        self._sync_canvas_selection()
        self._render_inspector()
    def _accept_editor_viewport_region(self, region_id):
        editor = self._editor_drawer
        if editor is None:
            return
        region = next((r for r in self.model.get("geometry", {}).get("regions", []) if r.get("id") == region_id), None)
        if region is None:
            return
        editor["select_region_in_viewport"] = False
        editor["item_id"] = region_id
        draft = editor.setdefault("draft", {})
        draft["region_id"] = region_id
        if editor.get("kind") == "region_mesh_size" and not draft.get("value"):
            draft["value"] = self.model.get("mesh", {}).get("region_element_sizes", {}).get(region_id, "")
        self._message(f"Selected {region.get('name', 'region')} for {editor['kind'].replace('_', ' ')}.")
        self._sync_canvas_selection()
        self._render_inspector()

    @_history_tracked
    def _select_hp_targets_in_canvas_box(self, start, end, *, additive=False):
        project, unproject = self._canvas_projection()
        box = (min(start[0], end[0]), max(start[0], end[0]), min(start[1], end[1]), max(start[1], end[1]))
        window_selection = end[0] >= start[0]
        box_corners = [unproject((x, y)) for x in (box[0], box[1]) for y in (box[2], box[3])]
        region_ids = []
        edge_ids = []
        for region in self.model.get("geometry", {}).get("regions", []):
            vertices = region.get("vertices", [])
            points = [project(point) for point in vertices]
            if len(points) < 3:
                continue
            matches = (
                all(_point_in_box(point, box) for point in points)
                if window_selection
                else any(_point_in_box(point, box) for point in points)
                    or any(_segment_intersects_box(points[i], points[(i + 1) % len(points)], box) for i in range(len(points)))
                    or any(_point_in_or_on_polygon(corner, vertices) for corner in box_corners)
            )
            if matches:
                region_ids.append(region["id"])
        for edge in self.model.get("geometry", {}).get("edges", []):
            points = [project(point) for point in edge.get("vertices", [])]
            if len(points) != 2:
                continue
            if all(_point_in_box(point, box) for point in points) if window_selection else _segment_intersects_box(points[0], points[1], box):
                edge_ids.append(edge["id"])
        mesh = self.model.setdefault("mesh", {})
        if additive:
            region_ids = list(dict.fromkeys([*mesh.get("hp_region_ids", []), *region_ids]))
            edge_ids = list(dict.fromkeys([*mesh.get("hp_edge_ids", []), *edge_ids]))
        mesh["hp_region_ids"] = region_ids
        mesh["hp_edge_ids"] = edge_ids
        self.selected_region_ids = list(region_ids)
        self.selected_region_id = None
        self.selected_edge_ids = list(edge_ids)
        self.selected_edge_id = edge_ids[-1] if edge_ids else None
        self._message(f"Selected {len(region_ids)} hp-layer region(s) and {len(edge_ids)} boundary/boundaries.")
        self._sync_canvas_selection()
        self._render_inspector()

    def _toggle_hp_layer_selection(self, *args):
        self.hp_target_selection_active = not self.hp_target_selection_active
        self.active_section = "mesh"
        self.layout["active_section"] = "mesh"
        self._selected_condition_id = None
        if self.hp_target_selection_active:
            self.set_sketch_tool("select", announce=False)
            mesh = self.model.get("mesh", {})
            self.selected_region_ids = list(mesh.get("hp_region_ids", []))
            self.selected_edge_ids = list(mesh.get("hp_edge_ids", []))
            self.selected_region_id = None
            self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None
            self._message("HP layer selection active. Click or marquee regions and edges in the viewport.")
        else:
            self._message("HP layer selection finished.")
        self._render_inspector()
        self._sync_canvas_selection()

    def _toggle_hp_region_target(self, region_id):
        mesh = self.model.setdefault("mesh", {})
        targets = list(mesh.get("hp_region_ids", []))
        self._set_hp_layer_target("hp_region_ids", region_id, region_id not in targets)
        self.selected_region_ids = list(mesh.get("hp_region_ids", []))
        self.selected_region_id = None
        self.selected_edge_ids = list(mesh.get("hp_edge_ids", []))
        self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None
        self._sync_canvas_selection()
        self._render_inspector()

    def _toggle_hp_edge_target(self, edge_id):
        mesh = self.model.setdefault("mesh", {})
        targets = list(mesh.get("hp_edge_ids", []))
        self._set_hp_layer_target("hp_edge_ids", edge_id, edge_id not in targets)
        self.selected_region_ids = list(mesh.get("hp_region_ids", []))
        self.selected_region_id = None
        self.selected_edge_ids = list(mesh.get("hp_edge_ids", []))
        self.selected_edge_id = self.selected_edge_ids[-1] if self.selected_edge_ids else None
        self._sync_canvas_selection()
        self._render_inspector()

    def _clear_hp_layer_targets(self, *args):
        self._set_mesh("hp_region_ids", [])
        self._set_mesh("hp_edge_ids", [])
        self.selected_region_ids = []
        self.selected_edge_ids = []
        self.selected_region_id = self.selected_edge_id = None
        self._sync_canvas_selection()
        self._render_inspector()

    def _material_properties(self):
        regions = self.model.get("geometry", {}).get("regions", [])
        children = [_section_title("Materials", "Assign a material and mechanical participation to each region.")]
        selected_ids = self._selected_region_ids_live()
        if len(selected_ids) > 1:
            selected_regions = [region for region in regions if region.get("id") in set(selected_ids)]
            assigned_materials = {region.get("material_id") for region in selected_regions}
            default_material = next(iter(assigned_materials)) if len(assigned_materials) == 1 else "__keep__"
            material_options = [
                {"label": "Keep each region's current material", "value": "__keep__"},
                *self._material_options(),
            ]
            material_select = QSelect(
                ui_label=f"Material for {len(selected_regions)} selected regions",
                ui_options=material_options,
                ui_option_label="label", ui_option_value="value",
                ui_model_value=default_material, ui_emit_value=True,
                ui_map_options=True, ui_dense=True, ui_filled=True,
            )
            material_select.on_update_model_value(
                lambda event: setattr(self, "_bulk_material_choice", getattr(event, "value", event))
            )
            mechanics = {bool(region.get("mechanical", False)) for region in selected_regions}
            default_mechanics = str(next(iter(mechanics))).lower() if len(mechanics) == 1 else "keep"
            self._bulk_material_choice = default_material
            self._bulk_mechanical_choice = default_mechanics
            mechanics_select = QSelect(
                ui_label="Mechanical participation",
                ui_options=[
                    {"label": "Keep each region's current setting", "value": "keep"},
                    {"label": "Mechanical", "value": "true"},
                    {"label": "Not mechanical", "value": "false"},
                ],
                ui_option_label="label", ui_option_value="value",
                ui_model_value=default_mechanics, ui_emit_value=True,
                ui_map_options=True, ui_dense=True, ui_filled=True,
            )
            mechanics_select.on_update_model_value(
                lambda event: setattr(self, "_bulk_mechanical_choice", getattr(event, "value", event))
            )
            children.extend([
                Div(
                    f"Apply one material and optional mechanical setting to {len(selected_regions)} selected regions.",
                    ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted); padding:8px; border:1px solid var(--border); border-radius:6px;",
                ),
                material_select,
                mechanics_select,
                _button("Apply to selected regions", "mdi-check", self._apply_bulk_region_material, color="primary"),
            ])
        if not regions:
            return children + [
                Div("Create geometry before assigning materials.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
                _button("Open Geometry", "mdi-vector-square-outline", lambda *a: self.select_section("geometry")),
            ]
        known = {item.get("id"): item for item in [*builtin_materials(), *self._user_materials, *self.model.get("materials", [])]}
        for region in regions:
            material = known.get(region.get("material_id"))
            unassigned = material is None
            material_label = material.get("name", "Material") if material else "Material not assigned"
            details = Div(
                region.get("name", "Region"),
                Div(material_label, ui_style=("font-size:10px; margin-top:2px; color:" + ("#795b00" if unassigned else "var(--fg-muted)") + ";")),
                ui_style="min-width:0; flex:1 1 auto; text-align:left;",
            )
            open_button = QBtn(
                QIcon(ui_name="mdi-cube-outline"), details,
                ui_flat=True, ui_no_caps=True, ui_dense=True,
                ui_class="mmfem-material-region-row",
                ui_style="flex:1 1 auto; min-width:0; justify-content:flex-start; padding:6px 8px;",
            )
            open_button._props["aria-label"] = f"Assign material to {region.get('name', 'region')}"
            open_button.on_click(lambda *a, rid=region["id"]: self._open_region_material_editor(rid))
            edit_button = _icon_button("mdi-chevron-right", f"Edit material for {region.get('name', 'region')}", lambda rid=region["id"]: self._open_region_material_editor(rid))
            background = "#fff4cc" if unassigned else "var(--surface)"
            border = "#e5c868" if unassigned else "var(--border)"
            children.append(Div(
                open_button, edit_button,
                ui_style=f"display:flex; align-items:center; gap:4px; padding:3px; border:1px solid {border}; border-radius:7px; background:{background}; margin:5px 10px;",
            ))
        return children

    @_history_tracked
    def _apply_bulk_region_material(self, *args):
        selected_ids = self._selected_region_ids_live()
        if len(selected_ids) < 2:
            self._message("Select at least two regions before applying shared properties.", error=True)
            return False
        material_choice = getattr(self, "_bulk_material_choice", "__keep__")
        mechanical_choice = getattr(self, "_bulk_mechanical_choice", "keep")
        material = None
        if material_choice != "__keep__" and material_choice is not None:
            material = self._find_material(material_choice)
            if material is None:
                self._message("Choose a material from the available list.", error=True)
                return False
            if not any(item.get("id") == material_choice for item in self.model.get("materials", [])):
                self.model.setdefault("materials", []).append(copy.deepcopy(material))
        if material_choice == "__keep__" and mechanical_choice == "keep":
            self._message("Choose a material or mechanical participation change to apply.", error=True)
            return False
        regions = [
            region for region in self.model.get("geometry", {}).get("regions", [])
            if region.get("id") in set(selected_ids)
        ]
        for region in regions:
            if material_choice != "__keep__":
                region["material_id"] = material_choice
            if mechanical_choice != "keep":
                region["mechanical"] = mechanical_choice == "true"
        if mechanical_choice != "keep":
            self._rebuild_edges()
        self._refresh_model_tree()
        self._render_inspector()
        self._sync_canvas_selection()
        self.render_canvas()
        self._message(f"Updated shared properties for {len(regions)} regions.")
        return True

    def _boundary_properties(self):
        children = [_section_title("Boundary conditions", "Click a condition to make it active, then assign it directly in the viewport.")]
        self._boundary_selection_summary = Div(ui_style="display:flex; flex-direction:column; gap:4px; padding:9px 10px; border:1px solid var(--border); border-radius:6px; background:var(--surface-subtle, #f7f9fb);")
        children.append(_button("Add boundary condition", "mdi-plus", self.add_boundary, color="primary"))
        children.append(self._boundary_selection_summary)
        self._sync_boundary_selection_controls()
        conditions = self.model.get("boundary_conditions", [])
        if not conditions:
            children.append(Div("No boundary conditions yet. Add one, then activate it and click regions or edges in the viewport.", ui_style="font-size:12px; color:var(--fg-muted); line-height:1.45;"))
        for condition in conditions:
            assigned = sum(condition["id"] in _edge_condition_ids(edge) for edge in self.model.get("geometry", {}).get("edges", []))
            select = _button(
                f"{condition.get('name', 'Condition')}  ·  {assigned} edges",
                "mdi-vector-link",
                lambda *a, bid=condition["id"]: self.select_boundary(bid),
                color="primary" if condition.get("id") == self.selected_boundary_id else None,
                style="flex:1 1 auto; min-width:0; justify-content:flex-start; text-align:left;",
                align="left",
            )
            actions = []
            if not condition.get("automatic_for_axis") and not condition.get("automatic_for_mechanics"):
                actions.append(_icon_button("mdi-pencil-outline", f"Edit {condition.get('name', 'condition')}", lambda bid=condition["id"]: self._open_boundary_editor(bid)))
                actions.append(_icon_button("mdi-delete-outline", f"Delete {condition.get('name', 'condition')}", lambda bid=condition["id"]: self.remove_boundary(bid), color="negative"))
            children.append(Div(select, *actions, ui_style="display:flex; align-items:center; gap:4px; min-width:0;"))
            if condition.get("automatic_for_axis") or condition.get("automatic_for_mechanics"):
                children.append(Div("Automatic condition", ui_style="font-size:10px; color:var(--fg-muted); margin:-3px 0 4px 34px;"))
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
        mechanics = physics.get("mechanics", {})
        if isinstance(mechanics, dict) and ("rayleigh_alpha" in mechanics or "rayleigh_beta" in mechanics):
            children.extend([
                Div("RAYLEIGH DAMPING", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:10px;"),
                _input("Mass coefficient (alpha)", mechanics.get("rayleigh_alpha", "0"), lambda event: self._set_physics("mechanics", "rayleigh_alpha", event.value), suffix="1/s"),
                _input("Stiffness coefficient (beta)", mechanics.get("rayleigh_beta", "0"), lambda event: self._set_physics("mechanics", "rayleigh_beta", event.value), suffix="s"),
            ])
        if physics.get("harmonic_electromagnetic", {}).get("enabled", False):
            children.extend([
                Div("Set frequency points and sweep values in Study.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted); padding-top:8px;"),
                _button("Configure study frequencies", "mdi-play-box-outline", lambda *a: self.select_section("studies")),
            ])
        else:
            children.append(Div("Static electromagnetic studies do not require a frequency.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted); padding-top:8px;"))
        children.append(Div("Regional current densities and mechanical body forces are configured separately under Sources & loads.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"))
        children.append(Div("Coupling requires DC magnetic, time-harmonic EM and mechanics to be enabled.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted); padding-top:5px;"))
        return children

    def _sources_properties(self):
        loads = self.model.get("regional_loads", [])
        children = [_section_title("Sources & loads", "Define named loads, select one, then click regions in the viewport to assign it.")]
        children.append(_button("Add source/load", "mdi-plus", self._open_sources_editor, color="primary"))
        if not loads:
            children.append(Div("No regional loads defined yet.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"))
            return children
        for load in loads:
            selected = load.get("id") == self.selected_source_id
            count = len(self._assigned_regions_for_load(load.get("id")))
            label = f"{load.get('name', 'Source/load')} · {count} region{'s' if count != 1 else ''}"
            children.append(Div(
                _button(label, "mdi-flash-outline", lambda *a, lid=load.get("id"): self.select_regional_load(lid), color="primary" if selected else None, style="flex:1; min-width:0;"),
                _icon_button("mdi-pencil-outline", f"Edit {load.get('name', 'source/load')}", lambda *a, lid=load.get("id"): self._open_sources_editor(lid)),
                _icon_button("mdi-delete-outline", f"Delete {load.get('name', 'source/load')}", lambda *a, lid=load.get("id"): self.delete_regional_load(lid), color="negative"),
                ui_style="display:flex; align-items:center; gap:4px; padding:4px; border:1px solid " + ("var(--primary);" if selected else "var(--border);") + " border-radius:7px;",
            ))
        if self.selected_source_id:
            assigned = self._assigned_regions_for_load(self.selected_source_id)
            names = [region.get("name", "Region") for region in self.model.get("geometry", {}).get("regions", []) if region.get("id") in assigned]
            children.append(Div(
                "Click a region in the viewport to add or remove it from this load. Assigned: " + (", ".join(names) if names else "none"),
                ui_style="font-size:11px; line-height:1.45; color:var(--fg-muted); padding-top:4px;",
            ))
        return children
    def _mesh_properties(self):
        mesh = self.model["mesh"]
        order = QSelect(ui_label="Polynomial order", ui_options=[1, 2, 3, 4, 5, 6], ui_model_value=mesh["polynomial_order"], ui_dense=True, ui_filled=True)
        order.on_update_model_value(lambda event: self._set_mesh("polynomial_order", int(event.value)))
        curve_order = QSelect(
            ui_label="Curved edge order",
            ui_options=[1, 2, 3, 4, 5, 6],
            ui_model_value=mesh.get("curve_order", mesh["polynomial_order"]),
            ui_dense=True,
            ui_filled=True,
        )
        curve_order.on_update_model_value(
            lambda event: self._set_mesh("curve_order", int(event.value))
        )
        children = [
            _section_title("Mesh", "Control global density, local overrides, and geometric hp layers."),
            _input("Target element size", mesh["element_size"], lambda event: self._set_mesh("element_size", event.value), suffix="m"),
            order,
            curve_order,
            Div(
                "1 keeps mesh edges straight. Higher orders follow the sketch curves more closely; this is separate from polynomial order.",
                ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);",
            ),
            Div("REGION-WISE ELEMENT SIZING", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:7px;"),
            Div("Only regions with a local override are listed. Other regions use the global target.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
            _button("Add region-wise element sizing", "mdi-plus", lambda *args: self._open_region_mesh_size_editor(), color="primary"),
        ]
        regions = self.model.get("geometry", {}).get("regions", [])
        names = {region.get("id"): region.get("name", "Region") for region in regions}
        region_sizes = mesh.setdefault("region_element_sizes", {})
        for region_id, value in region_sizes.items():
            if region_id not in names:
                continue
            edit = _icon_button("mdi-pencil-outline", f"Edit sizing for {names[region_id]}", lambda rid=region_id: self._open_region_mesh_size_editor(rid))
            delete = _icon_button("mdi-delete-outline", f"Remove sizing for {names[region_id]}", lambda rid=region_id: self._remove_region_mesh_size(rid), color="negative")
            children.append(Div(
                Div(names[region_id], Div(f"{value} m", ui_style="font-size:10px; color:var(--fg-muted);"), ui_style="min-width:0;"),
                edit, delete,
                ui_style="display:flex; align-items:center; gap:3px; padding:5px 6px; border:1px solid var(--border); border-radius:6px; min-width:0;",
            ))
        children.extend([
            Div("GEOMETRIC HP LAYERS", ui_style="font-size:10px; font-weight:700; letter-spacing:.07em; color:var(--fg-muted); padding-top:12px;"),
            Div("Choose a layer count and grading, then select targets directly in the viewport.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted);"),
        ])
        layer_count = QSelect(ui_label="Number of hp layers", ui_options=list(range(0, 9)), ui_model_value=mesh.get("hp_layers", 0), ui_dense=True, ui_filled=True)
        layer_count.on_update_model_value(lambda event: self._set_mesh("hp_layers", int(event.value)))
        grading = _input("Layer grading factor", mesh.get("hp_grading_factor", 0.3), lambda event: self._set_mesh("hp_grading_factor", event.value), number=True, hint="Between 0 and 1; smaller values make tighter layers")
        children.extend([layer_count, grading])
        targets = list(mesh.get("hp_region_ids", [])) + list(mesh.get("hp_edge_ids", []))
        if self.hp_target_selection_active:
            children.append(Div("Selection mode is active. Click a region or edge, or drag a marquee across targets.", ui_style="font-size:11px; line-height:1.4; color:var(--primary); padding:5px 0;"))
        children.append(_button(
            "Finish selection" if self.hp_target_selection_active else "Select regions and edges",
            "mdi-cursor-default-click-outline",
            self._toggle_hp_layer_selection,
            color="primary" if self.hp_target_selection_active else None,
        ))
        if targets:
            children.append(Div("Selected refinement targets", ui_style="font-size:11px; font-weight:600; padding-top:4px;"))
            edge_names = {edge.get("id"): edge.get("name", "Boundary") for edge in self.model.get("geometry", {}).get("edges", [])}
            for region_id in mesh.get("hp_region_ids", []):
                label = names.get(region_id, "Removed region")
                children.append(Div(QIcon(ui_name="mdi-vector-square-outline"), label, ui_style="display:flex; align-items:center; gap:7px; padding:4px 6px; background:var(--surface-subtle,#f7f9fb); border-radius:5px;"))
            for edge_id in mesh.get("hp_edge_ids", []):
                label = edge_names.get(edge_id, "Removed boundary")
                children.append(Div(QIcon(ui_name="mdi-vector-line"), label, ui_style="display:flex; align-items:center; gap:7px; padding:4px 6px; background:var(--surface-subtle,#f7f9fb); border-radius:5px;"))
            children.append(_button("Clear all targets", "mdi-eraser", self._clear_hp_layer_targets))
        else:
            children.append(Div("No hp layer targets selected.", ui_style="font-size:11px; color:var(--fg-muted);"))
        children.append(Div("The sketch must contain valid, non-overlapping regions before mesh generation.", ui_style="font-size:11px; line-height:1.4; color:var(--fg-muted); padding-top:5px;"))
        return children

    def _remove_region_mesh_size(self, region_id):
        self._set_region_mesh_size(region_id, "")
        self._render_inspector()

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
        harmonic_enabled = bool(
            self.model.get("physics", {}).get("harmonic_electromagnetic", {}).get("enabled", False)
        )
        description = (
            "One DC + time-harmonic study; enter a single frequency or a comma-separated sweep."
            if harmonic_enabled else
            "Static electromagnetic study; no frequency is required."
        )
        children = [_section_title("Study", description)]
        for study in studies:
            controls = [
                _input("Study name", study.get("name", ""), lambda event, sid=study["id"]: self._set_study(sid, "name", event.value)),
            ]
            if harmonic_enabled:
                controls.append(
                    _input("Frequency points (comma separated)", ", ".join(str(v) for v in study.get("frequency_hz", [])), lambda event, sid=study["id"]: self._set_study_frequencies(sid, event.value), suffix="Hz")
                )
            children.append(Div(
                *controls,
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

    def _canvas_snap_step(self):
        """Return the sketch snap interval for a five-by-five grid subdivision."""
        return self._canvas_grid_step() / _CANVAS_GRID_SUBDIVISIONS

    def _snap_canvas_point(self, point):
        if not self.snap_to_grid:
            return point
        project, unproject = self._canvas_projection()
        radial, axial = unproject(point)
        step = self._canvas_snap_step()
        radial = max(0.0, round(radial / step) * step)
        axial = round(axial / step) * step
        return project((radial, axial))

    def _render_canvas_after_interaction(self):
        """Flush a committed interaction redraw in deterministic frontend order."""
        with self._batch_frontend_updates():
            self.render_canvas()

    def _prepare_canvas_view_commit_snapshot(self):
        """Hold the visible camera frame while the committed SVG is patched."""
        try:
            return bool(self.js.eval(
                "(() => { const begin = window.__ngsolveBeginSketchViewCommit; "
                "return begin ? begin() : false; })()"
            ))
        except Exception:
            # Standalone tests and non-browser frontends do not expose JS.
            return False

    def _schedule_canvas_interaction_reset(self):
        """Reset browser transforms after the matching SVG render reaches the DOM."""
        try:
            version = int(getattr(self, "_canvas_view_render_version", 0))
            self.js.eval(
                "(() => { const reset = window.__ngsolveResetSketchPreview; "
                "if (reset) reset("
                f"{version}); }})()"
            )
        except Exception:
            # Standalone tests and non-browser frontends do not expose JS.
            pass

    def _canvas_region_path_data(self, region, project=None):
        if project is None:
            project, _ = self._canvas_projection()

        geometry = self.model.get("geometry", {})
        edges_by_id = {
            edge.get("id"): edge
            for edge in geometry.get("edges", [])
            if isinstance(edge, dict) and edge.get("id")
        }

        def contour_points(item):
            # Use the same sampled curve records used to draw selectable edges.
            # Region vertices are a topology polygon and can be much coarser
            # than an arc/spline boundary; drawing them directly creates
            # straight chords across curved faces.
            loops = item.get("boundary_loops", [])
            outer = next(
                (loop for loop in loops if isinstance(loop, dict) and loop.get("role") == "outer"),
                None,
            )
            references = outer.get("edges", []) if isinstance(outer, dict) else []
            points = []
            for reference in references:
                if not isinstance(reference, dict):
                    continue
                edge = edges_by_id.get(reference.get("edge_id"))
                if not isinstance(edge, dict):
                    continue
                curve = edge.get("curve", {})
                samples = curve.get("sampled_points") if isinstance(curve, dict) else None
                samples = samples if isinstance(samples, list) and len(samples) >= 2 else edge.get("vertices", [])
                try:
                    segment = [(float(point[0]), float(point[1])) for point in samples]
                except (TypeError, ValueError, IndexError, OverflowError):
                    continue
                if len(segment) < 2:
                    continue
                if not reference.get("forward", True):
                    segment.reverse()
                if points and math.dist(points[-1], segment[0]) <= 1e-12:
                    segment = segment[1:]
                points.extend(segment)
            if len(points) > 1 and math.dist(points[0], points[-1]) <= 1e-12:
                points.pop()
            return points if len(points) >= 3 else item.get("vertices", [])

        def subpath(points):
            projected = [project(point) for point in points]
            if not projected:
                return ""
            commands = [f"M{projected[0][0]:.3f},{projected[0][1]:.3f}"]
            commands.extend(f"L{x:.3f},{y:.3f}" for x, y in projected[1:])
            return " ".join(commands) + " Z"

        regions = geometry.get("regions", [])
        child_loops = [
            contour_points(child) for child in regions
            if child.get("parent_id") == region.get("id")
        ]
        void_loops = [
            contour_points(void) for void in geometry.get("voids", [])
            if void.get("parent_id") == region.get("id")
        ]
        return " ".join(
            item for item in [
                subpath(contour_points(region)),
                *(subpath(loop) for loop in child_loops),
                *(subpath(loop) for loop in void_loops),
            ] if item
        )

    def render_canvas(self):
        width, height = self._canvas_width, self._canvas_height
        _update_svg_props(self._canvas, **{"viewBox": f"0 0 {width} {height}"})
        _update_svg_props(self._canvas_background, width=width, height=height)
        _update_svg_props(
            self._canvas_grid_layer,
            display="inline" if self._canvas_background_visible else "none",
        )
        plot = self._canvas_plot_bounds()
        xy, unproject = self._canvas_projection()
        regions = self.model["geometry"]["regions"]
        lower_left = unproject((plot[0], plot[3]))
        upper_right = unproject((plot[2], plot[1]))
        grid_step = self._canvas_grid_step()
        radial_ticks = self._ticks_for_step(max(0.0, lower_left[0]), max(0.0, upper_right[0]), grid_step)
        axial_ticks = self._ticks_for_step(lower_left[1], upper_right[1], grid_step)
        snap_step = self._canvas_snap_step()
        radial_subticks = self._ticks_for_step(max(0.0, lower_left[0]), max(0.0, upper_right[0]), snap_step)
        axial_subticks = self._ticks_for_step(lower_left[1], upper_right[1], snap_step)
        grid_signature = (
            tuple(plot), tuple(lower_left), tuple(upper_right), grid_step, snap_step,
            tuple(radial_ticks), tuple(axial_ticks), tuple(radial_subticks), tuple(axial_subticks),
        )
        grid_text_style = "pointer-events:none; user-select:none; -webkit-user-select:none;"
        grid_children = []
        scene_children = []
        # Keep the existing labelled grid cadence, then add four quieter lines
        # between each pair of labelled lines for a five-by-five sketch grid.
        for value in radial_subticks:
            multiple = value / grid_step
            if abs(multiple - round(multiple)) <= 1e-8 or abs(value) <= snap_step * 1e-10:
                continue
            x = xy((value, 0))[0]
            grid_children.append(_svg("line", x1=x, y1=plot[1], x2=x, y2=plot[3], stroke="var(--grid-minor, #e8edf2)", stroke_width="0.75"))
        for value in axial_subticks:
            multiple = value / grid_step
            if abs(multiple - round(multiple)) <= 1e-8 or abs(value) <= snap_step * 1e-10:
                continue
            y = xy((0, value))[1]
            grid_children.append(_svg("line", x1=plot[0], y1=y, x2=plot[2], y2=y, stroke="var(--grid-minor, #e8edf2)", stroke_width="0.75"))
        for value in radial_ticks:
            if abs(value) <= grid_step * 1e-10:
                continue
            x = xy((value, 0))[0]
            grid_children.append(_svg("line", x1=x, y1=plot[1], x2=x, y2=plot[3], stroke="var(--border, #d9dfe7)", stroke_width="1"))
            label_x = min(max(x, plot[0] + 28), plot[2] - 28)
            grid_children.append(_svg("text", x=label_x, y=plot[3] - 5, fill="var(--fg-muted, #697586)", font_size="16", text_anchor="middle", style=grid_text_style, children=f"{value:.5g}"))
        for value in axial_ticks:
            if abs(value) <= grid_step * 1e-10:
                continue
            y = xy((0, value))[1]
            grid_children.append(_svg("line", x1=plot[0], y1=y, x2=plot[2], y2=y, stroke="var(--border, #d9dfe7)", stroke_width="1"))
            label_y = min(max(y + 4, plot[1] + 12), plot[3] - 3)
            grid_children.append(_svg("text", x=plot[0] + 6, y=label_y, fill="var(--fg-muted, #697586)", font_size="16", text_anchor="start", style=grid_text_style, children=f"{value:.5g}"))
        if lower_left[0] <= 0 <= upper_right[0]:
            xaxis = xy((0, 0))[0]
            grid_children.append(_svg("line", x1=xaxis, y1=plot[1], x2=xaxis, y2=plot[3], stroke="#557187", stroke_width="2", stroke_dasharray="6 4"))
            grid_children.append(_svg("text", x=xaxis + 7, y=plot[1] + 15, fill="#405e75", font_size="12", font_weight="600", style=grid_text_style, children="Axis of rotation  ·  r = 0"))
        if lower_left[1] <= 0 <= upper_right[1]:
            zaxis = xy((0, 0))[1]
            grid_children.append(_svg("line", x1=plot[0], y1=zaxis, x2=plot[2], y2=zaxis, stroke="#557187", stroke_width="1.5"))
            grid_children.append(_svg("text", x=plot[2] - 4, y=zaxis - 5, fill="#405e75", font_size="12", text_anchor="end", style=grid_text_style, children="z = 0"))
        grid_children.append(_svg("text", x=plot[2] - 6, y=height - 8, fill="var(--fg-muted, #697586)", font_size="12", text_anchor="end", style=grid_text_style, children="r  [m]"))
        grid_children.append(_svg("text", x=plot[0] + 8, y=plot[1] + 15, fill="var(--fg-muted, #697586)", font_size="12", style=grid_text_style, children="z  [m]"))

        material_index = {item["id"]: index for index, item in enumerate(self.model["materials"])}
        region_cursor = "pointer" if self.sketch_tool == "select" else "crosshair"
        live_region_ids = set()
        for region in regions:
            region_id = region["id"]
            live_region_ids.add(region_id)
            path_data = self._canvas_region_path_data(region, xy)
            color = _MATERIAL_COLORS[material_index.get(region.get("material_id"), 0) % len(_MATERIAL_COLORS)]
            selected = region_id in set(self._selected_region_ids_live())
            props = {
                "d": path_data,
                "id": f"solve-sketch-region-{region_id}",
                "fill": color,
                "display": "inline" if self._canvas_geometry_visible else "none",
                "fill_opacity": "0.33" if not selected else "0.48",
                "stroke": "none",
                "stroke_width": "0",
                "data_sketch_region_id": region_id,
                "data_sketch_region_role": "shape",
                "data_sketch_region_selected": "true" if selected else "false",
                "style": f"cursor:{region_cursor}; fill-rule:evenodd; outline:none;",
            }
            shape = self._canvas_region_nodes.get(region_id)
            if shape is None:
                shape = _svg("path", **props)
                shape.on("click", lambda event, rid=region_id: self._on_canvas_region_click(rid, event))
                self._canvas_region_nodes[region_id] = shape
            else:
                _update_svg_props(shape, **props)
            scene_children.append(shape)
        for region_id in self._canvas_region_nodes.keys() - live_region_ids:
            del self._canvas_region_nodes[region_id]
        self._canvas_host.ui_children = [
            self._canvas,
            self._canvas_resize_observer,
            self._canvas_context_menu,
        ]

        if self.mesh_preview_visible and self.mesh_preview_edges:
            mesh_paths = []
            for edge_points in self.mesh_preview_edges:
                projected = [xy(point) for point in edge_points]
                if len(projected) < 2:
                    continue
                path = f"M{projected[0][0]:.2f},{projected[0][1]:.2f}"
                path += "".join(
                    f" L{x:.2f},{y:.2f}" for x, y in projected[1:]
                )
                mesh_paths.append(path)
            mesh_path = " ".join(mesh_paths)
            _update_svg_props(
                self._canvas_mesh_path,
                d=mesh_path,
                display="inline",
            )
            scene_children.append(self._canvas_mesh_path)
        else:
            _update_svg_props(self._canvas_mesh_path, d="", display="none")

        region_for_edge = {
            edge_id: region["id"]
            for region in regions
            for edge_id in region.get("edge_ids", [])
        }
        highlighted_edges = {
            edge["id"] for edge in self.model["geometry"].get("edges", [])
            if self._selected_condition_id in _edge_condition_ids(edge)
        }
        live_edge_ids = set()
        for edge in self.model["geometry"].get("edges", []):
            edge_id = edge["id"]
            live_edge_ids.add(edge_id)
            start, end = edge["vertices"]
            curve = edge.get("curve", {})
            curve_points = curve.get("sampled_points") if isinstance(curve, dict) else None
            display_points = curve_points if isinstance(curve_points, list) and len(curve_points) >= 2 else [start, end]
            projected = [xy(point) for point in display_points]
            edge_path = "M" + f"{projected[0][0]:.3f},{projected[0][1]:.3f}"
            edge_path += " ".join(f"L{x:.3f},{y:.3f}" for x, y in projected[1:])
            region_id = region_for_edge.get(edge_id)
            selected = edge_id in self.selected_edge_ids or region_id in set(self._selected_region_ids_live())
            condition_highlight = edge_id in highlighted_edges
            edge_color = "#ff8738" if condition_highlight else ("#0877b9" if selected else "#263746")
            curve_type = str(curve.get("type", "line")) if isinstance(curve, dict) else "line"
            curve_center = curve.get("center") if isinstance(curve, dict) else None
            curve_center_x = curve_center[0] if isinstance(curve_center, (list, tuple)) and len(curve_center) >= 2 else None
            curve_center_y = curve_center[1] if isinstance(curve_center, (list, tuple)) and len(curve_center) >= 2 else None
            visible_props = {
                "d": edge_path,
                "fill": "none",
                "stroke": edge_color,
                "stroke_width": "3.4" if condition_highlight else ("2.2" if selected else "1.6"),
                "display": "inline" if self._canvas_geometry_visible else "none",
                "style": "pointer-events:none;" + (" animation:mmfem-condition-edge-pulse 2.8s ease-in-out infinite;" if condition_highlight else ""),
                "data_sketch_edge_id": edge_id,
                "data_sketch_edge_role": "visible",
                "data_sketch_curve_type": curve_type,
                "data_sketch_curve_center_x": curve_center_x,
                "data_sketch_curve_center_y": curve_center_y,
                "data_sketch_selected": str(selected).lower(),
                "data_sketch_condition_highlight": str(edge_id in highlighted_edges).lower(),
                "data_sketch_region_id": region_id,
                "style": "pointer-events:none;",
            }
            hit_props = {
                "d": edge_path,
                "fill": "none",
                "stroke": "transparent", "stroke_width": "12",
                "display": "inline" if self._canvas_geometry_visible else "none",
                "data_sketch_edge_id": edge_id,
                "data_sketch_edge_role": "hit",
                "data_sketch_region_id": region_id,
                "style": f"cursor:{region_cursor}; pointer-events:stroke;",
            }
            nodes = self._canvas_edge_nodes.get(edge_id)
            if nodes is None:
                visible = _svg("path", **visible_props)
                hit = _svg("path", **hit_props)
                hit.on("click", lambda event, eid=edge_id: self._on_canvas_edge_click(eid, event))
                nodes = (visible, hit)
                self._canvas_edge_nodes[edge_id] = nodes
            else:
                visible, hit = nodes
                _update_svg_props(visible, **visible_props)
                _update_svg_props(hit, **hit_props)
            scene_children.extend([visible, hit])
        for edge_id in self._canvas_edge_nodes.keys() - live_edge_ids:
            del self._canvas_edge_nodes[edge_id]

        grid_changed = grid_signature != self._canvas_grid_signature
        if grid_changed:
            self._canvas_grid.ui_children = grid_children
            self._canvas_grid_signature = grid_signature
        scene_component_ids = tuple(id(component) for component in scene_children)
        if scene_component_ids != self._canvas_scene_component_ids:
            self._canvas_scene.ui_children = scene_children
            self._canvas_scene_component_ids = scene_component_ids
        self._render_canvas_dimensions()
        self._sync_canvas_interaction_settings()
        self._canvas_view_render_version += 1
        render_version = str(self._canvas_view_render_version)
        if grid_changed:
            self._canvas_grid_render_version = self._canvas_view_render_version
            _update_svg_props(
                self._canvas_grid,
                data_sketch_render_version=str(self._canvas_grid_render_version),
            )
        _update_svg_props(self._canvas_scene, data_sketch_render_version=render_version)
        _update_svg_props(self._canvas_dimensions, data_sketch_render_version=render_version)
        _update_svg_props(
            self._canvas,
            data_sketch_render_version=render_version,
            data_sketch_grid_revision=str(self._canvas_grid_render_version),
            data_sketch_scene_revision=render_version,
            data_sketch_dimensions_revision=render_version,
        )
        self._schedule_canvas_interaction_reset()

    def _build_3d_preview_component(self):
        """Revolve the current meridian regions for the modal read-only preview."""
        from netgen.occ import (
            Axis, Circle, Dir, Face, MakePolygon, OCCGeometry, Pnt,
            Revolve, Vec, Vertex, Wire,
        )
        from ngapp.components import WebgpuComponent
        from ngsolve_webgpu import GeometryRenderer
        from webgpu import CoordinateAxes

        regions = self.model.get("geometry", {}).get("regions", [])
        live_region_ids = {region.get("id") for region in regions if region.get("id")}
        self._preview_region_visibility = {
            region_id: self._preview_region_visibility.get(region_id, "opaque")
            for region_id in live_region_ids
        }
        material_names = {
            material.get("id"): material.get("name", "")
            for material in self.model.get("materials", [])
        }
        revolved = {}
        for region in regions:
            vertices = region.get("vertices", [])
            if len(vertices) < 3:
                raise ValueError(f"Region '{region.get('name', 'unnamed')}' needs at least three vertices.")
            shape = region.get("shape", {})
            if shape.get("type") == "circle":
                # The editable sketch stores circles as a 48-sided polygon for
                # the solver. Revolving that polygon creates dozens of tiny
                # OCC edges, which render as distracting rings on the torus.
                # Use an exact circular profile in the read-only preview.
                profile = Face(Wire(Circle(
                    Pnt(
                        float(shape["r_center"]),
                        0.0,
                        float(shape["z_center"]),
                    ),
                    Dir(0.0, 1.0, 0.0),
                    float(shape["radius"]),
                )))
            else:
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
            for void in self.model.get("geometry", {}).get("voids", []):
                if void.get("parent_id") != region["id"]:
                    continue
                void_shape = void.get("shape", {})
                void_vertices = void.get("vertices", [])
                if void_shape.get("type") == "circle":
                    void_profile = Face(Wire(Circle(
                        Pnt(
                            float(void_shape["r_center"]),
                            0.0,
                            float(void_shape["z_center"]),
                        ),
                        Dir(0.0, 1.0, 0.0),
                        float(void_shape["radius"]),
                    )))
                else:
                    void_profile_vertices = [
                        Vertex(Pnt(float(radius), 0.0, float(axial)))
                        for radius, axial in void_vertices
                    ]
                    if len(void_profile_vertices) >= 3:
                        void_profile_vertices.append(void_profile_vertices[0])
                        void_profile = Face(MakePolygon(void_profile_vertices))
                    else:
                        continue
                solid = solid - Revolve(
                    void_profile,
                    Axis(Pnt(0.0, 0.0, 0.0), Vec(0.0, 0.0, 1.0)),
                    360.0,
                )
            solid = solid.mat(material_names.get(region.get("material_id")) or region.get("name", "Region"))
            preview_solids.append(solid)

        renderers = []
        self._preview_3d_renderers = {}
        self._preview_3d_scene = None
        self._preview_face_region_ids = {}
        self._preview_edge_region_ids = {}
        if preview_solids:
            # Keep each region in its own renderer. The WebGPU renderer has a
            # renderer-wide opaque/transparent pass switch; combining all
            # regions made one translucent exterior force opaque interiors
            # through the transparent pass too.
            for region, solid in zip(regions, preview_solids):
                for face in solid.faces:
                    self._preview_face_region_ids.setdefault(hash(face), []).append(region["id"])
                for edge in solid.edges:
                    self._preview_edge_region_ids.setdefault(hash(edge), set()).add(region["id"])

            for region, solid in zip(regions, preview_solids):
                geometry = OCCGeometry(solid)
                renderer = GeometryRenderer(geometry)
                renderer.faces.active = True
                # Keep true geometric edges, while exact circle profiles avoid
                # the artificial segmented wire cage on toroidal surfaces.
                renderer.edges.active = True
                self._preview_3d_renderers[region["id"]] = renderer
                renderers.append(renderer)
        axes = CoordinateAxes()
        renderers.append(axes)

        preview = WebgpuComponent(
            ui_style="display:block; flex:1 1 auto; width:100%; height:100%; min-width:0; min-height:0;",
        )
        preview.ui_class = "fit"
        preview.on_mounted(lambda *_: self._resize_3d_preview(preview))
        scene = preview.draw(renderers)
        self._preview_3d_scene = scene
        if preview_solids:
            scene.options.camera.reset(*scene.bounding_box)
            scene.render()
            self._apply_3d_preview_visibility(render=False)
            scene.render()
        return preview

    def _render_3d_preview_region_table(self):
        """Refresh the per-region visibility controls in the preview sidebar."""
        self._preview_visibility_controls = {}
        rows = []
        regions = self.model.get("geometry", {}).get("regions", [])
        for region_index, region in enumerate(regions):
            region_id = region.get("id")
            state = self._preview_region_visibility.get(region_id, "opaque")
            toggle = QBtnToggle(
                QTooltip("Opaque: fully visible · Translucent: see through · Hidden: invisible"),
                ui_model_value=state,
                ui_options=[
                    {"value": "opaque", "icon": "mdi-eye", "title": "Opaque"},
                    {"value": "translucent", "icon": "mdi-circle-opacity", "title": "Translucent"},
                    {"value": "hidden", "icon": "mdi-eye-off", "title": "Hidden"},
                ],
                ui_color="grey-3",
                ui_text_color="grey-9",
                ui_toggle_color="blue-9",
                ui_toggle_text_color="white",
                ui_outline=False,
                ui_dense=True,
                ui_no_caps=True,
                ui_no_wrap=True,
                ui_size="sm",
                ui_padding="xs",
                ui_style="display:inline-flex; width:fit-content !important; min-width:fit-content !important; max-width:100%; justify-self:start; flex:0 0 auto; background:transparent;",
            )
            toggle._props["aria-label"] = f"Visibility for {region.get('name') or 'unnamed region'}"
            toggle.on(
                "update:model-value",
                lambda event, rid=region_id: self._set_3d_region_visibility(
                    rid, getattr(event, "value", event)
                ),
            )
            self._preview_visibility_controls[region_id] = toggle
            rows.append(
                Div(
                    Div(
                        Div(
                            ui_style=(
                                "width:9px; height:24px; flex:none; border-radius:5px; "
                                f"background:{_MATERIAL_COLORS[region_index % len(_MATERIAL_COLORS)]};"
                            ),
                        ),
                        Div(
                            region.get("name") or f"Region {region_index + 1}",
                            ui_style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;",
                        ),
                        ui_style="display:flex; align-items:center; gap:9px; min-width:0;",
                    ),
                    toggle,
                    ui_style=(
                        "display:grid; grid-template-columns:minmax(0, 1fr) max-content; "
                        "align-items:center; gap:8px; min-height:42px; padding:5px 0; "
                        "border-bottom:1px solid var(--border); font-size:12px;"
                    ),
                )
            )

        self._preview_3d_region_rows.ui_children = rows or [
            Div("No regions defined", ui_style="padding:10px 0; color:var(--fg-muted); font-size:12px;")
        ]

    def _set_3d_region_visibility(self, region_id, state):
        """Update one preview-only visibility state and redraw its region."""
        if state not in _PREVIEW_VISIBILITY_ALPHA or region_id not in self._preview_region_visibility:
            return
        self._preview_region_visibility[region_id] = state
        control = self._preview_visibility_controls.get(region_id)
        if control is not None and control.ui_model_value != state:
            control.ui_model_value = state
        self._apply_3d_preview_visibility()

    def _apply_3d_preview_visibility(self, *, render=True):
        """Apply opacity independently so opaque child regions keep the opaque pass."""
        renderers = getattr(self, "_preview_3d_renderers", {})
        if not renderers:
            return
        import numpy as np

        material_index = {
            material.get("id"): index
            for index, material in enumerate(self.model.get("materials", []))
        }
        region_order = {
            region.get("id"): index
            for index, region in enumerate(self.model.get("geometry", {}).get("regions", []))
        }
        regions_by_id = {
            region.get("id"): region
            for region in self.model.get("geometry", {}).get("regions", [])
        }

        for own_region_id, renderer in renderers.items():
            faces = list(renderer.geo.faces)
            face_colors = np.tile(
                np.array([0.42, 0.55, 0.68, 1.0], dtype=np.float32), len(faces)
            )
            face_alphas = []
            for face_index, face in enumerate(faces):
                region_ids = self._preview_face_region_ids.get(hash(face), (own_region_id,))
                visible_regions = [rid for rid in region_ids if rid in regions_by_id]
                if not visible_regions:
                    visible_regions = [own_region_id]
                region_id = max(
                    visible_regions,
                    key=lambda rid: _PREVIEW_VISIBILITY_ALPHA[
                        self._preview_region_visibility.get(rid, "opaque")
                    ],
                )
                region = regions_by_id[region_id]
                palette_index = material_index.get(
                    region.get("material_id"), region_order.get(region_id, 0)
                )
                palette = _MATERIAL_COLORS[palette_index % len(_MATERIAL_COLORS)]
                rgb = tuple(int(palette[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
                alpha = max(
                    _PREVIEW_VISIBILITY_ALPHA[
                        self._preview_region_visibility.get(rid, "opaque")
                    ]
                    for rid in visible_regions
                )
                face_colors[4 * face_index:4 * face_index + 4] = (*rgb, alpha)
                face_alphas.append(alpha)
            renderer.faces.set_colors(face_colors)
            renderer.faces.transparent = any(0.0 < alpha < 1.0 for alpha in face_alphas)
            renderer.faces.active = any(alpha > 0.0 for alpha in face_alphas)

            edges = list(renderer.geo.edges)
            edge_colors = np.tile(
                np.array([0.12, 0.16, 0.21, 1.0], dtype=np.float32), len(edges)
            )
            for edge_index, edge in enumerate(edges):
                region_ids = self._preview_edge_region_ids.get(hash(edge), {own_region_id})
                alpha = max(
                    _PREVIEW_VISIBILITY_ALPHA[
                        self._preview_region_visibility.get(rid, "opaque")
                    ]
                    for rid in region_ids
                )
                edge_colors[4 * edge_index + 3] = 0.0 if alpha == 0.0 else 1.0
            renderer.edges.set_colors(edge_colors)
            renderer.edges.active = bool(edges) and any(edge_colors[3::4] > 0.0)
            # Alpha changes select a different GPU render pipeline. Invalidate
            # the cached pass so a formerly opaque shell cannot keep writing
            # depth over the opaque child renderers.
            renderer.set_needs_update()

        if render and self._preview_3d_scene is not None:
            self._preview_3d_scene.render()

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
        """Open or close the large read-only 3D preview window."""
        if self._preview_3d_active:
            self._preview_3d_active = False
            self._preview_3d_dialog.ui_model_value = False
            self._preview_3d_viewport.ui_children = []
            self._preview_3d_component = None
            self._preview_3d_scene = None
            self._preview_3d_renderers = {}
            self._preview_face_region_ids = {}
            self._preview_edge_region_ids = {}
            self._preview_visibility_controls = {}
            self._preview_3d_button.ui_label = "Preview in 3D"
            self._preview_3d_button.ui_icon = "mdi-cube-scan"
            self._preview_3d_button.ui_color = None
            self._preview_3d_button.ui_flat = True
            self._preview_3d_tooltip.ui_children = ["Preview the axisymmetric regions revolved into 3D."]
            self._message("Returned to the editable meridian sketch.")
            return

        try:
            preview = self._build_3d_preview_component()
        except Exception as error:
            self._message(f"Could not create the 3D preview: {error}", error=True)
            return

        self._preview_3d_component = preview
        self._preview_3d_viewport.ui_children = [preview]
        self._render_3d_preview_region_table()
        self._preview_3d_dialog.ui_model_value = True
        self._preview_3d_active = True
        self._preview_3d_button.ui_label = "Exit 3D Preview"
        self._preview_3d_button.ui_icon = "mdi-cube-outline"
        self._preview_3d_button.ui_color = "primary"
        self._preview_3d_button.ui_flat = False
        self._preview_3d_tooltip.ui_children = ["Return to the editable meridian sketch."]
        self._message("3D preview: left-drag to rotate, middle-drag or Shift+left-drag to pan, scroll to zoom.")

    @_history_tracked
    def _set_region_value(self, region_id, key, value):
        region = next((item for item in self.model["geometry"]["regions"] if item["id"] == region_id), None)
        if region is None:
            return
        if key == "material_id" and value:
            material = next((item for item in self.model["materials"] if item["id"] == value), None)
            if material is None:
                material = next((item for item in [*builtin_materials(), *self._user_materials] if item["id"] == value), None)
                if material is None:
                    self._message("Choose a material from the available list.", error=True)
                    return
                self.model["materials"].append(copy.deepcopy(material))
        region[key] = str(value).strip() if key == "name" else value
        if key == "mechanical":
            self._rebuild_edges()
        self._refresh_model_tree()
        if key in {"material_id", "mechanical"}:
            self._render_inspector()
        self.render_canvas()

    @_history_tracked
    def _set_region_dimension(self, region_id, key, value):
        region = next((item for item in self.model["geometry"]["regions"] if item["id"] == region_id), None)
        if region is None:
            return
        self._preserve_canvas_view()
        old_shape = copy.deepcopy(region["shape"])
        old_vertices = copy.deepcopy(region["vertices"])
        old_parents = {item["id"]: item.get("parent_id") for item in self.model["geometry"]["regions"]}
        old_geometry = copy.deepcopy(self.model["geometry"]) if region.get("shape", {}).get("type") in {"imported_contour", "sketch"} else None
        try:
            expression = str(value).strip()
            parameters = {item["name"]: item for item in self.model.get("parameters", [])}
            dimension = evaluate_expression(expression, parameters)
            if not math.isfinite(dimension) or dimension <= 0 and key not in {"r_min", "z_min", "r_center", "z_center"}:
                raise ValueError("Dimensions must be finite and positive.")
            shape = region["shape"]
            if shape.get("type") in {"imported_contour", "sketch"}:
                if key not in {"r_min", "z_min", "width", "height"}:
                    raise ValueError("Sketch contours support inner radius, bottom, radial width and axial height edits.")
                vertices = region.get("vertices", [])
                if len(vertices) < 3:
                    raise ValueError("This sketch contour has no editable closed outline.")
                radial = [float(point[0]) for point in vertices]
                axial = [float(point[1]) for point in vertices]
                old_r_min, old_r_max = min(radial), max(radial)
                old_z_min, old_z_max = min(axial), max(axial)
                new_r_min, new_r_max = old_r_min, old_r_max
                new_z_min, new_z_max = old_z_min, old_z_max
                if key == "r_min":
                    new_r_min, new_r_max = dimension, dimension + old_r_max - old_r_min
                elif key == "z_min":
                    new_z_min, new_z_max = dimension, dimension + old_z_max - old_z_min
                elif key == "width":
                    new_r_max = old_r_min + dimension
                else:
                    new_z_max = old_z_min + dimension
                self._transform_sketch_contour_bounds(
                    region, (new_r_min, new_r_max, new_z_min, new_z_max)
                )
                region = next(
                    (item for item in self.model["geometry"]["regions"] if item.get("id") == region_id),
                    None,
                )
                if region is None:
                    raise ValueError("The resized sketch contour no longer encloses a region.")
                region["shape"].setdefault("dimension_expressions", {})[key] = expression
                self._recompute_parent_links()
                self._rebuild_edges()
                self._rebuild_imported_parent_loops()
                _sync_axis_boundary_conditions(self.model)
                _sync_mechanical_boundary_conditions(self.model)
                self._refresh_model_tree()
                self._render_inspector()
                self._message("Updated sketch contour dimensions.")
                self.render_canvas()
                return
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
            # A dimension edit can move a region onto or off r = 0, which
            # changes the automatically managed axis boundary in the model tree.
            self._refresh_model_tree()
            self._render_inspector()
            self._message("Updated sketch dimension.")
        except (ValueError, TypeError, OverflowError) as error:
            if old_geometry is not None:
                self.model["geometry"] = old_geometry
            else:
                region["shape"] = old_shape
                region["vertices"] = old_vertices
                for item in self.model["geometry"]["regions"]:
                    item["parent_id"] = old_parents[item["id"]]
            self._refresh_model_tree()
            self._render_inspector()
            self._message(str(error), error=True)
        self.render_canvas()

    @_history_tracked
    def _set_edge_name(self, edge_id, value):
        edge = next((item for item in self.model["geometry"]["edges"] if item["id"] == edge_id), None)
        if edge:
            edge["name"] = str(value).strip()

    def _set_edge_condition(self, edge_id, physics, value):
        self._set_edge_conditions([edge_id], physics, value)

    @_history_tracked
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
            is_mechanical = str(kind).startswith("mechanical_") or kind == "transmission_interface"
            if condition.get("automatic_for_axis") or condition.get("automatic_for_mechanics"):
                self._message("Automatic boundary conditions cannot be assigned manually.", error=True)
                return
            if is_mechanical != (physics == "mechanical"):
                self._message("Choose a condition for the selected physics.", error=True)
                return

        editable = []
        protected = []
        for edge in edges:
            if _edge_is_on_rotation_axis(edge) or (
                physics == "mechanical" and edge.get("mechanical_boundary_locked")
            ):
                protected.append(edge)
            else:
                editable.append(edge)
        if not editable:
            if any(_edge_is_on_rotation_axis(edge) for edge in protected):
                self._message("The axis of symmetry is required and cannot be changed.", error=True)
            else:
                self._message("A shared mechanical interface is locked to its default mechanical condition.", error=True)
            return

        em_types = {"axis_of_symmetry", "magnetic_potential_zero", "natural"}
        def condition_type(condition_id):
            return conditions.get(condition_id, {}).get("type", "")

        for edge in editable:
            # Once the user explicitly edits an imported boundary assignment,
            # the captured Python marker must no longer override that choice.
            edge.pop("legacy_boundary_tag", None)
            condition_ids = _edge_condition_ids(edge)
            if physics == "mechanical":
                # Replacing transmission with fixed/prescribed/traction/Robin
                # is a user override of the automatic mechanical default.
                edge.pop("legacy_mechanical_boundary_override", None)
                edge["mechanical_boundary_override"] = True
                condition_ids = [
                    condition_id for condition_id in condition_ids
                    if not (
                        str(condition_type(condition_id)).startswith("mechanical_")
                        or condition_type(condition_id) == "transmission_interface"
                    )
                ]
            else:
                condition_ids = [
                    condition_id for condition_id in condition_ids
                    if condition_type(condition_id) not in em_types
                ]
            if value:
                condition_ids.append(value)
            edge["boundary_condition_ids"] = list(dict.fromkeys(condition_ids))
        self._rebuild_edges()
        self._refresh_model_tree()
        self._render_inspector()
        self.render_canvas()
        if protected:
            self._message(f"Updated {len(editable)} edge(s); skipped {len(protected)} protected edge(s).")

    def delete_region(self, region_id):
        return self.delete_regions([region_id])

    @_history_tracked
    def add_parameter(self, *args):
        index = len(self.model["parameters"]) + 1
        parameter = {"id": new_id("parameter"), "name": f"length_{index}", "expression": "0.01", "unit": "m"}
        self.model["parameters"].append(parameter)
        self.selected_parameter_id = parameter["id"]
        self._message("Added a parameter with the dimension Length (m). Choose a different dimension if needed.")
        self._refresh_model_tree()
        self._render_inspector()

    @_history_tracked
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
        for load in self.model.get("regional_loads", []):
            load_name = load.get("name", "unnamed")
            for key in ("dc_current_density", "ac_current_density_real", "ac_current_density_imaginary"):
                check(f"source/load '{load_name}' {key.replace('_', ' ')}", load.get(key))
            for component, expression in (load.get("mechanical_body_force") or {}).items():
                check(f"source/load '{load_name}' {component} body force", expression)
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

    @_history_tracked
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
                    shape[key] = evaluate_expression(expression, parameters)
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
        self._open_material_editor()

    @_history_tracked
    def remove_material(self, material_id):
        if any(region.get("material_id") == material_id for region in self.model["geometry"]["regions"]):
            self._message("This material is assigned to a region. Reassign the region before deleting it.", error=True)
            return
        self.model["materials"] = [item for item in self.model["materials"] if item["id"] != material_id]
        if self.selected_material_id == material_id:
            self.selected_material_id = self.model["materials"][0]["id"] if self.model["materials"] else None
        self._refresh_model_tree()
        self._render_inspector()

    def _save_user_materials(self):
        try:
            self._material_library.save(self._user_materials)
        except OSError as error:
            self._message(f"Could not save the local material library: {error}", error=True)
            return False
        return True

    def remove_material_from_library(self, material_id):
        material = next((item for item in self._user_materials if item["id"] == material_id), None)
        if material is None:
            return False
        remaining = [item for item in self._user_materials if item["id"] != material_id]
        try:
            self._material_library.save(remaining)
        except OSError as error:
            self._message(f"Could not update the local material library: {error}", error=True)
            return False
        self._user_materials = remaining
        self._message(f"Removed {material['name']} from the local library. Its copy in this model is unchanged.")
        self._render_inspector()
        self._update_geometry_inspector()
        return True

    @_history_tracked
    def _set_material_name(self, material_id, value):
        material = next((item for item in self.model["materials"] if item["id"] == material_id), None)
        if material:
            material["name"] = str(value).strip()
            library_material = next((item for item in self._user_materials if item["id"] == material_id), None)
            if library_material is not None:
                library_material["name"] = material["name"]
                self._save_user_materials()
            self._refresh_model_tree()

    @_history_tracked
    def _set_material_property(self, material_id, key, value):
        material = next((item for item in self.model["materials"] if item["id"] == material_id), None)
        if material:
            material.setdefault("properties", {})[key] = str(value).strip()
            library_material = next((item for item in self._user_materials if item["id"] == material_id), None)
            if library_material is not None:
                library_material.setdefault("properties", {})[key] = str(value).strip()
                self._save_user_materials()

    def add_boundary(self, *args):
        self._open_boundary_editor()

    @_history_tracked
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

    @_history_tracked
    def _set_boundary_value(self, boundary_id, key, value):
        condition = next((item for item in self.model.get("boundary_conditions", []) if item["id"] == boundary_id), None)
        if condition:
            if key == "type" and (condition.get("automatic_for_axis") or condition.get("automatic_for_mechanics")):
                return
            if key == "type" and value == "transmission_interface":
                return
            condition[key] = str(value).strip() if key != "type" else value
            self._refresh_model_tree()
            if key == "type":
                self._render_inspector()
                self._sync_canvas_selection()

    @_history_tracked
    def _set_region_source(self, region_id, key, value):
        region = next((item for item in self.model["geometry"]["regions"] if item["id"] == region_id), None)
        if region:
            region.setdefault("sources", {})[key] = str(value).strip()

    @_history_tracked
    def _set_region_body_force(self, region_id, component, value):
        region = next((item for item in self.model["geometry"]["regions"] if item["id"] == region_id), None)
        if region:
            region.setdefault("sources", {}).setdefault("mechanical_body_force", {"r": "0", "z": "0"})[component] = str(value).strip()

    @_history_tracked
    def _set_physics(self, physics, key, value):
        self.model["physics"].setdefault(physics, {})[key] = value
        if self.active_section == "physics":
            self._render_inspector()

    @_history_tracked
    def _set_mesh(self, key, value):
        self.model["mesh"][key] = value
        self._refresh_model_tree()

    @_history_tracked
    def _set_region_mesh_size(self, region_id, value):
        region_ids = {region.get("id") for region in self.model.get("geometry", {}).get("regions", [])}
        if region_id not in region_ids:
            return
        region_sizes = self.model.setdefault("mesh", {}).setdefault("region_element_sizes", {})
        expression = str(value or "").strip()
        if expression:
            region_sizes[region_id] = expression
        else:
            region_sizes.pop(region_id, None)

    @_history_tracked
    def _set_hp_layer_target(self, key, target_id, enabled):
        if key not in {"hp_region_ids", "hp_edge_ids"}:
            raise ValueError(f"Unsupported hp-layer target list: {key}")
        live_ids = {
            region.get("id") for region in self.model.get("geometry", {}).get("regions", [])
        } if key == "hp_region_ids" else {
            edge.get("id") for edge in self.model.get("geometry", {}).get("edges", [])
        }
        if target_id not in live_ids:
            return
        selected = list(self.model.setdefault("mesh", {}).get(key, []))
        if enabled and target_id not in selected:
            selected.append(target_id)
        elif not enabled:
            selected = [item for item in selected if item != target_id]
        self.model["mesh"][key] = selected

    @_history_tracked
    def _set_solver(self, key, value):
        self.model["solver"][key] = value

    @_history_tracked
    def _set_study(self, study_id, key, value):
        study = next((item for item in self.studies["studies"] if item["id"] == study_id), None)
        if study:
            study[key] = str(value).strip()

    @_history_tracked
    def _set_study_frequencies(self, study_id, value):
        study = next((item for item in self.studies["studies"] if item["id"] == study_id), None)
        if study:
            study["frequency_hz"] = [part.strip() for part in str(value).split(",") if part.strip()]

    def _mechanical_components(self):
        """Return bonded mechanical bodies and their non-internal boundary edges."""
        geometry = self.model.get("geometry")
        regions = geometry.get("regions", []) if isinstance(geometry, dict) else []
        if not isinstance(regions, list):
            return [], []
        mechanical = {
            region.get("id"): region
            for region in regions
            if isinstance(region, dict)
            and region.get("id")
            and region.get("mechanical") is True
            and isinstance(region.get("edge_ids", []), list)
        }
        parent = {region_id: region_id for region_id in mechanical}

        def find(region_id):
            while parent[region_id] != region_id:
                parent[region_id] = parent[parent[region_id]]
                region_id = parent[region_id]
            return region_id

        edge_owners = {}
        for region in regions:
            if not isinstance(region, dict):
                continue
            edge_ids = region.get("edge_ids", [])
            if not isinstance(edge_ids, list):
                continue
            for edge_id in edge_ids:
                if isinstance(edge_id, str):
                    edge_owners.setdefault(edge_id, set()).add(region.get("id"))

        for region_id, region in mechanical.items():
            linked = set(region.get("edge_ids", []))
            parent_id = region.get("parent_id")
            if parent_id in mechanical:
                linked.update(mechanical[parent_id].get("edge_ids", []))
            for other_id, other in mechanical.items():
                if other_id == region_id:
                    continue
                if linked.intersection(other.get("edge_ids", [])):
                    first, second = find(region_id), find(other_id)
                    if first != second:
                        parent[first] = second

        groups = {}
        for region_id in mechanical:
            groups.setdefault(find(region_id), set()).add(region_id)

        components = []
        for region_ids in groups.values():
            boundary_edges = set()
            for region_id in region_ids:
                for edge_id in mechanical[region_id].get("edge_ids", []):
                    owners = edge_owners.get(edge_id, set())
                    bonded_owners = owners.intersection(region_ids)
                    if len(bonded_owners) < 2:
                        boundary_edges.add(edge_id)
            # A non-mechanical child outline is a real boundary of its
            # mechanical parent (for example a cavity wall).
            for region in regions:
                if (
                    isinstance(region, dict)
                    and region.get("id") not in region_ids
                    and region.get("parent_id") in region_ids
                    and not region.get("mechanical")
                ):
                    boundary_edges.update(
                        edge_id for edge_id in region.get("edge_ids", [])
                        if isinstance(edge_id, str)
                    )
            components.append((region_ids, boundary_edges))
        return list(mechanical.values()), components

    def _robin_edge_supports_axial_translation(self, edge, condition, parameters):
        """Check whether a non-negative Robin spring restrains axial motion.

        The axisymmetric displacement has radial and axial components. A free
        body's remaining rigid mode is axial translation; for an edge with
        unit tangent (dr, dz), its Robin energy for that mode is proportional
        to k_normal*dr² + k_tangential*dz².
        """
        vertices = edge.get("vertices", [])
        if len(vertices) != 2:
            return False, ""
        try:
            (r0, z0), (r1, z1) = [tuple(float(value) for value in point) for point in vertices]
            dr, dz = r1 - r0, z1 - z0
            length_squared = dr * dr + dz * dz
            if not math.isfinite(length_squared) or length_squared <= 0:
                return False, ""
            # Three-point Gauss locations catch ordinary coordinate-dependent
            # stiffness profiles while avoiding endpoint-only support claims.
            samples = (0.1127016653792583, 0.5, 0.8872983346207417)
            axial_support = False
            for t in samples:
                values = dict(parameters)
                values.update({"r": r0 + t * dr, "z": z0 + t * dz})
                normal_stiffness = evaluate_expression(condition.get("stiffness_normal", "0"), values)
                tangential_stiffness = evaluate_expression(condition.get("stiffness_tangential", "0"), values)
                if normal_stiffness < 0 or tangential_stiffness < 0:
                    return False, "Robin support stiffnesses must be non-negative."
                axial_stiffness = (
                    normal_stiffness * dr * dr + tangential_stiffness * dz * dz
                ) / length_squared
                axial_support = axial_support or axial_stiffness > 0
            if axial_support:
                return True, ""
        except (TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError):
            # Model validation reports malformed expressions separately.
            return False, ""
        return False, ""

    def validation_errors(self):
        errors = validate_model(self.model, scope="solve")
        geometry = self.model.get("geometry")
        regions = geometry.get("regions", []) if isinstance(geometry, dict) else []
        if not isinstance(regions, list):
            regions = []
        if not regions:
            errors.append("Create at least one material region.")
        physics = self.model.get("physics", {})
        if not isinstance(physics, dict):
            physics = {}

        def physics_enabled(name):
            section = physics.get(name, {})
            return isinstance(section, dict) and section.get("enabled") is True

        dc = physics_enabled("dc_magnetic")
        harmonic = physics_enabled("harmonic_electromagnetic")
        mechanics = physics_enabled("mechanics")
        coupling = physics_enabled("coupling")
        parameter_items = self.model.get("parameters", [])
        if not isinstance(parameter_items, list):
            parameter_items = []
        material_items = self.model.get("materials", [])
        if not isinstance(material_items, list):
            material_items = []
        condition_items = self.model.get("boundary_conditions", [])
        if not isinstance(condition_items, list):
            condition_items = []
        geometry_edges = geometry.get("edges", []) if isinstance(geometry, dict) else []
        if not isinstance(geometry_edges, list):
            geometry_edges = []
        if not (dc or harmonic or mechanics):
            errors.append("Select at least one physics problem.")
        mechanical_regions, mechanical_components = self._mechanical_components()
        if mechanics and not mechanical_regions:
            errors.append("Choose at least one region to include in mechanics.")
        if coupling and not (dc and harmonic and mechanics):
            errors.append("Coupling requires DC magnetic, time-harmonic EM, and mechanics.")

        if mechanics and mechanical_regions:
            parameter_map = {
                item.get("name"): item for item in parameter_items
                if isinstance(item, dict) and item.get("name")
            }
            materials = {
                item.get("id"): item for item in material_items
                if isinstance(item, dict) and item.get("id")
            }
            condition_by_id = {
                item.get("id"): item for item in condition_items
                if isinstance(item, dict) and item.get("id")
            }
            edges_by_id = {
                edge.get("id"): edge for edge in geometry_edges
                if isinstance(edge, dict) and edge.get("id")
            }
            owners_by_edge = {}
            for region in regions:
                if not isinstance(region, dict):
                    continue
                edge_ids = region.get("edge_ids", [])
                if not isinstance(edge_ids, list):
                    continue
                for edge_id in edge_ids:
                    if isinstance(edge_id, str):
                        owners_by_edge.setdefault(edge_id, set()).add(region.get("id"))

            for edge in edges_by_id.values():
                for condition_id in _edge_condition_ids(edge):
                    condition = condition_by_id.get(condition_id, {})
                    kind = condition.get("type")
                    if not str(kind).startswith("mechanical_") and kind != "transmission_interface":
                        continue
                    if not owners_by_edge.get(edge.get("id"), set()).intersection(
                        region.get("id") for region in mechanical_regions
                    ):
                        errors.append(
                            f"Mechanical boundary '{condition.get('name', condition_id)}' is assigned "
                            "to an edge that does not touch a mechanical region."
                        )

            for region_ids, edge_ids in mechanical_components:
                has_inertia = False
                for region in mechanical_regions:
                    if region.get("id") not in region_ids:
                        continue
                    material = materials.get(region.get("material_id"), {})
                    try:
                        density = float(evaluate_expression(
                            material.get("properties", {}).get("density", "0"), parameter_map
                        ))
                        has_inertia = has_inertia or (math.isfinite(density) and density > 0)
                    except (AttributeError, TypeError, ValueError, SyntaxError, ZeroDivisionError, OverflowError):
                        pass

                direct_support = False
                robin_support = False
                reported_robin = set()
                for edge_id in edge_ids:
                    edge = edges_by_id.get(edge_id)
                    if edge is None:
                        continue
                    for condition_id in _edge_condition_ids(edge):
                        condition = condition_by_id.get(condition_id, {})
                        kind = condition.get("type")
                        if kind in {"mechanical_fixed", "mechanical_prescribed"}:
                            direct_support = True
                        elif kind == "mechanical_robin":
                            supports, issue = self._robin_edge_supports_axial_translation(
                                edge, condition, parameter_map
                            )
                            robin_support = robin_support or supports
                            if issue and condition_id not in reported_robin:
                                errors.append(f"Boundary '{condition.get('name', condition_id)}': {issue}")
                                reported_robin.add(condition_id)
                # Harmonic inertia makes a free body mathematically valid. A
                # massless harmonic component still has a rigid null mode.
                if not has_inertia and not direct_support and not robin_support:
                    names = ", ".join(
                        region.get("name", region.get("id", "unnamed"))
                        for region in mechanical_regions if region.get("id") in region_ids
                    )
                    errors.append(
                        f"Massless harmonic mechanical component '{names}' needs a fixed/prescribed "
                        "boundary or a positive Robin support to remove its rigid axial mode."
                    )

        parameter_map = {
            item.get("name"): item for item in parameter_items
            if isinstance(item, dict) and item.get("name")
        }
        errors.extend(validate_studies(
            self.studies, parameter_map, physics=self.model.get("physics"), for_solve=True
        ))
        return errors

    def validation_warnings(self):
        """Return useful cautions that should never block a valid solve."""
        warnings = []
        physics = self.model.get("physics", {})
        if not isinstance(physics, dict):
            return warnings

        def section(name):
            value = physics.get(name, {})
            return value if isinstance(value, dict) else {}

        dc = section("dc_magnetic").get("enabled") is True
        ac = section("harmonic_electromagnetic").get("enabled") is True
        mechanics = section("mechanics").get("enabled") is True
        geometry = self.model.get("geometry")
        if not isinstance(geometry, dict):
            geometry = {}
        regions = geometry.get("regions", [])
        if not isinstance(regions, list):
            regions = []
        boundary_conditions = self.model.get("boundary_conditions", [])
        if not isinstance(boundary_conditions, list):
            boundary_conditions = []
        regional_loads = self.model.get("regional_loads", [])
        if not isinstance(regional_loads, list):
            regional_loads = []
        if (dc or ac) and regions:
            assigned_types = {
                condition.get("id"): condition.get("type")
                for condition in boundary_conditions
                if isinstance(condition, dict)
            }
            edges = geometry.get("edges", [])
            if not isinstance(edges, list):
                edges = []
            has_explicit_magnetic_boundary = bool(regions) and any(
                assigned_types.get(condition_id) == "magnetic_potential_zero"
                for edge in edges if isinstance(edge, dict)
                for condition_id in _edge_condition_ids(edge)
            )
            if not has_explicit_magnetic_boundary:
                warnings.append(
                    "No magnetic-potential boundary is explicitly assigned. Unspecified edges use the "
                    "natural condition; confirm that the finite domain boundary matches the intended open-region model."
                )

        sources = []
        for region in regions:
            if isinstance(region, dict):
                sources.append(region.get("sources", {}))
        for load in regional_loads:
            if isinstance(load, dict):
                if not load.get("region_ids"):
                    warnings.append(f"Regional load '{load.get('name', load.get('id', 'unnamed'))}' is not assigned to any region.")
                sources.append(load)
        inactive = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            if not dc and str(source.get("dc_current_density", "0")).strip() not in {"", "0", "0.0", "0.0e0"}:
                inactive.append("DC current")
            if not ac and any(
                str(source.get(key, "0")).strip() not in {"", "0", "0.0", "0.0e0"}
                for key in ("ac_current_density_real", "ac_current_density_imaginary")
            ):
                inactive.append("AC current")
            if not mechanics:
                body = source.get("mechanical_body_force", {})
                if isinstance(body, dict) and any(
                    str(body.get(key, "0")).strip() not in {"", "0", "0.0", "0.0e0"}
                    for key in ("r", "z")
                ):
                    inactive.append("mechanical body force")
        if inactive:
            warnings.append(
                "Some defined loads target disabled physics and will be ignored: "
                + ", ".join(sorted(set(inactive))) + "."
            )

        if (dc or ac) and regions:
            current = section("dc_magnetic").get("source_current_density", "0") if dc else "0"
            ac_source = section("harmonic_electromagnetic")
            source_values = [str(current).strip()]
            if ac:
                source_values.extend([
                    str(ac_source.get("source_current_density_real", "0")).strip(),
                    str(ac_source.get("source_current_density_imaginary", "0")).strip(),
                ])
            for source in sources:
                if isinstance(source, dict):
                    source_values.extend(str(source.get(key, "0")).strip() for key in (
                        "dc_current_density", "ac_current_density_real", "ac_current_density_imaginary"
                    ))
            if all(value in {"", "0", "0.0", "0.0e0"} for value in source_values):
                warnings.append(
                    "All active electromagnetic current sources are zero; a zero-field solution may be correct."
                )
        if mechanics and regions:
            mechanical_sources = [
                str(section("mechanics").get(key, "0")).strip()
                for key in ("body_force_r", "body_force_z")
            ]
            mechanical_sources.extend(
                str(source.get("mechanical_body_force", {}).get(key, "0")).strip()
                for source in sources if isinstance(source, dict)
                and isinstance(source.get("mechanical_body_force", {}), dict)
                for key in ("r", "z")
            )
            if all(value in {"", "0", "0.0", "0.0e0"} for value in mechanical_sources):
                warnings.append(
                    "All explicit mechanical body forces are zero; verify that the applied boundary conditions or coupling provide the intended excitation."
                )
        return list(dict.fromkeys(warnings))

    def mesh_validation_errors(self):
        errors = validate_model(self.model, scope="mesh")
        if not self.model["geometry"].get("regions"):
            errors.append("Create at least one material region before generating a mesh.")
        return errors

    def validate_action(self, *args):
        errors = self.validation_errors()
        warnings = self.validation_warnings()
        self._show_setup_check_results(errors, warnings)
        if errors:
            self._message(f"Setup check found {len(errors)} issue{'s' if len(errors) != 1 else ''}. Select a message for guidance.", error=True)
        elif warnings:
            self._message(f"Setup inputs are valid with {len(warnings)} warning{'s' if len(warnings) != 1 else ''}; operator checks remain pending until solve.")
        else:
            self._message("Setup inputs passed. Mesh-dependent operator checks remain pending until solve.")
        self._flash_status_bar()

    def run_mesh_action(self, *args):
        errors = self.mesh_validation_errors()
        if errors:
            self._show_validation_issues(errors)
            self._message(f"Mesh setup found {len(errors)} issue{'s' if len(errors) != 1 else ''}.", error=True)
            return
        if self.on_mesh:
            if not self.begin_solver_job("Mesh"):
                return
            self._mesh_button.ui_loading = True
            try:
                self.on_mesh()
            except Exception as error:
                self.finish_solver_job(f"Could not start mesh generation: {error}", error=True)
        else:
            self._message("Mesh generation is unavailable in this environment.", error=True)

    def toggle_mesh_preview(self, *args):
        if not self.mesh_preview_edges:
            self._message("Generate a mesh before opening the mesh preview.", error=True)
            return
        self.mesh_preview_visible = not self.mesh_preview_visible
        self._mesh_preview_button.ui_label = "Hide mesh" if self.mesh_preview_visible else "Show mesh"
        self._mesh_preview_button.ui_color = "primary" if self.mesh_preview_visible else None
        self._mesh_preview_button.ui_flat = not self.mesh_preview_visible
        self.render_canvas()

    def _sync_canvas_visibility_buttons(self):
        background_button = getattr(self, "_canvas_background_button", None)
        if background_button is not None:
            background_button.ui_label = (
                "Hide background" if self._canvas_background_visible else "Show background"
            )
            background_button.ui_color = "primary" if self._canvas_background_visible else None
            background_button.ui_flat = not self._canvas_background_visible
        geometry_button = getattr(self, "_canvas_geometry_button", None)
        if geometry_button is not None:
            geometry_button.ui_label = (
                "Hide geometry" if self._canvas_geometry_visible else "Show geometry"
            )
            geometry_button.ui_color = "primary" if self._canvas_geometry_visible else None
            geometry_button.ui_flat = not self._canvas_geometry_visible

    def toggle_canvas_background(self, *args):
        """Toggle the grid, coordinate axes, and their labels."""
        self._canvas_background_visible = not self._canvas_background_visible
        self._sync_canvas_visibility_buttons()
        self.render_canvas()

    def toggle_canvas_geometry(self, *args):
        """Toggle model geometry while leaving the mesh preview independent."""
        self._canvas_geometry_visible = not self._canvas_geometry_visible
        self._sync_canvas_visibility_buttons()
        self.render_canvas()

    def _clear_mesh_preview(self):
        had_preview = bool(self.mesh_preview_edges) or self.mesh_preview_visible
        self.mesh_preview_edges = []
        self.mesh_preview_visible = False
        self._mesh_preview_signature = None
        button = getattr(self, "_mesh_preview_button", None)
        if button is not None:
            button.ui_label = "Mesh preview"
            button.ui_color = None
            button.ui_flat = True
            button.ui_disable = True
        if had_preview and hasattr(self, "_canvas_scene"):
            self.render_canvas()

    def set_mesh_preview(self, edges):
        """Show mesh edge polylines returned by a completed solver mesh build."""
        polylines = []
        for edge in edges:
            if not isinstance(edge, (list, tuple)) or len(edge) < 2:
                continue
            try:
                points = tuple(
                    (float(point[0]), float(point[1]))
                    for point in edge
                    if len(point) >= 2
                )
            except (TypeError, ValueError, IndexError, OverflowError):
                continue
            if len(points) >= 2:
                polylines.append(points)
        self.mesh_preview_edges = polylines
        self.mesh_preview_visible = bool(self.mesh_preview_edges)
        self._mesh_preview_signature = _mesh_signature(self.model) if self.mesh_preview_edges else None
        self._mesh_preview_button.ui_disable = not self.mesh_preview_edges
        self._mesh_preview_button.ui_label = "Hide mesh" if self.mesh_preview_visible else "Mesh preview"
        self._mesh_preview_button.ui_color = "primary" if self.mesh_preview_visible else None
        self._mesh_preview_button.ui_flat = not self.mesh_preview_visible
        self.render_canvas()

    def run_study_action(self, *args):
        errors = self.validation_errors()
        warnings = self.validation_warnings() if not errors else []
        if errors:
            self._show_setup_check_results(errors, warnings)
            self._message(f"Study setup found {len(errors)} issue{'s' if len(errors) != 1 else ''}.", error=True)
            return
        if warnings:
            self._show_setup_check_results(errors, warnings)
        if self.on_run:
            if not self.begin_solver_job("Study"):
                return
            self._run_button.ui_loading = True
            try:
                self.on_run()
            except Exception as error:
                self.finish_solver_job(f"Could not start the study: {error}", error=True)
        else:
            self._message("The solver is unavailable in this environment.", error=True)

    def begin_solver_job(self, kind):
        """Mark the mesh/study job active and expose cancellation controls."""
        if self._job_active:
            self._message("A mesh or solver job is already running.", error=True)
            return False
        self._job_active = True
        self._active_job_kind = str(kind)
        self._cancel_button.ui_hidden = False
        self._cancel_button.ui_disable = False
        self._cancel_button.ui_label = "Cancel run"
        self.show_log()
        return True

    def _cancel_solver_job(self, *args):
        if not self._job_active or not self.on_cancel:
            return
        self.mark_solver_cancel_requested()
        self.on_cancel()

    def mark_solver_cancel_requested(self):
        if not self._job_active:
            return
        self._cancel_button.ui_label = "Stopping…"
        self._cancel_button.ui_disable = True
        self._message("Stopping the active solver job…")

    def finish_solver_job(self, message, error=False, *, cancelled=False, run_kind=None, output_path=None, run_name=None, mesh_preview_edges=None):
        self._mesh_button.ui_loading = False
        self._run_button.ui_loading = False
        self._job_active = False
        self._active_job_kind = None
        self._cancel_button.ui_hidden = True
        self._cancel_button.ui_disable = False
        self._cancel_button.ui_label = "Cancel run"
        self._message(message, error=error)
        if not error and mesh_preview_edges is not None:
            self.set_mesh_preview(mesh_preview_edges)
        if run_kind:
            status = "Failed" if error else "Cancelled" if cancelled else "Complete"
            run_output_path = str(output_path or "")
            self.runs.append({
                "id": _run_history_path_key(run_output_path) if run_output_path else new_id("run"),
                "name": run_name or run_kind,
                "kind": run_kind,
                "status": status,
                "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
                "output_path": run_output_path,
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


def _polyline_polygon_intersections(path, polygon, tolerance=1e-10):
    """Return distinct path/boundary intersections and whether the path enters the polygon."""
    if len(path) < 2 or len(polygon) < 3:
        return [], False

    def cross(first, second):
        return first[0] * second[1] - first[1] * second[0]

    intersections = []
    enters_interior = False
    boundary = list(polygon) + [polygon[0]]
    for raw_start, raw_end in zip(path, path[1:]):
        start = tuple(float(value) for value in raw_start[:2])
        end = tuple(float(value) for value in raw_end[:2])
        direction = (end[0] - start[0], end[1] - start[1])
        length = math.hypot(*direction)
        if length <= tolerance:
            continue
        cuts = [0.0, 1.0]
        for raw_first, raw_second in zip(boundary, boundary[1:]):
            first = tuple(float(value) for value in raw_first[:2])
            second = tuple(float(value) for value in raw_second[:2])
            edge_direction = (second[0] - first[0], second[1] - first[1])
            edge_length = math.hypot(*edge_direction)
            if edge_length <= tolerance:
                continue
            offset = (first[0] - start[0], first[1] - start[1])
            denominator = cross(direction, edge_direction)
            cross_tolerance = tolerance * max(length, edge_length, tolerance)
            if abs(denominator) > cross_tolerance:
                t = cross(offset, edge_direction) / denominator
                u = cross(offset, direction) / denominator
                t_tolerance = tolerance / length
                u_tolerance = tolerance / edge_length
                if -t_tolerance <= t <= 1.0 + t_tolerance and -u_tolerance <= u <= 1.0 + u_tolerance:
                    t = min(1.0, max(0.0, t))
                    cuts.append(t)
                    point = (start[0] + t * direction[0], start[1] + t * direction[1])
                    if all(math.dist(point, previous) > tolerance for previous in intersections):
                        intersections.append(point)
                continue

            if abs(cross(offset, direction)) > cross_tolerance:
                continue
            for point in (first, second, start, end):
                if _point_on_segment(point, start, end, tolerance) and _point_on_segment(point, first, second, tolerance):
                    t = ((point[0] - start[0]) * direction[0] + (point[1] - start[1]) * direction[1]) / (length * length)
                    cuts.append(min(1.0, max(0.0, t)))
                    if all(math.dist(point, previous) > tolerance for previous in intersections):
                        intersections.append(point)

        cuts = sorted(cuts)
        for first_t, second_t in zip(cuts, cuts[1:]):
            if second_t - first_t <= tolerance / length:
                continue
            middle_t = 0.5 * (first_t + second_t)
            middle = (start[0] + middle_t * direction[0], start[1] + middle_t * direction[1])
            if _point_in_polygon(middle, polygon):
                enters_interior = True
                break
    return intersections, enters_interior


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



def _area_tolerance(first, second):
    first_bounds, second_bounds = _bounds(first), _bounds(second)
    scale = max(
        abs(first_bounds[1] - first_bounds[0]),
        abs(first_bounds[3] - first_bounds[2]),
        abs(second_bounds[1] - second_bounds[0]),
        abs(second_bounds[3] - second_bounds[2]),
        1e-12,
    )
    return max(abs(_polygon_area(first)), abs(_polygon_area(second)), 1e-30) * 1e-12


def _contains_or_touches(outer, inner):
    if len(outer) < 3 or len(inner) < 3:
        return False
    return all(_point_in_or_on_polygon(point, outer) for point in inner)


def _polygons_overlap_interior(first, second):
    if len(first) < 3 or len(second) < 3:
        return False
    a, b = _bounds(first), _bounds(second)
    if a[1] <= b[0] or b[1] <= a[0] or a[3] <= b[2] or b[3] <= a[2]:
        return False

    def strictly_inside(point, polygon):
        if any(
            _point_on_segment(point, polygon[index], polygon[(index + 1) % len(polygon)])
            for index in range(len(polygon))
        ):
            return False
        return _point_in_polygon(point, polygon)

    if any(strictly_inside(point, second) for point in first):
        return True
    if any(strictly_inside(point, first) for point in second):
        return True

    # Proper edge crossings imply positive-area overlap. Endpoint-only and
    # collinear contacts are deliberately excluded.
    scale = max(
        abs(a[1] - a[0]), abs(a[3] - a[2]),
        abs(b[1] - b[0]), abs(b[3] - b[2]), 1e-12,
    )
    tolerance = scale * scale * 1e-12

    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    for i, p in enumerate(first):
        q = first[(i + 1) % len(first)]
        for j, r in enumerate(second):
            s = second[(j + 1) % len(second)]
            o1, o2 = orient(p, q, r), orient(p, q, s)
            o3, o4 = orient(r, s, p), orient(r, s, q)
            if (
                ((o1 > tolerance and o2 < -tolerance) or (o1 < -tolerance and o2 > tolerance))
                and ((o3 > tolerance and o4 < -tolerance) or (o3 < -tolerance and o4 > tolerance))
            ):
                return True

    # Detect coincident polygons and interior containment whose vertices all
    # lie on the other boundary (for example, identical rectangles).
    first_probe = _polygon_interior_probe(first)
    second_probe = _polygon_interior_probe(second)
    return _point_in_polygon(first_probe, second) or _point_in_polygon(second_probe, first)


def _polygon_interior_probe(polygon):
    """Return a point inside a simple polygon, including concave outlines."""
    if len(polygon) < 3:
        return tuple(polygon[0]) if polygon else (0.0, 0.0)
    twice_area = sum(
        polygon[index][0] * polygon[(index + 1) % len(polygon)][1]
        - polygon[(index + 1) % len(polygon)][0] * polygon[index][1]
        for index in range(len(polygon))
    )
    if abs(twice_area) > 1e-30:
        cx = sum(
            (polygon[index][0] + polygon[(index + 1) % len(polygon)][0])
            * (polygon[index][0] * polygon[(index + 1) % len(polygon)][1]
               - polygon[(index + 1) % len(polygon)][0] * polygon[index][1])
            for index in range(len(polygon))
        ) / (3.0 * twice_area)
        cy = sum(
            (polygon[index][1] + polygon[(index + 1) % len(polygon)][1])
            * (polygon[index][0] * polygon[(index + 1) % len(polygon)][1]
               - polygon[(index + 1) % len(polygon)][0] * polygon[index][1])
            for index in range(len(polygon))
        ) / (3.0 * twice_area)
        centroid = (cx, cy)
        if _point_in_polygon(centroid, polygon):
            return centroid
    y_levels = sorted({float(point[1]) for point in polygon})
    candidates = [(first + second) / 2 for first, second in zip(y_levels, y_levels[1:]) if second > first]
    for y in candidates:
        crossings = []
        for index, start in enumerate(polygon):
            end = polygon[(index + 1) % len(polygon)]
            if (start[1] > y) != (end[1] > y):
                crossings.append(start[0] + (y - start[1]) * (end[0] - start[0]) / (end[1] - start[1]))
        crossings.sort()
        for left, right in zip(crossings[::2], crossings[1::2]):
            probe = ((left + right) / 2, y)
            if _point_in_polygon(probe, polygon):
                return probe
    return tuple(polygon[0])

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


def _edge_is_on_rotation_axis(edge):
    """Return whether the complete boundary curve lies on the r = 0 axis."""
    if not isinstance(edge, dict):
        return False
    vertices = edge.get("vertices", [])
    if not isinstance(vertices, (list, tuple)) or len(vertices) != 2:
        return False
    curve = edge.get("curve")
    sampled = curve.get("sampled_points") if isinstance(curve, dict) else None
    points = sampled if isinstance(sampled, (list, tuple)) and len(sampled) >= 2 else vertices
    try:
        return all(abs(float(point[0])) <= 1e-12 for point in points)
    except (TypeError, ValueError, IndexError, OverflowError):
        return False


def _sync_axis_boundary_conditions(model):
    """Keep the axis-of-symmetry condition attached to exactly the r=0 edges.

    The GUI's axis condition represents the electromagnetic regularity boundary
    used by the axisymmetric solver. It is geometry-managed: creating, editing,
    moving, or deleting a region updates the assignment automatically.
    """
    if not isinstance(model, dict):
        return
    geometry = model.get("geometry")
    if not isinstance(geometry, dict):
        return
    edges = geometry.get("edges", [])
    if not isinstance(edges, list):
        return
    conditions = model.setdefault("boundary_conditions", [])
    if not isinstance(conditions, list):
        return

    condition_by_id = {
        item.get("id"): item
        for item in conditions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    axis_condition = next(
        (item for item in conditions if isinstance(item, dict)
         and item.get("type") == "axis_of_symmetry"
         and item.get("automatic_for_axis")),
        None,
    )
    if axis_condition is None:
        axis_condition = next(
            (item for item in conditions if isinstance(item, dict)
             and item.get("type") == "axis_of_symmetry"),
            None,
        )
    axis_edges_exist = any(_edge_is_on_rotation_axis(edge) for edge in edges)
    if axis_edges_exist and (
        axis_condition is None or not isinstance(axis_condition.get("id"), str)
    ):
        axis_condition = {
            "id": new_id("boundary"),
            "name": "Axis of symmetry",
            "type": "axis_of_symmetry",
            "automatic_for_axis": True,
        }
        conditions.append(axis_condition)
    if axis_edges_exist:
        axis_condition["automatic_for_axis"] = True
        axis_condition_id = axis_condition["id"]
        condition_by_id[axis_condition_id] = axis_condition
    else:
        axis_condition_id = None

    electromagnetic_types = {"axis_of_symmetry", "magnetic_potential_zero", "natural"}

    def condition_type(condition_id):
        if not isinstance(condition_id, str):
            return None
        return condition_by_id.get(condition_id, {}).get("type")

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        on_axis = _edge_is_on_rotation_axis(edge)
        try:
            condition_ids = list(_edge_condition_ids(edge))
        except TypeError:
            # Let model validation report malformed assignment containers.
            continue
        if on_axis:
            # The axis condition is the only allowed condition on r=0.
            condition_ids = [axis_condition_id] if axis_condition_id else []
        else:
            # Axis regularity is only meaningful on the rotational axis.
            condition_ids = [
                condition_id for condition_id in condition_ids
                if condition_type(condition_id) != "axis_of_symmetry"
            ]
        edge["boundary_condition_ids"] = condition_ids
        edge.pop("boundary_condition_id", None)

    # Generated definitions disappear when the last axis edge moves away or
    # is deleted, keeping the boundary tree free of stale automatic entries.
    if axis_condition_id is None:
        conditions[:] = [
            item for item in conditions
            if not (isinstance(item, dict) and item.get("automatic_for_axis"))
        ]


def _mechanical_boundary_edge_states(model):
    """Return automatic-transmission and locked default mechanical edge ids."""
    geometry = model.get("geometry", {}) if isinstance(model, dict) else {}
    regions = geometry.get("regions", []) if isinstance(geometry, dict) else []
    edges = geometry.get("edges", []) if isinstance(geometry, dict) else []
    if not isinstance(regions, list) or not isinstance(edges, list):
        return set(), set()
    regions_by_id = {
        region.get("id"): region for region in regions
        if isinstance(region, dict) and isinstance(region.get("id"), str)
    }
    edge_ids = {
        edge.get("id") for edge in edges
        if isinstance(edge, dict) and isinstance(edge.get("id"), str)
    }
    owners_by_edge = {}
    for region in regions_by_id.values():
        for edge_id in region.get("edge_ids", []) if isinstance(region.get("edge_ids", []), list) else []:
            if edge_id in edge_ids:
                owners_by_edge.setdefault(edge_id, []).append(region)

    automatic_ids = set()
    locked_ids = set()
    for edge_id, owners in owners_by_edge.items():
        side_pairs = []
        for region in owners:
            parent = regions_by_id.get(region.get("parent_id"))
            side_pairs.append((bool(region.get("mechanical")), bool(parent and parent.get("mechanical"))))
        for index, first in enumerate(owners):
            for second in owners[index + 1:]:
                side_pairs.append((bool(first.get("mechanical")), bool(second.get("mechanical"))))
        if any(first and second for first, second in side_pairs):
            locked_ids.add(edge_id)
        elif any(first != second for first, second in side_pairs):
            automatic_ids.add(edge_id)
    return automatic_ids, locked_ids


def _sync_mechanical_boundary_conditions(model):
    """Refresh editable transmission defaults and protected shared interfaces."""
    if not isinstance(model, dict):
        return
    geometry = model.get("geometry")
    if not isinstance(geometry, dict):
        return
    edges = geometry.get("edges", [])
    conditions = model.setdefault("boundary_conditions", [])
    if not isinstance(edges, list) or not isinstance(conditions, list):
        return

    automatic_ids, locked_ids = _mechanical_boundary_edge_states(model)
    condition_by_id = {
        item.get("id"): item for item in conditions
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    edge_by_id = {
        edge.get("id"): edge for edge in edges
        if isinstance(edge, dict) and isinstance(edge.get("id"), str)
    }
    interface_condition = next(
        (
            item for item in conditions
            if isinstance(item, dict)
            and item.get("type") == "transmission_interface"
            and item.get("automatic_for_mechanics")
        ),
        None,
    )
    if automatic_ids and interface_condition is None:
        interface_condition = {
            "id": new_id("boundary"),
            "name": "Transmission interface",
            "type": "transmission_interface",
            "automatic_for_mechanics": True,
        }
        conditions.append(interface_condition)
        condition_by_id[interface_condition["id"]] = interface_condition
    interface_id = interface_condition.get("id") if interface_condition else None

    em_types = {"axis_of_symmetry", "magnetic_potential_zero", "natural"}
    def get_type(condition_id):
        return condition_by_id.get(condition_id, {}).get("type", "")
    def is_auto_transmission(condition_id):
        item = condition_by_id.get(condition_id, {})
        return item.get("type") == "transmission_interface" and item.get("automatic_for_mechanics")
    def is_mechanical_type(kind):
        return str(kind).startswith("mechanical_") or kind == "transmission_interface"

    for edge_id, edge in edge_by_id.items():
        try:
            condition_ids = _edge_condition_ids(edge)
        except TypeError:
            continue
        if any(not isinstance(condition_id, str) for condition_id in condition_ids):
            continue
        if _edge_is_on_rotation_axis(edge):
            axis_ids = [cid for cid in condition_ids if get_type(cid) == "axis_of_symmetry"]
            edge["boundary_condition_ids"] = axis_ids[:1]
            edge.pop("mechanical_boundary_locked", None)
            edge.pop("boundary_condition_id", None)
            continue

        if edge_id in locked_ids:
            edge["mechanical_boundary_locked"] = True
            edge.pop("mechanical_boundary_override", None)
            edge.pop("legacy_mechanical_boundary_override", None)
            # The locked default is mechanical only; an EM condition can still
            # be applied independently to this geometric interface.
            em_ids = [cid for cid in condition_ids if get_type(cid) in em_types]
            edge["boundary_condition_ids"] = em_ids[-1:]
            edge.pop("boundary_condition_id", None)
            continue
        edge.pop("mechanical_boundary_locked", None)

        em_ids = [cid for cid in condition_ids if get_type(cid) in em_types]
        mechanical_ids = [cid for cid in condition_ids if is_mechanical_type(get_type(cid))]
        manual_mechanical_ids = [cid for cid in mechanical_ids if not is_auto_transmission(cid)]
        if manual_mechanical_ids:
            mechanical_ids = manual_mechanical_ids[-1:]
            edge["mechanical_boundary_override"] = True
        elif edge_id in automatic_ids and not edge.get("mechanical_boundary_override") and not edge.get("legacy_mechanical_boundary_override"):
            mechanical_ids = [interface_id] if interface_id else []
        else:
            mechanical_ids = []

        # Keep malformed references intact for validate_model() to report.
        # Known condition assignments are normalized to one per physics.
        unknown_ids = [cid for cid in condition_ids if cid not in condition_by_id]
        em_ids = em_ids[-1:]
        edge["boundary_condition_ids"] = list(dict.fromkeys([*em_ids, *mechanical_ids, *unknown_ids]))
        edge.pop("boundary_condition_id", None)

    assigned_auto_ids = {
        condition_id for edge in edge_by_id.values()
        for condition_id in _edge_condition_ids(edge)
        if isinstance(condition_id, str) and is_auto_transmission(condition_id)
    }
    if not assigned_auto_ids:
        conditions[:] = [
            item for item in conditions
            if not (
                isinstance(item, dict)
                and item.get("automatic_for_mechanics")
                and item.get("type") == "transmission_interface"
            )
        ]
