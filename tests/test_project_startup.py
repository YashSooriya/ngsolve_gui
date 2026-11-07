from ngsolve_gui.app import NGSolveGui
from ngsolve_gui.axisymmetric_model import new_model, new_studies, serialize_model_json
from ngsolve_gui.three_d_project import new_3d_project, package_3d_project


def test_project_detection_uses_validated_file_contents():
    axisymmetric = serialize_model_json(
        new_model(),
        new_studies(),
        {"schema_version": 1, "active_section": "geometry", "camera": "fit"},
    )
    three_d = package_3d_project(new_3d_project("Shield"), {})

    axis_kind, axis_data, axis_assets = NGSolveGui._decode_project_file(axisymmetric)
    three_d_kind, three_d_data, three_d_assets = NGSolveGui._decode_project_file(three_d)

    assert axis_kind == "axisymmetric"
    assert axis_data[0]["schema"] == "ngsolve-gui.axisymmetric"
    assert axis_assets is None
    assert three_d_kind == "3d"
    assert three_d_data["name"] == "Shield"
    assert three_d_assets == {}


def test_invalid_file_is_rejected_as_an_unsupported_project():
    try:
        NGSolveGui._decode_project_file(b"not a project")
    except ValueError as error:
        assert "not a supported LORENTZ project" in str(error)
    else:
        raise AssertionError("Invalid project data was accepted.")


def test_workspaces_are_created_only_when_requested(monkeypatch):
    import ngsolve_gui.solve_workspace as solve_module
    import ngsolve_gui.three_d_workspace as three_d_module

    class Container:
        ui_children = []
        ui_hidden = True

    created = []

    class FakeSolveWorkspace:
        def __init__(self, **_kwargs):
            created.append("axisymmetric")

    class FakeThreeDWorkspace:
        def __init__(self, _app):
            created.append("3d")

    monkeypatch.setattr(solve_module, "SolveWorkspace", FakeSolveWorkspace)
    monkeypatch.setattr(three_d_module, "ThreeDWorkspace", FakeThreeDWorkspace)
    app = NGSolveGui.__new__(NGSolveGui)
    app._solve_workspace_instance = None
    app._solve_workspace = Container()
    app._three_d_workspace_instance = None
    app._three_d_workspace_container = Container()
    app._three_d_project_dialog = None
    app._refresh_solve_history_controls()

    assert created == []
    assert app.solve_workspace is app.solve_workspace
    assert created == ["axisymmetric"]
    assert len(app._solve_workspace.ui_children) == 1
    assert app._three_d_workspace is app._three_d_workspace
    assert created == ["axisymmetric", "3d"]
    assert len(app._three_d_workspace_container.ui_children) == 1


def test_recent_project_saves_absolute_path_name_and_kind(tmp_path):
    project_file = tmp_path / "Shield.json"
    project_file.write_text("{}", encoding="utf-8")
    settings_data = {}

    class Settings:
        def get(self, key, default=None):
            return settings_data.get(key, default)

        def set(self, key, value):
            settings_data[key] = value

    class RecentView:
        def __init__(self):
            self.ui_children = []

    app = type("RecentApp", (), {})()
    app.usersettings = Settings()
    app._startup_recent_list = RecentView()
    app._project_path_key = NGSolveGui._project_path_key
    app._recent_project_entries = lambda: NGSolveGui._recent_project_entries(app)
    app._refresh_recent_projects_view = lambda: None

    NGSolveGui._remember_recent_project(
        app, project_file, kind="axisymmetric", name="Shield model"
    )

    assert settings_data["recent_projects"] == [{
        "name": "Shield model",
        "path": str(project_file.resolve()),
        "kind": "axisymmetric",
    }]


def test_recent_project_view_can_render_after_relaunch(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ngapp.components import Component, Div

    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    project_file = tmp_path / "Shield.mmfem3d"
    project_file.write_bytes(b"sample")
    app = SimpleNamespace(
        _startup_recent_list=Div(),
        _recent_project_entries=lambda: [{"name":"Shield", "path":str(project_file)}],
        _open_recent_project=lambda *_args: None,
        _locate_recent_project=lambda *_args: None,
        _remove_recent_project=lambda *_args: None,
    )
    NGSolveGui._refresh_recent_projects_view(app)
    assert len(app._startup_recent_list.ui_children) == 1


def test_recent_project_tile_selects_and_opens_the_matching_project(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace
    from ngapp.components import Component, Div
    import ngsolve_gui.app as app_module

    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        app_module,
        "cached_thumbnail",
        lambda _path: "data:image/png;base64,iVBORw0KGgo=",
    )
    project_path = tmp_path / "Long engineering shield assembly.mmfem3d"
    project_path.write_bytes(b"project payload")
    opened = []
    selected = []
    app = SimpleNamespace(
        _startup_recent_list=Div(),
        _startup_selected_recent_path=None,
        _project_path_key=NGSolveGui._project_path_key,
        _recent_project_entries=lambda: [{
            "name": "Shield assembly",
            "path": str(project_path),
            "kind": "3d",
        }],
        _open_recent_project=lambda entry: opened.append(dict(entry)),
        _locate_recent_project=lambda entry: None,
        _remove_recent_project=lambda path: None,
    )
    app._refresh_recent_projects_view = lambda: NGSolveGui._refresh_recent_projects_view(app)
    app._select_recent_project = lambda path: (
        selected.append(path), NGSolveGui._select_recent_project(app, path)
    )

    NGSolveGui._refresh_recent_projects_view(app)

    card = app._startup_recent_list.ui_children[0]
    tile = card.ui_slots["default"][0]
    original_tile = tile
    preview, filename, _status = tile.ui_slots["default"]
    assert preview._props.get("src") == "data:image/png;base64,iVBORw0KGgo="
    assert filename.ui_slots["default"] == [project_path.name]
    assert tile._props["role"] == "button"
    assert tile._props["aria-label"] == f"Open {project_path.name}"
    assert "text-overflow:ellipsis" in filename._props["style"]

    tile._callbacks["click"][0](SimpleNamespace())
    assert app._startup_recent_list.ui_children[0].ui_slots["default"][0] is original_tile
    tile._callbacks["dblclick"][0](SimpleNamespace())

    assert selected == [str(project_path)]
    assert opened == [{
        "name": "Shield assembly",
        "path": str(project_path),
        "kind": "3d",
    }]
    assert "is-selected" in app._startup_recent_list.ui_children[0].ui_slots[
        "default"
    ][0]._props["class"]


def test_missing_recent_project_tile_offers_locate_action(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ngapp.components import Component, Div

    monkeypatch.setattr(Component, "_update_frontend", lambda *_args, **_kwargs: None)
    missing_path = tmp_path / "Moved model.json"
    located = []
    app = SimpleNamespace(
        _startup_recent_list=Div(),
        _recent_project_entries=lambda: [{
            "name": "Moved model",
            "path": str(missing_path),
            "kind": "axisymmetric",
        }],
        _open_recent_project=lambda _entry: None,
        _locate_recent_project=lambda entry: located.append(entry["path"]),
        _remove_recent_project=lambda _path: None,
        _select_recent_project=lambda _path: None,
    )

    NGSolveGui._refresh_recent_projects_view(app)

    card = app._startup_recent_list.ui_children[0]
    tile = card.ui_slots["default"][0]
    tile._callbacks["dblclick"][0](SimpleNamespace())

    assert located == [str(missing_path)]
    assert card.ui_slots["default"][0].ui_slots["default"][2].ui_hidden is False
