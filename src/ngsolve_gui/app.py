import os
import sys
import threading
import time
import copy
import io
import hashlib
import json
import mimetypes
import re
import subprocess
import tempfile
import queue
from datetime import datetime as _datetime, timezone as _timezone
from pathlib import Path

from ngapp.app import App
from ngapp.components import *
from ngapp.utils import UserSettings

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
from .mesh_geometry import axisymmetric_mesh_edge_polylines
from .axisymmetric_model import new_model, new_studies, unpack_model, package_model, serialize_model_json
from .three_d_project import PROJECT_SUFFIX, package_3d_project, unpack_3d_project
from . import __version__
from .branding import AXISYMMETRIC_ICON, PRODUCT_DESCRIPTION, PRODUCT_NAME
from .documentation import find_documentation_index
from .project_thumbnails import cached_thumbnail, generate_project_thumbnail
from .result_files import (
    MESH_SUFFIX,
    SOLUTION_SUFFIX,
    make_mesh_bundle,
    make_solution_bundle,
    problem_fingerprint,
    read_mesh_bundle,
    write_solution_bundle,
)


APP_MENU_BAR_HEIGHT = 32
APP_HEADER_HEIGHT = 50


def _solver_process_failure_detail(return_code):
    """Describe an exited solver process without hiding native status codes."""
    status = int(return_code)
    return (
        f"Solver process exited with status {status} "
        f"(0x{status & 0xFFFFFFFF:08X}). See solver.log for details."
    )


def _solver_subprocess_environment():
    """Keep the GUI package and configured modules importable after cwd changes."""
    environment = os.environ.copy()
    package_root = Path(__file__).resolve().parent.parent
    configured_paths = environment.get("PYTHONPATH", "").split(os.pathsep)
    paths = [package_root]
    paths.extend(Path(value) if value else Path.cwd() for value in configured_paths)

    normalized = []
    seen = set()
    for value in paths:
        path = value if value.is_absolute() else Path.cwd() / value
        absolute = os.path.abspath(path)
        key = os.path.normcase(absolute)
        if key not in seen:
            seen.add(key)
            normalized.append(absolute)
    environment["PYTHONPATH"] = os.pathsep.join(normalized)
    return environment


def _reap_solver_process(process):
    """Boundedly stop and reap a solver child left alive by an error path."""
    if process is None:
        return True
    try:
        running = process.poll() is None
    except OSError:
        return False
    if running:
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                return False
            except OSError:
                return False
    else:
        try:
            process.wait()
        except OSError:
            return False
    try:
        return process.poll() is not None
    except OSError:
        return False


def _request_guest_vm_shutdown():
    # Only the packaged VM sets this flag; standalone launches quit normally.
    if os.environ.get("MMFEM_VM_GUEST") != "1":
        return

    result = subprocess.run(
        ["sudo", "-n", "/usr/bin/systemctl", "poweroff"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            detail or f"Guest shutdown exited with status {result.returncode}."
        )

    # Let the guest-side disconnect watchdog avoid issuing a duplicate request.
    try:
        (Path.home() / ".mmfem-vm-shutdown-requested").write_text(
            "File menu requested shutdown.\n", encoding="utf-8"
        )
    except OSError:
        pass


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
@keyframes mmfem-condition-edge-pulse {
  0%, 100% { stroke-opacity: .72; stroke-width: 3.1px; }
  50% { stroke-opacity: 1; stroke-width: 4px; }
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
  [style*="mmfem-condition-edge-pulse"] {
    animation: none !important;
  }
}
.mmfem-compact-app-bar .cb-monitor {
  box-sizing: border-box;
  flex: 0 0 464px;
  width: 464px;
}
.mmfem-compact-app-bar .cb-monitor-stat {
  box-sizing: border-box;
  flex: 0 0 116px;
  min-width: 116px;
  width: 116px;
  gap: 6px;
  padding-left: 8px;
  padding-right: 8px;
}
.mmfem-compact-app-bar .cb-monitor-value { font-size: 9.5px; }
.mmfem-compact-app-bar .cb-monitor-subvalue,
.mmfem-compact-app-bar .cb-monitor-subbar { display: none; }
@media (max-width: 1600px) {
  .mmfem-compact-app-bar .cb-monitor { flex-basis: 420px; width: 420px; }
  .mmfem-compact-app-bar .cb-monitor-stat {
    flex-basis: 105px;
    min-width: 105px;
    width: 105px;
    gap: 5px;
    padding-left: 6px;
    padding-right: 6px;
  }
  .mmfem-compact-app-bar .cb-monitor-icon { display: none; }
}
@media (max-width: 1280px) {
  .mmfem-compact-app-bar .cb-monitor { flex-basis: 336px; width: 336px; }
  .mmfem-compact-app-bar .cb-monitor-stat {
    flex-basis: 84px;
    min-width: 84px;
    width: 84px;
    gap: 4px;
    padding-left: 4px;
    padding-right: 4px;
  }
  .mmfem-compact-app-bar .cb-monitor-label {
    font-size: 8px;
    letter-spacing: 0;
  }
  .mmfem-compact-app-bar .cb-monitor-value { font-size: 9px; }
}
"""
    js.document.head.appendChild(style)


def _inject_recent_project_tile_styles(js):
    style = js.document.createElement("style")
    style.id = "mmfem-recent-project-tile-styles"
    style.textContent = """
.mmfem-recent-project-tile {
  border: 1px solid var(--border);
}
.mmfem-recent-project-tile:hover {
  border-color: var(--accent);
  box-shadow: 0 4px 14px #14263816;
  transform: translateY(-1px);
}
.mmfem-recent-project-tile.is-selected {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}
.mmfem-recent-project-tile:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
.mmfem-recent-project-card .mmfem-recent-project-remove {
  opacity: .72;
  transition: opacity .12s ease;
}
.mmfem-recent-project-card:hover .mmfem-recent-project-remove,
.mmfem-recent-project-card:focus-within .mmfem-recent-project-remove {
  opacity: 1;
}
.mmfem-recent-project-image img {
  object-fit: contain;
}
@media (max-width: 620px) {
  .mmfem-recent-project-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr)) !important;
  }
}
@media (prefers-reduced-motion: reduce) {
  .mmfem-recent-project-tile,
  .mmfem-recent-project-card .mmfem-recent-project-remove {
    transition: none;
  }
}
"""
    existing = js.document.getElementById(style.id)
    if existing:
        existing.remove()
    js.document.head.appendChild(style)


def _set_window_title(js):
    """Give the standalone browser window the product name."""
    js.document.title = PRODUCT_NAME


def _set_window_icon(js, icon_data_uri):
    """Replace ngapp's default favicon with the MagMechSim application icon."""
    document = js.document
    old_icons = document.querySelectorAll(
        'link[rel="icon"], link[rel="shortcut icon"]'
    )
    for index in range(old_icons.length):
        old_icons.item(index).remove()

    icon = document.createElement("link")
    icon.rel = "icon"
    icon.type = "image/png"
    icon.href = icon_data_uri
    document.head.appendChild(icon)


def _schedule_initial_renderer_frame_pulse(js):
    """Commit a short identity transform so Chromium presents its first surface.

    The native startup gate has observed cases where the mounted page and a
    Chromium capture were correct while the Windows app window remained white.
    A temporary compositor layer update requests a real renderer frame without
    resizing the page or changing model state. The original inline styles are
    restored within three animation frames.
    """
    js.eval("""(() => {
  const root = document.querySelector('#q-app');
  if (!root || root.dataset.mmfemStartupPaintPulse === 'pending'
      || root.dataset.mmfemStartupPaintPulse === 'complete') return;
  const originalTransform = root.style.transform;
  const originalWillChange = root.style.willChange;
  root.dataset.mmfemStartupPaintPulse = 'pending';
  root.style.willChange = 'transform';
  root.style.transform = originalTransform
    ? `${originalTransform} translateZ(0)` : 'translateZ(0)';
  requestAnimationFrame(() => requestAnimationFrame(() => {
    root.style.transform = originalTransform;
    requestAnimationFrame(() => {
      root.style.willChange = originalWillChange;
      root.dataset.mmfemStartupPaintPulse = 'complete';
    });
  }));
})();""")


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


def _axisymmetric_mesh_edges(mesh):
    """Return unique mesh edges, sampling curved boundaries for display."""
    return axisymmetric_mesh_edge_polylines(mesh)


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
    """Keyboard-accessible switch between the Model and Results workspaces."""

    _MODES = ("solve", "post_process")

    def __init__(self, value="solve", on_change=None):
        if value not in self._MODES:
            raise ValueError(f"Unknown workspace mode: {value}")
        self._value = value
        self._on_change = on_change
        self._buttons = {}

        for mode, label, icon in (
            ("solve", "Model", "mdi-cube-outline"),
            ("post_process", "Results", "mdi-chart-bell-curve"),
        ):
            button = QBtn(
                ui_label=label,
                ui_icon=icon,
                ui_flat=True,
                ui_dense=True,
                ui_no_caps=True,
                ui_style=self._button_style(mode),
                ui_class="mmfem-workspace-tab",
            )
            button.on("click", lambda event=None, selected=mode: self.select(selected))
            self._buttons[mode] = button

        super().__init__(
            self._buttons["solve"], self._buttons["post_process"],
            ui_style=(
                "display:flex; width:190px; height:34px; flex:none; "
                "overflow:hidden; border:1px solid var(--border-strong); "
                "border-radius:var(--r-sm); background:var(--surface);"
            ),
        )

    def _button_style(self, mode):
        active = mode == self._value
        style = (
            "display:flex; flex:1; align-items:center; justify-content:center; "
            "height:32px; min-height:32px; padding:0 5px; white-space:nowrap; "
            "font-size:13px; transition:background-color 100ms ease, color 100ms ease;"
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


class Settings(Div):
    """Designer-styled preferences panel (sections, segmented theme, switches)."""

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




def _app_menu_item(
    label, callback=None, icon=None, shortcut=None, close_menu=None, href=None
):
    leading = []
    if icon:
        leading.append(QItemSection(QIcon(ui_name=icon, ui_size="xs"), ui_avatar=True))
    text = label + (f"   {shortcut}" if shortcut else "")
    item = QItem(
        *leading,
        QItemSection(text),
        ui_clickable=callback is not None or href is not None,
        ui_dense=True,
        **({"ui_href": href, "ui_target": "_blank"} if href else {}),
    )
    if callback is not None:
        def run_action(event=None, action=callback, close=close_menu):
            try:
                action()
            finally:
                if close is not None:
                    close()

        item.on("click", run_action)
    return item


def _app_menu_button(label, entries):
    menu_ref = {}

    def close_menu():
        menu = menu_ref.get("menu")
        if menu is not None:
            menu.ui_model_value = False
            menu.ui_hide()

    items = []
    menu_items = {}
    for entry in entries:
        if entry is None:
            items.append(QSeparator())
            continue
        item = _app_menu_item(
            entry[0], entry[1], entry[2], entry[3], close_menu,
            href=entry[4] if len(entry) > 4 else None,
        )
        items.append(item)
        menu_items[entry[0]] = item
    menu = QMenu(
        QList(*items, ui_dense=True),
        ui_anchor="bottom left",
        ui_self="top left",
        ui_auto_close=True,
    )
    menu_ref["menu"] = menu
    button = QBtn(
        QTooltip(label), menu,
        ui_label=label,
        ui_flat=True, ui_dense=True, ui_no_caps=True,
        ui_class="mmfem-app-menu-button",
        ui_style=(
            "height:32px; min-height:32px; padding:0 10px; "
            "font-size:13px; font-weight:600;"
        ),
    )
    button._mmfem_menu_items = menu_items
    return button


class NGSolveGui(App):
    @property
    def usersettings(self):
        """Keep the pre-rebrand settings key so existing preferences survive."""
        if self._usersettings is None:
            self._usersettings = UserSettings(app_id="NGSolve GUI")
        return self._usersettings

    @property
    def solve_workspace(self):
        if self._solve_workspace_instance is None:
            from .solve_workspace import SolveWorkspace

            workspace = SolveWorkspace(
                on_run=lambda: self._run_axisymmetric_solver(mesh_only=False),
                on_mesh=lambda: self._run_axisymmetric_solver(mesh_only=True),
                on_cancel=self._cancel_axisymmetric_solver,
                on_open_file=self._open_run_result,
                on_history_change=self._refresh_solve_history_controls,
            )
            self._solve_workspace_instance = workspace
            self._solve_workspace.ui_children = [workspace]
        return self._solve_workspace_instance

    @solve_workspace.setter
    def solve_workspace(self, workspace):
        self._solve_workspace_instance = workspace
        if hasattr(self, "_solve_workspace"):
            self._solve_workspace.ui_children = [workspace] if workspace is not None else []

    @property
    def _three_d_workspace(self):
        if self._three_d_workspace_instance is None:
            from .three_d_workspace import ThreeDWorkspace

            workspace = ThreeDWorkspace(self)
            self._three_d_workspace_instance = workspace
            self._three_d_project_dialog = workspace
            self._three_d_workspace_container.ui_children = [workspace]
        return self._three_d_workspace_instance

    def __init__(self, filename=None, local_path=None, script_args=None):
        self._local_path = local_path
        self._last_saved_path = None
        self._current_project_path = None
        self._current_project_kind = None
        self._active_project_type = None
        self._pending_project_type = None
        self.script_args = list(script_args or [])
        self.app_data = AppData()
        self._solver_job_lock = threading.RLock()
        self._solver_process = None
        self._solver_job_active = False
        self._solver_cancel_requested = False
        self._solver_run_context = None
        self._pending_problem_replacement = None
        self._loaded_mesh_path = None
        self._loaded_mesh_problem_sha256 = None
        self._solution_import_dirs = []
        self._startup_selected_recent_path = None
        self._recent_thumbnail_queue = queue.Queue()
        self._recent_thumbnail_lock = threading.Lock()
        self._recent_thumbnail_pending = {}
        self._recent_thumbnail_attempted = set()
        self._recent_thumbnail_worker_running = False

        # Frequent document and history actions stay one click away; the full
        # artifact workflow lives in the application menus below.
        def _tbtn(icon, tip, handler=None):
            btn = QBtn(
                QTooltip(tip), ui_flat=True, ui_dense=True, ui_icon=icon,
                ui_class=str(cb.topbar_icon),
            )
            if handler is not None:
                btn.on_click(handler)
            return btn

        new_problem_btn = _tbtn(
            "mdi-file-plus-outline", "New project of the active type (Ctrl+N)", self.new_problem
        )
        open_problem_btn = _tbtn(
            "mdi-folder-open-outline", "Open project (Ctrl+O)", self.open_project_file
        )
        save_problem_btn = _tbtn(
            "mdi-content-save-outline", "Save project (Ctrl+S)", self.save_problem_file
        )
        self._save_project_button = save_problem_btn
        self._save_project_button.ui_disable = True
        self._undo_button = _tbtn(
            "mdi-undo", "Undo model change (Ctrl+Z)", self._undo_solve_action
        )
        self._redo_button = _tbtn(
            "mdi-redo", "Redo model change (Ctrl+Y)", self._redo_solve_action
        )
        self._undo_button.ui_disable = True
        self._redo_button.ui_disable = True
        quick_actions = Div(
            new_problem_btn, open_problem_btn, save_problem_btn,
            Div(ui_class=cb.tb_sep),
            self._undo_button, self._redo_button,
            ui_class=cb.tb_group,
            ui_style="display:flex; align-items:center; gap:2px;",
        )

        cancel_new_problem = QBtn(
            ui_label="Cancel", ui_flat=True, ui_dense=True, ui_no_caps=True,
        )
        discard_new_problem = QBtn(
            ui_label="Discard", ui_flat=True, ui_dense=True, ui_no_caps=True,
            ui_color="negative",
        )
        save_new_problem = QBtn(
            ui_label="Save", ui_flat=True, ui_dense=True, ui_no_caps=True,
            ui_color="primary",
        )
        self._new_problem_dialog = QDialog(
            QCard(
                QCardSection(
                    Div("Save changes?", ui_style="font-size:18px; font-weight:650;")
                ),
                QCardSection(
                    "The current project has unsaved changes. Save them before replacing it, "
                    "discard them, or cancel to keep working."
                ),
                QCardActions(
                    cancel_new_problem,
                    discard_new_problem,
                    save_new_problem,
                    ui_align="right",
                ),
                ui_style="width:min(460px, 92vw);",
            ),
            ui_model_value=False,
            ui_persistent=False,
        )
        self._new_problem_dialog.on_update_model_value(
            self._on_new_problem_dialog_model_value
        )
        cancel_new_problem.on_click(self._close_new_problem_dialog)
        discard_new_problem.on_click(self._discard_and_continue_problem_replacement)
        save_new_problem.on_click(self._save_and_continue_problem_replacement)

        # Preferences and application exit are available from the menus.

        app_brand = Div(
            QImg(
                ui_src=self.load_asset("magmechsim_icon_128x128.png"),
                ui_height="30px",
                ui_width="30px",
                ui_fit="contain",
            ),
            Div(
                PRODUCT_NAME,
                ui_style="font-size:15px; font-weight:700; letter-spacing:.01em; color:var(--fg);",
            ),
            ui_class=cb.brand,
            ui_style="display:flex; align-items:center; gap:7px; white-space:nowrap;",
        )
        self._workspace_mode_toggle = WorkspaceModeToggle(
            "solve", self._set_workspace_mode
        )

        self.system_monitor = SystemMonitor() if system_monitor_available() else None

        # -- Redraw throttling state --
        self._redraw_lock = threading.Lock()
        self._redraw_pending = False
        self._redraw_timer_running = False
        self._last_redraw_time = 0.0
        self._redraw_interval = max(0, int(self.usersettings.get("redraw_interval_ms", 50))) / 1000.0

        # Keep the fixed app header compact: brand, workspace switch, common
        # actions, and system status.
        bar = QBar(
            app_brand,
            self._workspace_mode_toggle,
            quick_actions,
            QSpace(),
            *([self.system_monitor, Div(ui_class=cb.tb_sep)] if self.system_monitor is not None else []),
            ui_class=str(cb.app_bar) + " mmfem-compact-app-bar",
            ui_style=(
                f"height:{APP_HEADER_HEIGHT}px; "
                f"min-height:{APP_HEADER_HEIGHT}px;"
            ),
        )

        # Three-column layout using flex
        self.navigator = Navigator(
            self.app_data,
            self._click_tab,
            self._load_file,
            on_open_run_result=self._open_run_result,
            get_run_history=self._get_post_process_run_history,
        )
        # The property panel is owned by each component; this host swaps in the
        # active component's own panel (built via comp.build_property_panel()).
        self.property_host = StackHost(ui_class=panel_full)
        self.property_host.show(None, empty_property_panel)
        self.tab_panel = Panel(self.app_data)
        self._post_process_toolbar = Div(
            ui_hidden=True,
            ui_style=(
                "display:flex; flex-direction:column; flex:none; width:100%; "
                "min-width:0; background:var(--panel-header);"
            ),
        )
        self.controls = ControlBar()
        self.footer = StatusFooter()
        self._open_solutions_toolbar_button = QBtn(
            QTooltip("Open a saved solution bundle or field pickle"),
            ui_icon="mdi-folder-open-outline",
            ui_label="Open solutions",
            ui_flat=True, ui_dense=True, ui_no_caps=True,
            ui_class=str(cb.topbar_icon),
            ui_style="white-space:nowrap;",
        )
        self._open_solutions_toolbar_button.on_click(self.load_solutions_file)

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
            self._post_process_toolbar, page, self.controls, self.footer,
            ui_style=(
                "display:flex; flex-direction:column; flex:1 1 auto; "
                "min-height:0; width:100%;"
            ),
            ui_hidden=True,
        )
        self._solve_workspace_instance = None
        self._solve_workspace = Div(
            ui_hidden=True,
            ui_style="flex:1 1 auto; min-height:0; width:100%;",
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

        settings_close = QBtn(
            ui_label="Close", ui_flat=True, ui_dense=True, ui_no_caps=True,
        )
        self._settings_dialog = QDialog(
            QCard(
                QCardSection(Div("Preferences", ui_class=cb.prop_title_text)),
                QCardSection(
                    Settings(self),
                    ui_style="max-height:min(78vh, 760px); overflow:auto;",
                ),
                QCardActions(settings_close, ui_align="right"),
                ui_style="width:min(560px, 94vw);",
            ),
            ui_model_value=False,
        )
        settings_close.on_click(lambda *a: setattr(self._settings_dialog, "ui_model_value", False))
        about_close = QBtn(
            ui_label="Close", ui_color="primary", ui_flat=True, ui_dense=True, ui_no_caps=True,
        )
        self._about_dialog = QDialog(
            QCard(
                QCardSection(
                    QImg(
                        ui_src=self.load_asset("magmechsim_about_page_banner.png"),
                        ui_height="198px", ui_width="352px", ui_fit="contain",
                    ),
                    ui_style="display:flex; justify-content:center; padding:18px 20px 4px;",
                ),
                QCardSection(
                    Div(PRODUCT_NAME, ui_style="font-size:18px; font-weight:700; color:var(--fg);"),
                    Div(f"Version {__version__}", ui_style="font-size:12px; color:var(--fg-muted); margin-top:4px;"),
                    Div(PRODUCT_DESCRIPTION, ui_class=cb.hint),
                    Div("Powered by NGSolve and Netgen", ui_style="font-size:11px; color:var(--fg-muted); margin-top:8px;"),
                    ui_style="text-align:center; padding:8px 20px 16px;",
                ),
                QCardActions(about_close, ui_align="center"),
                ui_style="width:min(440px, 92vw);",
            ),
            ui_model_value=False,
        )
        about_close.on_click(lambda *a: setattr(self._about_dialog, "ui_model_value", False))
        self._three_d_workspace_instance = None
        self._three_d_workspace_container = Div(
            ui_hidden=True,
            ui_style=(
                "display:flex; flex-direction:column; flex:1 1 0%; width:100%; height:0; "
                "min-width:0; min-height:0; overflow:hidden;"
            ),
        )
        # Keep the older attribute for the project save/load helpers.
        self._three_d_project_dialog = None
        self._menu_bar = self._build_application_menu_bar()

        self._startup_status = Div(
            "",
            ui_style="min-height:20px; padding:0 24px 10px; color:var(--negative); font-size:13px;",
        )
        self._startup_recent_list = Div(
            ui_class="mmfem-recent-project-grid",
            ui_style=(
                "display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); "
                "gap:10px; max-height:38vh; overflow:auto; padding:2px;"
            ),
        )
        axisymmetric_choice = QBtn(
            ui_label="Axisymmetric",
            ui_icon=AXISYMMETRIC_ICON,
            ui_color="primary",
            ui_no_caps=True,
            ui_class="full-width",
        )
        axisymmetric_choice.on_click(self._start_axisymmetric_from_chooser)
        three_d_choice = QBtn(
            ui_label="3D",
            ui_icon="mdi-cube-scan",
            ui_outline=True,
            ui_no_caps=True,
            ui_class="full-width",
        )
        three_d_choice.on_click(self._start_3d_from_chooser)
        open_project_choice = QBtn(
            ui_label="Open project",
            ui_icon="mdi-folder-open-outline",
            ui_outline=True,
            ui_no_caps=True,
        )
        open_project_choice.on_click(self.open_project_file)
        self._startup_card = QCard(
            QCardSection(
                QImg(
                    ui_src=self.load_asset("magmechsim_full_logo_light.png"),
                    ui_height="150px", ui_width="200px", ui_fit="contain",
                ),
                ui_style="display:flex; justify-content:center; padding:18px 28px 8px;",
            ),
            QCardSection(
                Div("New project", ui_style="font-size:13px; font-weight:650; margin-bottom:10px;"),
                Div(
                    axisymmetric_choice,
                    three_d_choice,
                    ui_style="display:grid; grid-template-columns:1fr 1fr; gap:10px;",
                ),
                ui_style="padding:0 28px 16px;",
            ),
            QCardSection(
                Div(open_project_choice, ui_style="display:flex; justify-content:flex-end;"),
                ui_style="padding:0 28px 12px;",
            ),
            QCardSection(
                Div("Recent projects", ui_style="font-size:13px; font-weight:650; margin-bottom:10px;"),
                self._startup_recent_list,
                ui_style="padding:0 28px 8px;",
            ),
            self._startup_status,
            ui_style=(
                "box-sizing:border-box; width:min(820px, 92vw); "
                "max-height:88vh; overflow:hidden; border-radius:14px; "
                "box-shadow:0 16px 56px #14263818;"
            ),
        )
        self._startup_screen = Div(
            self._startup_card,
            ui_hidden=bool(filename),
            ui_style=(
                "position:fixed; inset:0; z-index:1500; display:grid; "
                "place-items:center; box-sizing:border-box; padding:24px; "
                "overflow:hidden; background:#fff;"
            ),
        )
        self._refresh_recent_projects_view()

        super().__init__(
            self._menu_bar, bar, self._post_process_workspace,
            self._solve_workspace, self._three_d_workspace_container,
            self._timer_dialog, self._new_problem_dialog,
            self._settings_dialog, self._about_dialog,
            self.kb.indicator, self.kb.help_overlay, self._startup_screen,
            ui_class=str(cb.app_root),
        )

        # Keep the footer's mode indicator in sync with the keybinding manager.
        self._wire_footer_mode()

        cb.install(self, default_theme=self.usersettings.get("theme", "system"))
        from .webgpu_tab import sync_default_viewport_clear
        sync_default_viewport_clear()
        keybinding_styles.inject(self)
        self.call_js(_inject_solve_status_styles)
        self.call_js(_inject_recent_project_tile_styles)
        self.call_js(_set_window_title)
        self.call_js(_set_window_icon, self.load_asset("magmechsim_icon_128x128.png"))
        self.on_load(self.__on_load)

        # Start in Model while retaining the mounted Results workspace so its
        # viewport and previously opened data survive mode switches.
        self._workspace_mode = "solve"
        self._set_application_menu_context()

        # -- Global keybindings (always active) --
        kb = self.kb
        kb.add("h", kb.toggle_help, "Show keyboard shortcuts", "General")
        kb.add("ctrl+n", lambda *a: self._run_menu_shortcut(self.new_problem), "New problem", "File")
        kb.add("ctrl+o", lambda *a: self._run_menu_shortcut(self.open_project_file), "Open project", "File")
        kb.add("ctrl+s", lambda *a: self._run_menu_shortcut(self.save_problem_file), "Save problem", "File")
        kb.add("ctrl+shift+s", lambda *a: self._run_menu_shortcut(self._save_as_project_file), "Save project as", "File")
        kb.add("delete", self._delete_selected_region_shortcut, "Delete selected geometry", "Solve")
        for key, tool, description in (
            ("l", "line", "Line tool"),
            ("c", "circle", "Circle tool"),
            ("r", "rectangle", "Rectangle tool"),
            ("a", "arc", "3-point arc tool"),
            ("t", "trim", "Trim tool"),
        ):
            kb.add(
                key,
                lambda *args, active_tool=tool: self._activate_sketch_tool_shortcut(active_tool),
                description,
                "Sketch",
            )
        kb.add("ctrl+z", self._undo_solve_action, "Undo model change", "Solve")
        kb.add("ctrl+y", self._redo_solve_action, "Redo model change", "Solve")
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
        self.add_keybinding("escape", self._handle_escape_shortcut)
        self.on_before_save(self.__on_before_save)
        startup_files = [filename] if isinstance(filename, str) else filename or []
        if startup_files:
            first, *remaining = startup_files
            if Path(str(first)).suffix.lower() in {".json", ".ngsmodel", PROJECT_SUFFIX}:
                self._open_project_path(first, from_startup=True)
            else:
                self._load_with_status(first)
            for f in remaining:
                self._load_with_status(f)

        self._refresh_solve_history_controls()

    @staticmethod
    def _project_path_key(path):
        return os.path.normcase(os.path.normpath(os.path.abspath(os.path.expanduser(str(path)))))

    def _recent_project_entries(self):
        try:
            stored = self.usersettings.get("recent_projects", [])
        except Exception:
            return []
        if not isinstance(stored, list):
            return []
        entries = []
        seen = set()
        for item in stored:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                continue
            path = os.path.abspath(os.path.expanduser(item["path"]))
            key = self._project_path_key(path)
            if key in seen:
                continue
            seen.add(key)
            entries.append({
                "name": str(item.get("name") or Path(path).stem),
                "path": path,
                "kind": item.get("kind") if item.get("kind") in {"axisymmetric", "3d"} else "",
            })
        return entries[:8]

    def _refresh_recent_projects_view(self):
        if not hasattr(self, "_startup_recent_list"):
            return
        entries = self._recent_project_entries()
        if not entries:
            self._startup_recent_list.ui_children = [
                Div("No recent projects.", ui_style="padding:10px 4px; color:var(--fg-muted); font-size:13px;")
            ]
            return
        tiles = []
        for entry in entries:
            path = entry["path"]
            exists = Path(path).is_file()
            thumbnail = cached_thumbnail(path) if exists else None
            if exists and thumbnail is None:
                queue_thumbnail = getattr(self, "_queue_recent_project_thumbnail", None)
                if callable(queue_thumbnail):
                    queue_thumbnail(entry)

            if thumbnail:
                preview = QImg(
                    ui_src=thumbnail,
                    ui_width="100%",
                    ui_height="112px",
                    ui_fit="contain",
                    ui_class="mmfem-recent-project-image",
                )
            else:
                preview = Div(
                    QIcon(
                        ui_name="mdi-image-outline",
                        ui_size="38px",
                        ui_color="grey-5",
                    ),
                    ui_class="mmfem-recent-project-placeholder",
                    ui_style=(
                        "height:112px; display:grid; place-items:center; "
                        "background:#f7f9fc; border-radius:6px;"
                    ),
                )
            filename = Path(path).name
            selected = NGSolveGui._project_path_key(path) == getattr(
                self, "_startup_selected_recent_path", None
            )
            tile = Div(
                preview,
                Div(
                    filename,
                    ui_class="mmfem-recent-project-filename",
                    ui_style=(
                        "margin-top:7px; font-size:12px; font-weight:600; "
                        "line-height:1.35; white-space:nowrap; overflow:hidden; "
                        "text-overflow:ellipsis;"
                    ),
                ),
                Div(
                    "File not found" if not exists else "",
                    ui_hidden=exists,
                    ui_style="margin-top:3px; color:var(--negative); font-size:10px;",
                ),
                ui_class="mmfem-recent-project-tile" + (" is-selected" if selected else ""),
                ui_style=(
                    "box-sizing:border-box; width:100%; min-width:0; padding:8px; "
                    "text-align:left; border-radius:8px; background:var(--surface); "
                    "cursor:pointer; transition:border-color .12s ease, "
                    "box-shadow .12s ease, transform .12s ease;"
                ),
            )
            tile._props["title"] = str(path)
            tile._props["role"] = "button"
            tile._props["tabindex"] = "0"
            tile._props["aria-label"] = f"Open {filename}"
            tile._props["aria-pressed"] = "true" if selected else "false"
            tile._props["data-project-key"] = NGSolveGui._project_path_key(path)
            tile.on("click", lambda _event=None, value=path: self._select_recent_project(value))
            tile.on(
                "dblclick",
                lambda _event=None, item=dict(entry), present=exists: (
                    self._open_recent_project(item)
                    if present else self._locate_recent_project(item)
                ),
            )
            tile.on(
                "keydown",
                lambda event, item=dict(entry), present=exists: (
                    self._open_recent_project(item)
                    if str(getattr(event, "key", "")) in {"Enter", " "}
                    and present
                    else self._locate_recent_project(item)
                    if str(getattr(event, "key", "")) in {"Enter", " "}
                    else None
                ),
            )
            remove_button = QBtn(
                QTooltip("Remove from recent"),
                ui_icon="mdi-close",
                ui_flat=True,
                ui_dense=True,
                ui_round=True,
                ui_size="sm",
                ui_class="mmfem-recent-project-remove",
                ui_style=(
                    "position:absolute; right:10px; top:10px; background:var(--surface); "
                    "box-shadow:0 1px 4px #14263822;"
                ),
            )
            remove_button.on_click(
                lambda *_args, value=path: self._remove_recent_project(value)
            )
            tiles.append(Div(
                tile,
                remove_button,
                ui_class="mmfem-recent-project-card",
                ui_style="position:relative; min-width:0;",
            ))
        self._startup_recent_list.ui_children = tiles

    def _select_recent_project(self, path):
        self._startup_selected_recent_path = self._project_path_key(path)
        # Patch the existing tiles so the first click of a double-click does
        # not replace its DOM target before the browser can deliver dblclick.
        for card in getattr(self._startup_recent_list, "ui_children", ()):
            try:
                tile = card.ui_slots["default"][0]
                classes = [
                    value for value in tile._props.get("class", "").split()
                    if value != "is-selected"
                ]
                selected = (
                    tile._props.get("data-project-key")
                    == self._startup_selected_recent_path
                )
                if selected:
                    classes.append("is-selected")
                tile._props["class"] = " ".join(classes)
                tile._props["aria-pressed"] = "true" if selected else "false"
            except (AttributeError, KeyError, IndexError, TypeError):
                continue

    def _queue_recent_project_thumbnail(
        self, entry, *, project_data=None, force=False
    ):
        """Generate a missing/stale recent preview on one bounded worker."""
        path = str(entry.get("path", ""))
        if not path:
            return
        try:
            source = Path(path)
            stat = source.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if not source.is_file():
                return
        except OSError:
            return
        if not force and cached_thumbnail(path) is not None:
            return
        path_key = self._project_path_key(path)
        if project_data is not None:
            project_data = bytes(project_data)
            job_signature = hashlib.sha256(project_data).hexdigest()
        else:
            job_signature = f"{signature[0]}-{signature[1]}"
        job_key = (path_key, job_signature)
        attempt_key = (path_key, signature[0], signature[1])
        with self._recent_thumbnail_lock:
            if self._recent_thumbnail_pending.get(path_key) == job_key:
                return
            if path_key in self._recent_thumbnail_pending and not force:
                return
            if not force and attempt_key in self._recent_thumbnail_attempted:
                return
            self._recent_thumbnail_pending[path_key] = job_key
            self._recent_thumbnail_queue.put((
                job_key, attempt_key, path, entry.get("kind"),
                project_data, signature,
            ))
            if self._recent_thumbnail_worker_running:
                return
            self._recent_thumbnail_worker_running = True
            threading.Thread(
                target=self._run_recent_thumbnail_queue,
                name="MMFEM-project-thumbnails",
                daemon=True,
            ).start()

    def _run_recent_thumbnail_queue(self):
        while True:
            try:
                job = self._recent_thumbnail_queue.get(timeout=0.25)
            except queue.Empty:
                with self._recent_thumbnail_lock:
                    if self._recent_thumbnail_queue.empty():
                        self._recent_thumbnail_worker_running = False
                        return
                continue
            job_key, attempt_key, path, kind, project_data, signature = job
            generated = False
            try:
                generated = generate_project_thumbnail(
                    path,
                    project_data,
                    kind=kind,
                    expected_stat=signature,
                )
            finally:
                self._recent_thumbnail_queue.task_done()
                with self._recent_thumbnail_lock:
                    self._recent_thumbnail_attempted.add(attempt_key)
                    if self._recent_thumbnail_pending.get(job_key[0]) == job_key:
                        self._recent_thumbnail_pending.pop(job_key[0], None)
            loop = getattr(self, "loop", None)
            if generated and loop is not None and loop.is_running():
                try:
                    loop.call_soon_threadsafe(self._refresh_recent_projects_view)
                except RuntimeError:
                    pass

    def _set_startup_status(self, message, *, error=True):
        self._startup_status.ui_children = [str(message)] if message else []
        self._startup_status.ui_style = (
            "min-height:20px; padding:0 24px 10px; font-size:13px; "
            + ("color:var(--negative);" if error else "color:var(--positive);")
        )

    def _hide_startup_chooser(self):
        if hasattr(self, "_startup_screen"):
            self._startup_screen.ui_hidden = True
            self._startup_card.ui_hidden = False

    def _remember_recent_project(self, path, *, kind, name=None, project_data=None):
        if not path:
            return
        try:
            actual_path = str(Path(path).expanduser().resolve(strict=False))
            if not Path(actual_path).is_file():
                return
            key = self._project_path_key(actual_path)
            entries = [
                item for item in self._recent_project_entries()
                if self._project_path_key(item["path"]) != key
            ]
            entries.insert(0, {
                "name": str(name or Path(actual_path).stem),
                "path": actual_path,
                "kind": kind,
            })
            entries = entries[:8]
            self.usersettings.set("recent_projects", entries)
            if project_data is not None:
                self._queue_recent_project_thumbnail(
                    entries[0], project_data=project_data, force=True
                )
            self._refresh_recent_projects_view()
        except (OSError, TypeError, ValueError):
            return

    def _remove_recent_project(self, path):
        key = self._project_path_key(path)
        entries = [
            item for item in self._recent_project_entries()
            if self._project_path_key(item["path"]) != key
        ]
        self.usersettings.set("recent_projects", entries)
        self._refresh_recent_projects_view()

    def _open_recent_project(self, entry):
        return self._open_project_path(entry["path"], from_startup=True)

    def _locate_recent_project(self, _entry):
        return self.open_project_file()

    @staticmethod
    def _decode_project_file(data):
        three_d_error = None
        try:
            project, assets = unpack_3d_project(data)
            return "3d", project, assets
        except Exception as error:
            three_d_error = error
        try:
            model, studies, layout = unpack_model(data)
            return "axisymmetric", (model, studies, layout), None
        except Exception as axis_error:
            raise ValueError(
                "This file is not a supported MagMechSim project. "
                f"3D check: {three_d_error}. Axisymmetric check: {axis_error}"
            ) from axis_error

    def _open_project_path(self, path, *, from_startup=False):
        try:
            actual_path = str(Path(path).expanduser().resolve(strict=True))
            data = Path(actual_path).read_bytes()
            kind, content, assets = self._decode_project_file(data)
            startup_context = from_startup or not getattr(
                getattr(self, "_startup_screen", None), "ui_hidden", True
            )
            project_name = (
                content.get("name", Path(actual_path).stem)
                if kind == "3d"
                else content[0].get("name", Path(actual_path).stem)
            )
            def apply_project():
                try:
                    if kind == "axisymmetric":
                        model, studies, layout = content
                        self.solve_workspace.set_model(model, studies, layout)
                        self._loaded_mesh_path = None
                        self._loaded_mesh_problem_sha256 = None
                        if self._workspace_mode != "solve":
                            self._workspace_mode_toggle.select("solve")
                        if self._three_d_workspace_instance is not None:
                            self._three_d_workspace_instance.ui_hidden = True
                            self._three_d_workspace_container.ui_hidden = True
                        self._active_project_type = "axisymmetric"
                        self._set_3d_workspace_active(False)
                        self._hide_startup_chooser()
                    else:
                        if not self._three_d_workspace.load_project_data(content, assets):
                            return False
                        self._active_project_type = "3d"
                        self._set_3d_workspace_active(True)
                        self._hide_startup_chooser()
                    if os.name == "nt" and Path(actual_path).is_file():
                        self._current_project_path = actual_path
                        self._current_project_kind = kind
                    else:
                        self._current_project_path = None
                        self._current_project_kind = None
                    self._remember_recent_project(
                        actual_path, kind=kind, name=project_name
                    )
                    self._set_startup_status("")
                    return True
                except Exception as error:
                    self._show_project_open_error(error, reveal_startup=startup_context)
                    return False

            return self._request_problem_replacement(apply_project)
        except Exception as error:
            self._show_project_open_error(
                error,
                reveal_startup=from_startup or not getattr(
                    getattr(self, "_startup_screen", None), "ui_hidden", True
                ),
            )
            return False

    def _show_project_open_error(self, error, *, reveal_startup=False):
        message = f"Could not open project: {error}"
        screen = getattr(self, "_startup_screen", None)
        if screen is not None and reveal_startup:
            screen.ui_hidden = False
            self._startup_card.ui_hidden = False
            self._set_startup_status(message)
        else:
            self._notify(message, type="negative", timeout=8000)

    def open_project_file(self, *_args):
        startup_at_start = not getattr(
            getattr(self, "_startup_screen", None), "ui_hidden", True
        )
        try:
            path, data = self._pick_artifact(
                title="Open MagMechSim project",
                description="MagMechSim projects",
                extension=".json",
                additional_extensions=(".ngsmodel", PROJECT_SUFFIX),
                force_native=True,
            )
            if not path:
                return False
            kind, content, assets = self._decode_project_file(data)
            # Keep project identification independent of the selected suffix.
            try:
                actual_path = str(Path(path).expanduser().resolve(strict=True))
            except OSError:
                actual_path = path
            startup_context = not getattr(
                getattr(self, "_startup_screen", None), "ui_hidden", True
            )
            project_name = (
                content.get("name", Path(actual_path).stem)
                if kind == "3d"
                else content[0].get("name", Path(actual_path).stem)
            )
            def apply_project():
                try:
                    if kind == "axisymmetric":
                        model, studies, layout = content
                        self.solve_workspace.set_model(model, studies, layout)
                        self._loaded_mesh_path = None
                        self._loaded_mesh_problem_sha256 = None
                        if self._workspace_mode != "solve":
                            self._workspace_mode_toggle.select("solve")
                        if self._three_d_workspace_instance is not None:
                            self._three_d_workspace_instance.ui_hidden = True
                            self._three_d_workspace_container.ui_hidden = True
                        self._active_project_type = "axisymmetric"
                        self._set_3d_workspace_active(False)
                        self._hide_startup_chooser()
                    else:
                        if not self._three_d_workspace.load_project_data(content, assets):
                            return False
                        self._active_project_type = "3d"
                        self._set_3d_workspace_active(True)
                        self._hide_startup_chooser()
                    if os.name == "nt" and Path(actual_path).is_file():
                        self._current_project_path = actual_path
                        self._current_project_kind = kind
                    else:
                        self._current_project_path = None
                        self._current_project_kind = None
                    self._remember_recent_project(actual_path, kind=kind, name=project_name)
                    self._set_startup_status("")
                    return True
                except Exception as error:
                    self._show_project_open_error(error, reveal_startup=startup_context)
                    return False

            return self._request_problem_replacement(apply_project)
        except Exception as error:
            self._show_project_open_error(error, reveal_startup=startup_at_start)
            return False

    def _start_axisymmetric_from_chooser(self, *_args):
        def start():
            result = self._start_new_problem()
            if result:
                self._active_project_type = "axisymmetric"
                self._hide_startup_chooser()
                self._set_startup_status("")
            return result
        return self._request_problem_replacement(start)

    def _start_3d_from_chooser(self, *_args):
        return self._request_problem_replacement(self._start_3d_project_from_menu)

    def _build_application_menu_bar(self):
        menus = [
            ("File", [
                ("New axisymmetric problem...", self._new_axisymmetric_project, "mdi-axis-arrow", None),
                ("Open project...", self.open_project_file, "mdi-folder-open-outline", "Ctrl+O"),
                ("Save project", self.save_problem_file, "mdi-content-save-outline", "Ctrl+S"),
                ("Save project as...", self._save_as_project_file, "mdi-content-save-edit-outline", "Ctrl+Shift+S"),
                None,
                ("New 3D project...", self._new_3d_project, "mdi-cube-scan", None),
                None,
                ("Import legacy Python problem...", self._choose_legacy_problem_file, "mdi-language-python", None),
                ("Open external file...", self._load_file, "mdi-file-import-outline", None),
                None,
                ("Load mesh...", self.load_mesh_file, "mdi-folder-open-outline", None),
                ("Save mesh", self.save_mesh_file, "mdi-content-save-outline", None),
                ("Load solutions...", self.load_solutions_file, "mdi-folder-open-outline", None),
                ("Save solutions", self.save_solutions_file, "mdi-content-save-outline", None),
                None,
                ("Exit", self._quit_application, "mdi-exit-to-app", None),
            ]),
            ("Edit", [
                ("Undo", self._undo_solve_action, "mdi-undo", "Ctrl+Z"),
                ("Redo", self._redo_solve_action, "mdi-redo", "Ctrl+Y"),
                None,
                ("Delete selected geometry", self._delete_selected_region_shortcut, "mdi-delete-outline", "Delete"),
                ("Preferences...", self._open_settings, "mdi-cog-outline", None),
            ]),
            ("View", [
                ("Fit view", self._menu_fit_view, "mdi-overscan", None),
                None,
                ("Isometric", lambda: self._menu_camera_view("isometric"), "mdi-cube-scan", None),
                ("View from +X", lambda: self._menu_camera_view("right"), "mdi-axis-x-arrow", None),
                ("View from -X", lambda: self._menu_camera_view("left"), "mdi-axis-x-arrow", None),
                ("View from +Y", lambda: self._menu_camera_view("front"), "mdi-axis-y-arrow", None),
                ("View from -Y", lambda: self._menu_camera_view("back"), "mdi-axis-y-arrow", None),
                ("View from +Z", lambda: self._menu_camera_view("top"), "mdi-axis-z-arrow", None),
                ("View from -Z", lambda: self._menu_camera_view("bottom"), "mdi-axis-z-arrow", None),
                ("Toggle parallel projection", lambda: self._active_view_action("toggle_projection"), "mdi-perspective-less", None),
                None,
                ("Toggle axes", lambda: self._active_view_action("toggle_axes"), "mdi-axis-arrow", None),
                ("Toggle navigation cube", lambda: self._active_view_action("toggle_navcube"), "mdi-cube-outline", None),
                ("Link or detach camera", lambda: self._active_view_action("toggle_camera_link"), "mdi-link-variant", None),
                ("Saved camera views", lambda: self._active_view_action("_toggle_bookmarks"), "mdi-bookmark-outline", None),
                ("Fullscreen viewport", lambda: self._active_view_action("_toggle_fullscreen"), "mdi-fullscreen", None),
                None,
                ("Toggle model tree", self._menu_toggle_tree, "mdi-file-tree-outline", None),
                ("Toggle properties", self._menu_toggle_properties, "mdi-tune-variant", None),
                ("Messages", self._menu_toggle_messages, "mdi-message-alert-outline", None),
                ("Solver log", self._menu_toggle_log, "mdi-console", None),
            ]),
            ("Model", [
                ("Geometry", lambda: self._menu_model_section("geometry"), "mdi-vector-square", None),
                ("Materials", lambda: self._menu_model_section("materials"), "mdi-cube-scan", None),
                ("Physics", lambda: self._menu_model_section("physics"), "mdi-magnet", None),
                ("Boundary conditions", lambda: self._menu_model_section("boundaries"), "mdi-vector-link", None),
                ("Sources & loads", lambda: self._menu_model_section("sources"), "mdi-flash-outline", None),
                ("Mesh settings", lambda: self._menu_model_section("mesh"), "mdi-vector-triangle", None),
                ("Solver settings", lambda: self._menu_model_section("solver"), "mdi-tune-variant", None),
                ("Study settings", lambda: self._menu_model_section("studies"), "mdi-play-box-multiple-outline", None),
                ("Run history", lambda: self._open_solve_section("runs"), "mdi-history", None),
                None,
                ("Toggle snap to grid", self._menu_toggle_snap, "mdi-magnet", None),
                ("Rename model...", self._menu_rename_model, "mdi-pencil-outline", None),
                ("Preview geometry in 3D", self._menu_preview_geometry, "mdi-cube-scan", None),
                ("Import STEP model...", self._menu_import_step, "mdi-cube-scan", None),
                ("Import wire traces...", self._menu_import_wire_trace, "mdi-vector-polyline", None),
            ]),
            ("Study", [
                ("Check setup", self._menu_check_setup, "mdi-check-decagram-outline", None),
                ("Generate mesh", self._menu_generate_mesh, "mdi-vector-triangle", None),
                ("Run study", self._menu_run_study, "mdi-play", None),
                ("Cancel current run", self._menu_cancel_run, "mdi-stop-circle-outline", None),
            ]),
            ("Results", [
                ("Open solutions...", self.load_solutions_file, "mdi-folder-open-outline", None),
                None,
                ("Field & colour options", lambda: self._open_postprocess_controls("Field"), "mdi-palette", None),
                ("Display & view options", lambda: self._open_postprocess_controls("Display & view"), "mdi-cube-scan", None),
                ("Vectors & flow options", lambda: self._open_postprocess_controls("Vectors & flow"), "mdi-vector-triangle", None),
                None,
                ("Toggle wireframe", lambda: self._active_view_action("toggle_wireframe"), "mdi-grid", None),
                ("Toggle clipping", lambda: self._active_view_action("toggle_clipping"), "mdi-content-cut", None),
                ("Toggle probe", lambda: self._active_view_action("toggle_probe"), "mdi-crosshairs-gps", None),
            ]),
            ("Help", [
                ("Keyboard shortcuts", self.kb.toggle_help, "mdi-keyboard-outline", "H"),
                ("Documentation", self._open_documentation, "mdi-book-open-page-variant-outline", None),
                (f"About {PRODUCT_NAME}", self._open_about, "mdi-information-outline", None),
            ]),
        ]
        self._application_menu_buttons = {}
        self._application_menu_items = {}
        for label, entries in menus:
            button = _app_menu_button(label, entries)
            self._application_menu_buttons[label] = button
            self._application_menu_items[label] = button._mmfem_menu_items
        return Div(
            *self._application_menu_buttons.values(),
            ui_class="mmfem-app-menu-bar",
            ui_style=(
                "display:flex; align-items:center; gap:2px; flex:0 0 auto; "
                f"min-height:{APP_MENU_BAR_HEIGHT}px; "
                "padding:0 12px; box-sizing:border-box; "
                "background:var(--panel-header); border-bottom:1px solid var(--border);"
            ),
        )

    def _set_application_menu_context(self, three_d=None):
        """Expose shared commands only where the active workspace can use them."""
        active_type = getattr(self, "_active_project_type", None)
        if three_d is None:
            three_d = active_type == "3d"
        else:
            three_d = bool(three_d)
        solving = getattr(self, "_workspace_mode", "solve") == "solve"
        has_project = active_type in {"axisymmetric", "3d"} or three_d
        has_result = (
            not solving
            and getattr(getattr(self, "tab_panel", None), "comp", None) is not None
        )

        menu_buttons = getattr(self, "_application_menu_buttons", {})
        for label in ("Model", "Study"):
            button = menu_buttons.get(label)
            if button is not None:
                button.ui_hidden = not has_project or not solving
        results_button = menu_buttons.get("Results")
        if results_button is not None:
            results_button.ui_hidden = not (has_project or has_result)

        menu_items = getattr(self, "_application_menu_items", {})

        def hide(menu, labels, hidden):
            items = menu_items.get(menu, {})
            for label in labels:
                item = items.get(label)
                if item is not None:
                    item.ui_hidden = bool(hidden)

        if hasattr(self, "_save_project_button"):
            self._save_project_button.ui_disable = not has_project
        hide("File", ("Save project", "Save project as..."), not has_project)
        hide("File", ("Import legacy Python problem...",), three_d)
        hide("File", ("Load mesh...", "Save mesh"), three_d or not solving)
        hide("File", ("Save solutions",), three_d or solving)
        hide("Edit", ("Undo", "Redo"), three_d or not solving)
        hide("Edit", ("Delete selected geometry",), not has_project or not solving)

        camera_commands = (
            "Isometric", "View from +X", "View from -X", "View from +Y",
            "View from -Y", "View from +Z", "View from -Z",
            "Toggle parallel projection", "Toggle navigation cube",
            "Link or detach camera", "Saved camera views", "Fullscreen viewport",
        )
        hide("View", camera_commands, not three_d)
        hide("View", ("Toggle axes",), not (three_d or has_result))
        hide("View", ("Messages", "Solver log"), three_d or not solving)

        hide("Model", ("Sources & loads", "Toggle snap to grid", "Rename model...", "Preview geometry in 3D"), three_d or not solving)
        hide("Model", ("Solver settings", "Run history"), three_d)
        hide("Model", ("Import STEP model...", "Import wire traces..."), not three_d or not solving)

        hide("Study", ("Check setup", "Generate mesh", "Run study"), not solving or not has_project)
        job_active = bool(getattr(self, "_solver_job_active", False))
        axisymmetric_job = active_type == "axisymmetric" and (
            job_active or getattr(
                getattr(self, "_solve_workspace_instance", None), "_job_active", False
            )
        )
        three_d_job = active_type == "3d" and (
            job_active or getattr(
                getattr(self, "_three_d_workspace_instance", None), "_study_running", False
            )
        )
        hide("Study", ("Cancel current run",), not (axisymmetric_job or three_d_job))
        result_commands = (
            "Field & colour options", "Display & view options", "Vectors & flow options",
            "Toggle wireframe", "Toggle clipping", "Toggle probe",
        )
        hide("Results", result_commands, not has_result)

    def _quit_application(self, *args):
        return self._request_problem_replacement(self._close_application_now)

    def _new_3d_project(self, *args):
        return self._request_problem_replacement(self._start_3d_project_from_menu)

    def _new_axisymmetric_project(self, *args):
        return self._request_problem_replacement(self._start_new_problem)

    def _menu_import_step(self, *args):
        if self._active_project_type == "3d":
            return self._three_d_workspace.import_step()
        return self._request_problem_replacement(
            lambda: self._start_3d_and_import(self._three_d_workspace.import_step)
        )

    def _menu_import_wire_trace(self, *args):
        if self._active_project_type == "3d":
            return self._three_d_workspace.import_wire_trace()
        return self._request_problem_replacement(
            lambda: self._start_3d_and_import(self._three_d_workspace.import_wire_trace)
        )

    def _menu_model_section(self, section):
        if self._active_project_type != "3d":
            return self._open_solve_section(section)
        workspace = self._three_d_workspace
        geometries = workspace.project.get("geometries", [])
        if section == "geometry":
            workspace.select_item("project", None)
        elif section in {"materials", "mesh"}:
            if geometries:
                workspace.select_item("geometry", geometries[0]["id"])
            else:
                workspace.select_item("project", None)
                workspace._set_status("Import STEP geometry before editing material or mesh settings.")
        elif section == "boundaries":
            selection = next((
                item
                for geometry in geometries
                for item in geometry.get("boundary_selections", [])
            ), None)
            if selection is not None:
                workspace.select_item("named_selection", selection["id"])
            elif geometries:
                workspace.select_item("geometry", geometries[0]["id"])
            else:
                workspace.select_item("project", None)
                workspace._set_status("Import and mesh geometry before defining named boundary selections.")
        elif section in {"physics", "studies"}:
            workspace.select_item("study", None)
        else:
            workspace._set_status(f"{section.replace('_', ' ').title()} is not available in the 3D workspace.")
            return False
        return True

    def _start_3d_and_import(self, importer):
        if not self._start_3d_project_from_menu():
            return False
        return importer()

    def _open_3d_project(self, *args):
        return self.open_project_file(*args)

    def _start_3d_project_from_menu(self):
        if not self._three_d_workspace.show_new():
            return False
        self._current_project_path = None
        self._current_project_kind = None
        self._active_project_type = "3d"
        self._hide_startup_chooser()
        self._set_3d_workspace_active(True)
        return True

    def _set_3d_workspace_active(self, active):
        active = bool(active)
        workspace = getattr(self, "_three_d_workspace_instance", None)
        if active:
            workspace = self._three_d_workspace
            container = getattr(self, "_three_d_workspace_container", None)
            if container is not None:
                container.ui_hidden = False
        else:
            if workspace is None:
                workspace = getattr(self, "_three_d_workspace", None)
            container = getattr(self, "_three_d_workspace_container", None)
            if container is not None:
                container.ui_hidden = True
        if workspace is not None:
            workspace.ui_hidden = not active
        self._workspace_mode_toggle.ui_hidden = False
        if active:
            solving = getattr(self, "_workspace_mode", "solve") == "solve"
            workspace.ui_hidden = not solving
            self._solve_workspace.ui_hidden = True
            self._post_process_workspace.ui_hidden = solving
        else:
            self._solve_workspace.ui_hidden = self._workspace_mode != "solve"
            self._post_process_workspace.ui_hidden = self._workspace_mode == "solve"
        self._refresh_solve_history_controls()
        sync_menu_context = getattr(self, "_set_application_menu_context", None)
        if callable(sync_menu_context):
            sync_menu_context(active)

    def _close_application_now(self):
        try:
            _request_guest_vm_shutdown()
        except Exception as error:
            self._notify(
                f"Could not shut down the solver VM: {error}",
                type="negative",
                timeout=8000,
            )
            return False
        return self.quit()

    def _open_settings(self, *args):
        self._settings_dialog.ui_model_value = True

    def _open_about(self, *args):
        self._about_dialog.ui_model_value = True

    def _open_documentation(self, *args):
        try:
            solver_root = self._find_solver_root()
        except Exception:
            solver_root = None
        index = find_documentation_index(solver_root)
        if index is None:
            self._notify(
                "Local MagMechSim documentation is not installed. Build the Sphinx HTML site from the solver docs or reinstall the packaged application.",
                type="warning",
                timeout=8000,
            )
            return False
        try:
            import webbrowser

            if not webbrowser.open(index.as_uri(), new=2):
                raise RuntimeError("The default browser did not accept the local documentation URL.")
            return True
        except Exception as error:
            self._notify(
                f"Could not open local documentation: {error}",
                type="negative",
                timeout=8000,
            )
            return False

    def _global_shortcut_context_active(self):
        try:
            focused = self.js.eval(
                "(() => { const e = document.activeElement; return !!e && "
                "(e.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.tagName)); })()"
            )
            return not bool(focused)
        except Exception:
            return True

    def _run_menu_shortcut(self, callback):
        if self._global_shortcut_context_active():
            return callback()
        return False

    def _handle_escape_shortcut(self, event=None):
        """Close active overlays first; otherwise return Solve sketching to Select."""
        kb = getattr(self, "kb", None)
        overlay_was_open = bool(
            kb
            and (
                getattr(kb, "_mode", None)
                or getattr(kb, "_help_visible", False)
            )
        )
        if kb is not None:
            kb.on_escape()
        if overlay_was_open or getattr(self, "_workspace_mode", None) != "solve":
            return
        workspace = getattr(self, "_solve_workspace_instance", None)
        if workspace is not None:
            if getattr(workspace, "_preview_3d_active", False):
                workspace.toggle_3d_preview()
                return
            workspace._handle_sketch_escape()

    def _open_solve_section(self, section):
        if self._active_project_type == "3d":
            self._notify("Axisymmetric model sections are not part of the 3D workspace.", type="info", timeout=3000)
            return False
        if self._workspace_mode != "solve":
            self._workspace_mode_toggle.select("solve")
        self.solve_workspace.select_section(section)

    def _menu_fit_view(self, *args):
        if self._active_project_type == "3d" and self._workspace_mode == "solve":
            return self._three_d_workspace.reset_camera()
        if self._workspace_mode == "solve":
            return self.solve_workspace.fit_canvas_view()
        return self._active_view_action("reset_camera")

    def _menu_camera_view(self, view):
        if self._active_project_type == "3d" and self._workspace_mode == "solve":
            return self._active_view_action("set_camera_view", view)
        if self._workspace_mode == "solve":
            if getattr(self.tab_panel, "comp", None) is None:
                self._notify("Open a result in Post Process before choosing a camera view.", type="info", timeout=3000)
                return False
            self._workspace_mode_toggle.select("post_process")
        return self._active_view_action("set_camera_view", view)

    def _active_view_action(self, method, *args):
        if self._active_project_type == "3d" and self._workspace_mode == "solve":
            callback = getattr(self._three_d_workspace, method, None)
            if callable(callback):
                return callback(*args)
            self._notify("That view command is not available in the 3D workspace yet.", type="info", timeout=3000)
            return False
        if self._workspace_mode != "post_process":
            self._notify("Switch to Post Process to use this view command.", type="info", timeout=3000)
            return False
        comp = getattr(getattr(self, "tab_panel", None), "comp", None)
        callback = getattr(comp, method, None) if comp is not None else None
        if callable(callback):
            return callback(*args)
        self._notify("Load a compatible result in Post Process to use this view command.", type="info", timeout=3000)
        return False

    def _open_postprocess_controls(self, label):
        if self._workspace_mode != "post_process":
            self._workspace_mode_toggle.select("post_process")
        return self._active_view_action("open_postprocess_controls", label)

    def _menu_toggle_tree(self, *args):
        if self._active_project_type == "3d" and self._workspace_mode == "solve":
            return self._three_d_workspace.toggle_tree()
        if self._workspace_mode == "solve":
            return self.solve_workspace._toggle_tree_panel()
        return self._toggle_navigator()

    def _menu_toggle_properties(self, *args):
        if self._active_project_type == "3d" and self._workspace_mode == "solve":
            return self._three_d_workspace.toggle_properties()
        if self._workspace_mode == "solve":
            return self.solve_workspace._toggle_properties_panel()
        return self._toggle_property_panel()

    def _menu_toggle_messages(self, *args):
        return self.solve_workspace.toggle_messages()

    def _menu_toggle_log(self, *args):
        return self.solve_workspace.toggle_log()

    def _menu_toggle_snap(self, *args):
        self._open_solve_section("geometry")
        return self.solve_workspace.toggle_snap_to_grid()

    def _menu_rename_model(self, *args):
        self._open_solve_section("geometry")
        return self.solve_workspace._begin_model_rename()

    def _menu_preview_geometry(self, *args):
        self._open_solve_section("geometry")
        return self.solve_workspace.toggle_3d_preview()

    def _menu_check_setup(self, *args):
        if self._active_project_type == "3d":
            workspace = self._three_d_workspace
            try:
                from .three_d_project import validate_3d_project
                from .three_d_solver import validate_3d_study_setup

                validate_3d_project(workspace.project, workspace.assets)
                setup = validate_3d_study_setup(workspace.project)
            except Exception as error:
                workspace._set_status(f"Study setup needs attention: {error}", error=True)
                return False
            label = {
                "harmonic_em": "time-harmonic electromagnetic",
                "harmonic_mechanical": "time-harmonic mechanics",
                "magnetostatic_dc": "static/DC magnetic",
            }[setup["physics"]]
            valid_at = (
                f" at {setup['frequency_hz']:g} Hz"
                if setup["frequency_hz"] is not None else ""
            )
            workspace._set_status(
                f"3D {label} setup is valid{valid_at}."
            )
            return True
        self._open_solve_section("studies")
        return self.solve_workspace.validate_action()

    def _menu_generate_mesh(self, *args):
        if self._active_project_type == "3d":
            workspace = self._three_d_workspace
            geometries = workspace.project.get("geometries", [])
            if geometries and workspace._selected_geometry() is None:
                workspace.select_item("geometry", geometries[0]["id"])
            return workspace.generate_mesh()
        self._open_solve_section("mesh")
        return self.solve_workspace.run_mesh_action()

    def _menu_run_study(self, *args):
        if self._active_project_type == "3d":
            return self._three_d_workspace.run_study()
        self._open_solve_section("studies")
        return self.solve_workspace.run_study_action()

    def _menu_cancel_run(self, *args):
        context = getattr(self, "_solver_run_context", None) or {}
        if context.get("project_type") == "3d":
            return self.cancel_3d_study(getattr(self, "_three_d_workspace", None))
        return self.solve_workspace._cancel_solver_job()

    def _refresh_solve_history_controls(self):
        workspace = getattr(self, "_solve_workspace_instance", None)
        solving = (
            getattr(self, "_workspace_mode", "solve") == "solve"
            and getattr(self, "_active_project_type", None) != "3d"
        )
        if hasattr(self, "_undo_button"):
            self._undo_button.ui_disable = not (solving and workspace and workspace.can_undo)
        if hasattr(self, "_redo_button"):
            self._redo_button.ui_disable = not (solving and workspace and workspace.can_redo)

    def _solve_shortcut_context_active(self):
        if (
            getattr(self, "_workspace_mode", None) != "solve"
            or getattr(self, "_active_project_type", None) == "3d"
        ):
            return False
        # Preserve native text editing shortcuts while an input or editable
        # control has focus. The fallback keeps callbacks usable in standalone
        # tests where no browser JavaScript context exists.
        try:
            focused_editor = self.js.eval(
                "(() => { const e = document.activeElement; return !!e && "
                "(e.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(e.tagName)); })()"
            )
            if focused_editor:
                return False
        except Exception:
            pass
        return True

    def _activate_sketch_tool_shortcut(self, tool):
        if not self._solve_shortcut_context_active():
            return False
        if hasattr(self, "_solve_workspace_instance"):
            workspace = self._solve_workspace_instance
        else:
            workspace = getattr(self, "solve_workspace", None)
        if workspace is None:
            return False
        default_variants = {
            "rectangle": "corner",
            "circle": "center-radius",
            "arc": "three-point",
        }
        workspace.set_sketch_tool(tool, variant=default_variants.get(tool))
        return True

    def _undo_solve_action(self, *args):
        if self._solve_shortcut_context_active():
            return self.solve_workspace.undo()
        return False

    def _redo_solve_action(self, *args):
        if self._solve_shortcut_context_active():
            return self.solve_workspace.redo()
        return False

    def _replacement_job_active(self):
        return bool(
            self._solver_job_active
            or getattr(getattr(self, "_solve_workspace_instance", None), "_job_active", False)
        )

    def _has_unsaved_problem_changes(self):
        workspace = self.solve_workspace
        return bool(
            workspace.dirty
            or getattr(workspace, "_editor_drawer", None) is not None
        )

    def _has_unsaved_project_changes(self):
        active = getattr(self, "_active_project_type", None)
        if active == "3d":
            return bool(getattr(getattr(self, "_three_d_project_dialog", None), "dirty", False))
        if active == "axisymmetric":
            return self._has_unsaved_problem_changes()
        return False

    def _request_problem_replacement(self, action):
        """Guard every operation that replaces the active project."""
        if self._replacement_job_active():
            self._notify(
                "Wait for the current solve to finish before replacing the problem.",
                type="warning",
                timeout=4000,
            )
            return False
        if self._has_unsaved_project_changes():
            self._pending_problem_replacement = action
            active = getattr(self, "_active_project_type", None)
            if active == "3d" or (
                active is None
                and getattr(getattr(self, "_three_d_project_dialog", None), "dirty", False)
                and not self._has_unsaved_problem_changes()
            ):
                self._pending_project_type = "3d"
            else:
                self._pending_project_type = "axisymmetric"
            self._new_problem_dialog.ui_model_value = True
            return False
        return action()

    def new_problem(self, *args):
        """Start a blank project of the active type, guarding unsaved edits."""
        active = getattr(self, "_active_project_type", None)
        start_name = "_start_3d_project_from_menu" if active == "3d" else "_start_new_problem"
        start_project = getattr(self, start_name, None)
        request = getattr(self, "_request_problem_replacement", None)
        if callable(request):
            return request(start_project) if callable(start_project) else False
        # Preserve the old direct-call behavior for lightweight integrations
        # that invoke this handler on a proxy instead of an NGSolveGui instance.
        if getattr(self, "_solver_job_active", False) or getattr(
            getattr(self, "solve_workspace", None), "_job_active", False
        ):
            self._notify(
                "Wait for the current solve to finish before starting a new problem.",
                type="warning",
                timeout=4000,
            )
            return False
        if getattr(self.solve_workspace, "dirty", False):
            self._new_problem_dialog.ui_model_value = True
            return False
        return start_project() if callable(start_project) else False

    def _close_new_problem_dialog(self, *args):
        self._new_problem_dialog.ui_model_value = False
        self._pending_problem_replacement = None
        self._pending_project_type = None

    def _on_new_problem_dialog_model_value(self, event):
        if not bool(getattr(event, "value", event)):
            # Backdrop and Escape dismissal are equivalent to Cancel. A pending
            # replacement must never run later because the dialog was closed.
            self._pending_problem_replacement = None
            self._pending_project_type = None

    def _continue_problem_replacement(self, save_changes):
        action = self._pending_problem_replacement
        project_type = self._pending_project_type
        self._close_new_problem_dialog()
        if action is None:
            return False
        if save_changes:
            if project_type == "3d":
                saved = self._three_d_project_dialog.save_project()
            else:
                if getattr(self.solve_workspace, "_editor_drawer", None) is not None:
                    self.solve_workspace._commit_editor()
                    if self.solve_workspace._editor_drawer is not None:
                        return False
                saved = self.save_problem_file()
            if not saved:
                self._pending_problem_replacement = action
                self._pending_project_type = project_type
                self._new_problem_dialog.ui_model_value = True
                return False
        elif project_type == "3d":
            self._three_d_project_dialog.discard_project()
        elif project_type == "axisymmetric":
            self.solve_workspace.set_model(
                new_model(),
                new_studies(),
                {"schema_version": 1, "active_section": "geometry", "camera": "fit"},
            )
            self._loaded_mesh_path = None
            self._loaded_mesh_problem_sha256 = None
        return action()

    def _discard_and_continue_problem_replacement(self, *args):
        return self._continue_problem_replacement(save_changes=False)

    def _save_and_continue_problem_replacement(self, *args):
        return self._continue_problem_replacement(save_changes=True)

    def _discard_and_start_new_problem(self, *args):
        return self._start_new_problem()

    def _save_and_start_new_problem(self, *args):
        self._close_new_problem_dialog()
        if not self.save_problem_file():
            # Keep the decision available if the file picker was cancelled or
            # saving failed; the current problem must remain intact.
            self._new_problem_dialog.ui_model_value = True
            return False
        return self._start_new_problem()

    def _save_as_project_file(self, *args):
        return self.save_problem_file(save_as=True)

    def _start_new_problem(self):
        if self._solver_job_active or getattr(self.solve_workspace, "_job_active", False):
            self._notify(
                "Wait for the current solve to finish before starting a new problem.",
                type="warning",
                timeout=4000,
            )
            return False
        self._close_new_problem_dialog()
        self._current_project_path = None
        self._current_project_kind = None
        self.solve_workspace.set_model(
            new_model(),
            new_studies(),
            {"schema_version": 1, "active_section": "geometry", "camera": "fit"},
        )
        three_d_workspace = getattr(self, "_three_d_workspace_instance", None)
        if three_d_workspace is not None:
            three_d_workspace.ui_hidden = True
            container = getattr(self, "_three_d_workspace_container", None)
            if container is not None:
                container.ui_hidden = True
        self._active_project_type = "axisymmetric"
        hide_chooser = getattr(self, "_hide_startup_chooser", None)
        if callable(hide_chooser):
            hide_chooser()
        self._loaded_mesh_path = None
        self._loaded_mesh_problem_sha256 = None
        if self._workspace_mode != "solve":
            self._workspace_mode_toggle.select("solve")
        set_3d_active = getattr(self, "_set_3d_workspace_active", None)
        if callable(set_3d_active):
            set_3d_active(False)
        self._notify("Started a new problem.", type="positive", timeout=2500)
        return True

    def _delete_selected_region_shortcut(self, *args):
        if getattr(self, "_active_project_type", None) == "3d":
            return self._three_d_workspace.delete_selected()
        if not self._solve_shortcut_context_active():
            return False
        return self.solve_workspace.delete_selected_geometry()

    def _set_workspace_mode(self, mode):
        if mode not in WorkspaceModeToggle._MODES:
            raise ValueError(f"Unknown workspace mode: {mode}")

        solving = mode == "solve"
        self._workspace_mode = mode
        navigator = getattr(self, "navigator", None)
        if navigator is not None:
            set_history_visible = getattr(navigator, "set_run_history_visible", None)
            if callable(set_history_visible):
                set_history_visible(not solving)
        if getattr(self, "_active_project_type", None) == "3d":
            self._three_d_workspace.ui_hidden = not solving
            self._post_process_workspace.ui_hidden = solving
            self._solve_workspace.ui_hidden = True
            self._set_application_menu_context(True)
            return
        self._post_process_workspace.ui_hidden = solving
        self._solve_workspace.ui_hidden = not solving
        refresh_history_controls = getattr(self, "_refresh_solve_history_controls", None)
        if refresh_history_controls:
            refresh_history_controls()
        self._set_application_menu_context(False)

    def _save_project_artifact(self, data, filename, *, kind, save_as=False):
        """Save to the current project path or request a new path."""
        current_path = getattr(self, "_current_project_path", None)
        current_kind = getattr(self, "_current_project_kind", None)
        target = Path(current_path) if current_path else None
        expected_suffix = Path(filename).suffix.casefold()
        if (
            os.name == "nt"
            and not save_as
            and target is not None
            and current_kind == kind
            and target.suffix.casefold() == expected_suffix
            and target.is_file()
        ):
            payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=target.parent,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            self._last_saved_path = str(target)
            return True

        if not self._save_artifact(data, filename):
            return False
        saved_path = getattr(self, "_last_saved_path", None)
        if saved_path and os.name == "nt":
            self._current_project_path = saved_path
            self._current_project_kind = kind
        elif save_as:
            # The browser File System Access API does not expose the saved
            # destination to Python, so it cannot be safely reused by Ctrl+S.
            self._current_project_path = None
            self._current_project_kind = None
        return True

    def _save_artifact(self, data, filename):
        self._last_saved_path = None
        # The browser File System Access writer is asynchronous. In the local
        # Windows application, use an OS save dialog and a synchronous write so
        # JSON, mesh, and solution bundles are complete before the action returns.
        if os.name == "nt":
            from .native_dialog import save_file_dialog

            initialdir = (
                self._local_path
                or self.usersettings.get("save_dir", "")
                or self.usersettings.get("load_dir", "")
                or os.path.expanduser("~")
            )
            suffix = Path(filename).suffix or "*"
            path = save_file_dialog(
                title=f"Save {PRODUCT_NAME} file",
                initialdir=initialdir,
                filename=filename,
                filters=[(f"{PRODUCT_NAME} file", f"*{suffix}"), ("All Files", "*")],
            )
            if not path:
                return False
            payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
            Path(path).write_bytes(payload)
            self._last_saved_path = path
            self._local_path = str(Path(path).parent)
            self.usersettings.set("save_dir", self._local_path)
            self.usersettings.set("load_dir", self._local_path)
            return True

        from ngapp.utils import get_environment

        write = get_environment().begin_save_file_local(filename)
        if write is None:
            return False
        write(data)
        return True

    def _pick_artifact(
        self, *, title, description, extension, additional_extensions=(),
        force_native=False, read_bytes=True,
    ):
        """Return ``(path, bytes)`` for a native or browser file selection."""
        from ngapp.utils import EnvironmentType, get_environment

        env = get_environment()
        use_browser_picker = os.environ.get("NGSOLVE_GUI_FILE_PICKER", "").strip().lower() == "browser"
        if env.type == EnvironmentType.LOCAL_APP and (force_native or not use_browser_picker):
            from .native_dialog import open_file_dialog

            path = open_file_dialog(
                title=title,
                initialdir=self._local_path or self.usersettings.get("load_dir", "") or os.path.expanduser("~"),
                filters=[(description, " ".join(f"*{suffix}" for suffix in (extension, *additional_extensions)))],
            )
            if not path:
                return None, None
            self._local_path = os.path.dirname(path)
            self.usersettings.set("load_dir", self._local_path)
            if not read_bytes:
                return path, None
            with open(path, "rb") as source:
                return path, source.read()

        picker_options = {"multiple": False}
        picker_extensions = (extension, *additional_extensions)
        # Chromium's File System Access API accepts alphanumeric suffixes only.
        # Our portable bundle suffixes contain hyphens (for example
        # ``.mmfem-mesh``), so passing them as ``types`` makes the picker throw
        # before it opens. In that case let the user choose any file and rely
        # on the bundle validator below to reject unrelated files safely.
        if all(re.fullmatch(r"\.[a-z0-9]+", suffix, re.IGNORECASE) for suffix in picker_extensions):
            accepted_types = {}
            for suffix in picker_extensions:
                mime_type = {".ngsmodel": "application/zip", ".json": "application/json"}.get(
                    suffix.lower(), mimetypes.guess_type("file" + suffix)[0]
                ) or "application/octet-stream"
                accepted_types.setdefault(mime_type, []).append(suffix)
            picker_options["types"] = [{
                "description": description,
                "accept": accepted_types,
            }]
        from .picker_wait import BROWSER_PICKER_WAIT_SECONDS, browser_file_picker_wait

        try:
            with browser_file_picker_wait():
                handles = self.js.showOpenFilePicker(picker_options)
        except TimeoutError as error:
            minutes = BROWSER_PICKER_WAIT_SECONDS // 60
            raise TimeoutError(
                f"The browser file picker did not return within {minutes} minutes. "
                "Choose the file again and leave the picker open until selection finishes."
            ) from error
        if not handles:
            return None, None
        js_file = handles[0].getFile()
        data = bytes(js_file.arrayBuffer())
        import tempfile

        directory = tempfile.mkdtemp(prefix="mmfem_open_")
        path = os.path.join(directory, js_file.name)
        with open(path, "wb") as target:
            target.write(data)
        return path, data if read_bytes else None

    def _choose_legacy_problem_file(self, *args):
        if self._replacement_job_active():
            self._notify("Wait for the current solve to finish before replacing the problem.", type="warning", timeout=4000)
            return False
        try:
            path, _data = self._pick_artifact(
                title="Import legacy Python problem file",
                description="Legacy Python problem file",
                extension=".py",
            )
            if not path:
                return
            from .legacy_problem_import import import_legacy_problem_file

            model, studies, layout = import_legacy_problem_file(path)

            def apply_imported_problem():
                try:
                    self.solve_workspace.set_model(model, studies, layout)
                    self._loaded_mesh_path = None
                    self._loaded_mesh_problem_sha256 = None
                    if self._workspace_mode != "solve":
                        self._workspace_mode_toggle.select("solve")
                    region_count = len(model.get("geometry", {}).get("regions", []))
                    boundary_count = len(model.get("boundary_conditions", []))
                    self._notify(
                        f"Imported {region_count} regions and {boundary_count} boundary condition types. "
                        "Review the mesh and study settings, then save this problem as JSON.",
                        type="positive",
                        timeout=6500,
                    )
                    self.solve_workspace._message(f"Imported legacy problem: {Path(path).name}.")
                    return True
                except Exception as error:
                    self._notify(f"Could not import legacy problem: {error}", type="negative", timeout=9000)
                    self.solve_workspace._message(f"Legacy import failed: {error}", error=True)
                    return False

            return self._request_problem_replacement(apply_imported_problem)
        except Exception as error:
            self._notify(f"Could not import legacy problem: {error}", type="negative", timeout=9000)
            self.solve_workspace._message(f"Legacy import failed: {error}", error=True)

    def save_problem_file(self, *_args, save_as=False):
        """Save the problem definition and its study settings."""
        if getattr(self, "_active_project_type", None) == "3d":
            if save_as:
                return self._three_d_workspace.save_project(save_as=True)
            return self._three_d_workspace.save_project()
        if getattr(self.solve_workspace, "_editor_drawer", None) is not None:
            self.solve_workspace._commit_editor()
            if self.solve_workspace._editor_drawer is not None:
                return False
        model = self.solve_workspace.model
        errors = self.solve_workspace.validation_errors()
        # Empty sketches are useful drafts; structural errors still prevent
        # saving, while the status message explains the missing geometry.
        try:
            data = serialize_model_json(model, self.solve_workspace.studies, self.solve_workspace.layout)
            title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in model.get("name", "axisymmetric-model")).strip()
            filename = (title or "axisymmetric-model") + ".json"
            if not self._save_project_artifact(
                data, filename, kind="axisymmetric", save_as=save_as
            ):
                return False
            self.solve_workspace.mark_saved()
            remember = getattr(self, "_remember_recent_project", None)
            if callable(remember):
                remember(
                    getattr(self, "_last_saved_path", None),
                    kind="axisymmetric",
                    name=model.get("name", "Axisymmetric project"),
                    project_data=data.encode("utf-8"),
                )
            self._notify(
                "Project saved." if save_as else "Project saved.",
                type="positive",
                timeout=2500,
            )
            self.solve_workspace._message(f"Saved {filename}.")
            if errors:
                self._notify("Model saved as a draft. Review Geometry and Physics before running.", type="warning", timeout=5000)
            return True
        except Exception as error:
            self._notify(f"Could not save problem file: {error}", type="negative", timeout=7000)
            self.solve_workspace._message(f"Save failed: {error}", error=True)
            return False

    def save_project(self):
        """Compatibility alias for the former Save Project action."""
        return self.save_problem_file()

    def load_problem_file(self):
        """Compatibility alias for the unified project picker."""
        return self.open_project_file()

    def load_project(self):
        """Compatibility alias for the former Load Project action."""
        return self.load_problem_file()

    def _matching_mesh_path(self):
        problem_hash = problem_fingerprint(self.solve_workspace.model)
        if (
            self._loaded_mesh_path
            and self._loaded_mesh_problem_sha256 == problem_hash
            and Path(self._loaded_mesh_path).is_file()
        ):
            return Path(self._loaded_mesh_path)
        for run in reversed(self.solve_workspace.runs):
            if run.get("status") != "Complete":
                continue
            output = Path(run.get("output_path", ""))
            mesh_path = output / "mesh.vol"
            if not mesh_path.is_file():
                continue
            manifest = self._read_run_manifest(output)
            if manifest.get("problem_sha256") == problem_hash:
                return mesh_path
            # Runs created before mesh bundles carried a digest can still be
            # matched through their saved problem snapshot.
            try:
                saved_model = json.loads((output / "problem.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if problem_fingerprint(saved_model) == problem_hash:
                return mesh_path
        return None

    def save_mesh_file(self):
        """Save the latest mesh generated for the current problem."""
        try:
            mesh_path = self._matching_mesh_path()
            if mesh_path is None:
                raise ValueError("Generate a mesh for this problem before saving it.")
            data = make_mesh_bundle(mesh_path.read_bytes(), self.solve_workspace.model)
            title = "".join(c if c.isalnum() or c in "-_ " else "_" for c in self.solve_workspace.model.get("name", "axisymmetric-model")).strip()
            filename = (title or "axisymmetric-model") + MESH_SUFFIX
            if self._save_artifact(data, filename):
                self._notify("Mesh saved with its matching problem definition.", type="positive", timeout=3000)
        except Exception as error:
            self._notify(f"Could not save mesh: {error}", type="negative", timeout=7000)

    def load_mesh_file(self):
        """Load a mesh only when its problem fingerprint matches the editor."""
        try:
            path, data = self._pick_artifact(
                title="Load mesh for the current problem",
                description=f"{PRODUCT_NAME} mesh bundle",
                extension=MESH_SUFFIX,
            )
            if not path:
                return
            mesh_data = read_mesh_bundle(data, self.solve_workspace.model)
            import tempfile

            directory = tempfile.mkdtemp(prefix="mmfem_mesh_")
            mesh_path = Path(directory) / "mesh.vol"
            mesh_path.write_bytes(mesh_data)
            self._loaded_mesh_path = str(mesh_path)
            self._loaded_mesh_problem_sha256 = problem_fingerprint(self.solve_workspace.model)
            self.solve_workspace._message("Matching mesh loaded. The next run will reuse it.")
            self._notify("Mesh loaded for this problem.", type="positive", timeout=3000)
        except Exception as error:
            self._notify(f"Could not load mesh: {error}", type="negative", timeout=7000)
            self.solve_workspace._message(f"Mesh load failed: {error}", error=True)

    def save_solutions_file(self):
        """Save all GUI fields from the latest completed matching study."""
        try:
            model_hash = problem_fingerprint(self.solve_workspace.model)
            for run in reversed(self.solve_workspace.runs):
                if run.get("kind") != "Study" or run.get("status") != "Complete":
                    continue
                output = Path(run.get("output_path", ""))
                manifest = self._read_run_manifest(output)
                if manifest.get("problem_sha256") != model_hash:
                    continue
                if os.name == "nt":
                    from .native_dialog import save_file_dialog

                    filename = ("".join(
                        c if c.isalnum() or c in "-_ " else "_"
                        for c in self.solve_workspace.model.get("name", "axisymmetric-model")
                    ).strip() or "axisymmetric-model") + SOLUTION_SUFFIX
                    initialdir = (
                        self._local_path
                        or self.usersettings.get("save_dir", "")
                        or self.usersettings.get("load_dir", "")
                        or os.path.expanduser("~")
                    )
                    path = save_file_dialog(
                        title=f"Save {PRODUCT_NAME} solution bundle",
                        initialdir=initialdir,
                        filename=filename,
                        filters=[(f"{PRODUCT_NAME} solution bundle", f"*{SOLUTION_SUFFIX}"), ("All Files", "*")],
                    )
                    if not path:
                        return
                    write_solution_bundle(output, self.solve_workspace.model, path)
                    self._last_saved_path = path
                    self._local_path = str(Path(path).parent)
                    self.usersettings.set("save_dir", self._local_path)
                    self.usersettings.set("load_dir", self._local_path)
                    self._notify("Solution bundle saved.", type="positive", timeout=3000)
                    return
                data, filename = make_solution_bundle(output, self.solve_workspace.model)
                if self._save_artifact(data, filename):
                    self._notify("Solution bundle saved.", type="positive", timeout=3000)
                return
            raise ValueError("Run a study for the current problem before saving its solutions.")
        except Exception as error:
            self._notify(f"Could not save solutions: {error}", type="negative", timeout=7000)

    def load_solutions_file(self):
        """Open a saved solution bundle in Post Process."""
        try:
            path, _data = self._pick_artifact(
                title=f"Open {PRODUCT_NAME} solution bundle",
                description=f"{PRODUCT_NAME} solution bundle or field pickle",
                extension=SOLUTION_SUFFIX,
                additional_extensions=(".pkl",),
                read_bytes=False,
            )
            if not path:
                return
            if self._workspace_mode != "post_process":
                self._workspace_mode_toggle.select("post_process")
            self._load_with_status(path)
        except Exception as error:
            self._notify(f"Could not load solutions: {error}", type="negative", timeout=7000)

    def run_3d_study(self, workspace):
        """Run a supported 3D harmonic study in a cancellable child process."""
        run_kind = "Study"
        project_name = str(workspace.project.get("name", "3D project"))
        run_name = f"{run_kind}: {project_name}"
        with self._solver_job_lock:
            if self._solver_job_active:
                workspace._set_status("A mesh or solver job is already running.", error=True)
                return False
            self._solver_job_active = True
            self._solver_cancel_requested = False
            self._solver_process = None
        self._set_application_menu_context(True)

        output_dir = None
        try:
            workspace._snapshot_referenced_materials()
            project = copy.deepcopy(workspace.project)
            assets = dict(workspace.assets)
            from .three_d_project import SUPPORTED_3D_STUDIES, validate_3d_project
            from .three_d_solver import validate_3d_study_setup

            validate_3d_project(project, assets)
            setup = validate_3d_study_setup(project)
            study_label = SUPPORTED_3D_STUDIES[setup["physics"]]
            solver_root = self._find_solver_root()
            runner = solver_root / "scripts" / "run_gui_3d_model.py"
            if not runner.is_file():
                raise FileNotFoundError(f"The solver checkout is missing its 3D GUI runner: {runner}")

            from .solve_workspace import default_run_history_root

            safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", project_name).strip("_") or "3d_project"
            output_dir = default_run_history_root() / (
                f"{safe_name}_{_datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
            )
            output_dir.mkdir(parents=True, exist_ok=False)
            model_bytes = package_3d_project(project, assets)
            model_path = output_dir / f"{safe_name}{PROJECT_SUFFIX}"
            model_path.write_bytes(model_bytes)
            manifest = {
                "schema": "mm-fem.run",
                "schema_version": 1,
                "status": "running",
                "execution_status": "running",
                "kind": "study",
                "created_utc": _datetime.now(_timezone.utc).isoformat(),
                "model_archive": model_path.name,
                "output_path": str(output_dir),
                "model_name": project_name,
                "problem_sha256": hashlib.sha256(model_bytes).hexdigest(),
                "physics": study_label,
                "polynomial_order": setup["polynomial_order"],
                "diagnostics": {
                    "process_context": "solver_process.json",
                    "native_crash_log": "native_crash.log",
                },
            }
            if setup["frequency_points_hz"]:
                manifest["frequency_points_hz"] = setup["frequency_points_hz"]
                manifest["frequency_points"] = [
                    {
                        "index": index,
                        "frequency_hz": value,
                        "execution_status": "pending",
                        "scientific_status": "pending",
                    }
                    for index, value in enumerate(setup["frequency_points_hz"])
                ]
                manifest["progress"] = {
                    "completed_frequencies": 0,
                    "total_frequencies": len(setup["frequency_points_hz"]),
                    "current_frequency_index": None,
                }
                if len(setup["frequency_points_hz"]) == 1:
                    manifest["frequency_hz"] = setup["frequency_points_hz"][0]
            if setup["physics"] == "harmonic_em":
                manifest["air_padding"] = setup["air_padding"]
            elif setup["physics"] == "magnetostatic_dc":
                manifest["air_padding"] = setup["air_padding"]
                manifest["applied_dc_background_field_t"] = setup[
                    "dc_background_field_t"
                ]
            else:
                manifest["mechanical_body_force_n_m3"] = setup[
                    "mechanical_body_force_n_m3"
                ]
            self._write_run_manifest(output_dir, manifest)
        except Exception as error:
            with self._solver_job_lock:
                self._solver_job_active = False
                self._set_application_menu_context(
                    getattr(self, "_active_project_type", None) == "3d"
                )
            workspace._set_status(f"Could not prepare study: {error}", error=True)
            self._notify(f"Could not prepare 3D study: {error}", type="negative", timeout=8000)
            return False

        workspace._study_running = True
        self._set_application_menu_context(True)
        workspace._update_study_controls()
        run_description = (
            ", ".join(f"{value:g}" for value in setup["frequency_points_hz"]) + " Hz "
            if setup["frequency_points_hz"] else ""
        )
        workspace._set_status(
            f"Running {run_description}{study_label} in {output_dir}…"
        )

        def dc_background_detail(run_manifest):
            source = run_manifest.get("dc_background_field_source")
            check = run_manifest.get("dc_background_field_reuse_check", {})
            if source == "reused":
                return "DC field reused"
            if source == "computed" and check.get("status") == "missing":
                return "DC field calculated · none was saved"
            if source == "computed" and check.get("status") == "no_compatible_saved_field":
                return "DC field recalculated · no compatible saved field"
            if source == "computed":
                return "DC field calculated"
            return None

        def run_job():
            log_path = output_dir / "solver.log"
            process = None
            try:
                command = [
                    sys.executable,
                    "-u",
                    str(runner),
                    "--model",
                    str(model_path),
                    "--output-dir",
                    str(output_dir),
                ]
                popen_options = {
                    "cwd": str(solver_root),
                    "env": _solver_subprocess_environment(),
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "bufsize": 1,
                }
                if os.name == "nt":
                    popen_options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                else:
                    popen_options["start_new_session"] = True
                process = subprocess.Popen(command, **popen_options)
                with self._solver_job_lock:
                    self._solver_process = process
                    cancel_now = self._solver_cancel_requested
                if cancel_now:
                    process.terminate()

                with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                    assert process.stdout is not None
                    for line in process.stdout:
                        log_file.write(line)
                        log_file.flush()
                        text = line.strip()
                        if text and (
                            text.startswith((PRODUCT_NAME, "MM-FEM"))
                            or text.startswith("The mesh has")
                            or text.startswith("Frequency=")
                            or "degrees of freedom" in text
                        ):
                            if text.startswith("MM-FEM"):
                                text = PRODUCT_NAME + text[len("MM-FEM"):]
                            self._dispatch_to_ui(workspace._set_status, text[:240])
                        start_match = re.match(
                            r"^FREQUENCY_START\s+index=(\d+)\s+Hz=([^\s]+)\s+total=(\d+)$",
                            text,
                        )
                        if start_match:
                            index, frequency, total = start_match.groups()
                            self._dispatch_to_ui(
                                workspace._set_status,
                                f"Frequency {int(index) + 1} of {total} · {frequency} Hz · running",
                            )
                        checkpoint_match = re.match(
                            r"^FREQUENCY_CHECKPOINT\s+index=(\d+)\s+Hz=([^\s]+)\s+converged=([^\s]+)\s+iterations=([^\s]+)$",
                            text,
                        )
                        if checkpoint_match:
                            index, frequency, converged, iterations = checkpoint_match.groups()
                            point_state = (
                                "converged" if converged.casefold() == "true"
                                else "not converged" if converged.casefold() == "false"
                                else "completed"
                            )
                            total = len(setup["frequency_points_hz"])
                            self._dispatch_to_ui(
                                workspace._set_status,
                                f"Frequency {int(index) + 1} of {total} · {frequency} Hz · "
                                f"{point_state} · {int(index) + 1}/{total} complete",
                            )
                return_code = process.wait()
                manifest = self._read_run_manifest(output_dir)
                with self._solver_job_lock:
                    cancelled = self._solver_cancel_requested and return_code != 0
                if cancelled:
                    points = manifest.get("frequency_points", [])
                    progress = manifest.get("progress", {})
                    active_index = progress.get("current_frequency_index")
                    if not isinstance(active_index, int) or not 0 <= active_index < len(points):
                        active_index = next((
                            index for index, point in enumerate(points)
                            if point.get("execution_status") in {"running", "pending"}
                        ), None)
                    if active_index is not None:
                        points[active_index]["execution_status"] = "cancelled"
                    for point in points:
                        if point.get("execution_status") == "pending":
                            point["execution_status"] = "not_started"
                    manifest["progress"] = {
                        **progress,
                        "completed_frequencies": sum(
                            point.get("execution_status") == "complete" for point in points
                        ),
                        "current_frequency_index": None,
                    }
                    manifest.update({
                        "status": "cancelled",
                        "execution_status": "cancelled",
                        "scientific_status": (
                            "partial" if any(
                                point.get("execution_status") == "complete" for point in points
                            ) else "unavailable"
                        ),
                        "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                    })
                    self._write_run_manifest(output_dir, manifest)
                    self._dispatch_to_ui(
                        workspace._set_status,
                        f"Study cancelled. Partial files are in {output_dir}",
                    )
                elif return_code != 0:
                    detail = manifest.get("error") or _solver_process_failure_detail(return_code)
                    manifest.update({
                        "status": "failed",
                        "execution_status": "failed",
                        "scientific_status": (
                            "partial" if any(
                                point.get("execution_status") == "complete"
                                for point in manifest.get("frequency_points", [])
                            ) else "unavailable"
                        ),
                        "error": detail,
                        "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                    })
                    self._write_run_manifest(output_dir, manifest)
                    raise RuntimeError(detail)
                elif manifest.get("scientific_status") == "nonconverged":
                    manifest.update({
                        "status": "complete",
                        "execution_status": "complete",
                        "run_name": run_name,
                        "solver_log": "solver.log",
                    })
                    self._write_run_manifest(output_dir, manifest)
                    self._dispatch_to_ui(workspace._set_dc_background_run_state, manifest)
                    points = manifest.get("frequency_points", [])
                    failed_points = [
                        point for point in points
                        if point.get("scientific_status") == "nonconverged"
                    ]
                    failed_hz = ", ".join(
                        f"{point['frequency_hz']:g}" for point in failed_points
                    )
                    detail = f" at {failed_hz} Hz" if failed_hz else ""
                    status_message = (
                        f"Execution complete · {len(failed_points)} of {len(points)} frequency points "
                        f"did not converge{detail}. Saved fields are diagnostic only at {output_dir}"
                    )
                    dc_detail = dc_background_detail(manifest)
                    if dc_detail:
                        status_message += f" · {dc_detail}"
                    self._dispatch_to_ui(
                        lambda: workspace._set_status(status_message)
                    )
                    self._dispatch_to_ui(lambda: self._notify(
                        f"3D {study_label}: {len(failed_points)} frequency points did not converge. "
                        "Results are marked diagnostic only.",
                        type="warning",
                        timeout=9000,
                    ))
                else:
                    fields_dir = output_dir / "ngsolve_gui" / "fields"
                    field_files = list(fields_dir.glob("*.pkl")) if fields_dir.is_dir() else []
                    if not field_files:
                        raise RuntimeError("The solver finished without exporting any GUI fields.")
                    manifest.update({
                        "status": "complete",
                        "execution_status": "complete",
                        "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                        "solver_log": "solver.log",
                    })
                    self._write_run_manifest(output_dir, manifest)
                    self._dispatch_to_ui(workspace._set_dc_background_run_state, manifest)
                    dc_detail = dc_background_detail(manifest)
                    completion_status = "Study complete"
                    if dc_detail:
                        completion_status += f" · {dc_detail}"
                    self._dispatch_to_ui(
                        workspace._set_status,
                        f"{completion_status} · results saved to {output_dir}",
                    )
                    self._dispatch_to_ui(lambda: self._notify(
                        f"3D {study_label} complete"
                        + (f" · {dc_detail}" if dc_detail else "")
                        + f" · {output_dir}",
                        type="positive",
                        timeout=7000,
                    ))
                    self._dispatch_to_ui(
                        self._open_run_result,
                        str(output_dir / "ngsolve_gui" / "solution.pkl"),
                    )
            except Exception as error:
                manifest = self._read_run_manifest(output_dir)
                manifest.update({
                    "status": "failed",
                    "execution_status": "failed",
                    "scientific_status": (
                        "partial" if any(
                            point.get("execution_status") == "complete"
                            for point in manifest.get("frequency_points", [])
                        ) else "unavailable"
                    ),
                    "error": f"{type(error).__name__}: {error}",
                    "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                    "solver_log": "solver.log",
                })
                self._write_run_manifest(output_dir, manifest)
                status_detail = f"Study failed: {error}"
                notification_detail = f"3D study failed: {error}"
                self._dispatch_to_ui(lambda detail=status_detail: workspace._set_status(
                    detail, error=True
                ))
                self._dispatch_to_ui(lambda detail=notification_detail: self._notify(
                    detail, type="negative", timeout=9000
                ))
            finally:
                if not _reap_solver_process(process):
                    try:
                        manifest = self._read_run_manifest(output_dir)
                        manifest.update({
                            "status": "failed",
                            "execution_status": "failed",
                            "error": "Solver child process remained alive after terminate and kill.",
                            "process_cleanup_error": "Child process could not be reaped.",
                            "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                        })
                        self._write_run_manifest(output_dir, manifest)
                    except Exception as cleanup_error:
                        print(f"Could not record solver cleanup failure: {cleanup_error}")
                    try:
                        self._dispatch_to_ui(
                            workspace._set_status,
                            "Study failed because the solver process could not be stopped.",
                            error=True,
                        )
                    except Exception as cleanup_error:
                        print(f"Could not update 3D cleanup status: {cleanup_error}")
                if process is not None and process.stdout is not None:
                    try:
                        process.stdout.close()
                    except OSError:
                        pass
                self._dispatch_to_ui(lambda: self._finish_3d_study_controls(workspace))
                with self._solver_job_lock:
                    self._solver_process = None
                    self._solver_run_context = None
                    self._solver_job_active = False
                    self._solver_cancel_requested = False
                self._dispatch_to_ui(
                    lambda: self._set_application_menu_context(
                        getattr(self, "_active_project_type", None) == "3d"
                    )
                )

        self._solver_run_context = {
            "kind": run_kind,
            "output_dir": output_dir,
            "run_name": run_name,
            "project_type": "3d",
        }
        threading.Thread(target=run_job, name="MMFEM3DStudy", daemon=True).start()
        return True

    def cancel_3d_study(self, workspace=None):
        with self._solver_job_lock:
            context = self._solver_run_context or {}
            if not self._solver_job_active or context.get("project_type") != "3d":
                return False
            self._solver_cancel_requested = True
            process = self._solver_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        if workspace is not None:
            workspace._set_status("Cancellation requested…")
        return True

    def _dispatch_to_ui(self, callback, *args):
        """Run a callback on the thread that handles ngapp component events."""
        try:
            from webgpu import platform

            server = platform.websocket_server
            loop = getattr(server, "_callback_loop", None)
            thread = getattr(server, "_callback_thread", None)
        except Exception:
            loop = thread = None

        if loop is not None and loop.is_running():
            if thread is not None and threading.get_ident() == thread.ident:
                callback(*args)
                return True
            try:
                loop.call_soon_threadsafe(callback, *args)
                return True
            except RuntimeError:
                # The app may be closing while a solver child exits.
                return False
        callback(*args)
        return True

    def _finish_3d_study_controls(self, workspace):
        workspace._study_running = False
        self._set_application_menu_context(
            getattr(self, "_active_project_type", None) == "3d"
        )
        workspace._update_study_controls()

    def _run_axisymmetric_solver(self, *, mesh_only=False):
        """Run a saved model in a cancellable child process on this computer."""
        workspace = self.solve_workspace
        run_kind = "Mesh" if mesh_only else "Study"
        run_name = f"{run_kind}: {workspace.model.get('name', 'axisymmetric model')}"
        with self._solver_job_lock:
            if self._solver_job_active:
                workspace._message("A mesh or solver job is already running.", error=True)
                return
            self._solver_job_active = True
            self._solver_cancel_requested = False
            self._solver_process = None
        self._set_application_menu_context(False)

        model = copy.deepcopy(workspace.model)
        studies = copy.deepcopy(workspace.studies)
        layout = copy.deepcopy(workspace.layout)
        model_hash = problem_fingerprint(model)
        loaded_mesh_path = None
        if self._loaded_mesh_path:
            if (
                self._loaded_mesh_problem_sha256 == model_hash
                and Path(self._loaded_mesh_path).is_file()
            ):
                loaded_mesh_path = self._loaded_mesh_path
            else:
                self._loaded_mesh_path = None
                self._loaded_mesh_problem_sha256 = None
                workspace._message(
                    "The loaded mesh no longer matches this problem; generating a fresh mesh."
                )
        model_name = re.sub(
            r"[^A-Za-z0-9_-]+", "_", model.get("name", "axisymmetric_model")
        ).strip("_") or "axisymmetric_model"
        stamp = _datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        # Use the same root as run-history discovery. A VM-provided output root
        # points at its shared folder; standalone runs use the user's home folder.
        from .solve_workspace import default_run_history_root

        output_dir = default_run_history_root() / f"{model_name}_{stamp}"
        model_path = output_dir / f"{model_name}.ngsmodel"
        try:
            solver_root = self._find_solver_root()
            runner = solver_root / "scripts" / "run_gui_model.py"
            if not runner.is_file():
                raise FileNotFoundError(
                    f"The solver checkout is missing its GUI runner: {runner}"
                )
            output_dir.mkdir(parents=True, exist_ok=False)
            model_path.write_bytes(package_model(model, studies, layout))
            manifest = {
                "schema": "mm-fem.run",
                "schema_version": 1,
                "status": "running",
                "kind": "mesh" if mesh_only else "study",
                "created_utc": _datetime.now(_timezone.utc).isoformat(),
                "model_archive": model_path.name,
                "output_path": str(output_dir),
                "model_name": model.get("name", "axisymmetric model"),
                "problem_sha256": model_hash,
                "physics": copy.deepcopy(model.get("physics", {})),
                "mesh_settings": copy.deepcopy(model.get("mesh", {})),
                "solver_settings": copy.deepcopy(model.get("solver", {})),
                "studies": copy.deepcopy(studies.get("studies", [])),
                "diagnostics": {
                    "process_context": "solver_process.json",
                    "native_crash_log": "native_crash.log",
                },
            }
            self._write_run_manifest(output_dir, manifest)
        except Exception as error:
            with self._solver_job_lock:
                self._solver_job_active = False
            self._set_application_menu_context(False)
            workspace.finish_solver_job(
                f"Could not prepare solver input: {error}",
                error=True,
                run_kind=run_kind,
                run_name=run_name,
                output_path=output_dir,
            )
            return

        workspace._message(
            ("Generating mesh" if mesh_only else "Running study")
            + f" in {output_dir}"
        )

        def run_job():
            log_buffer = _WorkspaceLogBuffer(workspace)
            log_path = output_dir / "solver.log"
            process = None
            try:
                command = [
                    sys.executable,
                    "-u",
                    str(runner),
                    "--model",
                    str(model_path),
                    "--output-dir",
                    str(output_dir),
                    "--kind",
                    "mesh" if mesh_only else "study",
                ]
                if loaded_mesh_path:
                    command.extend(["--mesh", loaded_mesh_path])
                popen_options = {
                    "cwd": str(solver_root),
                    "env": _solver_subprocess_environment(),
                    "stdin": subprocess.DEVNULL,
                    "stdout": subprocess.PIPE,
                    "stderr": subprocess.STDOUT,
                    "text": True,
                    "encoding": "utf-8",
                    "errors": "replace",
                    "bufsize": 1,
                }
                if os.name == "nt":
                    popen_options["creationflags"] = getattr(
                        subprocess, "CREATE_NO_WINDOW", 0
                    )
                else:
                    popen_options["start_new_session"] = True
                process = subprocess.Popen(command, **popen_options)
                with self._solver_job_lock:
                    self._solver_process = process
                    cancel_now = self._solver_cancel_requested
                if cancel_now:
                    process.terminate()

                with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                    assert process.stdout is not None
                    for line in process.stdout:
                        log_file.write(line)
                        log_file.flush()
                        log_buffer.write(line)
                return_code = process.wait()
                log_buffer.flush()
                with self._solver_job_lock:
                    cancelled = self._solver_cancel_requested and return_code != 0

                manifest = self._read_run_manifest(output_dir)
                if cancelled:
                    manifest.update({
                        "status": "cancelled",
                        "run_name": run_name,
                        "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                    })
                    self._write_run_manifest(output_dir, manifest)
                    workspace.finish_solver_job(
                        f"{run_kind} cancelled. Partial files are in {output_dir}",
                        cancelled=True,
                        run_kind=run_kind,
                        run_name=run_name,
                        output_path=output_dir,
                    )
                    return
                if return_code != 0:
                    detail = manifest.get("error") or _solver_process_failure_detail(return_code)
                    manifest.update({
                        "status": "failed",
                        "run_name": run_name,
                        "error": detail,
                        "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                    })
                    self._write_run_manifest(output_dir, manifest)
                    raise RuntimeError(detail)

                manifest.update({
                    "status": "complete",
                    "run_name": run_name,
                    "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                    "solver_log": "solver.log",
                })
                mesh_preview_path = output_dir / "mesh_preview.json"
                mesh_edges = []
                if mesh_preview_path.is_file():
                    mesh_payload = json.loads(mesh_preview_path.read_text(encoding="utf-8"))
                    mesh_edges = mesh_payload.get("edges", [])
                self._write_run_manifest(output_dir, manifest)
                if mesh_only:
                    workspace.finish_solver_job(
                        f"Mesh generated successfully ({len(mesh_edges):,} unique edges): {output_dir}",
                        run_kind=run_kind,
                        run_name=run_name,
                        output_path=output_dir,
                        mesh_preview_edges=mesh_edges,
                    )
                else:
                    fields_dir = output_dir / "ngsolve_gui" / "fields"
                    workspace.finish_solver_job(
                        f"Study complete. GUI-ready fields: {fields_dir}",
                        run_kind=run_kind,
                        run_name=run_name,
                        output_path=output_dir,
                        mesh_preview_edges=mesh_edges,
                    )
                    self._notify(
                        f"Study complete. Results saved to {output_dir}",
                        type="positive",
                        timeout=7000,
                    )
            except Exception as error:
                log_buffer.flush()
                manifest = self._read_run_manifest(output_dir)
                manifest.update({
                    "status": "failed",
                    "run_name": run_name,
                    "error": f"{type(error).__name__}: {error}",
                    "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                    "solver_log": "solver.log",
                })
                self._write_run_manifest(output_dir, manifest)
                workspace.finish_solver_job(
                    f"Solver failed: {error}",
                    error=True,
                    run_kind=run_kind,
                    run_name=run_name,
                    output_path=output_dir,
                )
                self._notify(
                    f"Solver failed: {error}", type="negative", timeout=9000
                )
            finally:
                if not _reap_solver_process(process):
                    try:
                        manifest = self._read_run_manifest(output_dir)
                        manifest.update({
                            "status": "failed",
                            "error": "Solver child process remained alive after terminate and kill.",
                            "process_cleanup_error": "Child process could not be reaped.",
                            "finished_utc": _datetime.now(_timezone.utc).isoformat(),
                        })
                        self._write_run_manifest(output_dir, manifest)
                    except Exception as cleanup_error:
                        print(f"Could not record solver cleanup failure: {cleanup_error}")
                    try:
                        workspace.finish_solver_job(
                            "Solver failed because the process could not be stopped.",
                            error=True,
                            run_kind=run_kind,
                            run_name=run_name,
                            output_path=output_dir,
                        )
                    except Exception as cleanup_error:
                        print(f"Could not update solver cleanup status: {cleanup_error}")
                if process is not None and process.stdout is not None:
                    try:
                        process.stdout.close()
                    except OSError:
                        pass
                with self._solver_job_lock:
                    self._solver_process = None
                    self._solver_run_context = None
                    self._solver_job_active = False
                    self._solver_cancel_requested = False
                self._dispatch_to_ui(
                    lambda: self._set_application_menu_context(False)
                )

        self._solver_run_context = {
            "kind": run_kind,
            "output_dir": output_dir,
            "run_name": run_name,
        }
        threading.Thread(
            target=run_job,
            name="NGSolveAxisymmetricChildJob",
            daemon=True,
        ).start()

    def _cancel_axisymmetric_solver(self, *args):
        """Stop the active solver child process without blocking the UI."""
        with self._solver_job_lock:
            if not self._solver_job_active:
                return
            self._solver_cancel_requested = True
            process = self._solver_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        self.solve_workspace.mark_solver_cancel_requested()

    @staticmethod
    def _read_run_manifest(output_dir):
        manifest_path = Path(output_dir) / "run_manifest.json"
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _write_run_manifest(output_dir, manifest):
        manifest_path = Path(output_dir) / "run_manifest.json"
        temporary = manifest_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        temporary.replace(manifest_path)

    def _open_run_result(self, filename):
        """Open a generated field in the existing Post Process workspace."""
        if not os.path.isfile(filename):
            self.solve_workspace._message(f"Result file no longer exists: {filename}", error=True)
            return
        # Use the toggle as the single source of mode changes so its selected
        # state stays in sync with the workspace after opening a run result.
        self._workspace_mode_toggle.select("post_process")
        self._load_with_status(filename)

    @staticmethod
    def _get_post_process_run_history():
        """Use the same run discovery as the Solve workspace for the navigator."""
        from .solve_workspace import _load_saved_run_history, default_run_history_root

        return _load_saved_run_history(default_run_history_root())

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
            # Development checkouts commonly keep the GUI and solver in
            # adjacent GitHub directories. Support that layout without making
            # it a deployment requirement; COUPLED_SOLVER_ROOT still wins.
            expanded.extend([
                resolved / "ngsolve-ui" / "coupled_solver",
                resolved.parent / "ngsolve-ui" / "coupled_solver",
                resolved / "coupled_solver",
            ])
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
            from .picker_wait import browser_file_picker_wait

            with browser_file_picker_wait():
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
        self._refresh_recent_projects_view()
        self.app_data._update = self._update_from_app_data
        # Ask Chromium for a fresh compositor frame after the frontend mounts.
        self.call_js(_schedule_initial_renderer_frame_pulse)

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
        self._sync_post_process_toolbar(comp)
        tab = self.app_data.get_tab(tabname) if tabname else None
        if comp is not None and hasattr(comp, "sync_camera"):
            comp.sync_camera()
        self._show_property_panel(comp)
        self.footer.set_component(comp, tab.get("type", "") if tab else "")
        self.kb.set_component(comp)

    def _sync_post_process_toolbar(self, comp):
        """Place active viewport controls above the full three-column page."""
        get_items = getattr(comp, "_page_toolbar_children", None)
        items = list(get_items()) if callable(get_items) else []
        if items:
            items = [self._open_solutions_toolbar_button, Div(ui_class=cb.vp_sep), *items]
        self._post_process_toolbar.ui_children = items
        self._post_process_toolbar.ui_hidden = not bool(items)

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
        if (
            self._active_project_type == "3d"
            and self._workspace_mode == "solve"
        ):
            return self._three_d_workspace.toggle_tree()
        self._nav_visible = not self._nav_visible
        self.usersettings.set("nav_visible", self._nav_visible)
        self._apply_panel_visibility()

    def _toggle_property_panel(self):
        if (
            self._active_project_type == "3d"
            and self._workspace_mode == "solve"
        ):
            return self._three_d_workspace.toggle_properties()
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

    def _update_from_app_data(self):
        """Apply worker-scheduled tab changes on the UI event-loop thread."""
        self._dispatch_to_ui(self._update)
