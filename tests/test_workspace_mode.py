from types import SimpleNamespace

import pytest

from ngsolve_gui.app import NGSolveGui, WorkspaceModeToggle


def test_workspace_mode_toggle_updates_selection_and_notifies(monkeypatch):
    selected = []
    toggle = WorkspaceModeToggle("post_process", on_change=selected.append)
    for button in toggle._buttons.values():
        monkeypatch.setattr(button, "_update_frontend", lambda payload: None)

    assert toggle.value == "post_process"
    assert [button.ui_label for button in toggle.ui_children] == ["Model", "Results"]
    assert all(
        "font-size:13px" in button.ui_style for button in toggle._buttons.values()
    )
    assert "width:190px" in toggle.ui_style
    assert "background:var(--accent-subtle)" in toggle._buttons["post_process"].ui_style
    assert "background:var(--surface)" in toggle._buttons["solve"].ui_style

    toggle._buttons["solve"]._callbacks["click"][0](None)

    assert toggle.value == "solve"
    assert selected == ["solve"]
    assert "background:var(--accent-subtle)" in toggle._buttons["solve"].ui_style
    assert "background:var(--surface)" in toggle._buttons["post_process"].ui_style


def test_workspace_mode_toggle_defaults_to_solve():
    toggle = WorkspaceModeToggle()

    assert toggle.value == "solve"
    assert "background:var(--accent-subtle)" in toggle._buttons["solve"].ui_style
    assert "background:var(--surface)" in toggle._buttons["post_process"].ui_style


def test_workspace_mode_switch_changes_workspace_and_keeps_header_visible():
    post_process = SimpleNamespace(ui_hidden=False)
    solve = SimpleNamespace(ui_hidden=True)
    brand, files, actions, monitor, separator = [
        SimpleNamespace(ui_hidden=False) for _ in range(5)
    ]
    header_components = [brand, files, actions, monitor, separator]
    app = SimpleNamespace(
        _post_process_workspace=post_process,
        _solve_workspace=solve,
        navigator=SimpleNamespace(set_run_history_visible=lambda _visible: None),
        # These stand in for the fixed controls in the single app-bar row.
        _brand=brand,
        _file_group=files,
        _view_group=actions,
        system_monitor=monitor,
        _system_monitor_separator=separator,
        _set_application_menu_context=lambda _three_d=None: None,
    )

    NGSolveGui._set_workspace_mode(app, "solve")
    assert app._workspace_mode == "solve"
    assert post_process.ui_hidden
    assert not solve.ui_hidden
    assert all(not item.ui_hidden for item in header_components)

    NGSolveGui._set_workspace_mode(app, "post_process")
    assert app._workspace_mode == "post_process"
    assert not post_process.ui_hidden
    assert solve.ui_hidden
    assert all(not item.ui_hidden for item in header_components)


def test_three_d_workspace_replaces_axisymmetric_views_and_restores_active_mode():
    three_d = SimpleNamespace(ui_hidden=True)
    toggle = SimpleNamespace(ui_hidden=False)
    solve = SimpleNamespace(ui_hidden=False)
    post_process = SimpleNamespace(ui_hidden=True)
    app = SimpleNamespace(
        _three_d_workspace=three_d,
        _workspace_mode_toggle=toggle,
        _solve_workspace=solve,
        _post_process_workspace=post_process,
        _workspace_mode="post_process",
        _refresh_solve_history_controls=lambda: None,
    )

    NGSolveGui._set_3d_workspace_active(app, True)

    assert three_d.ui_hidden is True
    assert toggle.ui_hidden is False
    assert solve.ui_hidden is True
    assert post_process.ui_hidden is False

    NGSolveGui._set_3d_workspace_active(app, False)

    assert three_d.ui_hidden is True
    assert toggle.ui_hidden is False
    assert solve.ui_hidden is True
    assert post_process.ui_hidden is False


def test_3d_context_hides_axisymmetric_commands_and_restores_them():
    buttons = {
        name: SimpleNamespace(ui_hidden=False)
        for name in ("File", "Edit", "View", "Model", "Study", "Results", "Help")
    }
    items = {
        "File": {
            "Open project...": SimpleNamespace(ui_hidden=False),
            "Load mesh...": SimpleNamespace(ui_hidden=False),
            "Save project": SimpleNamespace(ui_hidden=False),
        },
        "Edit": {"Undo": SimpleNamespace(ui_hidden=False)},
        "View": {
            "Isometric": SimpleNamespace(ui_hidden=False),
            "Toggle navigation cube": SimpleNamespace(ui_hidden=False),
        },
        "Model": {
            "Import STEP model...": SimpleNamespace(ui_hidden=False),
            "Import wire traces...": SimpleNamespace(ui_hidden=False),
            "Sources & loads": SimpleNamespace(ui_hidden=False),
        },
        "Study": {
            "Check setup": SimpleNamespace(ui_hidden=False),
            "Run study": SimpleNamespace(ui_hidden=False),
        },
    }
    app = SimpleNamespace(
        _application_menu_buttons=buttons,
        _application_menu_items=items,
        _workspace_mode="solve",
        _active_project_type="3d",
    )

    NGSolveGui._set_application_menu_context(app, True)

    assert buttons["Model"].ui_hidden is False
    assert buttons["Study"].ui_hidden is False
    assert buttons["Results"].ui_hidden is False
    assert items["File"]["Open project..."].ui_hidden is False
    assert items["File"]["Load mesh..."].ui_hidden is True
    assert items["Edit"]["Undo"].ui_hidden is True
    assert items["View"]["Isometric"].ui_hidden is False
    assert items["View"]["Toggle navigation cube"].ui_hidden is False
    assert items["Model"]["Import STEP model..."].ui_hidden is False
    assert items["Model"]["Sources & loads"].ui_hidden is True

    app._active_project_type = "axisymmetric"
    NGSolveGui._set_application_menu_context(app, False)

    assert buttons["Model"].ui_hidden is False
    assert buttons["Study"].ui_hidden is False
    assert items["File"]["Load mesh..."].ui_hidden is False
    assert items["View"]["Isometric"].ui_hidden is True
    assert items["Model"]["Import STEP model..."].ui_hidden is True

    app._active_project_type = None
    NGSolveGui._set_application_menu_context(app, False)
    assert buttons["Model"].ui_hidden is True
    assert buttons["Study"].ui_hidden is True
    assert buttons["Results"].ui_hidden is True


def test_results_menu_remains_available_when_a_solution_is_open_without_a_project():
    results = SimpleNamespace(ui_hidden=True)
    app = SimpleNamespace(
        _application_menu_buttons={"Results": results},
        _application_menu_items={},
        _workspace_mode="post_process",
        _active_project_type=None,
        tab_panel=SimpleNamespace(comp=object()),
    )

    NGSolveGui._set_application_menu_context(app, False)

    assert results.ui_hidden is False


def test_new_problem_uses_the_active_3d_project_type():
    called = []

    def start_3d():
        called.append("3d")
        return True

    def start_axisymmetric():
        called.append("axisymmetric")
        return True

    app = SimpleNamespace(
        _active_project_type="3d",
        _start_3d_project_from_menu=start_3d,
        _start_new_problem=start_axisymmetric,
        _request_problem_replacement=lambda action: action(),
    )

    assert NGSolveGui.new_problem(app) is True
    assert called == ["3d"]


def test_discarding_axisymmetric_before_switching_clears_hidden_model():
    old_model = {"name": "Unsaved axisymmetric model"}
    saved = []
    called = []
    app = SimpleNamespace(
        _pending_problem_replacement=lambda: called.append("continue") or True,
        _pending_project_type="axisymmetric",
        _close_new_problem_dialog=lambda: None,
        solve_workspace=SimpleNamespace(
            set_model=lambda *args: saved.append(args),
        ),
        _loaded_mesh_path="stale.vol",
        _loaded_mesh_problem_sha256="stale-hash",
    )

    assert NGSolveGui._continue_problem_replacement(app, save_changes=False) is True

    assert called == ["continue"]
    assert saved[0][0]["name"] != old_model["name"]
    assert app._loaded_mesh_path is None
    assert app._loaded_mesh_problem_sha256 is None


def test_opening_a_run_result_keeps_the_mode_toggle_in_sync(monkeypatch):
    loaded = []
    app = SimpleNamespace(
        _workspace_mode="solve",
        _post_process_workspace=SimpleNamespace(ui_hidden=True),
        _solve_workspace=SimpleNamespace(ui_hidden=False),
        _load_with_status=loaded.append,
        _set_application_menu_context=lambda _three_d=None: None,
    )
    toggle = WorkspaceModeToggle(
        "solve", on_change=lambda mode: NGSolveGui._set_workspace_mode(app, mode)
    )
    for button in toggle._buttons.values():
        monkeypatch.setattr(button, "_update_frontend", lambda _payload: None)
    app._workspace_mode_toggle = toggle
    monkeypatch.setattr("ngsolve_gui.app.os.path.isfile", lambda _filename: True)

    NGSolveGui._open_run_result(app, "B_DC.pkl")

    assert toggle.value == "post_process"
    assert app._workspace_mode == "post_process"
    assert app._post_process_workspace.ui_hidden is False
    assert app._solve_workspace.ui_hidden is True
    assert loaded == ["B_DC.pkl"]


def test_workspace_mode_rejects_unknown_mode():
    with pytest.raises(ValueError, match="Unknown workspace mode"):
        WorkspaceModeToggle("solver")

    app = SimpleNamespace()
    with pytest.raises(ValueError, match="Unknown workspace mode"):
        NGSolveGui._set_workspace_mode(app, "solver")


@pytest.mark.parametrize("panel,visibility_attr", [
    ("navigator", "_nav_visible"),
    ("property", "_prop_visible"),
])
def test_3d_post_process_panel_shortcuts_use_shared_application_panels(
    panel, visibility_attr
):
    calls = []
    app = SimpleNamespace(
        _active_project_type="3d",
        _workspace_mode="post_process",
        _nav_visible=True,
        _prop_visible=True,
        usersettings=SimpleNamespace(set=lambda key, value: calls.append((key, value))),
        _three_d_workspace=SimpleNamespace(
            toggle_tree=lambda: calls.append(("3d", "tree")),
            toggle_properties=lambda: calls.append(("3d", "properties")),
        ),
        _apply_panel_visibility=lambda: calls.append(("apply", None)),
    )

    method = (
        NGSolveGui._toggle_navigator
        if panel == "navigator"
        else NGSolveGui._toggle_property_panel
    )
    method(app)

    assert getattr(app, visibility_attr) is False
    assert calls == [
        ("nav_visible" if panel == "navigator" else "prop_visible", False),
        ("apply", None),
    ]


@pytest.mark.parametrize("panel,expected", [
    ("navigator", ("3d", "tree")),
    ("property", ("3d", "properties")),
])
def test_3d_solve_panel_shortcuts_use_workspace_panels(panel, expected):
    calls = []
    app = SimpleNamespace(
        _active_project_type="3d",
        _workspace_mode="solve",
        _three_d_workspace=SimpleNamespace(
            toggle_tree=lambda: calls.append(("3d", "tree")),
            toggle_properties=lambda: calls.append(("3d", "properties")),
        ),
    )

    method = (
        NGSolveGui._toggle_navigator
        if panel == "navigator"
        else NGSolveGui._toggle_property_panel
    )
    method(app)

    assert calls == [expected]


def test_new_problem_prompts_when_model_has_unsaved_changes():
    dialog = SimpleNamespace(ui_model_value=False)
    app = SimpleNamespace(
        _solver_job_active=False,
        solve_workspace=SimpleNamespace(dirty=True, _job_active=False),
        _new_problem_dialog=dialog,
    )

    assert NGSolveGui.new_problem(app) is False
    assert dialog.ui_model_value is True


def test_new_problem_starts_immediately_when_model_is_clean():
    started = []
    app = SimpleNamespace(
        _solver_job_active=False,
        solve_workspace=SimpleNamespace(dirty=False, _job_active=False),
        _start_new_problem=lambda: started.append(True) or True,
    )

    assert NGSolveGui.new_problem(app) is True
    assert started == [True]


def test_dismissing_dirty_project_prompt_cancels_pending_replacement():
    called = []
    app = SimpleNamespace(
        _pending_problem_replacement=lambda: called.append("replaced"),
        _pending_project_type="axisymmetric",
    )

    NGSolveGui._on_new_problem_dialog_model_value(
        app, SimpleNamespace(value=False)
    )

    assert app._pending_problem_replacement is None
    assert app._pending_project_type is None
    assert called == []


def test_cancelled_problem_save_keeps_current_problem_and_reopens_prompt():
    dialog = SimpleNamespace(ui_model_value=True)
    reset = []
    app = SimpleNamespace(
        _new_problem_dialog=dialog,
        _close_new_problem_dialog=lambda: setattr(dialog, "ui_model_value", False),
        save_problem_file=lambda: False,
        _start_new_problem=lambda: reset.append(True) or True,
    )

    assert NGSolveGui._save_and_start_new_problem(app) is False
    assert dialog.ui_model_value is True
    assert reset == []


def test_start_new_problem_resets_model_and_loaded_mesh():
    calls = []
    dialog = SimpleNamespace(ui_model_value=True)
    workspace = SimpleNamespace(
        _job_active=False,
        set_model=lambda *args: calls.append(args),
    )
    mode_selections = []
    app = SimpleNamespace(
        _solver_job_active=False,
        _new_problem_dialog=dialog,
        _close_new_problem_dialog=lambda: setattr(dialog, "ui_model_value", False),
        solve_workspace=workspace,
        _loaded_mesh_path="old.vol",
        _loaded_mesh_problem_sha256="old-hash",
        _workspace_mode="post_process",
        _workspace_mode_toggle=SimpleNamespace(select=mode_selections.append),
        _notify=lambda *args, **kwargs: None,
    )

    assert NGSolveGui._start_new_problem(app) is True
    model, studies, layout = calls[0]
    assert model["geometry"]["regions"] == []
    assert studies["studies"]
    assert layout["active_section"] == "geometry"
    assert app._loaded_mesh_path is None
    assert app._loaded_mesh_problem_sha256 is None
    assert mode_selections == ["solve"]
    assert dialog.ui_model_value is False


@pytest.mark.parametrize("save_succeeds", [True, False])
def test_problem_save_reports_success_and_marks_only_written_state(monkeypatch, save_succeeds):
    saved = []
    messages = []
    workspace = SimpleNamespace(
        model={"name": "Solenoid"},
        studies={"studies": []},
        layout={"active_section": "geometry"},
        validation_errors=lambda: [],
        mark_saved=lambda: saved.append(True),
        _message=lambda *args, **kwargs: messages.append((args, kwargs)),
    )
    app = SimpleNamespace(
        solve_workspace=workspace,
        _save_artifact=lambda data, filename: save_succeeds,
        _save_project_artifact=lambda data, filename, *, kind, save_as=False: save_succeeds,
        _notify=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("ngsolve_gui.app.serialize_model_json", lambda *args: b"problem")

    result = NGSolveGui.save_problem_file(app)

    assert result is save_succeeds
    assert saved == ([True] if save_succeeds else [])
