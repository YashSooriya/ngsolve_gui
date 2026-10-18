from types import SimpleNamespace

import ngapp.utils
from ngsolve_gui.app import NGSolveGui


def test_local_app_can_use_browser_file_picker(monkeypatch):
    monkeypatch.setenv("NGSOLVE_GUI_FILE_PICKER", "browser")
    monkeypatch.setattr(
        ngapp.utils,
        "get_environment",
        lambda: SimpleNamespace(type=ngapp.utils.EnvironmentType.LOCAL_APP),
    )

    loaded = []
    gui = SimpleNamespace(
        _pick_file_to_temp=lambda: "/tmp/selected.vol",
        _load_with_status=loaded.append,
    )

    NGSolveGui._load_file(gui)

    assert loaded == ["/tmp/selected.vol"]


def test_browser_picker_omits_invalid_hyphenated_bundle_extension(monkeypatch):
    monkeypatch.setattr(
        ngapp.utils,
        "get_environment",
        lambda: SimpleNamespace(type="browser"),
    )
    options = []
    gui = SimpleNamespace(
        js=SimpleNamespace(showOpenFilePicker=lambda value: options.append(value) or [])
    )

    assert NGSolveGui._pick_artifact(
        gui,
        title="Load mesh",
        description="MM-FEM mesh bundle",
        extension=".mmfem-mesh",
    ) == (None, None)
    assert options == [{"multiple": False}]


def test_browser_picker_keeps_filter_for_valid_extension(monkeypatch):
    monkeypatch.setattr(
        ngapp.utils,
        "get_environment",
        lambda: SimpleNamespace(type="browser"),
    )
    options = []
    gui = SimpleNamespace(
        js=SimpleNamespace(showOpenFilePicker=lambda value: options.append(value) or [])
    )

    NGSolveGui._pick_artifact(
        gui,
        title="Load problem",
        description="MM-FEM problem file",
        extension=".ngsmodel",
    )

    assert options[0]["types"][0]["accept"]["application/zip"] == [".ngsmodel"]



def test_browser_file_picker_uses_extended_wait_only_during_selection(monkeypatch):
    import threading
    from ngsolve_gui import picker_wait

    picker_wait._install_picker_aware_wait()
    observed = []
    monkeypatch.setattr(
        picker_wait,
        "_original_event_wait",
        lambda event, timeout=None: observed.append(timeout) or True,
    )

    picker_wait._picker_aware_event_wait(threading.Event(), 120)
    with picker_wait.browser_file_picker_wait():
        picker_wait._picker_aware_event_wait(threading.Event(), 120)

    assert observed == [120, 1800]


def test_artifact_picker_enters_extended_wait_context(monkeypatch):
    import ngapp.utils
    from ngsolve_gui import picker_wait
    from ngsolve_gui.app import NGSolveGui

    monkeypatch.setattr(
        ngapp.utils,
        "get_environment",
        lambda: SimpleNamespace(type="browser"),
    )
    observed = []
    gui = SimpleNamespace(
        js=SimpleNamespace(
            showOpenFilePicker=lambda options: observed.append(
                getattr(picker_wait._picker_wait_state, "timeout", None)
            ) or []
        )
    )

    assert NGSolveGui._pick_artifact(
        gui,
        title="Load solution",
        description="Solution",
        extension=".pkl",
    ) == (None, None)
    assert observed == [1800]


def test_windows_native_open_dialog_dispatches_to_winforms(monkeypatch):
    from ngsolve_gui import native_dialog

    called = []
    monkeypatch.setattr(native_dialog.sys, "platform", "win32")
    monkeypatch.setattr(
        native_dialog,
        "_windows_open_dialog",
        lambda title, initialdir, filters: called.append(
            (title, initialdir, filters)
        ) or r"C:\results\field.pkl",
    )

    selected = native_dialog.open_file_dialog(
        title="Open field",
        initialdir=r"C:\results",
        filters=[("Pickle", "*.pkl")],
    )

    assert selected == r"C:\results\field.pkl"
    assert called == [
        ("Open field", r"C:\results", [("Pickle", "*.pkl")])
    ]
