# MagMechSim packaged desktop verification

**Date:** 2 November 2026  
**Result:** Passed for the bounded packaged desktop checks listed below  
**Application:** MagMechSim `0.2.8.dev216`  
**Display:** 1920 × 1094 full desktop; app maximized after startup  
**VM note:** The VM had moved to an SSD. Timings are baselines for this configuration, not controlled comparisons with earlier storage or hardware.

## Purpose and test boundaries

This milestone establishes a repeatable Windows desktop test path that works independently of Codex Computer Use. It adds test-only, loopback DevTools access to the MagMechSim package and uses Win32 input against the real packaged executable. It does not bypass Computer Use security checks, alter the Mike variant, change the numerical solver, or add application features.

The test uses Win32 APIs for pointer movement/clicks, keyboard entry, wheel scrolling, window bounds and maximized-state checks. Pillow `ImageGrab` captures the complete Windows desktop. Playwright attaches through the test-only loopback CDP port for read-only DOM inspection and performance timing; it does not synthesize the user actions. Each test run uses a unique workspace and Chromium profile. Cleanup confirmed no package-owned processes remained. The screenshots use the full 1920 × 1094 desktop; unrelated windows were minimized before the final evidence run. Task Manager remained visible behind the transient splash.

The development-browser Playwright test and the packaged-desktop test are separate evidence types:

- **Browser-level E2E:** runs the axisymmetric geometry preview against the development server. It verifies app UI behavior in Chromium but does not exercise the Windows executable, native splash, Win32 input or full-desktop rendering.
- **Packaged desktop:** launches the actual `MagMechSim.exe` package and validates native windows, desktop input and screenshots. DOM access through Playwright is observational only.

## Verified workflow

| Area | Method | Result |
|---|---|---|
| Splash and maximized handoff | Two fresh packaged launches; Win32 window enumeration and full-desktop captures | Both branded splashes appeared and transitioned to the UI. Both windows were maximized on the 1920 × 1094 desktop. Launcher-to-ready took 7.945 s for the axisymmetric project and 6.779 s for the 3D project; the maximize-to-splash-dismiss interval was 0.028 s and 0.017 s. |
| Recent-project thumbnails | Inspect actual project chooser `<img>` payloads and pixel content; capture desktop | Both saved geometry previews were real 288 × 176 PNGs with substantial non-background geometry: axisymmetric 33,766 geometry pixels / 472 colors; STEP 17,195 geometry pixels / 770 colors. Native double-click opened both projects. |
| Axisymmetric geometry creation | Open the geometry-creation 3D preview by native click; inspect WebGL context and screenshot; native Exit click | The packaged app rendered two region traces in the preview, with a live WebGL context and visible geometry in the full-resolution screenshot. Exit returned to the sketch editor. No axisymmetric solver was run. |
| 3D STEP rendering and mesh | Open seeded STEP project by native double-click; click Generate mesh through Win32 | STEP geometry rendered in a 1368 × 801 CSS viewport at 2× canvas resolution in 4.094 s. A coarse 100 mm mesh completed in 13.641 s: 22,132 volume elements and 14,588 surface triangles. The harness enforces a 25,000-volume-element cap. |
| Native CAD face selection | Move and click the real Windows pointer over the rendered mesh; inspect selected-region summary | A native click selected Boundary 4 and the property editor reported 968 surface triangles. This confirms one actual desktop pick in the tested VM and mesh. |
| Property editor and persistence | Win32 wheel input, text entry and Ctrl+S; inspect saved project archive | Native wheel input changed property-editor scroll position from 0 to 194. The selected boundary was named, saved and found in the project archive with its CAD tag and boundary ID. |
| Process cleanup | Inspect processes owned by the isolated package workspace after both runs | No package-owned application processes remained; per-run controller sessions were removed. |

The packaged test passed all **17 checks** and produced **10 screenshots**. It did not run a 3D solver or an intentionally large mesh. The bounded mesh is for viewport interaction coverage only.

## Rendering and reliability interpretation

The axisymmetric geometry-creation preview is visible in the installed package using the existing Plotly WebGL renderer. The report observed a WebGL context that was available and not lost. The presence of `navigator.gpu` alone is not evidence of an available WebGPU adapter, and this preview check does not verify the physical-GPU path.

The “Getting started - coupled coil” axisymmetric project opened, and its 3D preview rendered, but its 2D sketch SVG contained zero sketch-region elements and appeared blank in the desktop screenshot despite the UI reporting two regions. This test therefore does **not** claim that the example project’s editable 2D sketch is correct. That display case remains open for investigation.

Historical Chrome renderer OOM and failed automatic recovery remain unresolved. The current 22,132-element mesh and single native face pick completed without reproducing those failures. This is not sufficient evidence to close repeated-interaction memory and recovery work in Phase A.

## Test results

| Test | Scope | Result |
|---|---|---|
| `tests.test_desktop_test_mode` | Test-mode instrumentation and native input structure | 4 passed |
| `tests/test_project_thumbnails.py tests/test_project_startup.py` | GUI unit coverage for project previews/startup | 11 passed |
| `tests/test_axisymmetric_geometry_preview_e2e.py` | Development-browser Playwright E2E | 1 passed in 24.44 s |
| `tests/test_packaged_desktop.py` | Actual packaged Windows executable | Passed; 17 checks; 10 screenshots |

The full GUI suite was not rerun for this test-harness milestone. The installed-package run is the native evidence; browser-level results are not presented as a substitute.

## Source and package references

- Harness: `ngsolve-ui/coupled_solver/release/native_windows/tests/test_packaged_desktop.py`
- Opt-in test instrumentation: `ngsolve-ui/coupled_solver/release/native_windows/desktop.py`, `_desktop_test_browser_arguments`
- Usage and security scope: `ngsolve-ui/coupled_solver/release/native_windows/README.md`
- Instrumentation tests: `ngsolve-ui/tests/test_desktop_test_mode.py`
- Test-only instrumentation and harness commit: `ngsolve-ui` `3480166ae192b48882d5812441f311a3158b2a1e` (`testing` branch)
- Browser E2E: `ngsolve_gui/tests/test_axisymmetric_geometry_preview_e2e.py`
- Living plan: `ngsolve_gui/docs/development-roadmap.md`
- Packaged executable SHA-256: `c83a1b7e8b319ead59743b58f066fe7cdfa5802405f3487baf3a1ea2477c0207`
- App source revisions in that package: GUI `e9085543d003f1ccfa09b576a078688fbe83b5b0`; solver/release `46d4ecaf45c484cb14aca7c578a202d1b6712f6b`. The packaged `desktop.py` includes the test-only instrumentation from `ngsolve-ui` commit `3480166ae192b48882d5812441f311a3158b2a1e`; it is disabled unless the packaged test explicitly sets `MMFEM_DESKTOP_TESTING=1`. No dependencies were installed for the harness; it uses the package's bundled Python, Playwright and Pillow.
- Launch path: `C:\Users\Yash\Documents\MM-FEM\Development\Launch-Latest-MagMechSim.cmd`
- Review ZIP: `C:\Users\Yash\Documents\MM-FEM\Handoffs\MagMechSim-Desktop-Automation-2026-11-02.zip`

## Recommendations

Keep the desktop harness as a milestone gate for splash, maximization, project thumbnails, native selection, persistence, rendering timing and process cleanup. Next, investigate the blank axisymmetric example sketch, reproduce or bound the historical 3D renderer OOM, and test repeated native mesh picks plus renderer recovery. Do not start the next physics-development milestone until those Phase A findings are reviewed.
