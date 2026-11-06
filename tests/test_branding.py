from pathlib import Path


def test_product_identity_uses_package_version_and_local_documentation(monkeypatch, tmp_path):
    from ngsolve_gui import __version__
    from ngsolve_gui.appconfig import config
    from ngsolve_gui.branding import PRODUCT_NAME
    from ngsolve_gui.documentation import find_documentation_index

    assert PRODUCT_NAME == config.name == "Lorentz"
    assert config.version == __version__
    monkeypatch.delenv("MMFEM_DOCUMENTATION_DIR", raising=False)
    monkeypatch.delenv("COUPLED_SOLVER_ROOT", raising=False)
    assert find_documentation_index() is None
    docs = tmp_path / "offline-docs"
    docs.mkdir()
    index = docs / "index.html"
    index.write_text("<html>offline</html>", encoding="utf-8")
    monkeypatch.setenv("MMFEM_DOCUMENTATION_DIR", str(docs))
    assert find_documentation_index() == index


def test_official_brand_assets_are_packaged_without_old_product_logos():
    assets = Path(__file__).parents[1] / "src" / "ngsolve_gui" / "assets"
    expected = {
        "lorentz_about_light.png",
        "lorentz_about_dark.png",
        "lorentz_home_light.png",
        "lorentz_wordmark_light.png",
        "lorentz_wordmark_dark.png",
        "lorentz_icon.ico",
        "lorentz_mark_128x128.png",
        "lorentz_splash_dark_static_2x.png",
        "lorentz_splash_dark_illumination_mask_2x.png",
        "lorentz_splash_dark.png",
    }
    assert expected <= {path.name for path in assets.iterdir()}
    obsolete_brand_assets = {
        "magmechsim_about_page_banner.png",
        "magmechsim_full_logo_dark.png",
        "magmechsim_full_logo_light.png",
        "magmechsim_icon.ico",
        "magmechsim_icon_128x128.png",
        "cerbsim-logo-full-dark.png",
        "cerbsim-logo-full-white.png",
        "cerbsim-logo-mark-dark.png",
        "cerbsim-logo-mark-white.png",
        "logo_withname_retina.png",
        "ngsolve-mark.png",
    }
    assert not obsolete_brand_assets.intersection(path.name for path in assets.iterdir())
    assert not (assets / "mm-fem-logo.png").exists()
    assert not (assets / "mm-fem-app-icon.png").exists()
    frontend_brand_assets = (
        "lorentz_mark_128x128.png",
        "lorentz_wordmark_light.png",
        "lorentz_wordmark_dark.png",
        "lorentz_home_light.png",
        "lorentz_about_light.png",
        "lorentz_about_dark.png",
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


def test_lorentz_palette_sets_the_brand_tokens_on_the_frontend_document():
    from types import SimpleNamespace

    from ngsolve_gui.app import _inject_lorentz_brand_styles
    from ngsolve_gui.branding import (
        BRAND_COPPER,
        BRAND_DARK_BACKGROUND,
        BRAND_NAVY,
        BRAND_SOFT_WHITE,
    )

    style = SimpleNamespace()
    variables = {}
    root = SimpleNamespace(
        style=SimpleNamespace(setProperty=lambda name, value: variables.update({name: value}))
    )
    appended = []
    document = SimpleNamespace(
        createElement=lambda _tag: style,
        getElementById=lambda _id: None,
        head=SimpleNamespace(appendChild=appended.append),
        documentElement=root,
    )

    _inject_lorentz_brand_styles(SimpleNamespace(document=document))

    assert appended == [style]
    assert style.id == "lorentz-brand-styles"
    assert ':root[data-theme="dark"]' in style.textContent
    assert "lorentz-wordmark-dark" in style.textContent
    assert ".lorentz-about-banner-dark .q-img__image" in style.textContent
    assert ".lorentz-wordmark-dark .q-img__loading" in style.textContent
    assert "a.powered-by { display: none !important; }" in style.textContent
    assert variables == {
        "--lorentz-copper": BRAND_COPPER,
        "--lorentz-navy": BRAND_NAVY,
        "--lorentz-soft-white": BRAND_SOFT_WHITE,
        "--lorentz-dark": BRAND_DARK_BACKGROUND,
    }
