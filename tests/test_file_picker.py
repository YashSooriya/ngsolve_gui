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
