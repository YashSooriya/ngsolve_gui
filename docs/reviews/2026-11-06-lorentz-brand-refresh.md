# LORENTZ brand and theme closeout

**Date:** 6 November 2026  
**Status:** Complete for the reviewed scope  
**Packaged application:** `0.2.8.dev328`  
**Executable SHA-256:** `46068c2bd2ae3c4d12e344aacaa726bcb239a12186d397fbf32658814a0b6722`  
**Review package:** `LORENTZ-Brand-Refresh-Review.zip`

## Changes

- Centralized user-visible product identity as `LORENTZ`; updated the GUI, About view, application title, splash, Windows launcher metadata, and native messages. Compatibility file paths and lower-case asset identifiers remain intact.
- Replaced the home and About artwork with transparent light/dark variants. The header wordmark now aligns to the full 50 px application bar. The LORENTZ application icon and animated wordmark-only splash use the supplied brand assets.
- Applied dark mode to the application shell, home surface, About dialog, and 3D workspace. The axisymmetric sketch canvas remains light by default; Preferences can set it to follow the application theme.
- Kept the change within presentation and GUI configuration. Project formats, solver algorithms, dependencies, and numerical behavior did not change.

## Architecture

The GUI owns its shared product name and brand tokens in `ngsolve_gui.branding`; the app shell chooses light/dark artwork from the existing theme and keeps the axisymmetric canvas preference independent from the shell theme. The native Windows launcher owns executable identity and splash presentation. No solver code or project schema was changed. The `Launch-Latest-LORENTZ.cmd` script targets the current full package; the prior `Launch-Latest-MagMechSim.cmd` name forwards to it for compatibility.

## Verification

- GUI branding, icon, file-picker and project-startup tests: **23 passed**, one benign pytest assertion-rewrite warning for the already imported `ngapp.e2e_webgpu` plugin.
- Native startup and renderer-recovery tests: **28 passed**.
- Packaged public-launcher smoke: **passed**. The final run took 19.97 s to the mounted home screen on the 1920 × 1094 desktop. The centered splash handoff completed, the first native frame was detected, the splash was dismissed, the startup overlay cleared, and the mounted native window title was exactly `LORENTZ`. The launcher smoke now rejects mixed-case product titles. The test-owned application/profile processes were cleaned up.
- Packaged UI interaction: **passed**. Evidence shows theme-aware home and About surfaces, transparent artwork, a rendered 3D STEP model, native model-tree selection feedback, and the axisymmetric square/circle sketch. Dark application mode retained the light sketch by default; changing Preferences to “Match app” darkened the sketch, and setting it back restored the light canvas. The test ran at maximized 1920 × 1094. No mesh generation or simulation was run for this brand closeout.
- The published executable hash matches `Build-info/build.json`; the latest public launcher points to that package.

## Startup observation

Startup is variable across these runs: the final public-launcher smoke reached home in **19.97 seconds** (backend ready at 7.8 s; first visible native frame at 18.3 s), one earlier launcher smoke took 26.3 s, and separate packaged UI launches reached a visible frame around 11.5–12.0 s. These are not controlled benchmarks; machine load and warm/cold state differ. Each successful run confirms the splash stays centered and the maximized main window replaces it only after a native frame is visible. Do not attribute timing differences to code or storage without a controlled comparison; the VM is on an SSD. A retry initially stopped before splash because the runtime temporary extraction exhausted available space. Once that failed process cleaned up, the next fresh public-launcher run passed with 6.15 GB free afterward.

A diagnostic full-desktop capture also contained a mixed-case `Lorentz` title from an already-running earlier package under the normal user Chrome profile. That window was outside the isolated v6 packaged-test profile, so the capture was excluded from the review evidence. The isolated v6 package's own native window and the final launcher screenshot show `LORENTZ`. An app window that was already open from the earlier package keeps its old caption until it is closed and relaunched.

## Evidence in the review ZIP

The ZIP contains full-desktop screenshots at 1920 × 1094 for the light and dark home, dark About dialog, light/dark 3D STEP workspace, native CAD tree selection, dark app with light axisymmetric sketch, and the optional dark sketch setting. It also includes the maximized public-launcher screenshot, startup JSON/log with exact centered splash bounds and native handoff events, focused test outputs, package build manifest, source revisions, and a Git status snapshot. The exploratory splash desktop capture is excluded because it included the unrelated earlier application window.

## Remaining limits

- Startup timing is variable and was not benchmarked under controlled warm/cold conditions.
- This closeout verifies UI presentation and startup only; it does not add numerical features or make a new claim about solver accuracy.
- Scientific terms such as “Lorentz force” retain their standard scientific capitalization. Product identity in the interface is `LORENTZ`.

The active roadmap remains the single planning document. Phase G stays gated on independent review.
