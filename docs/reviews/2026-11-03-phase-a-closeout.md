# Phase A Closeout and 3D Development Readiness

**Date:** 3 November 2026  
**Outcome:** Phase A verification is complete. Phase B has not started.  
**Full evidence:** `MagMechSim-PhaseA-Closeout-2026-11-03.zip`

## Summary

The packaged Windows application was tested through the independent desktop harness on the full 1920 × 1094 desktop. The app opened maximized. The saved axisymmetric project showed both sketch regions at its first captured geometry-editor frame, the grid filled the available drawing pane at maximized and restored window sizes, and the geometry-creation 3D preview rendered successfully. The 3D STEP model imported, meshed, accepted repeated native boundary picks, and recovered its saved model and selection after a controlled renderer crash.

The historical Chromium out-of-memory failure did not recur during three bounded interaction cycles on a mesh capped at 25,000 volume elements. This does not establish stability for larger meshes or longer sessions. The issue remains a monitored risk; it does not block proceeding with bounded Phase B preprocessing work.

No solver algorithms, numerical dependencies, project formats, or Mike-variant files were changed. No solver simulation was run.

## Changes

- Kept the replacement Chromium window eligible for focus through the complete recovery handshake, including while Windows still reports its temporary `ngapp` title.
- Reinstalled the `cerbsim_ngapp_style` stylesheet, keybinding styles, app-level styles, MagMechSim title and icon after renderer replacement. These styles had been injected only into the discarded document. The resulting missing flex rules had collapsed the replacement 3D workspace to zero height.
- Extended the recovery test to record layout boxes, overlays, loaded stylesheets, resource errors, JavaScript errors and unhandled rejections. The pass condition now requires the design-system flex root and a nonzero 3D viewport containing the geometry traces.

The failure diagnosis was reproduced before the fix: the recovered page had `.cb-app-root { display: block }` and a 0 px viewport height, with no JavaScript or resource errors. After the fix, `.cb-app-root` returned to `display:flex`; the maximized recovered viewport measured 1368 × 801 CSS pixels and contained the 3D canvas and both geometry traces. The native screenshot confirms the mesh is visible.

## Verification evidence

### Axisymmetric workspace

- The first geometry-editor screenshot showed the saved square air region and circular coil region immediately. The earlier single blank capture did not reproduce; its cause is not established, so it is not claimed as a confirmed rendering fix.
- The maximized sketch SVG measured 1338 × 806 px with a matching `viewBox` and no horizontal or vertical blank margins. At a restored 1495 × 895 window, the SVG and `viewBox` measured 862 × 559 px, again with no meaningful margins and unchanged physical coordinate scale.
- The geometry-creation preview rendered both regions in Plotly 2.35.2 with a working WebGL context, survived navigation back to the sketch editor, and was visible in a full-desktop screenshot.

### 3D workflow and repeated interactions

- The packaged app imported `magnet_shield_4k.step` and generated a bounded mesh of 22,132 volume elements and 14,588 surface triangles. No solver was launched.
- Three native interaction cycles included nine face clicks, clear-selection actions, mesh/geometry display switches, model rotation and property-editor scrolling. Two distinct boundary regions were selected and saved.
- Across seven measured click-to-editor updates, p50 was 444 ms and p95 was 646 ms. The overall native click response median was 658 ms.
- Chromium process-tree RSS rose by 99 MB from the first sample, with a 206 MB peak increase; page JavaScript heap rose by 27 MB at the final sample. DOM node count and Plotly trace count stayed stable. GPU memory was not measurable through this harness, so the numbers are a bounded trend check, not proof that no native/GPU leak exists.

### Renderer recovery

- The harness deliberately crashed only the isolated packaged-app renderer through CDP. The Python GUI/solver host stayed alive, exactly one automatic recovery was attempted, and the new renderer connected and mounted in 16.5 seconds.
- The project, saved boundary selection and maximized window were retained. The recovered viewport measured 1368 × 801 px, contained two geometry traces and rendered the mesh in the desktop screenshot.
- Test cleanup reported no remaining processes owned by the isolated package session.

### Test and build results

- Packaged-desktop harness: **25 checks passed**, 30 full-desktop screenshots captured. Native input used Win32 `SendInput`; screenshots used full-desktop Pillow `ImageGrab`; Playwright over test-only loopback CDP was used for read-only DOM inspection and timings.
- Focused renderer-recovery and desktop-test-mode unit tests: **14 passed, 4 subtests passed**. These native-package/unit checks are distinct from the packaged-desktop run.
- Splash transition was exercised from a fresh launcher start: the splash appeared in 0.41 s, the UI mounted in 5.85 s, the main window maximized in 7.10 s, and the splash dismissed in 7.12 s. The desktop harness also confirmed the main-window handoff completed 33 ms after maximizing.
- `Launch-Latest-MagMechSim.cmd` was smoke-tested after updating it to this package. It launched the matching `MagMechSim.exe` maximized on the 1920 × 1094 desktop; the latest app is left open at its project chooser for user review.
- Windows package: MagMechSim `0.2.8.dev216`, SHA-256 `c7c40e9bf975a8d3870ed6e082f271fdcbf8c2b5478d61bfdaca25e6f6cf5951`.
- GUI source revision: `296e850300598aaa9ccf34d603a19cb9f566032d`. Windows package/recovery source revision: `4b4b4525` (following focused commit `a68a6f78`). Both repositories were pushed with clean trees.
- Splash-to-ready measured 9.1 seconds for the axisymmetric launch and 7.3 seconds for 3D. The VM was moved to an SSD before these tests; there is no controlled before/after comparison, and these times are not attributed to code alone.

## Remaining risk and recommendation

The historical OOM was not reproduced with the 22k-element mesh and three interaction cycles. Larger meshes, prolonged selection sessions and physical-GPU memory use remain unverified. Keep mesh size bounded during early Phase B and retain renderer/memory diagnostics in acceptance runs. This residual risk does not prevent starting the model-preparation work; it should be revisited if memory grows monotonically or a renderer crash occurs on representative workloads.

Phase B is ready for review and can begin after approval. The next coherent milestone is the supported 3D model-preparation workflow, starting with geometry hierarchy, materials and safe assignment validation. DC background-field configuration and coupled physics remain out of scope until that work is reviewed.
