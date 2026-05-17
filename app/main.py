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

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="build123d dashboard")


class CodeIn(BaseModel):
    code: str


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
    ns: dict = {"__name__": "__user_script__", "bd": bd}
    # Make every top-level build123d export available without an import.
    for attr in dir(bd):
        if not attr.startswith("_"):
            ns.setdefault(attr, getattr(bd, attr))
    try:
        exec(compile(code, "<user-script>", "exec"), ns)
    except Exception:
        raise HTTPException(status_code=400, detail=traceback.format_exc())
    return _extract_shape(ns)


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


@app.post("/api/upload-py")
async def upload_py(file: UploadFile = File(...)) -> dict:
    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File must be UTF-8 text.")
    return {"code": text, "filename": file.filename}


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
