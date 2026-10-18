import numpy as np

from ngsolve_gui.meshing_preview import (
    _camera_view_center,
    _sync_camera_view_center,
)


def _affine(linear, translation):
    matrix = np.eye(4)
    matrix[:3, :3] = linear
    matrix[:3, 3] = translation
    return matrix


def test_camera_view_center_tracks_pan_from_the_fitted_domain_center():
    angle = np.deg2rad(31)
    rotation = np.array(
        [
            [np.cos(angle), -np.sin(angle), 0.0],
            [np.sin(angle), np.cos(angle), 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    linear = 1.7 * rotation
    domain_center = np.array([4.0, -2.0, 7.5])
    fitted = _affine(linear, -linear @ domain_center)

    np.testing.assert_allclose(_camera_view_center(fitted), domain_center)

    # A screen-space pan changes the camera-space translation while leaving
    # the domain itself unchanged. The new target is the point now at view 0.
    pan = np.array([0.8, -0.35, 0.0])
    panned = fitted.copy()
    panned[:3, 3] += pan
    expected_view_center = domain_center - np.linalg.solve(linear, pan)

    np.testing.assert_allclose(
        _camera_view_center(panned), expected_view_center, atol=1e-12
    )
    np.testing.assert_allclose(
        panned[:3, :3] @ expected_view_center + panned[:3, 3],
        np.zeros(3),
        atol=1e-12,
    )


def test_sync_updates_python_and_browser_pivots_to_current_view_center():
    class Transform:
        pass

    class Camera:
        pass

    class Scene:
        pass

    center_before_pan = np.array([2.0, 3.0, -1.0])
    linear = np.diag([2.0, 2.0, 2.0])
    matrix = _affine(linear, -linear @ center_before_pan + [1.0, 0.0, 0.0])

    scene = Scene()
    scene.options = type("Options", (), {})()
    scene.options.camera = Camera()
    scene.options.camera.transform = Transform()
    scene.options.camera.transform._mat = matrix
    scene.options.camera.transform._center = center_before_pan.copy()

    scene._js_engine = type("Engine", (), {})()
    scene._js_engine.camera = Camera()
    scene._js_engine.camera.transform = Transform()

    expected = _camera_view_center(matrix)
    actual = _sync_camera_view_center(scene)

    np.testing.assert_allclose(actual, expected)
    np.testing.assert_allclose(scene.options.camera.transform._center, expected)
    np.testing.assert_allclose(scene._js_engine.camera.transform._center, expected)
