# WebGPU capability detection and 3D WebGL fallback

**Date:** 4 November 2026  
**Scope:** Reliability follow-up after Phase E1. No solver equations, solver interfaces, numerical dependencies, project formats, or 2D field-viewer features were changed.

## Outcome

The application now checks whether WebGPU can render on the current browser/device before using the WebGPU viewport. If the interface or adapter is missing, device creation fails, the canvas triangle probe fails or is inconclusive, or adapter/device acquisition does not settle within five seconds, the viewport switches to a 3D Plotly/WebGL representation instead of invoking ngapp's `alert`/`sys.exit` error path. An important packaged-desktop edge case is covered: when `navigator.gpu` exists but `requestAdapter()` rejects or finds no adapter, the JavaScript promise is converted to a structured probe failure. The Python/WebSocket bridge otherwise reports the rejected promise only as a browser console error, leaving the viewport blank.

The shared renderer adapter is used by the geometry, mesh, and field result tabs. Geometry is tessellated from the existing OCC shape; meshes use their actual surface facets; field views sample the actual coefficient function on the displayed surface. Axisymmetric result fields use the existing revolved-gridfunction utility to remain 3D. Display data is bounded to 100,000 surface triangles. The fallback is display-only and clearly reports that WebGPU-only picking, clipping, and interior overlays are unavailable. The CPU-only 2D field viewer remains removed.

An already populated process-global GPU device is no longer treated as verified. The application records the identity of the device that passed its actual canvas probe and re-probes a different cached device before using it. The probe bridge uses a JavaScript property name supported by the Python/WebSocket proxy; double-underscore names are not supported by that proxy and previously caused fallback before the actual capability test could run.

## Architecture

- `AdaptiveWebgpuComponent` wraps ngapp's existing `WebgpuComponent`; all existing geometry/mesh/function scene construction remains intact when WebGPU passes.
- The probe reuses ngsolve_webgpu's adapter selection and triangle-render test. It does not introduce browser command-line flags or claim capability from `navigator.gpu` presence alone.
- A five-second Promise race bounds adapter/device calls that never settle. A late device from a timed-out request is destroyed if it eventually arrives.
- Rejected adapter-probe Promises return an `unavailable` result with a diagnostic instead of rejecting across the Python/WebSocket bridge. This was verified on the packaged VM, where the WebGPU API exists but both requested power preferences return no adapter.
- `WebgpuTab` owns the renderer fallback overlay and suppresses WebGPU-only properties/interactions only after failure. Normal WebGPU operation retains its established controls.
- Plotly's WebGL renderer is already an application dependency. No solver/backend or dependency change was required.

## Source references

- `src/ngsolve_gui/webgl_fallback.py:48` installs the bounded and rejection-safe adapter probe; `:120` verifies the device; `:192` switches the existing WebGPU component to its owner fallback.
- `src/ngsolve_gui/webgpu_tab.py:309` activates the visible fallback state and its capability notice.
- `src/ngsolve_gui/webgl_fallback.py:364`, `:400`, and `:470` build geometry, mesh, and field surfaces.
- `tests/test_webgl_fallback.py` covers probe outcomes, actual field values, axisymmetric revolution, and browser E2E fallback paths.
- `docs/development-roadmap.md` remains the single living roadmap; this report is linked from its Phase E1 follow-up and risk register.

## Verification

The following tests used the packaged Python runtime and the package's bundled frontend assets. Browser-level Playwright E2E tests are not represented as native-desktop checks.

| Check | Result |
|---|---:|
| `tests/test_webgl_fallback.py` — tessellation, field values, axisymmetric revolution, probe failures, missing-WebGPU, missing-adapter and stalled-adapter E2E | **14 passed** |
| Same focused fallback suite with the installed package modules/frontend | **14 passed** |
| `tests/test_three_d_preview.py tests/test_axisymmetric_revolution.py tests/test_axisymmetric_geometry_preview_e2e.py` | **20 passed** |
| `py_compile` for fallback and test modules | **Passed** |
| `git diff --check` | **Passed** |
| `tests/test_geometry.py::test_geometry_box` on this browser | WebGPU scene attached and was screenshot-read successfully; the pre-existing pixel baseline comparison then failed because output shape `(650, 637, 3)` differs from baseline `(644, 798, 3)`. This is a baseline/viewport mismatch, not a renderer startup failure. No baseline or tolerance was changed. |

The fallback screenshots show an OCC cube surface and a computed scalar field on a 3D mesh surface. Browser E2E exercises missing `navigator.gpu`, a present interface with no adapter, and a `requestAdapter()` Promise that never resolves. All three reach the visible fallback.

## Packaged application and screenshots

The full Windows package was refreshed in place to `0.2.8.dev301`; `Build-info/build.json` identifies GUI commit `8521bc3fb20a94f0b2cce765ddc12532abafbc82`. The executable remains the launcher and `Launch-Latest-MagMechSim.cmd` points to this full MagMechSim installation; the separate Mike variant was not modified.

Genuine packaged-desktop verification used the actual maximized Windows app at `1920 × 1094` (CSS viewport `1920 × 1031`). Win32 mouse/keyboard input selected the 3D workspace, opened **File → Load solutions**, and used the native file picker to load a small, 12-element cube mesh with the computed scalar field `x + 2y + 3z`. Playwright attached only to the test-owned loopback Chromium session for DOM/readback. In the unforced automatic run, `navigator.gpu` existed but neither high-performance nor low-power adapter was available; the app displayed the diagnostic banner and rendered the field on a Plotly `mesh3d` with 9 surface samples and 12 triangles. A separate run explicitly removed `navigator.gpu` and produced the same 3D fallback. Both runs reported no page errors and no leftover test application/browser processes.

A fresh `Launch-Latest-MagMechSim.cmd` smoke reached the maximized home workspace in `12.156 s`, cleared the downloading overlay, and left no test-owned processes. This is a single VM observation, not a controlled performance comparison. The executable SHA-256 remains `f36976cb55f2d8f7375b0bbb990622d033047cfe2460c88a328f1b620c956308`; only committed GUI Python modules and package metadata were refreshed.

The review ZIP contains:

- the maximized full-desktop native launcher screenshot;
- maximized full-desktop screenshots of the packaged app's automatically detected no-adapter fallback and the forced no-WebGPU-interface fallback;
- the browser-level Plotly/WebGL geometry and field screenshots (1280 × 864 test viewport, clearly identified as browser captures);
- sanitized native-run results, launcher/test summaries and package build metadata;
- the relevant implementation and focused test sources.

## Limitations

- Plotly/WebGL must be available. If a browser disables both WebGPU and WebGL, this implementation cannot provide an interactive 3D viewport.
- The fallback represents geometry and mesh surfaces and evaluates fields on the exterior surface. It does not emulate interior WebGPU field renderers, GPU picking, clipping, or selection tools.
- The native results workspace screenshot shows a substantial unused white area below the current work row despite the maximized window. The fallback uses the full existing central viewport; this surrounding workspace-height issue is outside this renderer fix.
- Two optional font asset requests returned HTTP 404 in the packaged desktop session. The page reported no uncaught errors and the rendering/workflow succeeded; the font requests were not traced in this renderer-focused change.
- The WebGPU baseline test remains sensitive to viewport dimensions; the observed shape mismatch is preserved as a known test-baseline issue.

## Git and next action

GUI changes are committed and pushed to `origin/testing` (`df5fe77`, `947c745`, `8521bc3`). The package version, source revision, and executable identity are listed in `Build-info/build.json` and the review ZIP. No solver repository change was needed. The latest-full-app launcher still opens this package. Do not begin Phase E2 as part of this follow-up; review the fallback behavior first.
