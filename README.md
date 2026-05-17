# step-forge

A tiny local dashboard for [build123d](https://github.com/gumyr/build123d). Paste Python, see a 3D preview, export a **real STEP file** — analytical B-rep geometry (cylinders, planes, edges as surfaces), not a triangle mesh in a STEP wrapper. The output opens cleanly in Shapr3D, Plasticity, Fusion 360, FreeCAD, SolidWorks, etc.

Built because OpenSCAD's STEP export is just triangulated mesh dressed up in STEP syntax. build123d uses OpenCascade under the hood (same kernel as FreeCAD and Shapr3D), so you get the real thing.

## What it does

- **Paste** build123d Python code, or **drop a `.py` file** on the left pane
- **Render** to see a tessellated 3D preview in the browser (three.js, orbit/pan/zoom)
- **Export STEP** to download a parametric CAD file
- Z-up axes, grid, edge overlay — CAD-style viewer, not a generic mesh viewer

The preview is a fast mesh, but the STEP export goes back to the original B-rep — so the file you download has true analytical surfaces.

## Install

Requires **Python 3.12** (build123d's OCP wheels don't support 3.13/3.14 yet).

```bash
git clone https://github.com/SOYLOBO/step-forge.git
cd step-forge
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
./run.sh
```

Open <http://127.0.0.1:8765>.

## Convention

Your script gets every top-level `build123d` symbol pre-imported. Assign your part to one of: `result`, `part`, `model`, `shape`, `obj`, `assembly`. The smallest valid script:

```python
result = Box(40, 30, 20)
```

Anything more interesting works too — see [`examples/rotor_v36.py`](examples/rotor_v36.py) for a real parametric mechanical part (a 335 mm forked-cross rotor with keyway, hub, and trunnion bores) ported from OpenSCAD.

## Keyboard

- `⌘/Ctrl + Enter` — render
- `⌘/Ctrl + Shift + E` — export STEP
- `Tab` — insert 4 spaces

## Why not OpenSCAD?

OpenSCAD works on triangulated meshes internally. Its STEP export wraps those triangles in STEP syntax — technically valid, but downstream CAD tools see millions of facets instead of clean cylinders and planes. You can't fillet a triangle; you can't pull an edge; you can't change a hole's diameter by selecting it.

build123d (and the underlying OpenCascade kernel) works on actual analytical surfaces. A `Cylinder` is a cylinder in the STEP file, not 240 triangles approximating one.

## Security note

The backend `exec()`s your pasted code in-process. Do not expose port 8765 beyond localhost.

## License

MIT.
