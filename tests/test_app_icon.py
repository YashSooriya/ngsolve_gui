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


def test_window_icon_replaces_ngapp_favicons_with_mm_fem_icon():
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
