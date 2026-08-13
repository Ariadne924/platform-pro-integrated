"""Deliverable evaluation from the web: export the factor panel and run the stage-3 pipeline.

``_STAGE3_CONFIG`` is monkeypatchable so tests can point the pipeline at a temp
config whose ``output.root`` keeps every artifact inside tmp_path — the real
``outputs/`` directory is never touched.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd
import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from superplatform.evaluation.experiment import ExperimentRunner
from superplatform_web.reports import build_batch_reports
from superplatform_web.research import build_batch_panel, normalize_for_experiment

router = APIRouter(prefix="/api/evaluation", tags=["evaluation"])

# Project root (routes/ → superplatform_web/ → src/ → root).
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
# Standalone deliverable config; tests monkeypatch this to a temp file.
_STAGE3_CONFIG = _PROJECT_ROOT / "config" / "config.yaml"
# Where the deliverable writes outputs (the config's ``output.root`` governs the
# actual path; kept as a constant for parity with app._EXPERIMENTS_PATH).
_OUTPUT_ROOT = _PROJECT_ROOT / "outputs"


class BatchPanelRequest(BaseModel):
    factors: list[str] = Field(min_length=1)
    symbols: list[str] = Field(min_length=1)
    start: str
    end: str
    frequency: str | None = None


class BatchReportRequest(BatchPanelRequest):
    """Serialized batch output used for lightweight in-app research reports."""

    results: list[dict] = Field(min_length=1)
    correlation: dict | None = None


def _load_stage3_config() -> dict:
    with open(_STAGE3_CONFIG, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _zip_bytes(output_dir: Path) -> bytes:
    """Zip an output directory using paths relative to the directory itself."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(output_dir.rglob("*")):
            if file.is_file():
                archive.write(file, file.relative_to(output_dir).as_posix())
    return buffer.getvalue()


def _run_warning(manifest: dict) -> str | None:
    """Summarize non-success run status for the frontend warning message."""
    status = manifest.get("status", "unknown")
    if status == "success":
        return None
    failed = manifest.get("failed_tasks") or []
    details = "；".join(
        f"{f.get('task')}: {f.get('error')}" for f in failed if f.get("error")
    )
    if details:
        return f"评估未完全成功（{status}）：{details}"
    return f"评估未完全成功（{status}）。"


async def _build_panel(request: Request, data: BatchPanelRequest) -> pd.DataFrame:
    """Run the UI-selected batch evaluation and return its combined panel."""
    try:
        return await build_batch_panel(
            base_config=request.app.state.config,
            providers=request.app.state.providers,
            factor_names=data.factors,
            symbols=data.symbols,
            start=data.start,
            end=data.end,
            frequency=data.frequency or "1d",
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/panel-export")
async def export_panel(data: BatchPanelRequest, request: Request) -> Response:
    """Export the selected factors' evaluation panel as CSV."""
    panel = await _build_panel(request, data)
    filename = f"factor-panel-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return Response(
        content=panel.to_csv(index=False).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/reports")
async def generate_reports(data: BatchReportRequest) -> dict:
    """Generate a cross-factor report plus an individual report per factor."""
    context = {
        "factors": data.factors,
        "symbols": data.symbols,
        "start": data.start,
        "end": data.end,
        "frequency": data.frequency or "1d",
    }
    return build_batch_reports(
        context=context,
        results=data.results,
        correlation=data.correlation,
    )


@router.post("/run")
async def run_pipeline(data: BatchPanelRequest, request: Request) -> Response:
    """Run the stage-3 deliverable pipeline on the UI panel and zip its outputs."""
    panel = await _build_panel(request, data)
    try:
        bar_interval = data.frequency or "1d"
        normalized = normalize_for_experiment(
            panel, _load_stage3_config(), bar_interval=bar_interval
        )
        stamp = datetime.now(timezone.utc)
        today = stamp.strftime("%Y%m%d")
        subdir = stamp.strftime("%H%M%S") + "-" + uuid4().hex[:6]
        output_dir = ExperimentRunner(
            _STAGE3_CONFIG,
            run_date=today,
            output_subdir=subdir,
            panel=normalized,
            bar_interval=bar_interval,
        ).run()
        manifest = json.loads(
            (output_dir / "run_manifest.json").read_text(encoding="utf-8")
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    headers = {
        "Content-Disposition": (
            f'attachment; filename="superplatform-evaluation-{today}-{subdir}.zip"'
        )
    }
    warning = _run_warning(manifest)
    if warning is not None:
        headers["X-Run-Status"] = str(manifest.get("status", "unknown"))
        headers["X-Run-Warning"] = warning
    return Response(
        content=_zip_bytes(output_dir),
        media_type="application/zip",
        headers=headers,
    )
