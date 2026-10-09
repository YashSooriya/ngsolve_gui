from types import SimpleNamespace

import pytest

from ngsolve_gui.app import NGSolveGui, WorkspaceModeToggle


def test_workspace_mode_toggle_updates_selection_and_notifies(monkeypatch):
    selected = []
    toggle = WorkspaceModeToggle(on_change=selected.append)
    for button in toggle._buttons.values():
        monkeypatch.setattr(button, "_update_frontend", lambda payload: None)

    assert toggle.value == "post_process"
    assert [button.ui_children[0] for button in toggle.ui_children] == [
        "Solve", "Post Process"
    ]
    assert "background:var(--accent-subtle)" in toggle._buttons["post_process"].ui_style
    assert "background:var(--surface)" in toggle._buttons["solve"].ui_style

    toggle._buttons["solve"]._callbacks["click"][0](None)

    assert toggle.value == "solve"
    assert selected == ["solve"]
    assert "background:var(--accent-subtle)" in toggle._buttons["solve"].ui_style
    assert "background:var(--surface)" in toggle._buttons["post_process"].ui_style


def test_workspace_mode_switch_changes_workspace_and_keeps_header_visible():
    post_process = SimpleNamespace(ui_hidden=False)
    solve = SimpleNamespace(ui_hidden=True)
    brand, files, actions, monitor, separator, mode_bar = [
        SimpleNamespace(ui_hidden=False) for _ in range(6)
    ]
    header_components = [brand, files, actions, monitor, separator, mode_bar]
    app = SimpleNamespace(
        _post_process_workspace=post_process,
        _solve_workspace=solve,
        # These stand in for fixed app-bar controls and the mode row beneath it.
        _brand=brand,
        _file_group=files,
        _view_group=actions,
        system_monitor=monitor,
        _system_monitor_separator=separator,
        _workspace_mode_bar=mode_bar,
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


def test_workspace_mode_rejects_unknown_mode():
    with pytest.raises(ValueError, match="Unknown workspace mode"):
        WorkspaceModeToggle("solver")

    app = SimpleNamespace()
    with pytest.raises(ValueError, match="Unknown workspace mode"):
        NGSolveGui._set_workspace_mode(app, "solver")
