from types import SimpleNamespace

import numpy as np

from ngsolve_gui.webgpu_tab import WebgpuTab
from webgpu.camera import Camera


def _tab_with_camera(camera):
    tab = WebgpuTab.__new__(WebgpuTab)
    scene = SimpleNamespace(
        options=SimpleNamespace(camera=camera),
        render=lambda: None,
    )
    tab.wgpu = SimpleNamespace(scene=scene)
    tab._camera_views_pop = SimpleNamespace(ui_hidden=False)
    return tab


def _set_camera_state(camera, center, scale):
    angle = np.deg2rad(23)
    rotation = np.array(
        [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    linear = scale * rotation
    camera.transform._center = np.asarray(center, dtype=float)
    camera.transform._mat = np.eye(4)
    camera.transform._mat[:3, :3] = linear
    camera.transform._mat[:3, 3] = -linear @ center


def _assert_target_centered_and_scale_preserved(camera, center, scale):
    transform = camera.transform
    np.testing.assert_allclose(
        transform._mat[:3, :3] @ center + transform._mat[:3, 3],
        np.zeros(3),
        atol=1e-12,
    )
    np.testing.assert_allclose(np.linalg.norm(transform._mat[:3, 0]), scale)


def test_camera_presets_preserve_current_view_center_and_zoom():
    camera = Camera()
    center = np.array([7.0, -2.0, 1.25])
    scale = 1.8
    _set_camera_state(camera, center, scale)
    tab = _tab_with_camera(camera)

    for view in ("top", "bottom", "front", "back", "right", "left", "isometric"):
        tab.set_camera_view(view)
        _assert_target_centered_and_scale_preserved(camera, center, scale)
        assert tab._camera_views_pop.ui_hidden


def test_unknown_camera_preset_is_rejected():
    tab = _tab_with_camera(Camera())

    try:
        tab.set_camera_view("diagonal")
    except ValueError as exc:
        assert "diagonal" in str(exc)
    else:
        raise AssertionError("unknown camera preset should raise ValueError")
