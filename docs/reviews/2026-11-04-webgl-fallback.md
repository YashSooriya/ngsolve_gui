# WebGPU capability detection and 3D WebGL fallback

**Date:** 4 November 2026  
**Scope:** Reliability follow-up after Phase E1. No solver equations, solver interfaces, numerical dependencies, project formats, or 2D field-viewer features were changed.

## Outcome

The application now checks whether WebGPU can render on the current browser/device before using the WebGPU viewport. If the interface or adapter is missing, device creation fails, the canvas triangle probe fails or is inconclusive, or adapter/device acquisition does not settle within five seconds, the viewport switches to a 3D Plotly/WebGL representation instead of invoking ngapp's `alert`/`sys.exit` error path.

The shared renderer adapter is used by the geometry, mesh, and field result tabs. Geometry is tessellated from the existing OCC shape; meshes use their actual surface facets; field views sample the actual coefficient function on the displayed surface. Axisymmetric result fields use the existing revolved-gridfunction utility to remain 3D. Display data is bounded to 100,000 surface triangles. The fallback is display-only and clearly reports that WebGPU-only picking, clipping, and interior overlays are unavailable. The CPU-only 2D field viewer remains removed.

An already populated process-global GPU device is no longer treated as verified. The application records the identity of the device that passed its actual canvas probe and re-probes a different cached device before using it. The probe bridge uses a JavaScript property name supported by the Python/WebSocket proxy; double-underscore names are not supported by that proxy and previously caused fallback before the actual capability test could run.

## Architecture

- `AdaptiveWebgpuComponent` wraps ngapp's existing `WebgpuComponent`; all existing geometry/mesh/function scene construction remains intact when WebGPU passes.
- The probe reuses ngsolve_webgpu's adapter selection and triangle-render test. It does not introduce browser command-line flags or claim capability from `navigator.gpu` presence alone.
- A five-second Promise race bounds adapter/device calls that never settle. A late device from a timed-out request is destroyed if it eventually arrives.
- `WebgpuTab` owns the renderer fallback overlay and suppresses WebGPU-only properties/interactions only after failure. Normal WebGPU operation retains its established controls.
- Plotly's WebGL renderer is already an application dependency. No solver/backend or dependency change was required.

## Verification

The following tests used the packaged Python runtime and the package's bundled frontend assets. Browser-level Playwright E2E tests are not represented as native-desktop checks.

| Check | Result |
|---|---:|
| `tests/test_webgl_fallback.py` — tessellation, field values, axisymmetric revolution, device-probe cases, missing-WebGPU E2E, stalled-adapter E2E | **13 passed** |
| `tests/test_three_d_preview.py tests/test_axisymmetric_revolution.py tests/test_axisymmetric_geometry_preview_e2e.py` | **20 passed** |
| `py_compile` for fallback and test modules | **Passed** |
| `tests/test_geometry.py::test_geometry_box` on this browser | WebGPU scene attached and was screenshot-read successfully; the pre-existing pixel baseline comparison then failed because output shape `(650, 637, 3)` differs from baseline `(644, 798, 3)`. This is a baseline/viewport mismatch, not a renderer startup failure. No baseline or tolerance was changed. |

The fallback screenshots show an OCC cube surface and a computed scalar field on a 3D mesh surface. The test suite also simulates a browser with no `navigator.gpu` and one whose `requestAdapter()` Promise never resolves; both reach the visible fallback.

## Packaged application and screenshots

The full Windows package was refreshed in place and its `Build-info` identifies the committed GUI source. `Launch-Latest-MagMechSim.cmd` continues to point to this full MagMechSim installation; the separate Mike variant was not modified. A fresh launcher smoke is recorded separately from the browser E2E result and verifies the maximized native app window and mounted workspace.

The review ZIP contains:

- the maximized full-desktop native launcher screenshot;
- the browser-level Plotly/WebGL geometry and field screenshots (1280 × 864 test viewport, clearly identified as browser captures);
- sanitized launcher/test summaries and package build metadata;
- the relevant implementation and focused test sources.

## Limitations

- Plotly/WebGL must be available. If a browser disables both WebGPU and WebGL, this implementation cannot provide an interactive 3D viewport.
- The fallback represents geometry and mesh surfaces and evaluates fields on the exterior surface. It does not emulate interior WebGPU field renderers, GPU picking, clipping, or selection tools.
- The fallback view itself was validated through real browser E2E in the packaged runtime. The native launcher/splash/maximization path was separately checked, but the desktop harness does not currently force WebGPU unavailable, so a full-desktop screenshot of the fallback inside the native executable is not claimed here.
- The WebGPU baseline test remains sensitive to viewport dimensions; the observed shape mismatch is preserved as a known test-baseline issue.

## Git and next action

GUI changes are committed and pushed to `origin/testing`; the source revision and packaged runtime revision are listed in `Build-info/build.json` and the review ZIP. No solver repository change was needed. Do not begin Phase E2 as part of this follow-up; review the fallback behavior first.
