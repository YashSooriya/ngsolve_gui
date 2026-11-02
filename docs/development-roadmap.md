# MagMechSim: Forward Development Roadmap

**Planning baseline:** 1 November 2026  
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

Based on `MagMechSim-Stage8B-Next-Milestone-Review-2026-11-02.zip` (the latest Codex review package inspected):

- 3D and axisymmetric time-harmonic solver execution and focused regression checks have been demonstrated for selected cases. The latest 3D focused suite reported **63 passes** and axisymmetric model/revolution tests **41 passes, 2 deselected**. The full historical suite was *not* rerun; its last recorded baseline was **513 pass / 11 fail / 3 skip**.
- 3D harmonic mechanics has manufactured-solution convergence evidence. Bounded nonzero-frequency free-body inertial tests at 50 and 100 Hz agreed with expected rigid translation; fixed-support enforcement was also checked. These do not constitute a full verification of coupled 3D magneto-mechanics.
- CAD-based selection identity and a user-facing `Needs review` reassignment workflow have been added. Unresolved active assignments block solves rather than silently reattach to unintended geometry.
- On a 22,132-tetrahedron model, repeated 3D face-selection-to-editor latency was approximately **1.6 s median**. Python overlay changes helped, but the full browser-to-backend-to-render path needs profiling.
- A freshly built and launched Windows executable was reported. A single startup-to-GUI-mounted measurement was **16.3 s** on a VM newly moved to SSD. No controlled cold/warm hardware benchmark or packaged native check of the axisymmetric 3D preview exists yet.
- Axisymmetric geometry 3D preview produced valid WebGPU renderer readback, but the Playwright screenshot canvas appeared blank. Distinguish the VM/automated capture limitation from the real desktop experience.

**Do not re-do these completed milestones.** Build upon their evidence, and re-check only when changed code, a failed test or release-critical risk warrants it.

## 3. Immediate milestone: application-wide UI coherence and targeted correctness fixes

### Goal

Make the top-level application layout, commands, modal behaviour, branding and axisymmetric geometry workflow feel cohesive and predictable. Fix reported functional defects while preserving the stable 3D model/solver work. This is the **next Codex implementation milestone**, ahead of major new coupled-physics UI functionality.

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
- The actual packaged axisymmetric **geometry-creation** 3D preview remains blank on this VM. The installed browser has no working WebGPU adapter with the VirtualBox WDDM display adapter. Headless renderer readback succeeds, but this is not a native-desktop pass. Capability detection and a non-WebGPU preview fallback are now a high-priority follow-up; post-processing is a separate issue.
- Computer Use can enumerate the application but its native screenshot capture timed out after the Windows restart. The release screenshots were captured from the full desktop and visually inspected at the real 1920×1094 resolution. This capture-channel fault does not change the app's independently queried maximized state.
- No numerical solver algorithms were changed. The VM's move to SSD is a performance confound; the prior 16.3 s startup and 2.49 s selection timing are not controlled same-hardware comparisons. The review package records the final executable identity, logs, screenshots, performance observations, and remaining preview limitation.

## 4. Future phases, after the immediate milestone

These stages are **directional**, with dependencies and scopes to be refined by Codex after inspection. They need not be exactly one Codex prompt each.

### Phase A. 3D interactive performance and preprocessing refinement (high priority, may run alongside later work)

**Outcome:** CAD picking, selection highlighting, property editing and model navigation feel immediate on realistic engineering geometry.

- Profile the complete user interaction pipeline, including browser rendering, Plotly redraws, WebSocket communication, ngapp state updates, NGSolve/OCC work, logging and panel changes.
- Eliminate unnecessary full redraws and heavy recomputations on selection, reuse costly geometry/mesh data, and keep UI updates responsive. Re-evaluate renderer choices only on proven benefits, not WebGPU fashion.
- For the axisymmetric geometry-creation 3D preview, detect WebGPU adapter availability before rendering. Provide a supported WebGL preview fallback when WebGPU is unavailable and a clear reduced-preview state when neither renderer is available. Validate geometry fidelity, selection-independent navigation, resize behaviour and actual packaged desktop output; the CPU 2D results viewer does not cover this workflow.
- Benchmark repeatable selection p50/p95, viewport interaction, panel switching, geometry loading and startup on representative models and **unchanged hardware**. Separate VM/SwiftShader limits from production hardware. Avoid claiming speedups after storage/hardware changes without a controlled baseline.
- Aim for near-immediate common interactions. Set numerical latency budgets after obtaining reproducible hardware conditions; do not trade correctness for a number.

**Gate:** Repeatable profiling identifies bottlenecks, representative interactions improve measurably, no CAD assignment/solver regression.

**Milestone status (2 November 2026 — rendering and responsiveness pass): Partial; keep Phase A open.** The implementation is in GUI commit `25450a9476197de9244024c20391c81a7dc53f8b`; the Windows package was built from that GUI revision and solver/release commit `b27702905a9db63edca0d12439cfa89a15bb7d5a`.

- The axisymmetric geometry-creation preview now uses a bounded OCC tessellation rendered with Plotly WebGL. It shows the revolved region with physical XYZ proportions, feature edges and per-region visibility. A clear message is shown if WebGL is unavailable. The old WebGPU renderer remains in the main STEP and results workspaces; the axisymmetric preview itself now uses WebGL even on WebGPU-capable systems. The VirtualBox adapter reports GPU/WebGPU unavailable, so the fallback was verified in the packaged app, but physical-GPU behavior has not been checked.
- In the maximized packaged application, a drawn annulus with a bore rendered, rotated, zoomed and returned to the editable sketch. Ten wheel events did not make the sketch grid disappear, and the measured sketch dimensions remained unchanged. A saved axisymmetric project was reopened with its geometry intact. This grid issue did not reproduce, so no forced redraw was added.
- 3D selection handling now avoids rebuilding the model tree when a face pick leaves the selected tree item unchanged, and boundary-overlay edge data is cached. On the same post-SSD VM and 22,132-element test mesh, the Playwright profile's five repeated event-to-editor samples improved from median `2734.37 ms` / p95 `3286.34 ms` to median `443.98 ms` / p95 `474.17 ms`. The first/cold after sample was `1914.59 ms`. These events are emitted programmatically to measure the event-to-editor path; they do not measure real pointer hit-testing. Do not treat them as proof that native CAD picking is correct or near-immediate.
- The actual packaged 3D workspace imported the STEP fixture and generated/rendered a coarse 100 mm mesh (`22,132` volume elements, `14,588` surface triangles). Direct native mesh clicks did not visibly create a boundary-region assignment in the recorded checks. A later pointer move over the mesh produced Chrome's `Aw, Snap! Out of Memory` page. An earlier, denser 30 mm mesh (`105,523` volume elements, `69,978` surface triangles) also preceded a renderer OOM after repeated plot interactions. Mesh generation succeeded; no solver was run. Mesh-hover stability and physical face picking remain release-blocking Phase A findings.
- The packaged executable is `0.2.8.dev216`, SHA-256 `da3e597dd12cbf0c0c541c80b23ed68489e392bfdf4919bbca9de6a7d39c2a95`. The latest launcher opened the maximized app. After the renderer crash, a same-package browser relaunch stayed white while the loopback server still returned HTTP 200; stopping only the verified stale package controller/GUI-host processes and relaunching restored the maximized project chooser. This is a recovery workaround, not a code fix. The app window and full-desktop screenshots were verified at `1920 × 1094`.
- Focused regression evidence: `290 passed` in the GUI unit suite, `1 passed` for the axisymmetric preview E2E, and `1 passed` for the bounded 3D workspace E2E. The 3D E2E imports STEP and wire-trace fixtures, meshes and exercises model/named-selection flows; its selection timing dispatches synthetic Plotly events. No numerical solver algorithm or dependency changed, and no simulation was run. The VM's move to SSD prevents comparison with pre-SSD timings as a controlled benchmark.
- The release package repository `ngsolve-ui` was confirmed private on 2 November 2026. The separate GUI source repository `ngsolve_gui` remains public.

**Next Phase A gate:** diagnose and contain the native WebGL/Plotly renderer OOM, verify that actual desktop pointer movement/face selection updates the boundary editor, and make renderer-crash recovery work without manually restarting package processes. Do not start Phase B until these checks pass or the limitation is explicitly accepted.

### Phase B. Complete the 3D model-preparation workflow (high priority)

**Outcome:** Engineers can create, edit and validate all supported 3D inputs directly within the GUI, without editing Python.

- Continue strengthening STEP import, geometry hierarchy, robust face/body/edge selection and CAD-change recovery.
- Complete material-property participation and domain assignment; make selection repair clear, safe and persistent.
- Expose supported electromagnetic and mechanical BCs, sources, vector directions, boundary/domain scoping, and supported connections. Preserve the working A=0 condition, mechanical fixed support and body force flows.
- Finish supported global/local mesh controls, mesh quality diagnostics, numerical discretisation and polynomial/geometry order distinction, frequency study configuration and solver settings.
- Validate against the real backend capability matrix, not an aspirational generic FEM feature list. Avoid general-purpose contact/nonlinear UI where not implemented.

**Gate:** Full model setup and solver execution for representative supported 3D EM-only and mechanics-only cases, including project save/reopen and invalid-input checks.

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
NEXT: Complete Phase A native selection and renderer/recovery gate
    |------> Complete supported 3D preprocessing (Phase B)
                      |
                      v
            DC background configuration (Phase C)
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
8. **Native input verification:** Test desktop input independently from screenshot capture. Record whether Computer Use can navigate by keyboard, type into a real field, click, move the pointer, and scroll. If the screenshot channel fails, continue interaction when possible and use an established full-desktop capture method. If Computer Use mouse input is unavailable, use a verified Windows alternative such as Win32/pywin32, pywinauto or UI Automation. Prove input with visible before/after changes, including navigation, geometry selection and viewport response. The current Computer Use API has no standalone pointer-move action, so verify movement with the fallback and do not infer it from a scroll. Document the exact tools, errors and outcomes in the handoff.
9. **Handoff:** Concise report, actual screenshots, focused test evidence, performance observations, outstanding issues, next recommendation and a ZIP. Never claim native GPU/splash behaviour was verified from a browser screenshot alone.

## 7. Risk register

| Risk | Why it matters | Mitigation |
|---|---|---|
| 3D face selection and renderer stability | Actual mesh picking did not visibly update the native boundary editor; mesh hover caused Chrome OOM on the VM | Profile real pointer hit-testing and memory/lifecycle behavior; keep synthetic event timings separate; add native regression coverage |
| Geometry identity ambiguity | Can silently change loads or supports | Continue safe invalidation, user-guided reassignment and verification |
| Coupling not exposed in 3D | Core product capability unavailable | Implement and verify DC prerequisite, then coupled pipeline, rather than unrelated UI controls |
| Axisymmetric viewport/tangent errors | Foundational geometry UX and model validity | Fix now with scale/geometry tests |
| Menus expose irrelevant commands | Confusing and potentially hazardous workflow | Context-aware command taxonomy and capability checks |
| Axisymmetric geometry-creation preview needs WebGPU on some systems | Users see a blank preview on the installed VirtualBox display adapter | Packaged VM now uses Plotly WebGL and reports unavailable WebGL; physical-GPU path remains unverified |
| Reload after a renderer crash can leave a blank window | Users cannot return to the model without restarting the package UI service | Fix frontend/server recovery and verify cold launch, reload and restart lifecycle |
| Startup measured ~16.3 s | First-use experience feels sluggish | Profile native/backend/frontend stages and optimise controlled bottlenecks |
| Documentation is linked to GitHub | Poor offline and packaged UX | Bundle local Sphinx HTML now; refresh/index in later documentation phase |
| Broad CI has known failures | Can conceal regressions and waste iteration time | Keep focused gates, track known failures, schedule comprehensive checks |
| Frequent rebuilds and tests | Slows development | Build/package once per milestone; use risk-based, cached focused tests |

## 8. Roadmap governance

Suggested repository home for this plan: a documented planning location under the GUI repository, chosen by Codex after inspecting existing conventions. Keep this file as the **single current roadmap**, with date, completed milestones, changed priorities and links to implementation reports. Historical roadmaps may remain archived but should not conflict with the current one.

**First action:** complete the immediate milestone in Section 3 and record its native-app release-gate evidence. **Do not start Phases C through H before the immediate UI defects have been handled and verified**, except for small prerequisite improvements clearly justified by the existing codebase.
