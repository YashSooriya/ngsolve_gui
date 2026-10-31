# MagMechSim user guide

MagMechSim is a finite-element application for magneto-mechanical simulation. It provides an axisymmetric workflow and a developing 3D time-harmonic electromagnetic workflow.

## Start or open a project

From the project chooser, start an **Axisymmetric** or **3D** project, or choose **Open project** to load a saved project. Use **File → Save project** to save the current model. Axisymmetric project files use `.ngsmodel` or `.json`; 3D projects use `.mmfem3d`.

## Axisymmetric workflow

1. Open **Geometry** and create or edit the radial–axial cross-section with the sketch tools. Use the geometry preview to inspect the generated 3D revolution when needed.
2. Assign materials to the model regions and configure the available physics, boundary conditions, and sources.
3. Configure mesh and study settings. Use **Check setup** to review required inputs before meshing or solving.
4. Generate the mesh, run the study, and review the run log and history. Open a completed result in **Post Process**.

The axisymmetric workspace retains the specialised sketch, geometry, and magneto-mechanical workflow.

## 3D workflow

1. Create a **3D** project and import STEP geometry or wire traces from the **Geometry** workspace.
2. Organise geometry and named selections, assign materials, and configure the supported time-harmonic electromagnetic study and its boundary conditions.
3. Set mesh controls and generate the mesh. Inspect the geometry and mesh in the viewport before running the study.
4. Save the project and review the solver log and results after a successful run.

The current 3D solver workflow supports time-harmonic electromagnetics. Other physics should be configured in the axisymmetric workflow when supported there; the 3D interface does not imply numerical support for unavailable physics.

## Meshes and results

Use **File → Load mesh** and **Save mesh** to reuse a compatible mesh. Use **Load solutions** or **Save solutions** to exchange result bundles. A saved mesh is checked against its source problem before reuse. Results should be reviewed together with the run status and solver log.

## Help and project compatibility

Open **Help → Keyboard shortcuts** for the current shortcut list. Existing `.ngsmodel`, JSON, `.mmfem3d`, mesh, and solution files remain supported; their established file suffixes and internal schemas are retained for compatibility.
