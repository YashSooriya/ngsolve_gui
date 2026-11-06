# LORENTZ Startup Handoff and Brand Presentation Follow-up

**Date:** 6 November 2026  
**Status:** Complete for the tested Windows VM  
**Packaged application:** `0.2.8.dev329`  
**Executable SHA-256:** `3c124b60a45f181ac2889437b363a6008bbda5b85abfb22875131b43f4662d10`  
**Public launcher:** `C:\Users\Yash\Documents\MM-FEM\Development\Launch-Latest-LORENTZ.cmd`  
**Review package:** `LORENTZ-Brand-Refresh-Review.zip`

## Outcome

The startup warning shown in the user’s screenshot was caused by treating a Windows foreground-activation refusal as proof that the application had not launched correctly. Windows may decline `SetForegroundWindow` for a visible, maximized application; this is not a rendering failure. The startup handoff now treats foreground activation as best-effort and gates splash dismissal on stronger evidence: a visible, non-minimized, maximized LORENTZ window with native desktop pixels showing that its client area has painted.

The main window is raised above the splash before the native-pixel check. When the first painted frame is confirmed, the splash is withdrawn and destroyed, and the temporary topmost state is released. The exact error wording from the screenshot is no longer present in active native startup code. A genuine failure to locate or paint the app still produces a diagnostic startup failure; the change does not conceal those conditions.

The centered native splash also now visibly animates only the LORENTZ wordmark. A gentle warm-white sweep moves across the dimmed wordmark at 20 fps on a 3.6-second cycle; the rest of the splash remains static. The dim baseline preserves the transparent logo and gives the light sweep visible contrast.

## Actual packaged-app verification

The current public launcher was run in a fresh isolated workspace and local-app-data profile on the maximized 1920×1094 Windows desktop. The native test harness sampled full-desktop pixels, used the package’s loopback CDP endpoint for DOM and timing readback, and cleaned up only processes owned by its isolated profile. Existing user-profile app processes were preserved.

| Check | Result |
|---|---|
| Centered splash | Passed; splash bounds were `[700,332,1220,722]`, exactly centered in the 1920×1054 work area |
| Wordmark light sweep | Passed; six native desktop frames captured; 2,913 changed pixels in the strongest 240 ms interval |
| First main-window observation | Maximized; bounds `[-8,-8,1928,1062]` |
| Native first paint | Passed; 7,192 visible client pixels against an 800-pixel minimum |
| Main window over splash | Passed; raised before first-paint verification |
| Splash dismissal | Passed; 0.039 s after the confirmed native paint; splash destroyed and temporary topmost state released |
| Home workspace | Passed; title `LORENTZ`, loading overlay absent, maximized home visible |
| Isolated process cleanup | Passed; no test-profile or package processes remained |
| Public launcher | Passed; `Launch-Latest-LORENTZ.cmd` opened the current v6 package |

The measured launcher-to-home time was 23.5 seconds in this run. Backend readiness was logged at 13.269 s, frontend mount at 17.078 s, and native first paint at 20.587 s. This was one run on a loaded SSD-hosted VM and is not a controlled startup benchmark or evidence that storage or the code alone caused the measured time.

## Tests

- Native desktop startup unit tests: **21 passed**.
- Isolated package-process cleanup regression: **1 passed**.
- Fresh packaged public-launcher smoke: **passed**; animation, native paint, z-order, splash dismissal and latest launcher target were verified.
- `git diff --check`: passed before commit.
- No simulation or solver change was made.

The unit tests explicitly model denied foreground activation while confirming that a visible, maximized, painted window can pass startup. The native packaged run itself foregrounded successfully; the denied-foreground case is covered by the Windows API test double rather than naturally reproduced in the final packaged run.

## Branding and workspace presentation baseline

The preceding packaged brand review remains the source for full application screenshots of transparent light/dark logo artwork, dark application surfaces, the light-by-default axisymmetric sketch canvas, the optional “match app” sketch theme, the dark 3D workspace, and the wordmark centered against the full application bar. The follow-up ZIP retains the previous brand-review ZIP under `baseline/` and adds current dev329 startup evidence separately. The older dev328 manifest and screenshots are clearly labeled as baseline evidence rather than the current build.

The dev329 screenshot set contains the maximized current home screen, centered splash capture, six full-desktop animation frames, and a cropped contact sheet for reviewing the sweep. All app screenshots were taken at the full 1920×1094 desktop size. The package excludes a desktop capture containing unrelated foreground applications.

## Architecture and scope

The fix is confined to the native Windows launch/paint handoff, splash presentation, and focused desktop-test harness. UI branding assets and GUI theme behavior are unchanged from the accepted brand refresh. Project formats, solver algorithms, numerical settings, dependencies, and the shared solver’s independent operation are unchanged. The current public launcher points to the rebuilt full LORENTZ application, not the separate retired Mike variant.

## Git and readiness

Native Windows package commits `bc526931` (`Make LORENTZ startup handoff resilient`) and `acc23317` (`Isolate LORENTZ launcher smoke cleanup`) are pushed to the native repository’s `testing` branch. The GUI roadmap and this report are committed separately to the GUI repository’s `testing` branch. Exact Git revisions, executable hash, launcher target, tests and screenshots are recorded in the review ZIP.

**Recommendation:** Ready for independent review of this startup and branding follow-up for the tested Windows VM. Keep the paint-based launch gate and the isolated-profile public-launcher smoke. Other Windows graphics/driver configurations and a naturally denied foreground activation remain untested. Do not begin Phase G until this package is reviewed.
