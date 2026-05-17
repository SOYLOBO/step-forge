from __future__ import annotations

import io
import tempfile
import traceback
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import build123d as bd
from build123d import export_step, export_stl, Shape

from .truss import trussify, analyze as analyze_shape

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="step-forge")


class CodeIn(BaseModel):
    code: str


class AnalyzeIn(CodeIn):
    density_g_per_cm3: float = 1.25


class TrussifyIn(CodeIn):
    cell_size: float = 12.0
    wall_thickness: float = 2.0
    safety_margin: float = 2.0
    density_g_per_cm3: float = 1.25
    cut_axis: str = "Z"


RESULT_NAMES = ("result", "part", "model", "shape", "obj", "assembly")


def _extract_shape(ns: dict) -> Shape:
    for name in RESULT_NAMES:
        val = ns.get(name)
        if isinstance(val, Shape):
            return val
    # Fallback: any Shape in the namespace, preferring later assignments.
    for name, val in reversed(list(ns.items())):
        if isinstance(val, Shape):
            return val
    raise HTTPException(
        status_code=400,
        detail=(
            f"No build123d Shape found. Assign your part to a variable named one of: "
            f"{', '.join(RESULT_NAMES)}."
        ),
    )


def _run_user_code(code: str) -> Shape:
    ns: dict = {"__name__": "__user_script__", "bd": bd, "trussify": trussify}
    # Make every top-level build123d export available without an import.
    for attr in dir(bd):
        if not attr.startswith("_"):
            ns.setdefault(attr, getattr(bd, attr))
    try:
        exec(compile(code, "<user-script>", "exec"), ns)
    except Exception:
        raise HTTPException(status_code=400, detail=traceback.format_exc())
    return _extract_shape(ns)


def _stats_headers(stats: dict, prefix: str = "X-Stats-") -> dict:
    """Flatten an analyze() dict into HTTP headers (str values)."""
    out = {}
    for k, v in stats.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                out[f"{prefix}{k}-{k2}"] = f"{v2:.4f}"
        else:
            out[f"{prefix}{k}"] = f"{v:.4f}" if isinstance(v, (int, float)) else str(v)
    return out


@app.post("/api/render")
def render(payload: CodeIn) -> Response:
    shape = _run_user_code(payload.code)
    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
        path = Path(f.name)
    try:
        export_stl(shape, path, tolerance=0.05, angular_tolerance=0.2, ascii_format=False)
        data = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    return Response(content=data, media_type="model/stl")


@app.post("/api/export-step")
def export_step_endpoint(payload: CodeIn) -> Response:
    shape = _run_user_code(payload.code)
    with tempfile.NamedTemporaryFile(suffix=".step", delete=False) as f:
        path = Path(f.name)
    try:
        export_step(shape, path)
        data = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)
    return Response(
        content=data,
        media_type="application/step",
        headers={"Content-Disposition": 'attachment; filename="model.step"'},
    )


@app.post("/api/analyze")
def analyze_endpoint(payload: AnalyzeIn) -> dict:
    shape = _run_user_code(payload.code)
    return analyze_shape(shape, density_g_per_cm3=payload.density_g_per_cm3)


@app.post("/api/trussify-preview")
def trussify_preview(payload: TrussifyIn) -> Response:
    """Run the user's script, apply trussify, return STL + before/after stats in headers."""
    shape = _run_user_code(payload.code)
    before = analyze_shape(shape, density_g_per_cm3=payload.density_g_per_cm3)
    try:
        trussed = trussify(
            shape,
            cell_size=payload.cell_size,
            wall_thickness=payload.wall_thickness,
            safety_margin=payload.safety_margin,
            cut_axis=payload.cut_axis,
        )
    except Exception:
        raise HTTPException(status_code=400, detail=traceback.format_exc())
    after = analyze_shape(trussed, density_g_per_cm3=payload.density_g_per_cm3)

    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
        path = Path(f.name)
    try:
        export_stl(trussed, path, tolerance=0.05, angular_tolerance=0.2, ascii_format=False)
        data = path.read_bytes()
    finally:
        path.unlink(missing_ok=True)

    headers = {}
    headers.update(_stats_headers(before, prefix="X-Before-"))
    headers.update(_stats_headers(after, prefix="X-After-"))
    savings_pct = 100.0 * (before["volume_mm3"] - after["volume_mm3"]) / max(before["volume_mm3"], 1e-9)
    headers["X-Savings-Pct"] = f"{savings_pct:.2f}"
    headers["Access-Control-Expose-Headers"] = ", ".join(headers.keys())
    return Response(content=data, media_type="model/stl", headers=headers)


@app.post("/api/upload-py")
async def upload_py(file: UploadFile = File(...)) -> dict:
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 text.")
    return {"code": text, "filename": file.filename}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
