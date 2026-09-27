#!/usr/bin/env python3
"""Local web UI for the UMD easy-course recommender.

    ../.venv/bin/uvicorn web.app:app --reload --port 8000   (run from project root)

Serves web/static/index.html at / and a single POST /api/analyze endpoint that
runs the same driver.run_pipeline() the CLI uses, so there's exactly one code
path for "PDF in, ranked recommendations out."
"""
import shutil
import sys
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from driver import run_pipeline  # noqa: E402

OUTPUT_DIR = ROOT / "output"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="UMD Easy Course Recommender")


def _truthy(s):
    return str(s).strip().lower() in ("1", "true", "on", "yes")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/analyze")
async def analyze(
    file: UploadFile = File(...),
    credits_min: float = Form(1),
    credits_max: float = Form(3),
    top: int = Form(5),
    open_only: str = Form("false"),
):
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / (file.filename or "audit.pdf")
        with open(pdf_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        try:
            result = run_pipeline(
                pdf_path, credits_min, credits_max, top, _truthy(open_only),
                out_dir=OUTPUT_DIR,
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    return result


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
