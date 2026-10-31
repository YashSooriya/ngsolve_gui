from pathlib import Path


def test_product_identity_uses_package_version_and_available_documentation():
    from ngsolve_gui import __version__
    from ngsolve_gui.appconfig import config
    from ngsolve_gui.branding import DOCUMENTATION_URL, PRODUCT_NAME

    assert PRODUCT_NAME == config.name == "MagMechSim"
    assert config.version == __version__
    assert DOCUMENTATION_URL.endswith("/docs/user-guide.md")
    assert DOCUMENTATION_URL.startswith("https://")
    assert (Path(__file__).parents[1] / "docs" / "user-guide.md").is_file()


def test_official_brand_assets_are_packaged_without_old_product_logos():
    assets = Path(__file__).parents[1] / "src" / "ngsolve_gui" / "assets"
    expected = {
        "magmechsim_about_page_banner.png",
        "magmechsim_full_logo_light.png",
        "magmechsim_full_logo_dark.png",
        "magmechsim_icon.ico",
        "magmechsim_icon_128x128.png",
    }
    assert expected <= {path.name for path in assets.iterdir()}
    assert not (assets / "mm-fem-logo.png").exists()
    assert not (assets / "mm-fem-app-icon.png").exists()
    frontend_brand_assets = (
        "magmechsim_icon_128x128.png",
        "magmechsim_full_logo_light.png",
        "magmechsim_about_page_banner.png",
    )
    # Keep the logo change from adding a large image payload to initial UI load.
    assert sum((assets / name).stat().st_size for name in frontend_brand_assets) < 500 * 1024


def test_rebranded_app_keeps_existing_user_settings_namespace(monkeypatch):
    from types import SimpleNamespace

    from ngsolve_gui import app as app_module
    from ngsolve_gui.app import NGSolveGui

    monkeypatch.setattr(
        app_module,
        "UserSettings",
        lambda app_id: SimpleNamespace(app_id=app_id),
    )
    app = NGSolveGui.__new__(NGSolveGui)

    assert app.usersettings.app_id == "NGSolve GUI"
