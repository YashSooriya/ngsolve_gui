from pathlib import Path
from types import SimpleNamespace

from ngsolve_gui.app import _set_window_icon


class _Link:
    def __init__(self):
        self.removed = False

    def remove(self):
        self.removed = True


class _NodeList:
    def __init__(self, items):
        self._items = items
        self.length = len(items)

    def item(self, index):
        return self._items[index]


def test_window_icon_replaces_ngapp_favicons_with_lorentz_mark():
    old_links = [_Link(), _Link()]
    appended = []
    document = SimpleNamespace(
        querySelectorAll=lambda _selector: _NodeList(old_links),
        createElement=lambda _tag: SimpleNamespace(),
        head=SimpleNamespace(appendChild=appended.append),
    )

    _set_window_icon(SimpleNamespace(document=document), "data:image/png;base64,mmfem")

    assert all(link.removed for link in old_links)
    assert len(appended) == 1
    icon = appended[0]
    assert icon.rel == "icon"
    assert icon.type == "image/png"
    assert icon.href == "data:image/png;base64,mmfem"


def test_lorentz_mark_and_all_ico_sizes_have_transparent_corners():
    from PIL import Image

    assets = Path(__file__).parents[1] / "src" / "ngsolve_gui" / "assets"
    png_path = assets / "lorentz_mark_128x128.png"
    ico_path = assets / "lorentz_icon.ico"

    with Image.open(png_path) as icon:
        rgba = icon.convert("RGBA")
        assert rgba.size == (128, 128)
        assert all(rgba.getpixel(point)[3] <= 4 for point in ((0, 0), (127, 0), (0, 127), (127, 127)))
        assert rgba.getpixel((64, 64))[3] == 255

    with Image.open(ico_path) as icon:
        sizes = icon.ico.sizes()
        assert {(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)} <= sizes
        for size in sizes:
            rgba = icon.ico.getimage(size).convert("RGBA")
            corners = ((0, 0), (rgba.width - 1, 0), (0, rgba.height - 1), (rgba.width - 1, rgba.height - 1))
            assert all(rgba.getpixel(point)[3] <= 8 for point in corners), (rgba.size, corners)
