import unittest

from ngsolve_gui.camera_pointer_focus import patch_camera_clipping, patch_engine_source


class PointerFocusCameraPatchTests(unittest.TestCase):
    def test_patches_zoom_and_rotation_to_use_pointer_focus(self):
        source = """  _onPointerDown(ev) {\n    this._moved = false;\n    // ctrl/alt are reserved for the host and never start a camera gesture.\n    const hostModified = ev.ctrlKey || ev.altKey;\n    if (!hostModified && ev.button === 0 && !ev.shiftKey) {\n      this._isRotating = true;\n    }\n  }\n  _onPointerMove(ev) {\n      t.rotate(s * ev.movementY, s * ev.movementX);\n  }\n  _onWheel(ev) {\n    t.scale(1 - ev.deltaY / 1000, t._center);\n  }\n"""
        patched, ok = patch_engine_source(source)
        self.assertTrue(ok)
        self.assertIn("_focusPoint(ev.clientX, ev.clientY)", patched)
        self.assertIn("this._rotationCenter || t._center", patched)
        self.assertIn("focus || t._center", patched)

    def test_patch_is_idempotent(self):
        source = """  _onPointerDown(ev) {\n    this._moved = false;\n    // ctrl/alt are reserved for the host and never start a camera gesture.\n    const hostModified = ev.ctrlKey || ev.altKey;\n    if (!hostModified && ev.button === 0 && !ev.shiftKey) {\n      this._isRotating = true;\n    }\n  }\n  _onPointerMove(ev) {\n      t.rotate(s * ev.movementY, s * ev.movementX);\n  }\n  _onWheel(ev) {\n    t.scale(1 - ev.deltaY / 1000, t._center);\n  }\n"""
        patched, ok = patch_engine_source(source)
        self.assertTrue(ok)
        again, ok_again = patch_engine_source(patched)
        self.assertTrue(ok_again)
        self.assertEqual(again, patched)


    def test_adapts_near_clip_for_zoom_while_preserving_fit_view(self):
        source = """class Camera {
  constructor() {
    this.transform = new Transform();
    this.orthographic = false;
    this._observers = [];
  }
  reset(pmin, pmax) {
    this.transform.init(pmin, pmax);
    this._notify();
  }
  updateUniforms(width, height) {
    const near = 0.1, far = 10, fov = 45, focal = 3;
  }
}
"""
        patched, ok = patch_camera_clipping(source)
        self.assertTrue(ok)
        self.assertIn("this._fitScale = this.transform._currentScale();", patched)
        self.assertIn("const zoomRatio = this.transform._currentScale()", patched)
        self.assertIn("Math.max(0.001, Math.min(0.1, 3 - 1.02 * zoomRatio))", patched)
        self.assertIn("const far = 10, fov = 45, focal = 3;", patched)
        again, ok_again = patch_camera_clipping(patched)
        self.assertTrue(ok_again)
        self.assertEqual(again, patched)

    def test_unknown_camera_layout_is_left_unchanged(self):
        source = "unchanged camera source"
        patched, ok = patch_camera_clipping(source)
        self.assertFalse(ok)
        self.assertEqual(patched, source)

    def test_unknown_upstream_input_layout_is_left_unchanged(self):
        source = "unchanged source"
        patched, ok = patch_engine_source(source)
        self.assertFalse(ok)
        self.assertEqual(patched, source)


if __name__ == "__main__":
    unittest.main()
