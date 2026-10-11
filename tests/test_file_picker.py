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
