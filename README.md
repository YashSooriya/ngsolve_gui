# MagMechSim

MagMechSim is a desktop finite-element application for magneto-mechanical modelling. Its mature axisymmetric workflow and developing 3D time-harmonic electromagnetic workflow are built on [NGSolve](https://ngsolve.org) and the ngapp interface stack.

The [MagMechSim user guide](docs/user-guide.md) covers project setup, geometry, meshing, studies, and saved results.

This repository is also the Python package used to develop and run the application. The `ngsolve_gui` package name, Python import path, user-settings identifiers, file suffixes, and internal `mm-fem.*` schemas remain stable for compatibility.

## Application workflows

- **Axisymmetric** — 2D radial–axial geometry, specialised sketch tools, materials, time-harmonic physics and the existing magneto-mechanical study workflow.
- **3D** — STEP and wire-trace geometry, named selections, materials, mesh controls, and supported time-harmonic electromagnetic studies.
- **Post-processing** — inspect saved meshes, fields, and solver output with the existing visualization workspaces.

## Technology

- **[Netgen/NGSolve](https://ngsolve.org)** — meshing & finite elements
- **[ngapp](https://github.com/CERBSim/ngapp)** — application framework (UI components, keybindings, state management)
- **[webgpu](https://github.com/CERBSim/webgpu)** — GPU-accelerated rendering
- **[ngsolve_webgpu](https://github.com/CERBSim/ngsolve_webgpu)** — NGSolve-specific WebGPU renderers (meshes, functions, picking)

## Interface capabilities

- **Geometry viewer** — visualize and inspect OCC geometries, pick faces/edges/vertices
- **Mesh viewer** — display 2D/3D meshes with wireframe, curved elements, volume shrink, region coloring
- **Function visualization** — scalar & vector fields, colormaps, deformation, clipping planes, fieldlines
- **Plots** — embedded Plotly charts for convergence studies and data analysis
- **Interactive clipping** — ctrl+click/drag/scroll to position and orient clipping planes
- **Element picking** — hover to highlight elements and regions, inspect coordinates
- **Keyboard shortcuts** — modal keybindings for fast navigation (`v` for view, `c` for clipping, etc.)
- **Navigator + property panel** — collapsible side panels with resizable splitters
- **Save/load projects** — persist state across sessions

## Installation

```/dev/null/sh#L1
pip install .
```

## Usage

```/dev/null/sh#L1-2
# Launch with a mesh/geometry file
ngsolve myfile.vol
```

```/dev/null/sh#L1-2
# Run a python script
ngsolve script.py
```

ngsolve.Draw and ngsolve.Redraw commands are automatically redirected to draw a new GUI item.

For a local app served from a headless machine, set
`NGSOLVE_GUI_FILE_PICKER=browser` to use the browser's file picker instead of
opening a native dialog on the server.

## Reuse

Feel free to use individual components in your own packages or take inspiration if you're building simulation tools with ngapp + webgpu. The main building blocks:

| Component | Purpose |
|-----------|---------|
| `WebgpuTab` | Base class for any 3D viewport tab with clipping, picking, camera |
| `MeshComponent` | Mesh visualization with all options |
| `FunctionComponent` | Scalar/vector field rendering |
| `GeometryComponent` | OCC geometry viewer with selection |
| `PlotComponent` | Plotly-based chart tab |
| `Navigator` | Left sidebar with item list |
| `PropertyPanel` | Right sidebar for per-tab settings |
| `sections/*` | Reusable UI sections (clipping, colorbar, deformation, etc.) |
