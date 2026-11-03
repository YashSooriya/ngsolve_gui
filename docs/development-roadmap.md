# MagMechSim: Forward Development Roadmap

**Planning baseline:** 3 November 2026 (updated after Phase B2)
**Status:** Living plan, not an immutable implementation contract  
**Primary users:** Engineers familiar with industrial CAE tools  
**Primary immediate scope:** Axisymmetric and 3D time-harmonic model preparation and execution, with 3D development prioritised  
**Out of scope for now:** New results visualisation or a post-processing redesign (but existing Solve/Results navigation must remain fully functional)

## 1. Product objective and working agreement

MagMechSim should provide a complete, reliable and responsive workflow from geometry creation/STEP import to supported electromagnetic, mechanical and coupled FEM studies. Engineers should recognise the organisation and interaction patterns from industrial CAE applications without needing to learn a fundamentally new interface.

- **Codex owns repository-level architecture and implementation choices.** This document states goals, dependencies, behaviours and acceptance criteria. Inspect the actual code and use sound judgement rather than treating example layouts or implementation suggestions as mandates.
- **Preserve working numerical and specialised functionality.** In particular, the axisymmetric drawing/geometry workflow, project loading, STEP and coil-trace assets, materials, meshing and solver paths must not regress. Preserve project compatibility and do not alter the separate Mike variant unintentionally.
- **UI controls must be genuine.** Do not present unsupported physics, algorithms or conditions as operational.
- **Performance, quality and Git hygiene are permanent requirements.** Use fast relevant tests while iterating; milestone-level integration tests; comprehensive tests at appropriate gates. Make focused commits and push verified work. Provide honest reports of skipped or failing checks.
- **One coherent milestone at a time.** Keep this roadmap current when discoveries change the order. Do not implement every section in one Codex turn.

## 2. Current verified baseline and risks

The bullets below retain historical baseline evidence where useful. The accepted Phase A closeout and completed Phase B1/B2 statuses below supersede earlier open/blank/recovery findings.

**Latest verified status (3 November 2026):** Phase A was accepted complete for its tested workloads. Phase B1 completed bounded 3D geometry organisation, materials and safe CAD recovery. Phase B2 now completes the tested supported 3D single-physics source, boundary-condition and load workflows. The maximized packaged app passed native CAD picking, save/reopen assignment checks, and renderer recovery; small real NGSolve EM and mechanics cases passed. The Phase A, B1 and B2 reports are [`docs/reviews/2026-11-03-phase-a-closeout.md`](reviews/2026-11-03-phase-a-closeout.md), [`docs/reviews/2026-11-03-phase-b1.md`](reviews/2026-11-03-phase-b1.md), and [`docs/reviews/2026-11-03-phase-b2.md`](reviews/2026-11-03-phase-b2.md). B2's report links to its review ZIP and full-resolution desktop screenshots.

- The saved `Getting started - coupled coil` project rendered its square and circle regions in the first geometry-editor screenshot. The sketch grid filled the viewport at maximized and restored sizes with correct physical scaling. The earlier single blank capture was not reproduced; its cause remains unknown.
- The axisymmetric geometry-creation 3D preview rendered both regions using Plotly WebGL. 3D STEP import, a 22,132-element bounded mesh, repeated native CAD selection, display switching, rotation and selection persistence all passed. The historical OOM did not reproduce; the test covered only three bounded cycles, so larger and longer sessions remain a risk.
- A controlled renderer crash recovered in 16.5 s with one automatic attempt, preserved the saved model and selection, and restored a visible maximized viewport. Recovery now re-applies design-system and app styles that were lost with the crashed document.
- The VM had moved to an SSD before these checks. Latest splash-to-ready times were 9.1 s axisymmetric and 7.3 s 3D; there is no controlled before/after comparison.
- At Phase A closeout, no numerical run or solver-algorithm change occurred. Phase B1 later ran only bounded 3D EM/mechanics cases to verify model-to-solver propagation; no numerical algorithm or dependency changed.
- Phase B2 ran actual NGSolve checks on 10 mm STEP blocks. Doubling a scoped domain-current source produced a magnetic-field proportionality error below `1e-5`; the 50 Hz mechanics case checked phasor body-force and prescribed-displacement loading plus fixed and spring supports. The 22,132-element STEP model used for desktop verification was restricted to preprocessing, selection and persistence checks and was not submitted to the solver.
- Phase B2 desktop evidence was captured at full 1920×1094 resolution with MagMechSim maximized. The packaged checks exercised the axisymmetric editor and Plotly WebGL geometry preview, 3D STEP import/mesh, native face selection, condition editing, save/reopen and controlled renderer recovery. Isolated app processes were cleaned up.

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
- Remove the **2D field viewer** shortcut from the top right. Do not delete an otherwise working tool; relocate it to an appropriate contextual command (for example Results or View) if it should still be accessible.
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

- The shared application shell now presents consistent Model/Results navigation, shared file actions, context-aware menus, local documentation help, and the relocated CPU field viewer. Axisymmetric sketching and the existing 3D STEP/wire-trace workflow remain in place.
- Tangent-arc eligibility, mechanics-only harmonic frequencies, dismissal of safe overlays, and alpha-clean application icons have targeted regression coverage.
- Focused GUI unit checks pass (**341 passed**); axisymmetric geometry 3D preview and repeated small axisymmetric solver workflow pass; the 22,132-tetrahedron 3D mesh/selection workflow passes without running a 3D solver. The complete Sphinx HTML build succeeds offline. Four Windows launcher tests also verify MagMechSim maximization, delayed window-manager confirmation, Mike compatibility, and explicit failure if maximization cannot be confirmed.
- Fresh 3D selection profiling still reports a **2.49 s median** click-to-editor time, while Python tree/property rebuilds are approximately **10 ms / 7 ms**. The residual latency is outside those measured methods and needs browser/Plotly/transport profiling in Phase A. The VM's move to SSD means comparisons with the earlier measurements are not controlled.
- A post-restart review found that `--start-maximized` alone did not reliably restore an existing Chrome app-profile window. The MagMechSim launcher now locates the browser process for its isolated install profile, requests maximization after the interface mounts, checks the Windows maximized state, and reports a clear startup error if that state cannot be confirmed. The Mike (`MM-FEM`) branch retains its previous window-restoration behavior.
- At the time this earlier UI-coherence review was recorded, the packaged axisymmetric geometry-creation preview appeared blank in the available capture. The native desktop verification in the later Phase A update below supersedes that observation: the preview is visible using Plotly WebGL on this VM. Its preview is no longer a WebGPU dependency; the main 3D STEP renderer's WebGPU/driver path is separate.
- This earlier review depended on Codex Computer Use for interaction/capture. The reusable Win32 and desktop-capture harness described below now verifies the installed package independently; Computer Use availability is no longer a gate for future GUI milestones.
- No numerical solver algorithms were changed. The VM's move to SSD is a performance confound; the prior 16.3 s startup and 2.49 s selection timing are not controlled same-hardware comparisons. The review package records the final executable identity, logs, screenshots, performance observations, and remaining preview limitation.

## 4. Roadmap phases and current status

These stages are **directional**, with dependencies and scopes to be refined by Codex after inspection. They need not be exactly one Codex prompt each.

### Phase A. 3D interactive performance and preprocessing refinement (closeout complete; residual risk carried forward)

**Outcome:** CAD picking, selection highlighting, property editing and model navigation feel immediate on realistic engineering geometry.

- Profile the complete user interaction pipeline, including browser rendering, Plotly redraws, WebSocket communication, ngapp state updates, NGSolve/OCC work, logging and panel changes.
- Eliminate unnecessary full redraws and heavy recomputations on selection, reuse costly geometry/mesh data, and keep UI updates responsive. Re-evaluate renderer choices only on proven benefits, not WebGPU fashion.
- For the axisymmetric geometry-creation 3D preview, detect WebGPU adapter availability before rendering. Provide a supported WebGL preview fallback when WebGPU is unavailable and a clear reduced-preview state when neither renderer is available. Validate geometry fidelity, selection-independent navigation, resize behaviour and actual packaged desktop output; the CPU 2D results viewer does not cover this workflow.
- Benchmark repeatable selection p50/p95, viewport interaction, panel switching, geometry loading and startup on representative models and **unchanged hardware**. Separate VM/SwiftShader limits from production hardware. Avoid claiming speedups after storage/hardware changes without a controlled baseline.
- Aim for near-immediate common interactions. Set numerical latency budgets after obtaining reproducible hardware conditions; do not trade correctness for a number.

**Gate:** Repeatable profiling identifies bottlenecks, representative interactions improve measurably, no CAD assignment/solver regression.

**Milestone status (2 November 2026 — rendering and responsiveness pass): Partial; keep Phase A open.** The implementation is in GUI commit `25450a9476197de9244024c20391c81a7dc53f8b`; the Windows package was built from that GUI revision and solver/release commit `b27702905a9db63edca0d12439cfa89a15bb7d5a`.

The following findings are the initial status at that milestone, before the packaged desktop verification recorded in the next subsection. Later native checks supersede the earlier statements about preview visibility and whether a physical CAD pick can update the property editor.

- The axisymmetric geometry-creation preview now uses a bounded OCC tessellation rendered with Plotly WebGL. It shows the revolved region with physical XYZ proportions, feature edges and per-region visibility. A clear message is shown if WebGL is unavailable. The old WebGPU renderer remains in the main STEP and results workspaces; the axisymmetric preview itself now uses WebGL even on WebGPU-capable systems. The VirtualBox adapter reports GPU/WebGPU unavailable, so the fallback was verified in the packaged app, but physical-GPU behavior has not been checked.
- In the maximized packaged application, a drawn annulus with a bore rendered, rotated, zoomed and returned to the editable sketch. Ten wheel events did not make the sketch grid disappear, and the measured sketch dimensions remained unchanged. A saved axisymmetric project was reopened with its geometry intact. This grid issue did not reproduce, so no forced redraw was added.
- 3D selection handling now avoids rebuilding the model tree when a face pick leaves the selected tree item unchanged, and boundary-overlay edge data is cached. On the same post-SSD VM and 22,132-element test mesh, the Playwright profile's five repeated event-to-editor samples improved from median `2734.37 ms` / p95 `3286.34 ms` to median `443.98 ms` / p95 `474.17 ms`. The first/cold after sample was `1914.59 ms`. These events are emitted programmatically to measure the event-to-editor path; they do not measure real pointer hit-testing. Do not treat them as proof that native CAD picking is correct or near-immediate.
- The actual packaged 3D workspace imported the STEP fixture and generated/rendered a coarse 100 mm mesh (`22,132` volume elements, `14,588` surface triangles). Earlier direct native clicks did not visibly create a boundary-region assignment; a later pointer move produced Chrome's `Aw, Snap! Out of Memory` page. An earlier, denser 30 mm mesh (`105,523` volume elements, `69,978` surface triangles) also preceded a renderer OOM after repeated plot interactions. Mesh generation succeeded; no solver was run. These OOM events remain unresolved, although a subsequent native pick and persistence check succeeded on the coarse mesh.
- The packaged executable is `0.2.8.dev216`, SHA-256 `da3e597dd12cbf0c0c541c80b23ed68489e392bfdf4919bbca9de6a7d39c2a95`. The latest launcher opened the maximized app. After the renderer crash, a same-package browser relaunch stayed white while the loopback server still returned HTTP 200; stopping only the verified stale package controller/GUI-host processes and relaunching restored the maximized project chooser. This is a recovery workaround, not a code fix. The app window and full-desktop screenshots were verified at `1920 × 1094`.
- Focused regression evidence: `290 passed` in the GUI unit suite, `1 passed` for the axisymmetric preview E2E, and `1 passed` for the bounded 3D workspace E2E. The 3D E2E imports STEP and wire-trace fixtures, meshes and exercises model/named-selection flows; its selection timing dispatches synthetic Plotly events. No numerical solver algorithm or dependency changed, and no simulation was run. The VM's move to SSD prevents comparison with pre-SSD timings as a controlled benchmark.
- The release package repository `ngsolve-ui` was confirmed private on 2 November 2026. The separate GUI source repository `ngsolve_gui` remains public.

**Milestone status (2 November 2026 — reusable packaged desktop verification): Completed for the requested GUI reliability gate; Phase A remains open.** Evidence and instructions are in [`docs/reviews/2026-11-02-desktop-automation.md`](reviews/2026-11-02-desktop-automation.md) and the shareable review ZIP.

- A small standalone harness now launches the packaged MagMechSim executable in a unique test workspace. Win32 `SendInput` drives real desktop mouse, keyboard and wheel input; Pillow `ImageGrab` captures the complete Windows desktop; Playwright attaches only to a test-only, loopback CDP endpoint to inspect DOM state and measure timings. No Codex Computer Use interaction or screenshot capability is required. The opt-in CDP flag is scoped to MagMechSim and is disabled on ordinary launches and in the separate Mike variant.
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
- No solver algorithms, numerical dependencies or project formats changed, and no simulation was run. The separate Mike variant was not modified.
- The full desktop harness also confirmed visible saved axisymmetric geometry on first capture, full-pane grid at maximized/restored sizes, an actual Plotly WebGL geometry preview, real STEP-face interaction and persistence. The earlier blank sketch frame was not reproduced; its original cause is unknown. Physical GPU behavior remains unverified.
- Latest package: `0.2.8.dev216`, SHA-256 `c7c40e9bf975a8d3870ed6e082f271fdcbf8c2b5478d61bfdaca25e6f6cf5951`. `Launch-Latest-MagMechSim.cmd` points to this package. Current startup timings on the SSD-migrated VM are not controlled comparisons with earlier storage conditions.

**Phase A exit:** stop for review after the Phase A evidence. Following the independent acceptance, bounded Phase B1 model-preparation work proceeded; DC background-field and coupled-physics work remain later phases.

### Phase B. Complete the 3D model-preparation workflow (B1 and B2 complete for their tested scopes; Phase C next)

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

Solver changes were limited to input/configuration assembly and provenance: body-force maps are converted to material coefficient functions, spring maps are applied on their selected boundaries, and wire-trace phase scales the existing complex source. No element formulation, numerical algorithm, dependency or project format changed. External mechanical surface traction, contact, nonlinear mechanics, fully coupled EM-mechanics, DC background-field and ROM workflows remain unsupported and are not exposed as available.

Verification combined real packaged-app interaction and project save/reopen evidence with two small NGSolve runs. A 10 mm block EM case doubled a 1e6 A/m² current-density source to 2e6 A/m² and kept the sampled magnetic-field proportionality error below `1e-5`. A 50 Hz mechanics case checked a 1e8 N/m³ scoped load, 1e-6 m prescribed motion, spring and fixed supports; constrained DOFs had the expected imaginary amplitude and near-zero real component. Full evidence, test logs and limitations are in [`docs/reviews/2026-11-03-phase-b2.md`](reviews/2026-11-03-phase-b2.md).

The broader Phase B backlog retains verification of discretisation/order distinctions and numerical-setting coverage where a future backend-capability review identifies a gap. That work is not an implicit promise to add unsupported controls and does not change the next priority stated for this milestone.

#### Next milestone: Phase C. 3D DC magnetic background-field integration

Expose and validate the existing backend-supported DC background-field configuration required for the subsequent coupled workflow. First map its inputs, dependencies and sequencing. Keep the 3D harmonic single-physics paths and axisymmetric workflow intact; do not add coupled or ROM controls before those stages are implemented and verified.

### Phase C. 3D DC magnetic background and static-field integration (dependency for the intended coupled workflows)

**Outcome:** The engineer can configure and execute the static/DC magnetic stage needed by existing 3D coupling paths.

- Map the genuine existing 3D numerical implementation, required excitation/material/boundary inputs and dependencies on wire traces, imposed background fields or magnet sources.
- Expose only meaningful supported controls, appropriate sequencing and preflight validation.
- Verify that configured DC inputs reach the correct solver, and compare against established reference cases.

**Gate:** Reproducible DC magnetic solve with verified fields and correct interaction with a subsequent harmonic study when supported.

### Phase D. Fully coupled 3D magneto-mechanical harmonic analysis

**Outcome:** Engineers can configure and execute the existing supported coupled model through the GUI, with understandable convergence/solver feedback.

- Integrate static background, harmonic EM and harmonic mechanical response in the validated sequence required by the backend.
- Expose appropriate physics participation and coupling parameters, fixed-point/block Gauss-Seidel iterations, tolerances, relaxation/acceleration where truly implemented, and numerical compatibility rules.
- Detect and report nonconvergence separately from a successful process exit; preserve solver evidence and job state correctly. Validate the actual coupled response with existing trusted problems, p/h refinement or independently verified comparisons.

**Gate:** Bounded real coupled study works end-to-end, numerical checks pass, and failure/partial-convergence handling is correct.

### Phase E. ROM, study flexibility and advanced numerical settings

**Outcome:** Supported research-grade numerical features become approachable through professional study/property configuration.

- Expose validated reduced-order model creation/reuse and its prerequisites; clearly distinguish full-order vs reduced-order solves.
- Improve frequency sweeps, multiple studies and job/resource controls where implemented; ensure correct complex-valued inputs and units.
- Extend time-integration and other study variants **only after** the relevant backend and project state are demonstrably ready, with appropriate controls and verification. Unsupported combinations remain unavailable.

**Gate:** Each new method has an executable GUI-to-backend example, state persistence, numerical reference and error handling.

### Phase F. Axisymmetric and 3D UX unification, without loss of specialist capabilities

**Outcome:** One coherent MagMechSim design language, with specialised workflows that belong in each formulation.

- Reuse the application shell, command taxonomy, property and validation patterns, materials and job management where appropriate.
- Preserve axisymmetric sketching, arcs, lines, region behaviour, hp-layer controls and dedicated numerical semantics.
- Remove duplicated infrastructure only where justified by maintainability or user benefit; avoid large numerical code migrations for visual uniformity alone.

**Gate:** Engineers can move between formulations without relearning basic actions, and existing axisymmetric regression examples still pass.

### Phase G. Sphinx documentation, in-app help and documentation search

**Outcome:** Useful, offline-capable, version-matched product documentation with fast contextual discovery.

- Update the Sphinx documentation to match the actual MagMechSim workflows; include core geometry, CAD scoping, materials, supported BCs, meshing, studies, coupling, errors and examples. Build/version/package with the app.
- Add context-sensitive Help links from settings where helpful, without cluttering panels.
- **Then** add the desired upper-right documentation search: rank actual indexed sections, show up to **five results** with title and useful excerpt, keyboard navigation, and deep links to local Sphinx heading anchors. Ensure acceptable startup impact and offline operation; no fabricated results or hard-coded examples.

**Gate:** Docs build reliably; links and section anchors work offline in packaged builds; search results reflect real content and open the correct section.

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
NEXT: DC background configuration (Phase C)
                      |
                      v
            Coupled 3D harmonic workflow (Phase D)
                      |
                      v
            ROM / studies / advanced options (Phase E)

Shared UX unification (Phase F) continues in small compatible increments.
Local Sphinx access starts NOW; comprehensive docs and 5-result search follow in Phase G.
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
9. **Latest executable:** Keep `Launch-Latest-MagMechSim.cmd` pointed at the newest tested full MagMechSim package. Execute the launcher script itself in a fresh profile; verify the real app reaches its mounted workspace, the “Downloading App” overlay clears, and Win32 confirms the main window is maximized on the full native display. Record the executable SHA-256, launcher target and exact GUI/solver source revisions. Include the launcher smoke log and full-desktop screenshot in the review ZIP. Keep the separate Mike variant distinct except for startup-only improvements explicitly intended for it.
10. **Handoff:** Concise report, actual full-resolution screenshots, focused test evidence, performance observations, outstanding issues, next recommendation and a ZIP. Record that the VM moved to SSD and do not attribute timing changes to code without a controlled same-hardware comparison. Never claim native rendering or splash behaviour from a browser screenshot alone.

## 7. Risk register

| Risk | Why it matters | Mitigation |
|---|---|---|
| 3D face selection and renderer stability | Phase A's repeated native picking/recovery passed on a capped 22,132-element mesh. Phase B1 additionally verified a visible orange solid outline on a two-body 24-triangle preview; the historical OOM did not recur | Keep the Chromium memory concern monitored; include larger representative models and long-session memory in a later targeted gate. It does not block B2 |
| Geometry identity ambiguity | Can silently change loads or supports | Continue safe invalidation, user-guided reassignment and verification |
| Coupling not exposed in 3D | Core product capability unavailable | Implement and verify DC prerequisite, then coupled pipeline, rather than unrelated UI controls |
| Axisymmetric display and geometry-tool regressions | The saved sketch and full-pane grid now render on first open at maximized and restored sizes; the earlier blank capture did not recur. Tangent-arc tool behavior and physical-GPU rendering were not specifically revalidated in this closeout | Retain first-open/grid checks and targeted geometry-tool regression coverage; investigate if a blank frame or geometry-editing defect recurs |
| Menus expose irrelevant commands | Confusing and potentially hazardous workflow | Context-aware command taxonomy and capability checks |
| Axisymmetric sketch/preview rendering | Saved regions, full-pane grid and the geometry-creation Plotly WebGL preview are visible in the packaged app; physical-GPU behavior is unverified | Keep representative sketch/preview regression coverage; reopen if blank frames or fidelity issues recur |
| Reload after a renderer crash can leave a blank window | Controlled recovery now restores styles, project state and a rendered viewport | Retain one-attempt recovery guard and packaged crash regression; revisit if broader crash reports occur |
| Startup performance baseline | Latest app reached ready in 7.3–9.1 s on the SSD-migrated VM | Profile stages and compare only under controlled storage/hardware before attributing improvement |
| Documentation is linked to GitHub | Poor offline and packaged UX | Bundle local Sphinx HTML now; refresh/index in later documentation phase |
| Broad CI has known failures | Can conceal regressions and waste iteration time | Keep focused gates, track known failures, schedule comprehensive checks |
| Frequent rebuilds and tests | Slows development | Build/package once per milestone; use risk-based, cached focused tests |

## 8. Roadmap governance

Suggested repository home for this plan: a documented planning location under the GUI repository, chosen by Codex after inspecting existing conventions. Keep this file as the **single current roadmap**, with date, completed milestones, changed priorities and links to implementation reports. Historical roadmaps may remain archived but should not conflict with the current one.

**Current action:** Phase A remains accepted for its tested workloads, with historical Chromium memory growth monitored. Phase B1 and Phase B2 are complete for their tested scopes and documented in [`docs/reviews/2026-11-03-phase-b1.md`](reviews/2026-11-03-phase-b1.md) and [`docs/reviews/2026-11-03-phase-b2.md`](reviews/2026-11-03-phase-b2.md). Stop for independent review. The next intended priority is Phase C (DC magnetic background-field integration); do not begin it automatically. Keep the packaged Win32 desktop harness and latest-full-app launcher as required milestone gates.
