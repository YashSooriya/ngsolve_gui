"""Pointer-centred zoom and orbit behavior for the bundled WebGPU camera."""

_PATCH_MARKER = "  // MM-FEM: map the pointer to a view-parallel focal plane."
_CLIPPING_PATCH_MARKER = "    // MM-FEM: adapt the near plane to deep zoom."


def patch_engine_source(source):
    """Patch WebGPU's in-browser input handler without forking its package."""
    if _PATCH_MARKER in source:
        return source, True

    focus_method = """  // MM-FEM: map the pointer to a view-parallel focal plane.
  _focusPoint(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const ndcX = 2 * (clientX - rect.left) / rect.width - 1;
    const ndcY = 1 - 2 * (clientY - rect.top) / rect.height;
    const transform = this.camera.transform;
    const centerView = transform.mapPoint(transform._center);
    const viewDepth = 3 - centerView[2];
    const tanHalfFov = Math.tan(45 * Math.PI / 360);
    const aspect = rect.width / rect.height;
    if (!Number.isFinite(viewDepth) || (!this.camera.orthographic && viewDepth <= 0.1)) return null;
    const x = this.camera.orthographic
      ? ndcX * aspect * 3 * tanHalfFov
      : ndcX * aspect * viewDepth * tanHalfFov;
    const y = this.camera.orthographic
      ? ndcY * 3 * tanHalfFov
      : ndcY * viewDepth * tanHalfFov;
    const inverse = mat4Inv(transform._mat);
    const z = centerView[2];
    const w = inverse[12] * x + inverse[13] * y + inverse[14] * z + inverse[15];
    if (!Number.isFinite(w) || Math.abs(w) < 1e-12) return null;
    const point = [
      (inverse[0] * x + inverse[1] * y + inverse[2] * z + inverse[3]) / w,
      (inverse[4] * x + inverse[5] * y + inverse[6] * z + inverse[7]) / w,
      (inverse[8] * x + inverse[9] * y + inverse[10] * z + inverse[11]) / w,
    ];
    return point.every(Number.isFinite) ? point : null;
  }

  _onPointerDown(ev) {"""

    replacements = (
        (
            "  _onPointerDown(ev) {",
            focus_method,
        ),
        (
            "    this._moved = false;\n    // ctrl/alt are reserved for the host and never start a camera gesture.",
            "    this._moved = false;\n    this._rotationCenter = null;\n    // ctrl/alt are reserved for the host and never start a camera gesture.",
        ),
        (
            "    if (!hostModified && ev.button === 0 && !ev.shiftKey) {\n      this._isRotating = true;",
            "    if (!hostModified && ev.button === 0 && !ev.shiftKey) {\n      this._rotationCenter = this._focusPoint(ev.clientX, ev.clientY);\n      this._isRotating = true;",
        ),
        (
            "      t.rotate(s * ev.movementY, s * ev.movementX);",
            "      t.rotate(s * ev.movementY, s * ev.movementX, this._rotationCenter || t._center);",
        ),
        (
            "    t.scale(1 - ev.deltaY / 1000, t._center);",
            "    const focus = this._focusPoint(ev.clientX, ev.clientY);\n    t.scale(1 - ev.deltaY / 1000, focus || t._center);",
        ),
    )
    patched = source
    for old, new in replacements:
        if patched.count(old) != 1:
            return source, False
        patched = patched.replace(old, new, 1)
    return patched, True


def patch_camera_clipping(source):
    """Tighten the perspective near plane as the user zooms into a scene."""
    if _CLIPPING_PATCH_MARKER in source:
        return source, True

    replacements = (
        (
            "    this.transform = new Transform();\n    this.orthographic = false;",
            "    this.transform = new Transform();\n    this._fitScale = 1;\n    this.orthographic = false;",
        ),
        (
            "    this.transform.init(pmin, pmax);\n    this._notify();",
            "    this.transform.init(pmin, pmax);\n    this._fitScale = this.transform._currentScale();\n    this._notify();",
        ),
        (
            "    const near = 0.1, far = 10, fov = 45, focal = 3;",
            "    // MM-FEM: adapt the near plane to deep zoom.\n"
            "    const zoomRatio = this.transform._currentScale() / Math.max(1e-12, this._fitScale || 1);\n"
            "    const near = Math.max(0.001, Math.min(0.1, 3 - 1.02 * zoomRatio));\n"
            "    const far = 10, fov = 45, focal = 3;",
        ),
    )
    patched = source
    for old, new in replacements:
        if patched.count(old) != 1:
            return source, False
        patched = patched.replace(old, new, 1)
    return patched, True


def install_webgpu_pointer_focus():
    """Install camera interaction and deep-zoom clipping patches in WebGPU."""
    try:
        import webgpu.engine as engine
    except Exception as error:  # pragma: no cover - environment-specific fallback
        print(f"Pointer-focused camera controls unavailable: {error}")
        return False
    patched, ok = patch_engine_source(engine.engine_js)
    if not ok:
        print("Pointer-focused camera controls unavailable: WebGPU input code changed.")
        return False
    patched, clipping_ok = patch_camera_clipping(patched)
    if not clipping_ok:
        print("Adaptive camera clipping unavailable: WebGPU camera code changed.")
    engine.engine_js = patched
    return clipping_ok
