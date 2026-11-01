# Development verification strategy

The verification path is tiered so routine edits get quick feedback while the
full numerical and GUI regression suite remains available at integration and
release checkpoints. Assertions and failure handling remain unchanged across
all tiers.

## Fast development checks

Run the narrowest tests that exercise the edited behavior. For example, from
PowerShell in the repository root:

```powershell
$env:PYTHONPATH = 'src'
python -m pytest -q `
  tests/test_three_d_project.py::test_step_region_tags_follow_geometry_not_import_order_or_assembly_scale `
  tests/test_three_d_solver_setup.py::test_mechanical_builder_sets_structural_sources_supports_and_no_air_domain `
  tests/test_three_d_workspace.py::test_mechanical_study_property_editor_exposes_only_relevant_configuration
```

When using the bundled Windows runtime, add the companion release package's
`native_windows` helper directory to `PYTHONPATH`; the standalone GUI source
checkout does not contain the packaged `frontend_cache` helper.

Do not rerun the whole suite after small edits. After the focused local check
passes, use the blocking CI result as the second environment; avoid rerunning
the same unchanged tests locally just to duplicate that result.

## Milestone verification

At a coherent feature checkpoint, run the related project-schema, meshing,
workspace, solver-input and UI workflow tests together. For the 3D boundary
scoping milestone, the blocking `Core preprocessing and branding` CI job runs
the complete 3D project, mesh, workspace, solver-setup and Playwright workflow
test files. It also retains the existing failed-run and cancellation checks.
The finite-element integration case constructs a single 10 mm block and checks
the constrained NGSolve space without solving. A separate smoke case launches
the real solver process for the same small geometry, caps the mesh below
50,000 elements and checks a finite non-zero magnetic flux density. The observed
run used 79 volume elements. Stage 8B adds a second bounded process run on a
single 10 mm block. It checks that fixed support and body force reach the
existing solver and requires finite non-zero displacement output. The
companion UI workflow also switches the rendered project from the EM
configuration to the mechanics configuration.

The core job is the routine blocking pull-request check. WebGPU and Playwright
tests stay sequential within their job because they share browser and renderer
resources. Assertions are not relaxed to make this job faster. Tests that need
the companion solver checkout retain their explicit skip reason when that
checkout is absent; with the solver available, the suite also runs the bounded
3D smoke case above. Docker BuildKit layers use the GitHub Actions cache, and
only one test image is built for each workflow event.

## Comprehensive verification

The full pytest suite runs:

- on pushes to `main` or `master`;
- weekly on Monday at 03:17 UTC;
- on a pull request carrying the `comprehensive-tests` label; or
- manually through **Actions → Tests → Run workflow** with `comprehensive` enabled.

When a comprehensive run is selected, it replaces the overlapping core run;
the full suite includes those tests, so CI builds one test image and executes
the shared cases once.

The comprehensive job publishes `tests/output/`, including JUnit XML, even
when the test command fails. Its failures remain visible as a failed job. This
keeps routine pull requests fast without hiding regressions or making the
known full-suite failures look successful.

## Review screenshots

Screenshots included in milestone review packages must show the full
application at a maximized, screen-sized desktop viewport. Use at least
1920×1080 when the host display is not directly available, or the host's full
resolution when it is larger. Capture the complete app window rather than a
cropped panel or full-page document image, inspect the resulting dimensions,
and record them in the evidence manifest. Compact viewports remain useful for
responsive-layout tests, but must not be used as the primary review screenshots.

## Launching the reviewed version

Every completed work package must leave a Windows executable that launches the
reviewed application source. Rebuild the standalone package after the final
source changes. If an older packaged app is running, build the new executable
side-by-side rather than replacing files beneath the running process, and give
the user the new executable path.

Launch the new executable and verify that its GUI reaches a ready state and
loads the reviewed GUI source. Include the GUI and solver source revisions, app
version, executable path and SHA-256, and launch verification evidence in the
review ZIP. Include a copyable launch instruction. The source-checkout launcher
may be documented as a development alternative, but does not count as updating
the Windows executable.

## Known comprehensive-suite baseline

The latest recorded full run before this strategy change completed with 513
passing, 11 failing and 3 skipped tests, with no warnings. See [GitHub Actions
run 41309092306](https://github.com/YashSooriya/ngsolve_gui/actions/runs/41309092306).
Those failures remain an open integration/release issue; this policy does not
mark them as expected passes or alter their assertions. The Stage 8B review
records whether a comprehensive run was performed for that milestone.
