from types import SimpleNamespace

import pytest

from ngsolve_gui.app import NGSolveGui, WorkspaceModeToggle


def test_workspace_mode_toggle_updates_selection_and_notifies(monkeypatch):
    selected = []
    toggle = WorkspaceModeToggle("post_process", on_change=selected.append)
    for button in toggle._buttons.values():
        monkeypatch.setattr(button, "_update_frontend", lambda payload: None)

    assert toggle.value == "post_process"
    assert [button.ui_children[0] for button in toggle.ui_children] == [
        "Solve", "Post Process"
    ]
    assert all(
        "font-size:15px" in button.ui_style for button in toggle._buttons.values()
    )
    assert "width:174px" in toggle.ui_style
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
        # These stand in for the fixed controls in the single app-bar row.
        _brand=brand,
        _file_group=files,
        _view_group=actions,
        system_monitor=monitor,
        _system_monitor_separator=separator,
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


def test_opening_a_run_result_keeps_the_mode_toggle_in_sync(monkeypatch):
    loaded = []
    app = SimpleNamespace(
        _workspace_mode="solve",
        _post_process_workspace=SimpleNamespace(ui_hidden=True),
        _solve_workspace=SimpleNamespace(ui_hidden=False),
        _load_with_status=loaded.append,
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
        _notify=lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("ngsolve_gui.app.package_model", lambda *args: b"problem")

    result = NGSolveGui.save_problem_file(app)

    assert result is save_succeeds
    assert saved == ([True] if save_succeeds else [])
