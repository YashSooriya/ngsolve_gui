# LORENTZ: Forward Development Roadmap

**Planning baseline:** 7 November 2026 (Phase F2 accepted; Phase G1 offline documentation and packaged Help verification in progress)
**Status:** Living plan, not an immutable implementation contract  
**Primary users:** Engineers familiar with industrial CAE tools  
**Primary immediate scope:** Phase G1: accurate, version-matched offline engineering documentation and verification through the packaged app; Phase G2 contextual search remains deferred
**Out of scope for now:** New results visualisation or a post-processing redesign (but existing Solve/Results navigation must remain fully functional)

## 1. Product objective and working agreement

LORENTZ should provide a complete, reliable and responsive workflow from geometry creation/STEP import to supported electromagnetic, mechanical and coupled FEM studies. Engineers should recognise the organisation and interaction patterns from industrial CAE applications without needing to learn a fundamentally new interface.

- **Codex owns repository-level architecture and implementation choices.** This document states goals, dependencies, behaviours and acceptance criteria. Inspect the actual code and use sound judgement rather than treating example layouts or implementation suggestions as mandates.
- **Preserve working numerical and specialised functionality.** In particular, the axisymmetric drawing/geometry workflow, project loading, STEP and coil-trace assets, materials, meshing and solver paths must not regress. Preserve project compatibility, stable storage identifiers and the shared NGSolve solver while maintaining only the LORENTZ application.

**Brand presentation follow-up (6 November 2026, complete for tested scope):** The user-facing product identity, app window title, native launcher and About label use the official all-caps spelling, `LORENTZ`. Light/dark home and About artwork uses transparent assets; the header wordmark is centered against the complete application bar. Dark mode covers the app shell, while the axisymmetric sketch stays light by default and can optionally match the app theme in Preferences. The 3D viewport follows the app theme. Project formats, solver behavior, and lowercase asset identifiers remain compatible. The original brand UI was reviewed in packaged `0.2.8.dev328`; the follow-up packaged build `0.2.8.dev329` also verifies the visible splash wave and corrected native startup handoff. See the [brand refresh report](reviews/2026-11-06-lorentz-brand-refresh.md) and [startup follow-up report](reviews/2026-11-06-lorentz-startup-follow-up.md).
- **UI controls must be genuine.** Do not present unsupported physics, algorithms or conditions as operational.
- **Performance, quality and Git hygiene are permanent requirements.** Use fast relevant tests while iterating; milestone-level integration tests; comprehensive tests at appropriate gates. Make focused commits and push verified work. Provide honest reports of skipped or failing checks.
- **One coherent milestone at a time.** Keep this roadmap current when discoveries change the order. Do not implement every section in one Codex turn.

## 2. Current verified baseline and risks

The bullets below retain historical baseline evidence where useful. The accepted Phase A closeout and completed Phase B1/B2/C statuses below supersede earlier open/blank/recovery findings.

**Latest verified status (6 November 2026):** Phases A, B1, B2, C, D1, D2, E1, E2, E3, F1, F1.1 and F2 are complete or accepted for their documented scopes. Phase F2 retired the active Mike-only product path, archived its obsolete artifacts, clarified execution-versus-convergence status, made the unavailable 3D Run study action visibly disabled, and preserved per-frequency axisymmetric convergence diagnostics in run manifests. The Phase F2 package (`0.2.8.dev319`, SHA-256 `c3218cf13a021b7e78697cc198252bd1458b093b58b5c992424bc174ffe9e9af`) passed native axisymmetric, 3D coupled, geometry-preview and public-launcher checks at 1920×1094. The coupled model was deliberately small (79 tetrahedra); no large 3D solve was run. The brand-refresh package (`0.2.8.dev328`, SHA-256 `46068c2bd2ae3c4d12e344aacaa726bcb239a12186d397fbf32658814a0b6722`) passed focused branding tests and public-launcher startup; actual UI screenshots show the light/dark home, About, 3D STEP and axisymmetric sketch themes at 1920×1094. No solver run or numerical change was part of the brand refresh. The latest full workflow check rendered the axisymmetric geometry-creation preview through WebGL; an earlier blank preview capture occurred once and its underlying cause remains unknown. WebGPU has no usable adapter on this VM; the tested WebGL fallback remains the available 3D path here. The VM is on an SSD, so timing observations are not controlled comparisons. Stable `MM-FEM` schemas, kernel IDs and user-data compatibility paths remain. Persistent/offline ROM remains deferred and the CPU-only 2D field viewer stays retired. See the [Phase F2 report](reviews/2026-11-06-phase-f2.md) and [brand refresh report](reviews/2026-11-06-lorentz-brand-refresh.md).

- The saved `Getting started - coupled coil` project rendered its square and circle regions in the first geometry-editor screenshot. The sketch grid filled the viewport at maximized and restored sizes with correct physical scaling. The earlier single blank capture was not reproduced; its cause remains unknown.
- The axisymmetric geometry-creation 3D preview rendered both regions using Plotly WebGL. 3D STEP import, a 22,132-element bounded mesh, repeated native CAD selection, display switching, rotation and selection persistence all passed. The historical OOM did not reproduce; the test covered only three bounded cycles, so larger and longer sessions remain a risk.
- A controlled renderer crash recovered in 16.5 s with one automatic attempt, preserved the saved model and selection, and restored a visible maximized viewport. Recovery now re-applies design-system and app styles that were lost with the crashed document.
- The VM had moved to an SSD before these checks. Latest splash-to-ready times were 9.1 s axisymmetric and 7.3 s 3D; there is no controlled before/after comparison.
- At Phase A closeout, no numerical run or solver-algorithm change occurred. Phase B1 later ran only bounded 3D EM/mechanics cases to verify model-to-solver propagation; no numerical algorithm or dependency changed.
- Phase B2 ran actual NGSolve checks on 10 mm STEP blocks. Doubling a scoped domain-current source produced a magnetic-field proportionality error below `1e-5`; the 50 Hz mechanics case checked phasor body-force and prescribed-displacement loading plus fixed and spring supports. The 22,132-element STEP model used for desktop verification was restricted to preprocessing, selection and persistence checks and was not submitted to the solver.
- Phase B2 desktop evidence was captured at full 1920×1094 resolution with MagMechSim maximized. The packaged checks exercised the axisymmetric editor and Plotly WebGL geometry preview, 3D STEP import/mesh, native face selection, condition editing, save/reopen and controlled renderer recovery. Isolated app processes were cleaned up.
- Phase C ran the packaged native DC study from the maximized application: a 10 mm STEP block, 3,993 tetrahedra, p=1, 14,248 solve DOFs, Jz=1e6 A/m² and Bz=0.002 T. The run completed, wrote `dc_background_field.npz` with 25,779 H(div) coefficients, and reloaded it against the same mesh and input fingerprint. Reopening in a fresh process preserved its configuration; changing Bz invalidated the old field.
- A small permeable-sphere reference used an analytic center field of 0.030 T for μr=2 under 0.020 T applied field. The center sample improved from 0.0287751 T at h=2 mm (11,915 tetrahedra) to 0.0292694 T at h=1.5 mm (23,152 tetrahedra); both remained within 5% of the analytic reference. The μr=1 control returned 0.0200000 T. This is one bounded h-refinement step at p=1, not an asymptotic convergence-order study.
- The VM had moved to an SSD before Phase A and remained there for the Phase C runs. The Phase C packaged-launch times of 8.93 s and 7.38 s are observations from separate launches, not a controlled storage or code comparison.

Historical baseline reviewed on 2 November:

- 3D and axisymmetric time-harmonic solver execution and focused regression checks have been demonstrated for selected cases. The latest 3D focused suite reported **63 passes** and axisymmetric model/revolution tests **41 passes, 2 deselected**. The full historical suite was *not* rerun; its last recorded baseline was **513 pass / 11 fail / 3 skip**.
- 3D harmonic mechanics has manufactured-solution convergence evidence. Bounded nonzero-frequency free-body inertial tests at 50 and 100 Hz agreed with expected rigid translation; fixed-support enforcement was also checked. These do not constitute a full verification of coupled 3D magneto-mechanics.
- CAD-based selection identity and a user-facing `Needs review` reassignment workflow have been added. Unresolved active assignments block solves rather than silently reattach to unintended geometry.
- On a 22,132-tetrahedron model, repeated 3D face-selection-to-editor latency was approximately **1.6 s median**. Python overlay changes helped, but the full browser-to-backend-to-render path needs profiling.
- An earlier Windows release report recorded **16.3 s** startup-to-GUI-mounted on a VM newly moved to SSD. No controlled cold/warm comparison was available then. The VM has since had multiple packaged desktop checks; their timings are below and are not controlled comparisons with the 16.3 s result.
- The 2 November packaged desktop check now confirms that the axisymmetric geometry-creation preview renders in the actual application with Plotly WebGL, recent-project cards contain geometry images, and a native Win32 CAD-face click updates and persists a named boundary selection. This supersedes the earlier blank-preview and unverified-native-selection findings below.
- The packaged “Getting started - coupled coil” project opens with a blank 2D sketch canvas despite two reported regions; its 3D geometry preview renders. The 2D sketch content remains unverified for that legacy/example project and is tracked separately from the confirmed 3D preview.
- Historical 3D renderer OOM/recovery failures remain unresolved. The current bounded desktop check produced and selected a 22,132-element mesh without reproducing the crash, but does not establish renderer stability under repeated native selection or higher memory pressure.

**Do not re-do these completed milestones.** Build upon their evidence, and re-check only when changed code, a failed test or release-critical risk warrants it.

## 3. Completed milestone: application-wide UI coherence and targeted correctness fixes

### Goal

Make the top-level application layout, commands, modal behaviour, branding and axisymmetric geometry workflow feel cohesive and predictable. This milestone is complete; its status and evidence below are retained as the historical baseline for later 3D work.

### 3.1 Professional navigation and top-area architecture

- Reassess the entire top of the application **in both axisymmetric and 3D**, including File/Edit/View/Model/Study/Results/Help, quick-access icons, project name, context-sensitive actions and workspace navigation.
- **New, Open, Save, Save As** must be easily found where engineers expect them, normally under File and/or compact quick-access controls. Avoid scattering file actions among geometry controls or study actions.
- Rework **Solve / Post Process** navigation. Treat it as a clear workspace/mode distinction, not two awkward floating buttons. Retain the ability to switch and preserve unsaved state as appropriate. Do not redesign results visualisation itself.
- Keep a prominent, discoverable route to **Validate / Generate Mesh / Run**, but avoid unnecessary stacked header rows or excessive vertical consumption. Preserve an adequately sized viewport and resizable model/property panels.
- **Context-sensitive menus and commands:** Axisymmetric View should expose relevant 2D sketch/grid, zoom and selection operations, not arbitrary 3D camera views; axisymmetric Model must not offer STEP import unless it is genuinely supported; 3D View should expose legitimate camera/orientation tools; physics/study commands should reflect current workspace and selected model object. Generic File/Edit/Help commands remain consistently located. Hide or disable inapplicable commands according to a clear rule, and avoid dead-end entries.
- The CPU-only **2D field viewer is retired and removed** from the full application and package by user direction. Do not use it, restore it, or treat it as a fallback. Preserve real 3D geometry/mesh previews; diagnose missing WebGPU adapters and use a genuine 3D renderer path where supported.
- Retain concise engineering labels; remove redundant helper prose and unexplained icon-only actions. Use contextual tooltips for genuinely needed guidance.
- Consider smallest practical laptop window as well as 1920×1080 and larger. Resizing should not truncate essential actions or make the viewport unusable.

**Acceptance:** Axisymmetric and 3D show consistent shell organisation; the location of core actions is predictable; menus are accurate for each workspace; viewport available height is visibly improved; Solve/Results transitions work without losing context or data.

### 3.2 Axisymmetric viewport fills its entire pane

- Fix the black/empty strips on the left and right of the axisymmetric viewport. The background grid must **cover the entire visible viewport**, at every supported window/panel size and aspect ratio.
- Preserve correct **physical scale, axis labels, r-z conventions and aspect ratio of geometric objects**. Stretch or repaint the background/grid, not the model geometry. Avoid clipping, grid discontinuities and jitter after panel/window resizing.
- Verify maximised, restored, narrow, wide, panel-collapsed and DPI-scaled layouts, including bottom Messages/Log panel changes.

**Acceptance:** No unexplained black margins across tested sizes; grid remains continuous; the same geometry keeps correct physical dimensions and selection mapping after resizing.

### 3.3 Tangent arc construction correctness

- A **tangent arc may only begin at a genuine loose/free endpoint of an existing line segment** that provides the entry tangent. Starting it on empty canvas, the middle of a line, a fully-connected endpoint or another unsupported place must be prevented.
- Determine and apply the true geometric/topological rules from the existing sketch model: tangent continuity must be well-defined and construction must not corrupt geometry or leave a half-created arc on invalid attempts.
- Provide subtle, actionable feedback when the start location is invalid (cursor/tooltip/status), rather than an intrusive modal. Keep ordinary arc tools unaffected.

**Acceptance:** Valid loose-end construction works; invalid origins are rejected without altering geometry; tangency is demonstrable; cancel/undo and saving/reopening work.

### 3.4 Dialogs and overlays

- Users should be able to dismiss normal overlay/dialog interactions by **clicking the dimmed backdrop**, and by Escape where appropriate.
- If a dialog has unsaved/dirty inputs that would be discarded, request confirmation **only when actual data would be lost**. Do not nag on read-only dialogs, empty forms or unchanged values.
- Truly non-dismissable critical states (if any) must be justified and show clear actions; closing must never corrupt a running process or application state. Ensure focus management, keyboard access and no accidental click-through.

**Acceptance:** Safe backdrop dismissal across representative dialogs; appropriate discard confirmation for dirty forms; no unnecessary confirmation or accidental data loss.

### 3.5 Correct time-harmonic mechanics study access

- The **Configure study frequencies** command must remain available when **time-harmonic mechanics only** is selected, in addition to harmonic electromagnetic and supported coupled cases.
- Establish eligibility based on whether the chosen study actually uses a harmonic frequency, not whether electromagnetic physics is enabled. Pure DC cases may legitimately have no harmonic-frequency configuration.
- Preserve previously configured frequencies when switching compatible physics modes, and validate mode/study compatibility.

**Acceptance:** Harmonic mechanics-only users can access and edit frequencies; changes reach the correct solver study; switching physics does not silently discard valid settings.

### 3.6 Visual identity and icons

- Replace the **Axisymmetric** selection icon that currently resembles three orthogonal coordinate axes with a semantically accurate icon, such as an **r-z cross-section with a rotation cue**. Avoid confusing it with the 3D option. Use the established visual style and supplied branding assets where appropriate.
- Fix the MagMechSim **Windows taskbar/application icon** white rounded-corner artifacts by ensuring proper transparency/alpha in all packaged icon sizes and correct usage across window, shortcut, titlebar and taskbar. Respect Windows icon caching when testing.
- Confirm that current user-facing labels and assets show MagMechSim, not historical MM-FEM branding, while preserving any legacy IDs essential for file compatibility.

**Acceptance:** A meaningful axisymmetric icon is visibly distinct from 3D; icon corners blend correctly over light and dark taskbars; the packaged executable and shortcuts use the right assets.

### 3.7 Local Sphinx documentation

- Inspect the existing documentation sources (`conf.py`, `.rst`/Markdown, build outputs and packaging setup). Confirm what is available rather than assuming the docs are current or already built.
- Change **Help → Documentation** to open the **locally installed/bundled Sphinx HTML documentation**, not GitHub, including in the packaged Windows application and when offline.
- Choose a packaging/build and URL/path strategy compatible with ngapp and the Windows launcher. Handle missing/unbuilt docs gracefully with actionable feedback. Open the documentation landing page; preserve existing section anchors where possible.
- This milestone does **not** require fully rewriting the documentation or building the future top-right search UI. Add that to the later documentation phase.

**Acceptance:** Help opens an actual bundled local docs landing page in both development and packaged executable, without internet or an absolute developer-machine path.

### 3.8 Scoped performance and verification

- Capture a small baseline for key changed interactions: menu/workspace switching, axisymmetric viewport resize, dialog open/close and (if feasible) representative 3D face selection. Profile the known ~1.6 s 3D face-selection latency rather than assuming it is a Python-only issue.
- Fix clear local bottlenecks encountered in this milestone without turning it into a major rendering engine rewrite. The UI must remain usable during mesh/solve tasks.
- Test both workspaces using relevant targeted checks plus a few representative GUI interactions. Verify final packaged Windows executable and the source revisions it contains. Report untested behaviour explicitly.

**Deliverables:** Changes implemented; actual native desktop screenshots at normal and constrained sizes; UX and regression checks; simple timing comparisons; Git commit/push records; concise outstanding issues; review ZIP. Update this roadmap only when meaningful discoveries change priorities.

**Milestone status (2 November 2026):** The UI-coherence milestone and native release gate are complete. The final MagMechSim executable was built from GUI commit `e57d95695315dca061a83c242fa77448ec9e5c4c` and solver/release commit `b27702905a9db63edca0d12439cfa89a15bb7d5a`, launched, and verified maximized on the 1920×1094 desktop.

- At that 2 November milestone the CPU field viewer was relocated into Results. User direction on 4 November superseded that decision: the viewer and fallback were removed from the full application/package. The shared shell still presents consistent Model/Results navigation, shared file actions, context-aware menus and local documentation help. Axisymmetric sketching and the existing 3D STEP/wire-trace workflow remain in place.
- Tangent-arc eligibility, mechanics-only harmonic frequencies, dismissal of safe overlays, and alpha-clean application icons have targeted regression coverage.
- Focused GUI unit checks pass (**341 passed**); axisymmetric geometry 3D preview and repeated small axisymmetric solver workflow pass; the 22,132-tetrahedron 3D mesh/selection workflow passes without running a 3D solver. The complete Sphinx HTML build succeeds offline. At that earlier milestone, four Windows launcher tests also verified MagMechSim maximization, delayed window-manager confirmation, compatibility with the then-existing Mike branch, and explicit failure if maximization could not be confirmed. Phase F2 retires that branch.
- Fresh 3D selection profiling still reports a **2.49 s median** click-to-editor time, while Python tree/property rebuilds are approximately **10 ms / 7 ms**. The residual latency is outside those measured methods and needs browser/Plotly/transport profiling in Phase A. The VM's move to SSD means comparisons with the earlier measurements are not controlled.
- A post-restart review found that `--start-maximized` alone did not reliably restore an existing Chrome app-profile window. The MagMechSim launcher now locates the browser process for its isolated install profile, requests maximization after the interface mounts, checks the Windows maximized state, and reports a clear startup error if that state cannot be confirmed. At the F1 closeout, the separate Mike (`MM-FEM`) branch still retained its previous restoration behavior; Phase F2 retires that path.
- At the time this earlier UI-coherence review was recorded, the packaged axisymmetric geometry-creation preview appeared blank in the available capture. The native desktop verification in the later Phase A update below supersedes that observation: the preview is visible using Plotly WebGL on this VM. Its preview is no longer a WebGPU dependency; the main 3D STEP renderer's WebGPU/driver path is separate.
- This earlier review depended on Codex Computer Use for interaction/capture. The reusable Win32 and desktop-capture harness described below now verifies the installed package independently; Computer Use availability is no longer a gate for future GUI milestones.
- No numerical solver algorithms were changed. The VM's move to SSD is a performance confound; the prior 16.3 s startup and 2.49 s selection timing are not controlled same-hardware comparisons. The review package records the final executable identity, logs, screenshots, performance observations, and remaining preview limitation.

## 4. Roadmap phases and current status

These stages are **directional**, with dependencies and scopes to be refined by Codex after inspection. They need not be exactly one Codex prompt each.

### Phase A. 3D interactive performance and preprocessing refinement (closeout complete; residual risk carried forward)

**Outcome:** CAD picking, selection highlighting, property editing and model navigation feel immediate on realistic engineering geometry.

- Profile the complete user interaction pipeline, including browser rendering, Plotly redraws, WebSocket communication, ngapp state updates, NGSolve/OCC work, logging and panel changes.
- Eliminate unnecessary full redraws and heavy recomputations on selection, reuse costly geometry/mesh data, and keep UI updates responsive. Re-evaluate renderer choices only on proven benefits, not WebGPU fashion.
- For 3D geometry creation, retain the existing genuine WebGL preview where it provides the required 3D interaction. For any WebGPU-dependent workspace, test adapter, device and render capability in the actual packaged desktop; identify the limiting graphics stack and prefer a supported 3D-capable backend. The retired CPU-only 2D field viewer is not a fallback and must not be used or reintroduced.
- Benchmark repeatable selection p50/p95, viewport interaction, panel switching, geometry loading and startup on representative models and **unchanged hardware**. Separate VM/SwiftShader limits from production hardware. Avoid claiming speedups after storage/hardware changes without a controlled baseline.
- Aim for near-immediate common interactions. Set numerical latency budgets after obtaining reproducible hardware conditions; do not trade correctness for a number.

**Gate:** Repeatable profiling identifies bottlenecks, representative interactions improve measurably, no CAD assignment/solver regression.

**Milestone status (2 November 2026 — rendering and responsiveness pass): Partial; keep Phase A open.** The implementation is in GUI commit `25450a9476197de9244024c20391c81a7dc53f8b`; the Windows package was built from that GUI revision and solver/release commit `b27702905a9db63edca0d12439cfa89a15bb7d5a`.

The following findings are the initial status at that milestone, before the packaged desktop verification recorded in the next subsection. Later native checks supersede the earlier statements about preview visibility and whether a physical CAD pick can update the property editor.

- The axisymmetric geometry-creation preview uses a bounded OCC tessellation rendered with Plotly WebGL. It shows the revolved region with physical XYZ proportions, feature edges and per-region visibility. On 4 November the packaged VM reported WebGL2 available but no WebGPU adapter; the 3D geometry/mesh preview rendered. The CPU-only 2D results viewer and automatic fallback have since been removed. Physical GPU behavior remains to be checked outside this VirtualBox guest.
- In the maximized packaged application, a drawn annulus with a bore rendered, rotated, zoomed and returned to the editable sketch. Ten wheel events did not make the sketch grid disappear, and the measured sketch dimensions remained unchanged. A saved axisymmetric project was reopened with its geometry intact. This grid issue did not reproduce, so no forced redraw was added.
- 3D selection handling now avoids rebuilding the model tree when a face pick leaves the selected tree item unchanged, and boundary-overlay edge data is cached. On the same post-SSD VM and 22,132-element test mesh, the Playwright profile's five repeated event-to-editor samples improved from median `2734.37 ms` / p95 `3286.34 ms` to median `443.98 ms` / p95 `474.17 ms`. The first/cold after sample was `1914.59 ms`. These events are emitted programmatically to measure the event-to-editor path; they do not measure real pointer hit-testing. Do not treat them as proof that native CAD picking is correct or near-immediate.
- The actual packaged 3D workspace imported the STEP fixture and generated/rendered a coarse 100 mm mesh (`22,132` volume elements, `14,588` surface triangles). Earlier direct native clicks did not visibly create a boundary-region assignment; a later pointer move produced Chrome's `Aw, Snap! Out of Memory` page. An earlier, denser 30 mm mesh (`105,523` volume elements, `69,978` surface triangles) also preceded a renderer OOM after repeated plot interactions. Mesh generation succeeded; no solver was run. These OOM events remain unresolved, although a subsequent native pick and persistence check succeeded on the coarse mesh.
- The packaged executable is `0.2.8.dev216`, SHA-256 `da3e597dd12cbf0c0c541c80b23ed68489e392bfdf4919bbca9de6a7d39c2a95`. The latest launcher opened the maximized app. After the renderer crash, a same-package browser relaunch stayed white while the loopback server still returned HTTP 200; stopping only the verified stale package controller/GUI-host processes and relaunching restored the maximized project chooser. This is a recovery workaround, not a code fix. The app window and full-desktop screenshots were verified at `1920 × 1094`.
- Focused regression evidence: `290 passed` in the GUI unit suite, `1 passed` for the axisymmetric preview E2E, and `1 passed` for the bounded 3D workspace E2E. The 3D E2E imports STEP and wire-trace fixtures, meshes and exercises model/named-selection flows; its selection timing dispatches synthetic Plotly events. No numerical solver algorithm or dependency changed, and no simulation was run. The VM's move to SSD prevents comparison with pre-SSD timings as a controlled benchmark.
- The release package repository `ngsolve-ui` was confirmed private on 2 November 2026. The separate GUI source repository `ngsolve_gui` remains public.

**Milestone status (2 November 2026 — reusable packaged desktop verification): Completed for the requested GUI reliability gate; Phase A remains open.** Evidence and instructions are in [`docs/reviews/2026-11-02-desktop-automation.md`](reviews/2026-11-02-desktop-automation.md) and the shareable review ZIP.

- A small standalone harness now launches the packaged MagMechSim executable in a unique test workspace. Win32 `SendInput` drives real desktop mouse, keyboard and wheel input; Pillow `ImageGrab` captures the complete Windows desktop; Playwright attaches only to a test-only, loopback CDP endpoint to inspect DOM state and measure timings. No Codex Computer Use interaction or screenshot capability is required. At the Phase A harness milestone, the opt-in CDP flag was scoped to MagMechSim and disabled on ordinary launches and in the then-existing Mike variant; Phase F2 removes that variant while retaining the test-only guard.
- The tested package is `0.2.8.dev216`, executable SHA-256 `c83a1b7e8b319ead59743b58f066fe7cdfa5802405f3487baf3a1ea2477c0207`, based on GUI `e9085543d003f1ccfa09b576a078688fbe83b5b0` and solver/release `46d4ecaf45c484cb14aca7c578a202d1b6712f6b` before this test-harness commit. Both fresh launches were maximized on the full `1920 × 1094` display. Launcher-to-ready times were `7.945 s` (axisymmetric) and `6.779 s` (3D); the maximize-to-splash-dismiss interval was `0.028 s` and `0.017 s`. The VM's move to SSD is a confounder, so these are baselines, not code-only speedup claims.
- Both recent-project tiles displayed actual saved geometry thumbnails (`288 × 176` PNGs); axisymmetric and STEP projects opened from native double-clicks. The axisymmetric geometry-creation preview opened, rendered its regions in WebGL, and returned to the sketch editor. A fresh screenshot of that preview showed visible geometry in a maximized window. The sample's 2D sketch screenshot was blank with zero rendered sketch-region elements, so this run does not claim successful 2D sketch visual verification.
- In 3D, the app displayed the imported STEP in `4.094 s`, generated a capped coarse mesh (`22,132` volume elements / `14,588` surface triangles; `13.641 s` generation and publish), and a native Win32 pointer click selected a face and populated the boundary property editor. A name typed through native keyboard input was saved; the project archive reopened with boundary ID and CAD tag present. The test did not launch a solver.
- The full evidence run passed all `17` packaged-desktop checks, captured `10` full-desktop screenshots, and reported no remaining package-owned processes after cleanup. Focused tests passed: `4` instrumentation unit tests, `11` project-thumbnail/startup tests, and `1` development-browser axisymmetric-preview Playwright E2E. The browser E2E and packaged-desktop checks are separate evidence types. The all-GUI suite was not rerun for this test-harness milestone.
- A historical renderer OOM and recovery failure is not fixed by these checks. It was not reproduced by the capped mesh or single native pick; higher-memory rendering, repeated native picks and recovery after a real renderer crash remain open Phase A work.
- The `Launch-Latest-MagMechSim.cmd` launcher now targets the tested package folder. Each future handoff must keep the launcher pointed at the newest tested executable, record its SHA-256 and source revisions, and include the SSD/storage-change caveat when reporting timings.

**Phase A closeout (3 November 2026): Complete; Phase B not started.** The latest packaged executable was built from GUI revision `296e850300598aaa9ccf34d603a19cb9f566032d` and Windows package/recovery revision `4b4b4525` (preceded by focused recovery-focus commit `a68a6f78`). It passed **25 packaged-desktop checks** with 30 full-desktop captures and **14 focused tests plus 4 subtests**. The app was maximized on a 1920 × 1094 desktop. Win32 `SendInput` provided actual mouse, keyboard and wheel input; Pillow `ImageGrab` captured the desktop; Playwright over test-only loopback CDP provided read-only DOM inspection and timing. The evidence ZIP contains representative maximized screenshots, test JSON, diagnostics, build identity and logs.

- Renderer recovery root cause: replacement documents did not receive the one-time `cerbsim_ngapp_style` and app-level CSS. Before the correction the recovered root computed to `display:block` and the 3D viewport was 0 px tall. Recovery now restores those styles and window identity before resending the component tree; the controlled native crash test recovered the saved model, named selection and visible 3D mesh in 16.5 s with one attempt.
- In three bounded interaction cycles on a mesh capped at 25,000 volume elements, nine native face clicks selected two distinct boundary regions. Click-to-editor p50/p95 was 444/646 ms. Chromium RSS increased by 99 MB from the first sample and peaked 206 MB above it; JS heap ended 27 MB above its first sample. GPU memory was not measurable. These measurements do not prove absence of leaks or OOM on larger models.
- The historical Chromium OOM did not reproduce. It remains a risk for larger models and long sessions, but does not block beginning bounded Phase B model-preparation work. Keep memory trends in acceptance checks and revisit if usage grows monotonically or an OOM recurs.
- No solver algorithms, numerical dependencies or project formats changed, and no simulation was run. The separate Mike variant was outside that milestone; Phase F2 subsequently retires it.
- The full desktop harness also confirmed visible saved axisymmetric geometry on first capture, full-pane grid at maximized/restored sizes, an actual Plotly WebGL geometry preview, real STEP-face interaction and persistence. The earlier blank sketch frame was not reproduced; its original cause is unknown. Physical GPU behavior remains unverified.
- Latest package: `0.2.8.dev216`, SHA-256 `c7c40e9bf975a8d3870ed6e082f271fdcbf8c2b5478d61bfdaca25e6f6cf5951`. `Launch-Latest-MagMechSim.cmd` points to this package. Current startup timings on the SSD-migrated VM are not controlled comparisons with earlier storage conditions.

**Phase A exit:** stop for review after the Phase A evidence. Following the independent acceptance, bounded Phase B1 model-preparation work proceeded; DC background-field and coupled-physics work remain later phases.

### Phase B. Complete the 3D model-preparation workflow (B1 and B2 complete; Phase C also complete)

**Outcome:** Engineers can create, edit and validate all supported 3D inputs directly within the GUI, without editing Python.

- Continue strengthening STEP import, geometry hierarchy, robust face/body/edge selection and CAD-change recovery.
- Complete material-property participation and domain assignment; make selection repair clear, safe and persistent.
- Expose supported electromagnetic and mechanical BCs, sources, vector directions, boundary/domain scoping, and supported connections. Preserve the working A=0 condition, mechanical fixed support and body force flows.
- Finish supported global/local mesh controls, mesh quality diagnostics, numerical discretisation and polynomial/geometry order distinction, frequency study configuration and solver settings.
- Validate against the real backend capability matrix, not an aspirational generic FEM feature list. Avoid general-purpose contact/nonlinear UI where not implemented.

**Gate:** Full model setup and solver execution for representative supported 3D EM-only and mechanics-only cases, including project save/reopen and invalid-input checks.

#### Phase B1. Geometry organisation, materials and safe preparation (complete for the bounded milestone)

The 3D model tree now exposes stable STEP solids with rename, visibility/isolation, material assignment and reusable named-domain selections. The material library edits the supported linear material properties (relative permeability, electrical conductivity, Young's modulus, Poisson ratio and density), validates the properties used by active physics and embeds project snapshots. Direct solid assignments use the existing named-domain selection and CAD identity/recovery format; no project schema migration was needed.

The GUI adapter propagates these physical values to the existing NGSolve problem dictionaries. Zero conductivity maps to an insulating domain flag. A small EM solve and a mechanics-only solve verified real backend execution and material response. No solver algorithm or numerical dependency changed. Packaged native tests covered selection highlighting, body visibility, invalid material editing, save/reopen/remesh, and user-guided recovery after STEP topology changed. The 107 focused 3D tests, 43 axisymmetric geometry tests, three bounded solver checks and eight Windows builder tests passed. Full evidence, exact package identity, screenshots, performance observations and limitations are in [`docs/reviews/2026-11-03-phase-b1.md`](reviews/2026-11-03-phase-b1.md).

The desktop sample used two small STEP solids and a 24-triangle preview mesh; it is functional workflow evidence, not a representative large-model performance benchmark. The historical Chromium memory concern from Phase A remains monitored and is not a blocker.

#### Phase B2. Supported 3D boundary conditions, sources and loads (complete for the tested scope)

The contextual inspector configures the supported single-physics 3D conditions against stable named geometry selections. EM supports the existing A=0 boundary condition, wire-trace excitation with complex current phase, and scoped complex current-density vectors on volume domains. Mechanics supports global and per-domain complex body forces, fixed supports, prescribed-displacement phasors, and boundary-scoped normal/tangential springs. Validation follows the active physics, validates region kind and required materials, and blocks unresolved or incompatible active assignments. Saved projects retain stable CAD IDs/tags, and changed STEP geometry cannot silently move a condition to another entity.

At B2 closeout, solver changes were limited to input/configuration assembly and provenance: body-force maps were converted to material coefficient functions, spring maps were applied on selected boundaries, and wire-trace phase scaled the existing complex source. At that point fully coupled EM-mechanics and DC background-field workflows were deferred; Phase C and D1 subsequently delivered their tested scopes. No element formulation, numerical algorithm or dependency changed in B2. External mechanical traction, contact, nonlinear mechanics and ROM remain unsupported and are not exposed as available.

Verification combined real packaged-app interaction and project save/reopen evidence with two small NGSolve runs. A 10 mm block EM case doubled a 1e6 A/m² current-density source to 2e6 A/m² and kept the sampled magnetic-field proportionality error below `1e-5`. A 50 Hz mechanics case checked a 1e8 N/m³ scoped load, 1e-6 m prescribed motion, spring and fixed supports; constrained DOFs had the expected imaginary amplitude and near-zero real component. Full evidence, test logs and limitations are in [`docs/reviews/2026-11-03-phase-b2.md`](reviews/2026-11-03-phase-b2.md).

The broader Phase B backlog retains verification of discretisation/order distinctions and numerical-setting coverage where a future backend-capability review identifies a gap. That work is not an implicit promise to add unsupported controls and does not change the next priority stated for this milestone.

### Phase C. 3D DC magnetic background and static-field integration (dependency for the intended coupled workflows)

**Outcome:** The engineer can configure and execute the static/DC magnetic stage needed by existing 3D coupling paths.

- Map the genuine existing 3D numerical implementation, required excitation/material/boundary inputs and dependencies on wire traces, imposed background fields or magnet sources.
- Expose only meaningful supported controls, appropriate sequencing and preflight validation.
- Verify that configured DC inputs reach the correct solver, and compare against established reference cases.

**Gate:** Reproducible DC magnetic solve, numerical evidence, exact-mesh/input field persistence and stale-state rejection. Downstream coupled use is the Phase D gate.

#### Phase C status: complete for the verified supported scope (3 November 2026)

The 3D study selector now supports `Static/DC magnetics`. Its contextual inspector exposes the uniform applied field, volume current density, DC wire-trace current and applicable A=0 boundaries, while hiding harmonic frequency. Active-physics validation requires relative permeability but does not require harmonic conductivity or mechanical data. The existing geometry, mesh, stable CAD selections, solver settings and run infrastructure are reused.

The GUI adapter sends the static problem to the existing `solveBDC_3D` NGSolve formulation. It applies the configured uniform field through the vector-potential boundary data, assembles the DC current source and existing wire-trace source, and uses the assigned linear relative permeability. The completed B field is saved as H(div) coefficients with mesh SHA256, polynomial order, DOF count and a fingerprint of physics-relevant inputs. Save/reopen, exact reload and stale-input rejection were verified.

Targeted GUI, solver, axisymmetric, geometry-preview and packaged desktop results are documented in [`docs/reviews/2026-11-03-phase-c.md`](reviews/2026-11-03-phase-c.md) and the self-contained review ZIP. Tests include a small uniform-field/permeable-sphere reference with one h-refinement step and the real 3D native workflow. A bounded 3D domain-current direction/reference check is retained in the report. The app/solver code was committed and pushed; the latest launcher now targets the tested Phase C package.

One fresh packaged launch produced a white native frame at the first screenshot immediately after the splash, although a subsequent fresh launch showed the home screen without input. The packaged desktop check now samples the real screen for up to five seconds before DevTools or mouse/keyboard interaction and records the first visible frame; two later end-to-end launches and the normal shortcut launch passed. The cold first-frame race was not reproduced consistently and its root cause remains unconfirmed, so it remains a monitored startup risk.

**Supported-scope limitations at Phase C closeout:** At that point the saved field had not yet been consumed by a coupled GUI study; Phase D1 now verifies that integration for a bounded case. The sphere result is a coarse bounded model, p=1, with one h-refinement step. A deprecation warning for NGSolve H(curl) keyword flags remains in the existing backend. D2 should extend coupled response and mesh/order evidence.

#### Phase D1. Fully coupled 3D magneto-mechanical solver integration (accepted for tested scope; 4 November 2026)

Phase D1 connected the saved DC background, harmonic electromagnetic and mechanical response through the existing 3D block Gauss-Seidel sequence. The GUI now configures one supported coupled analysis, computes or reuses only a matching DC field, and reports convergence distinctly from process completion. The D1 implementation retained the existing solver and passed the bounded verification recorded below. Phase D2 is the next workflow and verification refinement after independent review; ROM and unrelated physics controls remain outside Phase D.

### Phase D. Fully coupled 3D magneto-mechanical harmonic analysis (D1 and D2 accepted for tested scope)

**Outcome:** Engineers can configure and execute the existing supported coupled model through the GUI, with understandable convergence/solver feedback.

- Integrate static background, harmonic EM and harmonic mechanical response in the validated sequence required by the backend.
- Expose appropriate physics participation and coupling parameters, fixed-point/block Gauss-Seidel iterations, tolerances, relaxation/acceleration where truly implemented, and numerical compatibility rules.
- Detect and report nonconvergence separately from a successful process exit; preserve solver evidence and job state correctly. Validate the actual coupled response with existing trusted problems, p/h refinement or independently verified comparisons.

**Gate:** Bounded real coupled study works end-to-end, numerical checks pass, and failure/partial-convergence handling is correct.

**D1 status (4 November 2026):** Accepted for the tested scope. The existing 3D block Gauss-Seidel/Anderson method was retained. The GUI runner computes a DC field when needed or reuses a field only after its mesh, polynomial order and DC input fingerprint validate; it passes H(div) coefficients into the existing space and records converged and nonconverged outcomes distinctly. Phase D2 strengthened this with bounded coupling-isolation, phase/magnitude, analytical mechanics, mesh/order, persistence, failure and packaged desktop evidence. Details and limits are in the [D1 report](reviews/2026-11-04-phase-d1.md) and [D2 report](reviews/2026-11-04-phase-d2.md).

#### Phase D2. Coupled workflow, diagnostics and verification refinement (accepted for bounded tested scope; 4 November 2026)

Completed: refined run-history diagnostics, bounded independent coupled-response and mesh/order evidence, and verified DC reuse/invalidation, convergence outcomes, and persistence. No solver equations were changed. Frequency sweeps remain deferred until the supported backend path and its verification are established.

**Gate:** Accepted for tested scope. The evidence and known limitations are recorded in the [Phase D2 closeout report](reviews/2026-11-04-phase-d2.md).

### Phase E. ROM, study flexibility and advanced numerical settings

**Outcome:** Supported research-grade numerical features become approachable through professional study/property configuration.

- Expose supported mechanical modal reduction and its prerequisites; reuse the basis within a study run, and add persistent cross-run basis artifacts only if measured workloads justify their storage and validation lifecycle.
- Improve frequency sweeps, multiple studies and job/resource controls where implemented; ensure correct complex-valued inputs and units.
- Extend time-integration and other study variants **only after** the relevant backend and project state are demonstrably ready, with appropriate controls and verification. Unsupported combinations remain unavailable.

**Gate:** Each new method has an executable GUI-to-backend example, state persistence, numerical reference and error handling.

#### Phase E1. Harmonic frequency sweeps and study management (complete for the tested scope; 4 November 2026)

The Study properties expose single-frequency, explicit-list and range definitions (up to 50 points, bounded at 1e9 Hz), while project schema v2 preserves schema-v1 projects and single-frequency defaults. EM-only, mechanics-only and coupled 3D jobs pass the configured frequencies to the existing solver path; frequency-specific snapshots, progress and execution/scientific status are recorded in the manifest and run history. Cancellation, failed/pending points, completed-but-nonconverged coupled points and exact DC-input/mesh reuse are represented separately. No solver equations, numerical dependencies or new post-processing features were added.

Verification includes same-mesh EM sweep/single-frequency relative L2 differences of `7.332e-15` at 50 Hz and `7.507e-15` at 100 Hz; mechanics comparisons below `1e-8`; coupled 50/60 Hz comparisons within the configured `1e-4` fixed-point tolerance; and a 79-element packaged coupled sweep with both frequencies converged in two iterations. A stricter two-iteration rerun was correctly recorded as execution-complete but scientifically nonconverged. The full focused 3D study suite passed 407 tests. The packaged application passed three fresh maximized launches, persisted/reopened the sweep settings, and the latest launcher smoke reached the mounted workspace. Full results are in the [Phase E1 report](reviews/2026-11-04-phase-e1.md).

The user-retired CPU-only 2D field viewer remains absent and must not be used or restored. The packaged VM exposes `navigator.gpu` but historically returned no adapter; its WebGL2 renderer identifies the VirtualBox WDDM Direct3D 11 adapter. The axisymmetric geometry-creation preview is a real, interactive Plotly WebGL 3D renderer: packaged tests verified the region meshes, native mouse rotation and return to the sketch editor at full desktop resolution. This preview does not depend on WebGPU.

**WebGPU reliability follow-up (4 November 2026):** the three shared result-view tabs (geometry, mesh and fields) verify WebGPU with a real canvas triangle render probe and automatically switch to interactive Plotly/WebGL 3D surfaces when the API, adapter, device or bounded probe is unavailable. The adapter/render probe is bounded at five seconds to handle driver requests that never settle. Existing unverified process-cached devices are not trusted. Rejected JavaScript adapter Promises are converted to structured probe failures because the Python/WebSocket bridge otherwise logs the rejection without returning it to Python. Fields show actual sampled surface values; axisymmetric fields are revolved into 3D. The fallback clearly disables WebGPU-only picking, clipping and interior overlays. The numerical solver and dependencies were not changed, and the CPU-only 2D field viewer remains removed. Focused browser E2E tests passed for a missing WebGPU interface, a present API with no adapter, and a deliberately stalled adapter. Native packaged tests then confirmed automatic fallback on this VM (the API exists, but both high-performance and low-power adapter requests return no adapter), as well as fallback when the API is absent. Both runs rendered a small computed 3D field and cleaned up their application/browser processes. Full-resolution evidence is in the [WebGL fallback report](reviews/2026-11-04-webgl-fallback.md).

#### Phase E2. Reduced-order modelling integration (complete for the supported mechanical modal subset; 5 November 2026)

The existing mechanical modal method is now configured in the shared Study/property workflow. 3D harmonic mechanics-only and fully coupled studies can use SciPy or PINVIT modes and optional static load/boundary enrichment; the axisymmetric editor offers modal ROM only when harmonic EM and mechanics are both active, and correctly forces its 3D-only enrichment option off. Unsupported axisymmetric mechanics-only and electromagnetic ROM combinations are rejected or unavailable. Coupled ROM reduces only the mechanical block; DC and harmonic electromagnetic systems remain full order. The project stores method and settings; run manifests and history record mode counts, eigensolver, basis setup, online harmonic solve, DOFs, enrichment and PINVIT diagnostics. A basis is built for the current inputs at solve time and reused through the frequency points of that solve. No offline ROM artifact is saved or reused, so there is no persisted basis that can become stale; a new run rebuilds from the current model and mesh.

Same-mesh reference comparisons passed: on a 12-tetrahedron 3D mechanical block, relative displacement error fell from `5.869e-2` at 6 modes to `1.464e-2` at 13 modes at 50 and 100 Hz; on a 79-tetrahedron coupled block, the 6-to-13-mode displacement error fell from `8.968e-3` to `2.937e-3`, while both EM field differences stayed below the `1e-4` fixed-point tolerance and coupling converged in two iterations. A 302-element axisymmetric coupled comparison reduced relative displacement error from `3.093e-1` to `5.576e-2` to `1.199e-2` for 8/24/40 modes. On these small examples, measured wall times varied and did not establish a general speedup; see the [Phase E2 report](reviews/2026-11-05-phase-e2.md) for timings, screenshots, test evidence and limitations.

The maximized packaged Windows app (`0.2.8.dev303`) completed a genuine 3D ROM run and exposed the axisymmetric ROM controls. Native UI automation saved the axisymmetric configuration to JSON, reopened that file through File > Open in a fresh app process, and read the selected ROM method and mode count back from the property editor. The 450-test focused GUI suite and seven focused ROM numerical tests passed. E1 frequency-sweep references and regression tests remain in the repositories. The package launcher points at the tested E2 build. The 2D field viewer remains removed; the WebGPU-to-WebGL fallback remains in the product.

**Scope boundary:** The underlying implementation is a mechanical modal reduction, not a general electromagnetic or parametric ROM. It has no offline training-frequency selection, saved reusable basis, ROM-file validity fingerprint, or cross-run model cache. The current interface does not imply those capabilities. Any future reusable/offline ROM work must add input fingerprints and held-out range verification before it exposes loading, reuse or stale-model controls.

#### Phase E3. Advanced numerical configuration and ROM performance assessment (complete for tested scope; 5 November 2026)

The 3D Study editor now separates finite-element approximation order from curved mesh geometry order. Geometry order can explicitly follow the FE order (preserving legacy project behavior) or be selected from 1–6. Mechanics-only and coupled studies expose Rayleigh mass- and stiffness-proportional damping with correct units. Coupled studies expose Anderson depth (0 disables acceleration) and relaxation alongside the existing iteration limit and convergence tolerance. Values are range-checked, serialized in existing `.mmfem3d` projects, propagated to mesh/solver settings and recorded in run metadata. DC cache fingerprints now include geometry order because it changes the mesh and H(div) coefficient space; damping and Anderson controls do not invalidate an unrelated DC field. The axisymmetric mesh and numerical workflow remains specialized and unchanged.

The numerical capability inventory found that 3D FE order (p=1–4), per-domain target mesh size, sweep configuration, coupled convergence settings and ROM/PINVIT settings were already available. Axisymmetric order (p=1–6), independent curved order, hp layers and its existing solver controls remain available in its mature editor. The 3D adapter selects formulation-specific FE spaces and direct sparse solve policy internally; lower-level iterative/preconditioner code is not a selectable, verified end-to-end GUI path. Static-condensation policy and quadrature are fixed by formulation. Automatic adaptive h/p refinement, user-selectable 3D mesh-quality algorithms and CPU-thread controls are not available in the packaged 3D workflow. These internal options were not exposed speculatively. See the Phase E3 report for the complete matrix and source references.

Real NGSolve response checks verified that custom geometry order 4 improved a curved sphere's computed volume from `4.74507572e-7 m³` at p1 geometry to `5.23601691e-7 m³`, against the analytic `5.23598776e-7 m³`, and that nonzero Rayleigh stiffness damping reduced a supported small-block displacement norm to `0.954028` of its undamped value. A bounded coupled regression exercised nondefault Anderson depth and recorded the actual solver-returned settings. The 166-test GUI/project/solver setup group passed with one existing `PytestAssertRewriteWarning`; 11 DC-runner and axisymmetric ROM regression tests passed.

The bounded ROM measurement used 378 tetrahedra, 130 vertices, 315 free mechanical DOFs, six modes and three frequencies. Full-order solve wall time was `0.867 s`; ROM runs took `0.338 s` first and `0.581 s` on repeat, with basis construction `0.049/0.037 s` and online harmonic solve `0.142 s`. Six-mode displacement error was approximately `5.54%` across 50–150 Hz. Process RSS was `176.9 MB` full order and `183.0–183.1 MB` with ROM; these are whole-process measurements, not basis-only memory. At this size, basis construction is under 50 ms and run timings vary, so the evidence does not justify a persistent ROM artifact or establish a general speedup/break-even point. Keep per-run basis construction and within-sweep reuse. Reconsider persistent bases only if repeated larger-model studies show material setup cost, with exact stiffness/mass/constraint fingerprints and held-out frequency/parameter verification.

Packaged desktop tests verified these controls in the maximized `0.2.8.dev305` application, save and fresh-process reopen, and the newest-app launcher. Full-desktop screenshots and test logs are in the [Phase E3 review package](reviews/2026-11-05-phase-e3.md). At E3 closeout, one cold first frame was blank after the splash had been dismissed; the underlying launcher only required the window to be maximized and could not prove that Windows had foregrounded or painted its contents. Phase F1 addresses that exact handoff, rather than attributing the improvement to the SSD move. No numerical formulation or dependency change is part of F1, and the then-separate Mike variant was not changed in E3; Phase F2 now retires it. The retired CPU-only 2D field viewer remains removed.

**Gate:** Accepted for the verified configuration and ROM-assessment scope; persistent/offline ROM remains deferred unless representative repeated-model benchmarks show a worthwhile benefit.

### Phase F. Axisymmetric and 3D UX unification, without loss of specialist capabilities

**Outcome:** One coherent MagMechSim design language, with specialised workflows that belong in each formulation.

- Reuse the application shell, command taxonomy, property and validation patterns, materials and job management where appropriate.
- Preserve axisymmetric sketching, arcs, lines, region behaviour, hp-layer controls and dedicated numerical semantics.
- Remove duplicated infrastructure only where justified by maintainability or user benefit; avoid large numerical code migrations for visual uniformity alone.

#### Phase F1. Cross-workspace cohesion and startup reliability (F1.1 plus startup follow-up verified for tested Windows VM; 6 November 2026)

- Keep one shared application shell and command hierarchy while retaining distinct axisymmetric sketch and OCC CAD toolbars.
- Compact the shared top navigation modestly; make long 3D assignment labels discoverable in the model tree; preserve contextual menus, quick actions and Model/Results navigation.
- Keep Chromium's first native paint at a normal launch size, then maximize and independently verify the full native client before dismissing the splash. MagMechSim's dedicated Chromium window also disables `CalculateNativeWinOcclusion`, which controlled A/B runs identified as the cause of delayed/blank native surfaces on this VM. No fixed wait was added.
- Exercise actual native input in both workflows: recent-project tiles, 3D geometry/property navigation, axisymmetric sketch selection and its 3D geometry-creation preview. Capture maximized full-desktop screenshots.
- Escape now closes the active axisymmetric 3D geometry preview before acting on the underlying sketch tool.
- Preserve all specialist geometry, mesh and solver paths; avoid numerical refactoring. F1.1 changed native Windows browser-launch arguments, build metadata and startup-focused tests; at that milestone the separate Mike/MM-FEM path did not receive the Chromium flag. Phase F2 retires that path.

**F1.1 verification:** Before the first-paint fix, the packaged browser DOM mounted while the native HWND remained white/unpainted in repeated native captures; six captures spanning 0.15–12 s after window restoration had zero pixels above the native paint threshold. In controlled A/B runs, disabling only Chromium `CalculateNativeWinOcclusion` produced visible restored frames in 0.047–0.079 s and maximized frames in 0.125–0.172 s. The package passed independent 3D and axisymmetric workflow launches at 1920×1094, including recent-project opening, native selection/navigation, axisymmetric 3D preview/Escape and maximized screenshots. Three fresh public-launcher runs passed the native first-paint/maximize gates, mounted the home page and cleared the loading overlay. These SSD-hosted VM timings are not controlled comparisons against the earlier storage environment. The 32 focused tests passed; no solver run was made. This records the F1.1 result; its earlier stop-for-review instruction was superseded when Phase F2 began.

**Startup and branding follow-up (6 November 2026):** A foreground activation refusal no longer produces the user-facing “did not confirm its maximized app window was foregrounded” error. Foreground activation is logged as best-effort; the launch gate instead requires a visible, maximized application window with native pixels proving its contents painted. The app window is raised over the centered splash before this check, and the splash is withdrawn/destroyed immediately after paint. The centered LORENTZ splash now shows the requested gentle light sweep across only the wordmark. The current full application and public launcher are `0.2.8.dev329` (SHA-256 `3c124b60a45f181ac2889437b363a6008bbda5b85abfb22875131b43f4662d10`). At 1920×1094, actual full-desktop frames observed the wordmark animation; the packaged smoke measured 2,913 changed pixels during one 240 ms interval, confirmed native first paint with 7,192 visible pixels, and dismissed the splash 0.039 s later. Launcher-to-home took 23.5 s on this run; it is a single loaded-machine observation, not a benchmark. The focused native tests passed (21 startup tests plus the isolated-process cleanup test); the fresh public-launcher smoke passed, and no test-owned processes remained. The exact foreground-warning text is absent from active native startup code. This does not disguise genuine failures to locate or paint the application. The VM is SSD-hosted; performance cannot be attributed to code versus storage or system load. See the [startup follow-up report](reviews/2026-11-06-lorentz-startup-follow-up.md) and review ZIP. No solver algorithm, dependency or numerical configuration changed. Stop for independent review before Phase G.

#### Phase F2. Engineering workflow refinement and legacy variant retirement (complete for tested scope; 6 November 2026)

- Retire the stripped standalone Mike edition while preserving shared NGSolve/Netgen functionality, solver interfaces, MagMechSim features, project schemas and stable user-data paths. Move obsolete package/source artifacts to the existing `C:\Users\Yash\Documents\deletable` directory and record their origins in a manifest; do not permanently delete uncertain material.
- Verify no current launcher, builder, package workflow, CI job or active test depends on the retired edition. Distinguish product-specific branches from retained `MM-FEM` project schemas, kernel IDs and compatibility paths.
- Exercise representative packaged axisymmetric and 3D workflows with native input, full-desktop captures, save/reopen, geometry selection, meshing and bounded solver cases. Use the two shared workspaces without flattening their specialist tools.
- Clarify run-history status so a process exit is not shown as a successful scientific result. Preserve manifests and show execution completion together with convergence/partial/unavailable numerical outcome.
- Make targeted command, property, validation and helper-text refinements only where packaged interaction exposes a concrete usability issue.

**Mike retirement:** The stripped package (42,907 files; 2,082,185,938 bytes) and Mike-only source/build assets were moved to the existing `C:\Users\Yash\Documents\deletable\MagMechSim-PhaseF2-Legacy-Mike` directory with a manifest. No files were permanently deleted. Tracked active Python, launcher, build, workflow and configuration files contain no Mike-only path or entry point; the only name-specific current artifact is the retirement manifest and historical roadmap/review context. Stable project schemas, kernel IDs, storage paths and shared numerical functionality remain. No solver algorithm or dependency changed.

**Workflow corrections:** The 3D geometry editor now visibly disables the Run study command when material assignments make the model invalid. Run history pairs process completion with the numerical outcome and retains frequency, iteration and residual diagnostics. Older manifests without a scientific result are labelled `Execution finished · numerical status unavailable`; nonconverged cases are never shown as converged. The axisymmetric runner now writes per-frequency convergence, residuals, tolerances, iteration limits and mesh metadata from the solver's returned values. The application applies the same status vocabulary to saved and live axisymmetric history. Escape closes the axisymmetric 3D geometry preview without changing the sketch.

**Packaged Windows verification:** The latest package `0.2.8.dev319` was built from GUI commit `36a14459927077ed4d39dfa2261256c8a9b479cb` and solver/package commit `5b4dfbc623a43682c3b16d0d63300a34db6c81ff` (EXE SHA-256 `c3218cf13a021b7e78697cc198252bd1458b093b58b5c992424bc174ffe9e9af`). Full-desktop screenshots are 1920×1094; the MagMechSim HWND was maximized and foregrounded for each packaged workflow. The native harness used Win32 mouse/keyboard input, Pillow/Win32 desktop capture, and test-only loopback Playwright CDP for DOM/state readback; these are packaged-desktop checks, not browser-only UI tests. No Codex Computer Use was used.

- The 3D coupled packaged workflow completed on a 79-tetrahedron mesh. The first run converged in 2 iterations (ΔA `2.34e-11`, Δu `7.51e-7`, tolerance `1e-4`) and computed a DC background field. A second run with a strict 2-iteration limit was recorded as nonconverged (Δu `1.50e-6`, tolerance `1e-8`) and reused the matching DC field. Save/reopen restored physics, source, DC field, frequency and coupling settings. The results navigator showed both `50 Hz: converged` and `Execution finished · converged` in the same visible history row. No larger 3D solver model was submitted.
- The axisymmetric example completed 250 Hz and 500 Hz on a 579-element / 2,410-DOF mesh. Both frequencies converged in 3 and 2 iterations respectively at configured tolerance `1e-6`; actual residuals were persisted to the manifest and visible in run history. Solver wall time was 1.53 s. The project snapshot and run outputs persisted.
- A fresh 3D and axisymmetric application workflow passed. Native selection of the air region updated selection state, then the axisymmetric geometry-creation preview rendered two WebGL `mesh3d` traces (1,468 vertices total), with no lost WebGL context; native screenshots contained 4,226 contrast pixels in the measured sample. Escape dismissed the preview without changing geometry. The test explicitly gates on the painted native preview, not only on Plotly data. One prior capture in this VM was blank; subsequent packaged runs passed and the cause is not established.
- `Launch-Latest-MagMechSim.cmd` opened the same EXE, cleared the startup overlay, reached the mounted workspace, and maximized to the 1920×1094 desktop. Launcher-to-home was 12.67 s; two fresh D2 workflow launches reached native readiness in 10.52 s and 8.82 s. These are SSD-hosted VM observations, not controlled before/after performance comparisons.
- Each packaged run cleaned its isolated app/browser processes. WebGPU API presence did not yield an adapter or device in the VirtualBox guest; the validated geometry-creation 3D preview uses WebGL fallback. Physical Windows GPU behavior remains a release-stage check.

**Focused checks:** `coupled_solver/tests/test_run_gui_model.py`: 7 passed. GUI run-history, navigator and targeted workspace regressions: 19 passed, 254 deselected. The packaged Phase D2 workflow passed with two solve outcomes and fresh-process persistence; the Phase F1 workflow passed on the current package with 15 full-desktop screenshots; the packaged axisymmetric Phase F2 workflow passed twice with fresh launches; and the public launcher smoke passed. The full historical suite was not rerun. Logs, JSON reports and selected actual screenshots are included in the review ZIP.

**Gate:** Phase F2 is complete for this tested scope and ready for independent review. Do not begin Phase G until the review is accepted. Known limits are the one unreproduced blank preview capture, physical-GPU verification, broad predictive validation beyond the bounded coupled reference, and no full post-processing redesign. No large 3D simulation was run.

### Phase G. Sphinx documentation, in-app help and documentation search

**Outcome:** Accurate, offline-capable, version-matched product documentation with fast contextual discovery.

- Keep step-by-step LORENTZ GUI workflows as the primary documentation; retain Python/API, research notebooks and solver-internal material in a clearly labelled advanced reference. Describe only controls and numerical capabilities verified in the application.
- Build Sphinx from saved notebook outputs without executing numerical notebooks. Package the site and its dependencies locally, identify the product version in the site and build manifest, and verify local links, heading anchors, theme assets, scripts and mathematical fonts with network access blocked.
- Verify **Help → Documentation** opens the bundled site from both axisymmetric and 3D workspaces in the maximized packaged Windows application. Preserve the installed side-by-side package; update the canonical `Launch-Latest-LORENTZ.cmd` only after testing the replacement package.
- Capture full-desktop screenshots and record package hashes, source revisions, test results and launch target in the review ZIP. Do not search, restore, archive, move or delete historical Mike artifacts as part of this documentation milestone.
- **Phase G2, deferred until G1 review:** add the upper-right documentation search; rank actual indexed sections, show up to **five results** with title and useful excerpt, support keyboard navigation and deep links to local Sphinx heading anchors, and measure startup impact. Do not fabricate results or hard-code examples.

#### Phase G1. Accurate, version-matched offline engineering documentation (in progress; 7 November 2026)

The local Help integration already exists. G1 audits the current application workflows, expands the user guide, moves Python/API and research material under a clearly separated advanced reference, bundles all runtime documentation assets, proves the site is version-matched and offline-capable, and verifies Help through actual maximized packaged-app workspaces. This phase does not implement contextual search or new solver/UI features.

**Acceptance:** Strict Sphinx build succeeds; generated links and static assets resolve locally; MathJax equations load local font assets with external network access blocked; Help opens the matching version in packaged axisymmetric and 3D applications; final package, canonical launcher, screenshots, test evidence, roadmap and ZIP agree on the same build provenance.

#### Phase G2. Contextual documentation search (planned after G1 review)

Implement the existing upper-right search request against actual generated Sphinx sections, with up to five ranked results, useful excerpts, keyboard interaction, and local heading-anchor navigation. Keep search offline, fast and version-matched. G2 starts only after independent review of G1.

**Gate:** Search results reflect the packaged site, open the correct local section, and do not add material startup or interaction delays.

### Phase H. Release-quality performance, robustness and packaging

**Outcome:** A stable Windows product ready for sustained engineering use.

- Test native splash centring, drag, seamless transition, maximised main window, taskbar icons and Windows DPI/multi-monitor behaviour; compare production GPU vs SwiftShader when possible.
- Stabilise core CI. Run comprehensive suite at planned integration/release gates, classify known legacy failures, and preserve numerical regression coverage. Validate larger representative engineering models with bounded resource use.
- Examine memory growth, long sessions, save/reopen/migration, malformed geometry and inputs, cancellation and fault handling, help/docs packaging and startup speed.
- Produce versioned releases, traceable source commits, acceptance evidence and installation/upgrading instructions. Avoid changing current project formats destructively.

**Gate:** Documented release acceptance tests pass; unresolved limitations are disclosed; source, executable and version are traceable.

## 5. Practical sequencing and dependencies

```text
Phase A: accepted complete for tested workloads; historical Chromium memory risk monitored
    |
Phase B1: geometry organisation/materials/safe preparation — complete
    |
Phase B2: supported 3D boundary conditions, sources and loads — complete
    |
Phase C: 3D DC background-field study — complete for tested scope
                      |
                      v
Phase D1: 3D coupled solver integration — accepted for tested scope
                      |
                      v
Phase D2: coupled workflow and verification refinement — accepted for tested scope
                      |
                      v
            Frequency sweeps and study management (Phase E1 — complete)
                      |
                      v
            Mechanical modal ROM workflow (Phase E2 — complete for verified scope)
                      |
                      v
            Advanced numerical settings and ROM assessment (Phase E3 — complete for tested scope)
                      |
                      v
            Cross-workspace cohesion and startup reliability (Phase F1/F1.1 — accepted for tested scope)
                      |
                      v
            Engineering workflow refinement and legacy variant retirement (Phase F2 — complete for tested scope)
                      |
                      v
        Version-matched offline user documentation (Phase G1 — active)
                      |
                      v
             Contextual Help search (Phase G2 — after G1 review)

Local Help already exists; G1 aligns its complete contents and runtime assets with the packaged product. G2 search is deferred until G1 is reviewed.
Release, packaging and comprehensive quality gates occur throughout, with final signoff in Phase H.
```

**Change control:** When a dependency or architecture finding changes this sequence, Codex should update the roadmap's status/risks and explain the change in its milestone summary. Do not hold up useful 3D work on unrelated legacy post-processing failures.

## 6. Standing engineering quality gates (every milestone)

1. **Correctness:** UI settings actually reach the solver; numerical responses and constraints remain correct; no unsupported settings are presented as functional.
2. **Usability:** Engineering terminology; predictable navigation; minimal filler text; no accidental loss of work; selection and state are always visible.
3. **Responsiveness:** Profile regressions in affected interaction paths; keep long tasks nonblocking; compare timings under controlled conditions and identify any VM limits.
4. **Reliability:** Save/reopen, cancel/fail, invalid data and geometry-change paths exercised where affected. Preserve stable CAD identity and fail-safe recovery.
5. **Testing efficiency:** Targeted tests while editing; related end-to-end/numerical tests at milestone boundary; full suite at planned integration/release gates. Never weaken tests to hide failures.
6. **Git discipline:** Protect existing work, logically commit and push verified changes, and report branch/SHAs, clean/dirty working tree and blockers.
7. **Native review evidence:** Build a Windows executable from the current reviewed source, launch it and capture the real native app; record exact revisions, version and executable hash. The tested executable must be the newest source version and open maximized. Capture screenshots at the full native display resolution (at least a normal 1920×1080-class screen), never in a smaller restored window. Do this once at a milestone gate, not after every trivial code change. Preserve a currently running side-by-side installation.
8. **Native input verification:** Do not make a milestone depend on Codex Computer Use availability. The Windows packaged-app harness uses Win32 mouse/keyboard/wheel input and Pillow full-desktop screenshots, with Playwright over test-only loopback CDP for read-only DOM checks and timing. Test actual controls and visible state transitions, including geometry selection and viewport response. Keep browser-only Playwright results clearly separate from genuine packaged-desktop checks. Do not bypass Computer Use security checks or any mandatory stop instruction; use the independent desktop harness through normal development tools instead. Document the exact input and screenshot methods in the handoff.
9. **Latest executable:** Keep `Launch-Latest-LORENTZ.cmd` pointed at the newest tested full LORENTZ package. There is no `Launch-Latest-MagMechSim.cmd` compatibility launcher in the current Development directory; do not claim that it exists. Execute the canonical launcher itself in a fresh profile; verify the real app reaches its mounted workspace, the “Downloading App” overlay clears, and Win32 confirms the main window is maximized on the full native display. Record the executable SHA-256, launcher target and exact GUI/solver source revisions. Include the launcher smoke log and full-desktop screenshot in the review ZIP. LORENTZ is the sole maintained application; preserve compatible legacy IDs and storage paths only where required for users and project files.
10. **Handoff:** Concise report, actual full-resolution screenshots, focused test evidence, performance observations, outstanding issues, next recommendation and a ZIP. Record that the VM moved to SSD and do not attribute timing changes to code without a controlled same-hardware comparison. Never claim native rendering or splash behaviour from a browser screenshot alone. Do not use or package the retired CPU-only 2D field viewer. When WebGPU is unavailable, include actual packaged adapter/device/render diagnostics and the supported path to a 3D-capable graphics adapter; do not represent a 2D view as equivalent to 3D rendering.

## 7. Risk register

| Risk | Why it matters | Mitigation |
|---|---|---|
| 3D face selection and renderer stability | Phase A's repeated native picking/recovery passed on a capped 22,132-element mesh. Phase B1 additionally verified a visible orange solid outline on a two-body 24-triangle preview; the historical OOM did not recur | Keep the Chromium memory concern monitored; include larger representative models and long-session memory in a later targeted gate. It does not block B2 |
| Geometry identity ambiguity | Can silently change loads or supports | Continue safe invalidation, user-guided reassignment and verification |
| Coupled 3D harmonic workflow | D2 passed bounded 79-element packaged, coupling-isolation, analytical mechanics and manufactured p/h checks; broad practical predictive evidence remains limited | Retain exact mesh/input checks and distinct convergence status; do not claim accuracy beyond bounded references; extend verification as Phase E adds workflows |
| WebGPU adapter absent or stalled in VirtualBox guest | `navigator.gpu` can exist without a usable adapter, and driver requests can stall. Geometry, mesh and field results fall back to interactive 3D Plotly/WebGL surfaces after an adapter, device or verified canvas probe fails or reaches its five-second bound. Native automatic fallback is verified on this VM. WebGL remains required; GPU-only selection, clipping and interior field overlays are unavailable in fallback mode | Keep the retired CPU-only 2D field viewer removed. Retain tests for missing API, missing adapter, failed/inconclusive probes and timeouts. Monitor both WebGPU and WebGL rendering on supported physical hardware; do not substitute a flat 2D view. |
| Axisymmetric display and geometry-tool regressions | The saved sketch and full-pane grid now render on first open at maximized and restored sizes; the earlier blank capture did not recur. Tangent-arc tool behavior and physical-GPU rendering were not specifically revalidated in this closeout | Retain first-open/grid checks and targeted geometry-tool regression coverage; investigate if a blank frame or geometry-editing defect recurs |
| Menus expose irrelevant commands | Confusing and potentially hazardous workflow | Context-aware command taxonomy and capability checks |
| Axisymmetric sketch/preview rendering | Saved regions, full-pane grid and the geometry-creation Plotly WebGL preview are visible in the packaged app; physical-GPU behavior is unverified | Keep representative sketch/preview regression coverage; reopen if blank frames or fidelity issues recur |
| Reload after a renderer crash can leave a blank window | Controlled recovery now restores styles, project state and a rendered viewport | Retain one-attempt recovery guard and packaged crash regression; revisit if broader crash reports occur |
| Startup and first-frame reliability | The no-flag blank/delayed native frame was reproduced on the Windows VM and controlled A/B testing tied it to Chromium native-window occlusion. The final package passed five fresh launches across full workflows and the public launcher. | Keep the splash/native-pixel gates and MagMechSim-only occlusion workaround. Verify on physical Windows graphics and other Chromium/driver versions when available; monitor resource use while the app is occluded/minimized. Do not attribute timing changes to the SSD move. |
| Documentation can drift from the running executable or request online assets | Users may follow controls that moved or fail to read help offline | Build docs using the committed package version, record source revisions and hashes, audit local links/assets, and exercise Help in the packaged GUI with network access blocked |
| Broad CI has known failures | Can conceal regressions and waste iteration time | Keep focused gates, track known failures, schedule comprehensive checks |
| Frequent rebuilds and tests | Slows development | Build/package once per milestone; use risk-based, cached focused tests |

## 8. Roadmap governance

Suggested repository home for this plan: a documented planning location under the GUI repository, chosen by Codex after inspecting existing conventions. Keep this file as the **single current roadmap**, with date, completed milestones, changed priorities and links to implementation reports. Historical roadmaps may remain archived but should not conflict with the current one.

**Current action (7 November 2026):** Phase F2 and the LORENTZ branding/startup handoff are accepted for their documented scopes. Complete Phase G1's actual packaged Help checks, build a new version-matched package from committed sources, update `Launch-Latest-LORENTZ.cmd`, then capture and package the evidence. Keep the existing installation untouched. Do not start G2 search until G1 is independently reviewed. Preserve the `CalculateNativeWinOcclusion` workaround and native first-paint gates; physical Windows/driver verification remains a later release check. The SSD move remains a performance confounder, not a code improvement. Persistent/offline ROM remains deferred; retain the CPU-only 2D viewer removal and WebGPU-to-WebGL fallback.
