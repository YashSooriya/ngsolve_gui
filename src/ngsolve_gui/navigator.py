from ngapp.components import *
from pathlib import Path

from .cerbsim_style import (
    nav_panel,
    panel_header,
    panel_header_title,
    panel_scroll,
    nav_search,
    nav_group_row,
    nav_group_caret,
    nav_group_caret_collapsed,
    nav_group_name,
    nav_count_badge,
    nav_item,
    nav_item_active,
    nav_number_hint,
    nav_side,
    nav_empty,
    avatar_min,
    grow,
)

_TYPE_GROUPS = {
    "geometry": ("Geometries", "mdi-cube"),
    "mesh": ("Meshes", "mdi-vector-triangle"),
    "function": ("Solutions", "mdi-function-variant"),
    "plot": ("Plots", "mdi-chart-line"),
}


class Navigator(Div):
    def __init__(
        self,
        app_data,
        on_select,
        on_load_file=None,
        on_open_run_result=None,
        get_run_history=None,
    ):
        self.app_data = app_data
        self._on_select = on_select
        self._on_load_file = on_load_file
        self._on_open_run_result = on_open_run_result
        self._get_run_history = get_run_history
        self._collapsed = {}   # group label -> bool
        self._run_collapsed = {}
        self._run_history_visible = False
        self._run_history = []
        self._query = ""
        self._index_to_name = {}
        self._items = {}       # tab name -> (signature, QItem, QIcon, hint Div)
        self._groups = {}      # group label -> (row, caret, badge)
        self._structure = None
        self._active = None
        self._spacer = Div(ui_style="height: 12px;")

        load_btn = QBtn(
            QTooltip("Load file  ·  geometry / mesh / .py"),
            ui_flat=True, ui_dense=True, ui_round=True, ui_size="sm",
            ui_icon="mdi-plus", ui_color="grey-7",
        )
        if on_load_file is not None:
            load_btn.on_click(lambda *a: on_load_file())

        header = Div(
            Div("Navigator", ui_class=panel_header_title),
            load_btn,
            ui_class=panel_header,
        )

        self._search = QInput(
            ui_model_value="",
            ui_dense=True,
            ui_borderless=True,
            ui_clearable=True,
            ui_debounce=150,
            ui_label="Filter objects…",
            ui_class=str(grow),
        )
        self._search.on_update_model_value(self._on_search)
        search_box = Div(
            QIcon(ui_name="mdi-magnify", ui_size="16px"),
            self._search,
            ui_class=nav_search,
        )

        self._scroll = Div(ui_class=panel_scroll)

        super().__init__(
            header,
            search_box,
            self._scroll,
            ui_class=nav_panel,
        )
        self.update()

    # -- search ---------------------------------------------------------
    def _on_search(self, event):
        self._query = (event.value or "").strip().lower()
        self.update()

    # -- collapse -------------------------------------------------------
    def _toggle_group(self, label):
        self._collapsed[label] = not self._collapsed.get(label, False)
        self.update()

    # -- public ---------------------------------------------------------
    def update(self):
        """Refresh the list, rebuilding the DOM only if its structure changed."""
        structure = self._structure_sig()
        if structure == self._structure:
            self._apply_active()
            return
        self._structure = structure
        self._render()

    def set_run_history_visible(self, visible):
        visible = bool(visible)
        was_visible = self._run_history_visible
        self._run_history_visible = visible
        if visible:
            self.refresh_run_history()
        elif was_visible:
            self.update()

    def refresh_run_history(self):
        """Refresh run/result entries when Post Process becomes active."""
        previous_run_ids = {
            str(run.get("id") or run.get("output_path"))
            for run in self._run_history
        }
        if callable(self._get_run_history):
            try:
                runs = list(self._get_run_history() or [])
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                print(f"Could not refresh run history: {error}")
                runs = []
        else:
            runs = []
        try:
            from .run_history import load_run_result_tree

            self._run_history = [load_run_result_tree(run) for run in runs]
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            print(f"Could not read saved run results: {error}")
            self._run_history = []

        run_ids = [str(run.get("id") or run.get("output_path")) for run in self._run_history]
        for stale in set(self._run_collapsed) - set(run_ids):
            del self._run_collapsed[stale]
        for run_id in run_ids:
            self._run_collapsed.setdefault(run_id, True)
        if run_ids:
            newest_run_id = run_ids[-1]
            if newest_run_id not in previous_run_ids:
                # Surface the newest run's convergence or failure summary as
                # soon as history refreshes. Otherwise a prior expanded run
                # keeps the new diagnostics hidden behind a collapsed row.
                for run_id in run_ids[:-1]:
                    self._run_collapsed[run_id] = True
                self._run_collapsed[newest_run_id] = False
            elif not any(not self._run_collapsed[run_id] for run_id in run_ids):
                self._run_collapsed[newest_run_id] = False
        self.update()

    def _toggle_run(self, run_id):
        key = str(run_id)
        self._run_collapsed[key] = not self._run_collapsed.get(key, True)
        self.update()

    def _structure_sig(self):
        return (
            self._query,
            tuple(
                (
                    label,
                    self._collapsed.get(label, False),
                    tuple((n, t["title"], t.get("icon", "")) for n, t in tabs),
                )
                for label, _icon, tabs in self._ordered_groups()
            ),
            self._run_history_visible,
            tuple(
                (
                    str(run.get("id") or run.get("output_path")),
                    run.get("name"),
                    run.get("status"),
                    run.get("diagnostic_summary"),
                    self._run_collapsed.get(
                        str(run.get("id") or run.get("output_path")), True
                    ),
                    tuple(
                        (item.get("id"), item.get("label"), item.get("kind"))
                        for item in run.get("results", [])
                    ),
                )
                for run in self._run_history
            ) if self._run_history_visible else (),
        )

    def _apply_active(self):
        """Move the active highlight without rebuilding anything."""
        active = self.app_data.active_tab
        if active == self._active:
            return
        for name in (self._active, active):
            entry = self._items.get(name)
            if entry is None:
                continue
            _sig, item, icon, _hint = entry
            is_active = name == active
            item.ui_active = is_active
            icon.ui_color = "primary" if is_active else "grey-7"
        self._active = active

    def select_by_index(self, n):
        """Select the nth item (1-based). Returns True if valid."""
        name = self._index_to_name.get(n)
        if name:
            self._on_select(name)
            return True
        return False

    # -- rendering ------------------------------------------------------
    def _ordered_groups(self):
        """Yield (label, icon, [(name, tab), ...]) in canonical order."""
        groups = {}
        for name, tab in self.app_data.get_tabs().items():
            groups.setdefault(tab.get("type", "unknown"), []).append((name, tab))

        for type_key, (label, icon) in _TYPE_GROUPS.items():
            if groups.get(type_key):
                yield label, icon, groups[type_key]
        for type_key, tab_list in groups.items():
            if type_key in _TYPE_GROUPS:
                continue
            yield type_key.title(), "mdi-help", tab_list

    def _render(self):
        # Assign keyboard number hints (1-9) across ALL tabs, in display order,
        # independent of filtering/collapse so the shortcuts stay stable.
        self._index_to_name = {}
        idx = 1
        numbers = {}
        for _label, _icon, tabs in self._ordered_groups():
            for tab_name, _tab in tabs:
                if idx <= 9:
                    numbers[tab_name] = idx
                    self._index_to_name[idx] = tab_name
                idx += 1

        active = self.app_data.active_tab
        self._active = active
        q = self._query
        children = []
        seen = set()
        for label, icon, tabs in self._ordered_groups():
            matches = [
                (n, t) for n, t in tabs
                if not q or q in t["title"].lower()
            ]
            if q and not matches:
                continue
            collapsed = self._collapsed.get(label, False)
            children.append(self._group_header(label, icon, len(tabs), collapsed))
            if collapsed:
                continue
            if not matches:
                children.append(Div("empty", ui_class=nav_empty))
                continue
            for tab_name, tab in matches:
                seen.add(tab_name)
                children.append(
                    self._item(tab_name, tab, tab_name == active,
                               numbers.get(tab_name), icon)
                )
        for stale in [n for n in self._items if n not in seen]:
            del self._items[stale]
        if self._run_history_visible:
            children.extend(self._render_run_history())
        children.append(self._spacer)
        self._scroll.ui_children = children

    def _render_run_history(self):
        children = [
            self._group_header(
                "Run history", "mdi-history", len(self._run_history),
                self._collapsed.get("Run history", False),
            )
        ]
        if self._collapsed.get("Run history", False):
            return children
        if not self._run_history:
            children.append(Div("No saved runs yet.", ui_class=nav_empty))
            return children

        query = self._query
        active_tab = self.app_data.get_tab(self.app_data.active_tab)
        active_component = active_tab.get("component") if active_tab else None
        active_path = getattr(active_component, "result_path", None)
        try:
            active_result_path = str(Path(active_path).resolve()).casefold()
        except (OSError, RuntimeError, TypeError, ValueError):
            active_result_path = None
        for run in reversed(self._run_history):
            run_id = str(run.get("id") or run.get("output_path") or run.get("name", "Run"))
            results = list(run.get("results", []))
            run_matches = not query or query in str(run.get("name", "")).lower()
            matching_results = [
                result for result in results
                if not query or query in str(result.get("label", "")).lower()
            ]
            if query and not run_matches and not matching_results:
                continue

            collapsed = self._run_collapsed.get(run_id, True) and not (
                query and matching_results and not run_matches
            )
            run_status = str(run.get("status", ""))
            icon = (
                "mdi-alert-outline" if "not converged" in run_status.casefold()
                else {
                    "Complete": "mdi-check-circle-outline",
                    "Nonconverged": "mdi-alert-outline",
                    "Failed": "mdi-alert-circle-outline",
                    "Cancelled": "mdi-stop-circle-outline",
                    "Interrupted": "mdi-progress-alert",
                }.get(run_status.title(), "mdi-progress-clock")
            )
            caret = "mdi-chevron-right" if collapsed else "mdi-chevron-down"
            run_label = str(run.get("name") or "Saved run")
            status = str(run.get("status") or "Unknown")
            header = QBtn(
                QIcon(ui_name=icon, ui_size="xs"),
                Div(
                    Div(run_label, ui_style="font-weight:600; overflow-wrap:anywhere;"),
                    Div(status, ui_style="font-size:10px; color:var(--fg-muted);"),
                    ui_style="min-width:0; flex:1; text-align:left; line-height:1.25;",
                ),
                QIcon(ui_name=caret, ui_size="16px"),
                ui_flat=True,
                ui_dense=True,
                ui_no_caps=True,
                ui_class="mmfem-run-tree-entry",
                ui_style=(
                    "display:flex; width:100%; min-width:0; min-height:38px; "
                    "height:auto; justify-content:flex-start; gap:8px; "
                    "padding:5px 7px; text-align:left;"
                ),
            )
            header._props["aria-label"] = run_label
            header.on_click(lambda _event=None, key=run_id: self._toggle_run(key))
            children.append(header)
            if collapsed:
                continue

            diagnostic_summary = run.get("diagnostic_summary")
            if diagnostic_summary:
                children.append(Div(
                    str(diagnostic_summary),
                    ui_style=(
                        "padding:4px 8px 6px 34px; font-size:10px; line-height:1.35; "
                        "color:var(--fg-muted); overflow-wrap:anywhere;"
                    ),
                ))

            visible_results = results if run_matches else matching_results
            if not visible_results:
                children.append(
                    Div(
                        "No exported fields or mesh found.",
                        ui_style="padding:4px 8px 7px 34px; font-size:11px; color:var(--fg-muted);",
                    )
                )
            for result in visible_results:
                path = result.get("path")
                if not path:
                    continue
                result_label = str(result.get("label") or Path(path).name)
                result_icon = (
                    "mdi-vector-triangle"
                    if result.get("kind") == "mesh"
                    else "mdi-function-variant"
                )
                try:
                    is_active_result = (
                        active_result_path is not None
                        and str(Path(path).resolve()).casefold() == active_result_path
                    )
                    if (
                        not is_active_result
                        and result.get("is_default")
                        and run.get("default_path")
                    ):
                        is_active_result = (
                            str(Path(run["default_path"]).resolve()).casefold()
                            == active_result_path
                        )
                except (OSError, RuntimeError, TypeError, ValueError):
                    is_active_result = False
                button = QBtn(
                    QIcon(ui_name=result_icon, ui_size="xs"),
                    Div(
                        result_label,
                        ui_style=(
                            "min-width:0; white-space:normal; overflow-wrap:anywhere; "
                            "word-break:normal; line-height:1.25; text-align:left;"
                        ),
                    ),
                    ui_color="primary" if is_active_result else None,
                    ui_flat=True,
                    ui_dense=True,
                    ui_no_caps=True,
                    ui_class="mmfem-run-result-entry",
                    ui_style=(
                        "display:flex; width:100%; min-width:0; min-height:30px; "
                        "height:auto; justify-content:flex-start; gap:8px; "
                        "padding:4px 8px 4px 29px; text-align:left;"
                    ),
                )
                button._props["aria-label"] = result_label
                if is_active_result:
                    button._props["aria-current"] = "true"
                if callable(self._on_open_run_result):
                    button.on_click(
                        lambda _event=None, file_path=str(path): self._on_open_run_result(file_path)
                    )
                else:
                    button.ui_disable = True
                children.append(button)
        return children

    def _group_header(self, label, icon, count, collapsed):
        caret_cls = str(nav_group_caret)
        if collapsed:
            caret_cls += " " + str(nav_group_caret_collapsed)
        entry = self._groups.get(label)
        if entry is None:
            caret = QIcon(ui_name="mdi-chevron-down", ui_size="16px")
            badge = Div(ui_class=nav_count_badge)
            row = Div(
                caret,
                QIcon(ui_name=icon, ui_size="15px"),
                Div(label, ui_class=nav_group_name),
                badge,
                ui_class=nav_group_row,
            )
            row.on("click", lambda e=None, l=label: self._toggle_group(l))
            self._groups[label] = entry = (row, caret, badge)
        row, caret, badge = entry
        caret.ui_class = caret_cls
        badge.ui_children = [str(count)]
        return row

    def _item(self, tab_name, tab, is_active, number, group_icon):
        sig = (tab["title"], tab.get("icon", group_icon))
        entry = self._items.get(tab_name)
        if entry is not None and entry[0] == sig:
            _sig, item, icon, hint = entry
            item.ui_active = is_active
            icon.ui_color = "primary" if is_active else "grey-7"
            hint.ui_children = [self._hint_text(number)]
            return item
        return self._build_item(tab_name, tab, is_active, number, group_icon, sig)

    @staticmethod
    def _hint_text(number):
        return str(number) if number else ""

    def _build_item(self, tab_name, tab, is_active, number, group_icon, sig):
        hint = Div(self._hint_text(number), ui_class=nav_number_hint)
        sections = []
        sections.append(QItemSection(hint, ui_side=True, ui_class=nav_side))
        icon = QIcon(
            ui_name=tab.get("icon", group_icon),
            ui_size="xs",
            ui_color="primary" if is_active else "grey-7",
        )
        sections.append(QItemSection(icon, ui_avatar=True, ui_class=avatar_min))
        sections.append(QItemSection(Div(tab["title"])))
        sections.append(self._build_context_menu(tab_name))

        item = QItem(
            *sections,
            ui_clickable=True,
            ui_active=is_active,
            ui_active_class=str(nav_item_active),
            ui_dense=True,
            ui_class=nav_item,
        )
        item.on_click(lambda e=None, n=tab_name: self._on_select(n))
        item.on("mousedown", lambda e, n=tab_name: self._on_middle_click(e, n))
        self._items[tab_name] = (sig, item, icon, hint)
        return item

    # -- context menu / actions -----------------------------------------
    def _build_context_menu(self, tab_name):
        delete_item = QItem(
            QItemSection(
                QIcon(ui_name="mdi-delete", ui_size="xs"),
                ui_avatar=True,
                ui_class=avatar_min,
            ),
            QItemSection("Delete"),
            ui_clickable=True,
            ui_dense=True,
        )
        delete_item.on_click(lambda e=None, n=tab_name: self._delete_tab(n))

        rename_input = QInput(
            ui_model_value=self.app_data.get_tab(tab_name)["title"],
            ui_dense=True,
            ui_autofocus=True,
        )
        rename_btn = QBtn(
            ui_label="OK", ui_color="primary", ui_flat=True, ui_dense=True
        )

        rename_dialog = QDialog(
            QCard(
                QCardSection("Rename"),
                QCardSection(rename_input),
                QCardActions(rename_btn, ui_align="right"),
            ),
        )
        rename_btn.on_click(
            lambda e=None, n=tab_name, inp=rename_input, dlg=rename_dialog: self._rename_tab(
                n, inp, dlg
            )
        )

        rename_item = QItem(
            QItemSection(
                QIcon(ui_name="mdi-pencil", ui_size="xs"),
                ui_avatar=True,
                ui_class=avatar_min,
            ),
            QItemSection("Rename"),
            rename_dialog,
            ui_clickable=True,
            ui_dense=True,
        )
        rename_item.on_click(
            lambda e=None, dlg=rename_dialog: setattr(dlg, "ui_model_value", True)
        )

        return QMenu(
            QList(rename_item, delete_item, ui_dense=True),
            ui_context_menu=True,
        )

    def _delete_tab(self, tab_name):
        self.app_data.delete_tab(tab_name)
        self.update()
        if self.app_data.active_tab:
            self._on_select(self.app_data.active_tab)

    def _rename_tab(self, tab_name, input_comp, dialog):
        new_title = input_comp.ui_model_value
        if new_title and new_title.strip():
            tab = self.app_data.get_tab(tab_name)
            if tab:
                tab["title"] = new_title.strip()
                self.update()
        dialog.ui_model_value = False

    def _on_middle_click(self, event, tab_name):
        if event.value.get("button") == 1:
            self._delete_tab(tab_name)
