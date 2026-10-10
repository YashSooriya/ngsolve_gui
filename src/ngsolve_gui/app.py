import os
import sys
import threading
import time
import copy
import contextlib
import io
import re
import importlib.util
from pathlib import Path

from ngapp.app import App
from ngapp.components import *

from .app_data import AppData
from .controls import ControlBar
from .file_loader import load_file
from ngapp.keybindings import KeybindingManager, keybinding_styles
from .navigator import Navigator
from .property_panel import empty_property_panel
from .prop_widgets import Segmented
from . import cerbsim_style as cb
from .cerbsim_style import theme, kb_theme, flex_fill, panel_full
from .system_monitor import SystemMonitor, available as system_monitor_available
from .footer import StatusFooter
from .solve_workspace import SolveWorkspace
from .axisymmetric_model import unpack_model, package_model


def _inject_solve_status_styles(js):
    style = js.document.createElement("style")
    style.id = "mmfem-solve-status-styles"
    style.textContent = """
@keyframes mmfem-solve-status-flash-a {
  0%, 100% { background-color: var(--surface); box-shadow: inset 0 0 0 0 transparent; }
  35%, 65% { background-color: var(--accent-subtle); box-shadow: inset 0 0 0 2px var(--accent); }
}
@keyframes mmfem-solve-status-flash-b {
  0%, 100% { background-color: var(--surface); box-shadow: inset 0 0 0 0 transparent; }
  35%, 65% { background-color: var(--accent-subtle); box-shadow: inset 0 0 0 2px var(--accent); }
}
.mmfem-solve-status-flash-a {
  animation: mmfem-solve-status-flash-a 0.68s ease-in-out 3;
}
.mmfem-solve-status-flash-b {
  animation: mmfem-solve-status-flash-b 0.68s ease-in-out 3;
}
@media (prefers-reduced-motion: reduce) {
  .mmfem-solve-status-flash-a,
  .mmfem-solve-status-flash-b {
    animation-duration: 0.12s;
    animation-iteration-count: 1;
  }
}
"""
    js.document.head.appendChild(style)


class _WorkspaceLogBuffer(io.StringIO):
    """Capture solver output and publish it to the Solve log as it arrives."""

    def __init__(self, workspace):
        super().__init__()
        self.workspace = workspace
        self._pending = ""
        self._write_lock = threading.Lock()

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        with self._write_lock:
            count = super().write(text)
            self._pending += text
            complete = self._pending.splitlines(keepends=True)
            ready = [line.rstrip("\r\n") for line in complete if line.endswith(("\n", "\r"))]
            self._pending = "".join(line for line in complete if not line.endswith(("\n", "\r")))
        if ready:
            self.workspace.append_solver_output(ready)
        return count

    def flush(self):
        with self._write_lock:
            pending, self._pending = self._pending, ""
            super().flush()
        if pending.strip():
            self.workspace.append_solver_output([pending])


class StackHost(Div):
    """Hosts several panels, keeping them mounted and showing only one.

    Swapping ``ui_children`` unmounts the old subtree, and every (re)mounted
    component costs a websocket round trip — for a viewport that also means a
    full WebGPU canvas teardown and scene reconnect. So panels are built once
    and switching only toggles ``ui_hidden``.
    """

    def __init__(self, **kwargs):
        self._panels = {}
        super().__init__(**kwargs)

    def show(self, key, factory=None):
        comp = self._panels.get(key)
        if comp is None and factory is not None:
            self._panels[key] = comp = factory()
            self._sync_children()
        for k, c in self._panels.items():
            c.ui_hidden = k != key
        return comp

    def discard(self, key):
        if self._panels.pop(key, None) is not None:
            self._sync_children()

    def prune(self, keep):
        """Drop panels whose key is gone (``None`` is the placeholder key)."""
        stale = [k for k in self._panels if k is not None and k not in keep]
        for key in stale:
            del self._panels[key]
        if stale:
            self._sync_children()

    def _sync_children(self):
        self.ui_children = list(self._panels.values())


class WorkspaceModeToggle(Div):
    """Prominent switch between the Solve and Post Process workspaces."""

    _MODES = ("solve", "post_process")

    def __init__(self, value="post_process", on_change=None):
        if value not in self._MODES:
            raise ValueError(f"Unknown workspace mode: {value}")
        self._value = value
        self._on_change = on_change
        self._buttons = {}

        for mode, label in (("solve", "Solve"), ("post_process", "Post Process")):
            button = Div(label, ui_style=self._button_style(mode))
            button.on("click", lambda event=None, selected=mode: self.select(selected))
            self._buttons[mode] = button

        super().__init__(
            self._buttons["solve"], self._buttons["post_process"],
            ui_style=(
                "display:flex; width:264px; height:36px; flex:none; "
                "overflow:hidden; border:1px solid var(--border-strong); "
                "border-radius:var(--r-sm); background:var(--surface);"
            ),
        )

    def _button_style(self, mode):
        active = mode == self._value
        style = (
            "display:flex; flex:1; align-items:center; justify-content:center; "
            "height:36px; padding:0 14px; white-space:nowrap; cursor:pointer; "
            "user-select:none; font-size:13px; transition:background-color 100ms ease, color 100ms ease;"
        )
        if mode == "post_process":
            style += " border-left:1px solid var(--border);"
        if active:
            style += " background:var(--accent-subtle); color:var(--accent); font-weight:600;"
        else:
            style += " background:var(--surface); color:var(--fg-muted); font-weight:500;"
        return style

    @property
    def value(self):
        return self._value

    def select(self, mode):
        if mode not in self._MODES:
            raise ValueError(f"Unknown workspace mode: {mode}")
        if mode == self._value:
            return
        self._value = mode
        for key, button in self._buttons.items():
            button.ui_style = self._button_style(key)
        if self._on_change:
            self._on_change(mode)


class Panel(StackHost):
    def __init__(self, app_data):
        self.app_data = app_data
        self.comp = None
        super().__init__(ui_class=panel_full)
        self.set_tab()

    def set_tab(self):
        name = self.app_data.active_tab
        tab = self.app_data.get_tab(name) if name is not None else None
        if tab is None:
            self.comp = None
            self.show(None)
            self.prune(set(self.app_data.get_tabs()))
            return
        comp = tab.get("component")
        if comp is None:
            cls = self._resolve_class(tab["type"])
            comp = cls(tab["name"], tab["data"], app_data=self.app_data)
            tab["component"] = comp
        if self._panels.get(name) is not comp:
            self.discard(name)   # tab reloaded under the same name
        self.comp = self.show(name, lambda: comp)
        self.prune(set(self.app_data.get_tabs()))

    def _resolve_class(self, type_key):
        from .registry import get_component_info

        info = get_component_info(type_key)
        if info is None:
            raise ValueError(f"Unknown component type: {type_key}")
        return info["cls"]


class Settings(QMenu):
    """Designer-styled settings dropdown (sections, segmented theme, switches)."""

    def __init__(self, app):
        self.app = app
        us = app.usersettings

        theme_seg = Segmented(
            [("light", "Light"), ("dark", "Dark"), ("system", "System")],
            us.get("theme", "system"), self._on_theme,
        )
        colormap_select = QSelect(
            ui_options=[
                {"label": "Auto (theme)", "value": ""},
                "rainbow", "turbo", "viridis", "plasma", "cet_l20",
                "matlab:jet", "matplotlib:coolwarm",
            ],
            ui_model_value=us.get("default_colormap", ""),
            ui_dense=True, ui_options_dense=True, ui_emit_value=True, ui_map_options=True,
            ui_style="width: 134px;",
        )
        colormap_select.on_update_model_value(us.update("default_colormap"))

        ncolors = QInput(
            QTooltip("Number of color bands when colorbars are discrete."),
            ui_type="number", ui_dense=True,
            ui_model_value=int(us.get("default_ncolors", 8)), ui_style="width: 72px;",
        )
        ncolors.on_update_model_value(us.update("default_ncolors"))

        vecdensity = QInput(
            QTooltip("Default arrow grid density for vector fields "
                     "(cells across the longest domain side)."),
            ui_type="number", ui_dense=True,
            ui_model_value=int(us.get("default_vector_grid_size", 20)),
            ui_style="width: 72px;",
        )
        vecdensity.on_update_model_value(us.update("default_vector_grid_size"))

        subdiv = QInput(
            QTooltip("Default subdivision for curved surface elements "
                     "(-1 auto, 0 linear, higher = finer)."),
            ui_type="number", ui_dense=True,
            ui_model_value=int(us.get("default_subdivision", -1)),
            ui_style="width: 72px;",
        )
        subdiv.on_update_model_value(us.update("default_subdivision"))
        subdiv3d = QInput(
            QTooltip("Default tessellation for curved 3D (volume) elements "
                     "(-1 auto, 0 linear, higher = finer)."),
            ui_type="number", ui_dense=True,
            ui_model_value=int(us.get("default_elements3d_subdivision", -1)),
            ui_style="width: 72px;",
        )
        subdiv3d.on_update_model_value(us.update("default_elements3d_subdivision"))

        nthreads = QInput(
            QTooltip("Threads used by NGSolve (0 = all cores). Restart to apply."),
            ui_type="number", ui_dense=True, ui_model_value=us.get("nthreads", 0),
            ui_style="width: 72px;",
        )
        nthreads.on_update_model_value(us.update("nthreads"))
        redraw = QInput(
            QTooltip("Min milliseconds between redraws while a script runs (0 = no throttle)."),
            ui_type="number", ui_dense=True, ui_suffix="ms",
            ui_model_value=int(us.get("redraw_interval_ms", 50)), ui_style="width: 96px;",
        )
        redraw.on_update_model_value(self._on_redraw)
        gpu = QSelect(
            QTooltip("Preferred GPU adapter. Restart to apply."),
            ui_options=["high-performance", "low-power"],
            ui_model_value=us.get("gpu_power_preference", "high-performance"),
            ui_dense=True, ui_options_dense=True, ui_emit_value=True,
            ui_style="width: 134px;",
        )
        gpu.on_update_model_value(us.update("gpu_power_preference"))

        timer_item = Div(
            QIcon(ui_name="mdi-timer-outline"), "Timer / profiling…",
            Div("diagnostics", ui_class=cb.menu_item_meta), ui_class=cb.menu_item,
        )
        timer_item.on("click", lambda e=None: app._open_timers())

        super().__init__(Div(
            Div("Appearance", ui_class=cb.menu_h),
            self._row("Theme", theme_seg),
            self._row("Default colormap", colormap_select),
            self._row("Discrete colormap", self._switch("default_discrete_colormap", False)),
            self._row("Default N colors", ncolors),
            Div(ui_class=cb.menu_sep),
            Div("Defaults", ui_class=cb.menu_h),
            self._row("Show axes", self._switch("axes_visible", True)),
            self._row("Show navigation cube", self._switch("navcube_visible", False)),
            self._row("Default vector density", vecdensity),
            Div(ui_class=cb.menu_sep),
            Div("Rendering", ui_class=cb.menu_h),
            self._row("Default subdivision", subdiv),
            self._row("Default 3D subdivision", subdiv3d),
            Div(ui_class=cb.menu_sep),
            Div("Performance", ui_class=cb.menu_h),
            self._row("Worker threads", nthreads),
            self._row("Redraw interval", redraw),
            self._row("GPU preference", gpu),
            Div(ui_class=cb.menu_sep),
            timer_item,
            ui_class=cb.menu_card,
        ))

    def _row(self, label, control):
        return Div(Div(label, ui_class=cb.menu_label), control, ui_class=cb.menu_row)

    def _scls(self, on):
        return str(cb.prop_switch) + (" " + str(cb.prop_switch_on) if on else "")

    def _switch(self, key, default, on_set=None):
        state = {"v": bool(self.app.usersettings.get(key, default))}
        sw = Div(ui_class=self._scls(state["v"]))

        def toggle(e=None):
            state["v"] = not state["v"]
            sw.ui_class = self._scls(state["v"])
            self.app.usersettings.set(key, state["v"])
            if on_set:
                on_set(state["v"])
        sw.on("click", toggle)
        return sw

    def _on_theme(self, val):
        self.app.usersettings.set("theme", val)
        cb.set_theme(self.app, val)
        self.app._apply_viewport_theme()

    def _on_redraw(self, event):
        v = int(event.value)
        self.app.usersettings.set("redraw_interval_ms", v)
        self.app._redraw_interval = max(0, v) / 1000.0




class NGSolveGui(App):
    def __init__(self, filename=None, local_path=None, script_args=None):
        self._local_path = local_path
        self.script_args = list(script_args or [])
        self.app_data = AppData()

        # -- Toolbar buttons (compact flat icon buttons, muted like the designer) --
        def _tbtn(icon, tip, handler=None):
            btn = QBtn(
                QTooltip(tip), ui_flat=True, ui_dense=True, ui_icon=icon,
                ui_class=str(cb.topbar_icon),
            )
            if handler is not None:
                btn.on_click(handler)
            return btn

        # File actions
        upload_file = _tbtn(
            "mdi-file-plus-outline", "Load file  ·  geometry / mesh / .py", self._load_file
        )
        savebtn = _tbtn("mdi-content-save-outline", "Save Project", self.save_project)
        loadbtn = _tbtn("mdi-folder-open-outline", "Load Project", self.load_project)
        file_group = Div(upload_file, savebtn, loadbtn, ui_class=cb.tb_group)

        # Settings + quit (panel toggles removed — sidebars are draggable;
        # theme lives in the settings menu).
        settings_btn = QBtn(
            Settings(self), QTooltip("User Settings"),
            ui_flat=True, ui_dense=True, ui_icon="mdi-cog-outline",
            ui_class=str(cb.topbar_icon),
        )
        close_btn = _tbtn("mdi-close", "Quit", self.quit)
        close_btn.ui_class = str(cb.topbar_icon) + " " + str(cb.topbar_icon_danger)
        view_group = Div(settings_btn, close_btn, ui_class=cb.tb_group)

        ngs_logo = Div(
            QImg(
                ui_src=self.load_asset("ngsolve-mark.png"),
                ui_height="34px",
                ui_width="34px",
                ui_fit="contain",
            ),
            Div("MM-FEM", ui_class=cb.brand_wordmark),
            ui_class=cb.brand,
        )
        self._workspace_mode_toggle = WorkspaceModeToggle(
            "post_process", self._set_workspace_mode
        )

        self.system_monitor = SystemMonitor() if system_monitor_available() else None

        # -- Redraw throttling state --
        self._redraw_lock = threading.Lock()
        self._redraw_pending = False
        self._redraw_timer_running = False
        self._last_redraw_time = 0.0
        self._redraw_interval = max(0, int(self.usersettings.get("redraw_interval_ms", 50))) / 1000.0

        # Keep this app bar's established layout stable: brand and file actions
        # stay at the left, system status and settings/quit stay at the right.
        bar = QBar(
            ngs_logo,
            file_group,
            QSpace(),
            *([self.system_monitor, Div(ui_class=cb.tb_sep)] if self.system_monitor is not None else []),
            view_group,
            ui_class=cb.app_bar,
        )
        workspace_mode_bar = Div(
            self._workspace_mode_toggle,
            ui_style=(
                "display:flex; flex:0 0 52px; height:52px; width:100%; "
                "box-sizing:border-box; align-items:center; justify-content:center; "
                "padding:8px 12px; background:var(--panel-header); "
                "border-bottom:1px solid var(--border);"
            ),
        )

        # Three-column layout using flex
        self.navigator = Navigator(self.app_data, self._click_tab, self._load_file)
        # The property panel is owned by each component; this host swaps in the
        # active component's own panel (built via comp.build_property_panel()).
        self.property_host = StackHost(ui_class=panel_full)
        self.property_host.show(None, empty_property_panel)
        self.tab_panel = Panel(self.app_data)
        self.controls = ControlBar()
        self.footer = StatusFooter()

        from .meshing_preview import install as _install_meshing_preview

        _install_meshing_preview(self)

        self._nav_visible = self.usersettings.get("nav_visible", True)
        self._prop_visible = self.usersettings.get("prop_visible", True)
        self._nav_width = self.usersettings.get("nav_width", 200)
        self._prop_width = self.usersettings.get("prop_width", 280)

        self.kb = KeybindingManager(self, theme=kb_theme)

        # Inner splitter: center | property panel (reverse so model = prop width)
        self._inner_splitter = QSplitter(
            ui_model_value=self._prop_width if self._prop_visible else 0,
            ui_unit="px",
            ui_reverse=True,
            ui_limits=[0, 500] if self._prop_visible else [0, 0],
            ui_emit_immediately=True,
            ui_slots={
                "before": [Div(self.tab_panel, ui_class=flex_fill)],
                "after": [self.property_host],
            },
            ui_class=cb.body_inner,
        )
        self._inner_splitter.on_update_model_value(self._on_prop_width_change)

        # Outer splitter: navigator | inner splitter
        self._outer_splitter = QSplitter(
            ui_model_value=self._nav_width if self._nav_visible else 0,
            ui_unit="px",
            ui_limits=[0, 500] if self._nav_visible else [0, 0],
            ui_emit_immediately=True,
            ui_slots={
                "before": [self.navigator],
                "after": [self._inner_splitter],
            },
            ui_class=cb.body_fill,
        )
        self._outer_splitter.on_update_model_value(self._on_nav_width_change)

        page = self._outer_splitter
        self._post_process_workspace = Div(
            page, self.controls, self.footer,
            ui_style=(
                "display:flex; flex-direction:column; flex:1 1 auto; "
                "min-height:0; width:100%;"
            ),
        )
        self.solve_workspace = SolveWorkspace(
            on_run=lambda: self._run_axisymmetric_solver(mesh_only=False),
            on_mesh=lambda: self._run_axisymmetric_solver(mesh_only=True),
            on_open_file=self._open_run_result,
        )
        self._solve_workspace = Div(
            self.solve_workspace,
            ui_style="flex:1 1 auto; min-height:0; width:100%;",
            ui_hidden=True,
        )

        # Timer / profiling diagnostics dialog (opened from the settings menu).
        self._timer_body = Div()
        timer_refresh = QBtn(QTooltip("Refresh"), ui_icon="mdi-refresh",
                             ui_flat=True, ui_dense=True, ui_round=True, ui_size="sm")
        timer_refresh.on_click(lambda *a: self._refresh_timers())
        self._timer_dialog = QDialog(QCard(
            Div(Div("Timer / profiling", ui_class=cb.prop_title_text), timer_refresh,
                ui_class=cb.prop_title),
            QSeparator(),
            self._timer_body,
            ui_class="cb-timer-card",
        ))

        super().__init__(
            bar, workspace_mode_bar, self._post_process_workspace,
            self._solve_workspace, self._timer_dialog,
            self.kb.indicator, self.kb.help_overlay,
            ui_class=str(cb.app_root),
        )

        # Keep the footer's mode indicator in sync with the keybinding manager.
        self._wire_footer_mode()

        cb.install(self, default_theme=self.usersettings.get("theme", "system"))
        from .webgpu_tab import sync_default_viewport_clear
        sync_default_viewport_clear()
        keybinding_styles.inject(self)
        self.call_js(_inject_solve_status_styles)
        self.on_load(self.__on_load)

        # Post Process is the existing/default view. Retain its hidden tree so
        # the viewport and loaded data survive a mode switch to Solve.
        self._workspace_mode = "post_process"

        # -- Global keybindings (always active) --
        kb = self.kb
        kb.add("h", kb.toggle_help, "Show keyboard shortcuts", "General")
        kb.add("ctrl+b", self._toggle_navigator, "Toggle navigator", "Panels")
        kb.add(
            "ctrl+alt+b", self._toggle_property_panel, "Toggle property panel", "Panels"
        )
        for i in range(1, 10):
            kb.add(
                str(i),
                lambda n=i: self.navigator.select_by_index(n),
                f"Select item {i}",
                "Navigation",
            )
        self.add_keybinding("escape", lambda e: self.kb.on_escape())
        self.on_before_save(self.__on_before_save)
        if isinstance(filename, str):
            self._load_with_status(filename)
        elif isinstance(filename, list):
            for f in filename:
                self._load_with_status(f)

    def _set_workspace_mode(self, mode):
        if mode not in WorkspaceModeToggle._MODES:
            raise ValueError(f"Unknown workspace mode: {mode}")

        solving = mode == "solve"
        self._workspace_mode = mode
        self._post_process_workspace.ui_hidden = solving
        self._solve_workspace.ui_hidden = not solving

    def save_project(self):
        """Save a declarative axisymmetric project in Solve, or the GUI state otherwise."""
        if self._workspace_mode != "solve":
            return self.save_local()
        from ngapp.utils import get_environment

        model = self.solve_workspace.model
        errors = self.solve_workspace.validation_errors()
        # Empty sketches are useful drafts; structural errors still prevent
        # saving, while the status message explains the missing geometry.
        try:
            data = package_model(model, self.solve_workspace.studies, self.solve_workspace.layout)
            title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in model.get("name", "axisymmetric-model")).strip()
            filename = (title or "axisymmetric-model") + ".ngsmodel"
            write = get_environment().begin_save_file_local(filename)
            if write is None:
                return
            write(data)
            self._notify("Axisymmetric model saved.", type="positive", timeout=2500)
            self.solve_workspace._message(f"Saved {filename}.")
            if errors:
                self._notify("Model saved as a draft. Review Geometry and Physics before running.", type="warning", timeout=5000)
        except Exception as error:
            self._notify(f"Could not save model: {error}", type="negative", timeout=7000)
            self.solve_workspace._message(f"Save failed: {error}", error=True)

    def load_project(self):
        """Load the active workspace's native project format."""
        if self._workspace_mode != "solve":
            return self.load_local()
        from ngapp.utils import EnvironmentType, get_environment

        env = get_environment()
        use_browser_picker = os.environ.get("NGSOLVE_GUI_FILE_PICKER", "").strip().lower() == "browser"
        try:
            if env.type == EnvironmentType.LOCAL_APP and not use_browser_picker:
                from .native_dialog import open_file_dialog

                path = open_file_dialog(
                    title="Open NGSolve axisymmetric model",
                    initialdir=self._local_path or self.usersettings.get("load_dir", "") or os.path.expanduser("~"),
                    filters=[("NGSolve model", "*.ngsmodel")],
                )
                if not path:
                    return
                self._local_path = os.path.dirname(path)
                self.usersettings.set("load_dir", self._local_path)
                with open(path, "rb") as model_file:
                    data = model_file.read()
            else:
                handles = self.js.showOpenFilePicker({
                    "multiple": False,
                    "types": [{"description": "NGSolve axisymmetric model", "accept": {"application/zip": [".ngsmodel"]}}],
                })
                if not handles:
                    return
                js_file = handles[0].getFile()
                data = js_file.arrayBuffer()
            model, studies, layout = unpack_model(data)
            self.solve_workspace.set_model(model, studies, layout)
            if self._workspace_mode != "solve":
                self._workspace_mode_toggle.select("solve")
            self._notify("Axisymmetric model loaded.", type="positive", timeout=2500)
        except Exception as error:
            self._notify(f"Could not load model: {error}", type="negative", timeout=7000)
            self.solve_workspace._message(f"Load failed: {error}", error=True)

    def _run_axisymmetric_solver(self, *, mesh_only=False):
        """Run a saved model on the machine hosting this GUI, off the UI thread."""
        workspace = self.solve_workspace
        model = copy.deepcopy(workspace.model)
        studies = copy.deepcopy(workspace.studies)
        layout = copy.deepcopy(workspace.layout)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        model_name = re.sub(r"[^A-Za-z0-9_-]+", "_", model.get("name", "axisymmetric_model")).strip("_") or "axisymmetric_model"
        output_dir = Path.home() / "NGSolve_results" / f"{model_name}_{stamp}"
        output_dir.mkdir(parents=True, exist_ok=True)
        model_path = output_dir / f"{model_name}.ngsmodel"

        try:
            from .axisymmetric_model import evaluate_expression

            h_global = evaluate_expression(model.get("mesh", {}).get("element_size", "0.01"), {
                item["name"]: item["expression"] for item in model.get("parameters", [])
            })
            if h_global <= 0:
                raise ValueError("Target element size must be positive")
            model_path.write_bytes(package_model(model, studies, layout))
        except Exception as error:
            workspace.finish_solver_job(f"Could not prepare solver input: {error}", error=True)
            return

        workspace._message(("Generating mesh" if mesh_only else "Running study") + f" in {output_dir}")

        def run_job():
            log_buffer = _WorkspaceLogBuffer(workspace)
            try:
                solver_root = self._find_solver_root()
                solver_entry = solver_root / "main.py"
                if not solver_entry.is_file():
                    raise FileNotFoundError(
                        "Could not find the coupled_solver checkout containing main.py. "
                        "Open NGSolve GUI from the coupled_solver directory."
                    )
                root_text = str(solver_root)
                src_text = str(solver_root / "src")
                for entry in (root_text, src_text):
                    if entry not in sys.path:
                        sys.path.insert(0, entry)
                spec = importlib.util.spec_from_file_location("_ngsolve_gui_solver_main", solver_entry)
                if spec is None or spec.loader is None:
                    raise ImportError(f"Could not load solver entry point: {solver_entry}")
                solver_module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(solver_module)
                with contextlib.redirect_stdout(log_buffer), contextlib.redirect_stderr(log_buffer):
                    result = solver_module.main(
                        h_global=h_global,
                        p=int(model.get("mesh", {}).get("polynomial_order", 3)),
                        problem_name=str(model_path),
                        h_local=h_global,
                        dt=1e-3,
                        output_path=str(output_dir),
                        mesh_only=mesh_only,
                    )
                log_buffer.flush()
                run_kind = "Mesh" if mesh_only else "Study"
                run_name = f"{run_kind}: {model.get('name', 'axisymmetric model')}"
                if mesh_only:
                    workspace.finish_solver_job(
                        f"Mesh generated successfully: {output_dir}",
                        run_kind=run_kind,
                        run_name=run_name,
                        output_path=output_dir,
                    )
                else:
                    fields_dir = output_dir / "ngsolve_gui" / "fields"
                    workspace.finish_solver_job(
                        f"Study complete. GUI-ready fields: {fields_dir}",
                        run_kind=run_kind,
                        run_name=run_name,
                        output_path=output_dir,
                    )
                    self._notify(f"Study complete. Results saved to {output_dir}", type="positive", timeout=7000)
            except Exception as error:
                log_buffer.flush()
                workspace.finish_solver_job(
                    f"Solver failed: {error}",
                    error=True,
                    run_kind="Mesh" if mesh_only else "Study",
                    run_name=f"{'Mesh' if mesh_only else 'Study'}: {model.get('name', 'axisymmetric model')}",
                    output_path=output_dir,
                )
                self._notify(f"Solver failed: {error}", type="negative", timeout=9000)

        threading.Thread(target=run_job, name="NGSolveAxisymmetricStudy", daemon=True).start()

    def _open_run_result(self, filename):
        """Open a generated field in the existing Post Process workspace."""
        if not os.path.isfile(filename):
            self.solve_workspace._message(f"Result file no longer exists: {filename}", error=True)
            return
        self._set_workspace_mode("post_process")
        self._load_with_status(filename)

    @staticmethod
    def _find_solver_root():
        candidates = []
        configured = os.environ.get("COUPLED_SOLVER_ROOT")
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.append(Path.cwd())
        candidates.append(Path(sys.argv[0]).expanduser().resolve().parent)
        expanded = []
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve()
            except OSError:
                continue
            expanded.extend([resolved, *resolved.parents])
        for candidate in expanded:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if (resolved / "main.py").is_file() and (resolved / "src" / "axi").is_dir():
                return resolved
        raise FileNotFoundError("The coupled_solver directory is not available from this GUI process")

    def _load_file(self):
        from ngapp.utils import EnvironmentType, get_environment

        # A local app normally uses the host OS dialog. Headless hosts (for
        # example a GUI served from a VirtualBox guest) have no desktop for
        # that dialog, so they can explicitly use the browser's file picker.
        use_browser_picker = (
            os.environ.get("NGSOLVE_GUI_FILE_PICKER", "").strip().lower()
            == "browser"
        )
        if (
            get_environment().type == EnvironmentType.LOCAL_APP
            and not use_browser_picker
        ):
            from .native_dialog import open_file_dialog

            initialdir = (
                self._local_path
                or self.usersettings.get("load_dir", "")
                or os.path.expanduser("~")
            )
            file_path = open_file_dialog(initialdir=initialdir)
            if file_path:
                self._local_path = os.path.dirname(file_path)
                self.usersettings.set("load_dir", self._local_path)
        else:
            file_path = self._pick_file_to_temp()
        self._load_with_status(file_path)

    def _pick_file_to_temp(self):
        """Browser-picker fallback: the File System Access API only hands us the
        file's content and name (never a real path), so materialise it in a temp
        dir keeping the original name (preserves multi-part suffixes like
        .vol.gz that the loader dispatches on)."""
        import tempfile

        try:
            handles = self.js.showOpenFilePicker({"multiple": False})
        except Exception:
            return None
        if not handles:
            return None
        js_file = handles[0].getFile()
        tmpdir = tempfile.mkdtemp(prefix="ngsolve_gui_")
        file_path = os.path.join(tmpdir, js_file.name)
        with open(file_path, "wb") as f:
            f.write(js_file.arrayBuffer())
        return file_path

    def _load_with_status(self, filename):
        if not filename:
            return
        self.controls.clear()
        result = load_file(filename, self)
        if result:
            thread, done_event = result
            name = os.path.basename(str(filename))
            self.footer.show(name, thread, done_event)

    def __on_before_save(self):
        self.storage.set("app_data", self.app_data.get_save_data(), use_pickle=True)

    def __on_load(self):
        data = self.storage.get("app_data")
        if data is not None:
            self.app_data._data.update(data)
        self._update()
        self.app_data._update = self._update

    def _click_tab(self, tabname):
        if tabname == self.app_data.active_tab and self.tab_panel.comp is not None:
            return
        self.app_data.active_tab = tabname
        self.tab_panel.set_tab()
        self.navigator.update()
        self._activate(tabname)

    def _activate(self, tabname):
        """Point property panel, footer and keybindings at the active tab."""
        comp = self.tab_panel.comp
        tab = self.app_data.get_tab(tabname) if tabname else None
        if comp is not None and hasattr(comp, "sync_camera"):
            comp.sync_camera()
        self._show_property_panel(comp)
        self.footer.set_component(comp, tab.get("type", "") if tab else "")
        self.kb.set_component(comp)

    def _show_property_panel(self, comp):
        """Host the active component's own property panel (or a placeholder).

        Panels are kept mounted per component (keyed by the component itself) —
        rebuilding one means re-creating every section on every tab click.
        """
        live = {t["component"] for t in self.app_data.get_tabs().values()
                if "component" in t}
        self.property_host.prune(live)
        if comp is None or not hasattr(comp, "build_property_panel"):
            self.property_host.show(None, empty_property_panel)
            return
        self.property_host.show(comp, comp.build_property_panel)

    def _toggle_navigator(self):
        self._nav_visible = not self._nav_visible
        self.usersettings.set("nav_visible", self._nav_visible)
        self._apply_panel_visibility()

    def _toggle_property_panel(self):
        self._prop_visible = not self._prop_visible
        self.usersettings.set("prop_visible", self._prop_visible)
        self._apply_panel_visibility()

    def _wire_footer_mode(self):
        """Mirror the keybinding manager's active mode into the footer."""
        kb = self.kb
        orig_enter = kb._enter_mode
        orig_exit = kb._exit_mode

        def _enter(name):
            orig_enter(name)
            self.footer.set_mode(kb._mode)

        def _exit():
            orig_exit()
            self.footer.set_mode(None)

        kb._enter_mode = _enter
        kb._exit_mode = _exit

    def _on_nav_width_change(self, event):
        val = int(event.value)
        if val > 0:
            self._nav_width = val
            self.usersettings.set("nav_width", val)

    def _on_prop_width_change(self, event):
        val = int(event.value)
        if val > 0:
            self._prop_width = val
            self.usersettings.set("prop_width", val)

    def _apply_panel_visibility(self):
        if not hasattr(self, "_outer_splitter"):
            return
        if self._nav_visible:
            self._outer_splitter.ui_model_value = self._nav_width
            self._outer_splitter.ui_limits = [0, 500]
        else:
            self._outer_splitter.ui_model_value = 0
            self._outer_splitter.ui_limits = [0, 0]
        if self._prop_visible:
            self._inner_splitter.ui_model_value = self._prop_width
            self._inner_splitter.ui_limits = [0, 500]
        else:
            self._inner_splitter.ui_model_value = 0
            self._inner_splitter.ui_limits = [0, 0]

    def _open_timers(self):
        self._refresh_timers()
        self._timer_dialog.ui_model_value = True

    def _refresh_timers(self):
        try:
            import ngsolve
            timers = sorted(ngsolve.Timers(), key=lambda t: -t.get("time", 0.0))[:40]
        except Exception:
            timers = []
        if not timers:
            rows = [Div("No timing data recorded yet.",
                        ui_class=cb.hint, ui_style="padding: 12px;")]
        else:
            rows = []
            for t in timers:
                rows.append(Div(
                    Div(t.get("name", ""), ui_class="ellipsis", ui_style="flex: 1; min-width: 0;"),
                    Div(f"{t.get('time', 0.0):.4f} s", ui_class=cb.mono, ui_style="flex: none;"),
                    ui_class="row items-center no-wrap " + str(cb.gap_sm),
                    ui_style="padding: 4px 2px; border-bottom: 1px solid var(--border-faint);"
                             " font-size: 12px;",
                ))
        self._timer_body.ui_children = [
            Div(*rows, ui_style="max-height: 56vh; overflow: auto; min-width: 400px; padding: 4px 14px 12px;")
        ]

    def _apply_viewport_theme(self):
        """Update the scene background of every open 3D tab to the active theme."""
        from .webgpu_tab import sync_default_viewport_clear
        sync_default_viewport_clear()  # so tabs opened later also start correct
        for tab in self.app_data.get_tabs().values():
            comp = tab.get("component")
            if comp is not None and hasattr(comp, "apply_viewport_theme"):
                comp.apply_viewport_theme()


    def redraw(self, *args, **kwargs):
        self.app_data.set_needs_redraw()
        self._request_redraw()

    def _request_redraw(self):
        """Coalesce rapid redraw calls into at most one actual redraw per interval.

        When a Python script calls ngs.Redraw() in a tight loop (e.g. time-stepping),
        this avoids blocking the script thread and flooding the GPU with renders.

        The trailing-edge timer guarantees the *last* requested redraw is always
        rendered, even if no further calls arrive.
        """
        interval = self._redraw_interval
        if interval <= 0:
            # No throttling — redraw immediately on every call.
            self._do_redraw()
            return

        now = time.monotonic()
        with self._redraw_lock:
            self._redraw_pending = True
            elapsed = now - self._last_redraw_time
            if elapsed >= interval:
                # Enough time passed — redraw immediately (leading edge).
                self._do_redraw()
            elif not self._redraw_timer_running:
                # Schedule a trailing-edge timer so the last redraw is never lost.
                self._redraw_timer_running = True
                delay = interval - elapsed
                threading.Timer(delay, self._deferred_redraw).start()

    def _deferred_redraw(self):
        """Trailing-edge timer callback — guarantees the final redraw fires."""
        with self._redraw_lock:
            self._redraw_timer_running = False
            if self._redraw_pending:
                self._do_redraw()

    def _do_redraw(self):
        """Actually perform the redraw (must be called with _redraw_lock held, or interval=0)."""
        self._redraw_pending = False
        self._last_redraw_time = time.monotonic()
        comp = self.tab_panel.comp
        if comp is not None and hasattr(comp, "redraw"):
            comp.redraw()

    def _update(self):
        self.navigator.update()
        self.tab_panel.set_tab()
        self._activate(self.app_data.active_tab)
