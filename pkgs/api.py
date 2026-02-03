from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime, timedelta

from msTools.data_manager import DataManager
from msGait.movement_detector import MovementDetector
from msCodeID.codeid_processor import CodeIDProcessor   

app = FastAPI(
    title="MS Monitoring API",
    version="0.1.0",
    description="API to display activity windows and gait detection using existing logic.",
    root_path="/msGait"
)

# ---- SINGLETONS SENCILLOS ----
_dm: Optional[DataManager] = None
_detector: Optional[MovementDetector] = None

@app.on_event("startup")
def _startup():
    """
    Create shared instances when starting the server.
    Adjust routes/params if your MovementDetector requires a different signature.
    """
    global _dm, _detector
    _dm = DataManager(config_path=".config.yaml")

    now = datetime.utcnow()
    fstart = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    fend   = now.strftime("%Y-%m-%d %H:%M:%S")

    _detector = MovementDetector(
        config_file=".config.yaml",
        sampling_rate=50,
        sect="movement",
        fstart=fstart,
        fend=fend,
        ids=None,
        verbose=0
    )

# ---- MODELS Pydantic ----

class GaitRequest(BaseModel):
    ids: List[int]
    save: bool = False
    head_rows: int = 5
    verbose: int = 1
    output_xlsx: Optional[str] = None  

class GaitResponse(BaseModel):
    effective_movement_rows: int
    effective_gait_rows: int
    preview_effective_movement: List[Dict[str, Any]] = []
    preview_effective_gait: List[Dict[str, Any]] = []


# ---- ENDPOINTS ----

@app.get("/")
async def root(request: Request):
    # Genera la URL completa para la documentación, incluyendo el /msGait
    return RedirectResponse(url=request.url_for("swagger_ui_html"))

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/codeids", response_model=List[str])
def list_codeids(
    start: Optional[str] = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end:   Optional[str] = Query(None, description="YYYY-MM-DD HH:MM:SS"),
):
    """
    Returns the list of CodeIDs (strings) in an optional range.
    Internally uses DataManager.get_codeids_in_range(start, end).
    """
    assert _dm is not None
    try:
        return _dm.get_codeids_in_range(start, end)
    except Exception as e:
        raise HTTPException(500, detail=f"Error retrieving codeids: {e}")

@app.get("/activity-windows")
def activity_windows(
    start: Optional[str] = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end:   Optional[str] = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    ids:   Optional[List[int]] = Query(None, description="activity_all IDs (repeat ?ids=x)"),
    verbose: int = 0,
):
    """
    Returns activity windows from activity_all (DataFrame -> JSON).
    Internally uses DataManager.segments_retrieval(fstart, fend, ids, verbose)
    """
    assert _dm is not None
    try:
        df = _dm.segments_retrieval(
            fstart=start, fend=end, ids=ids, verbose=verbose
        )
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(500, detail=f"Error retrieving activity windows: {e}")

@app.get("/sensor-data")
def sensor_data(
    codeid_id: int,
    foot: Literal["Left", "Right"],
    start: str = Query(..., description="YYYY-MM-DD HH:MM:SS"),
    end:   str = Query(..., description="YYYY-MM-DD HH:MM:SS"),
):
    """
    Returns raw sensor data (DataFrame -> JSON) for a codeid/foot in a range.
    Internally uses MovementDetector.fetch_sensor_data(start, end, codeid_id, foot)
    """
    assert _detector is not None
    try:
        df = _detector.fetch_sensor_data(start, end, codeid_id, foot)
        return df.to_dict(orient="records")
    except Exception as e:
        raise HTTPException(500, detail=f"Error retrieving sensor data: {e}")

@app.post("/gait/detect", response_model=GaitResponse)
def detect_gait(req: GaitRequest):
    """
    Run the detection on a list of activity_all IDs.
    - detect_effective_movement
    - detect_effective_gait
    - (optional) save in PG if req.save=True
    """
    assert _dm is not None and _detector is not None
    try:
        # 1) Base windows from activity_all by IDs
        base_windows = _dm.segments_retrieval(
            fstart=None, fend=None, ids=req.ids, verbose=req.verbose
        )

        # 2) Transform into rows by leg (add columns "codeid_id" and "foot")
        df_legs = _dm.recover_activity_all(base_windows, vb=req.verbose)

        # 3) Effective movement
        df_eff = _detector.detect_effective_movement(
            activity_windows=df_legs,
            nomf=req.output_xlsx,
            vb=req.verbose
        )

        # 4) Gait
        df_gait = _detector.detect_effective_gait(df_eff, vb=req.verbose)

        # 5) Optional storage
        if req.save:
            _detector.save_to_postgresql("effective_movement", df_eff, verbose=req.verbose)
            _detector.save_to_postgresql("effective_gait", df_gait, verbose=req.verbose)

        # 6) Response 
        return GaitResponse(
            effective_movement_rows=len(df_eff),
            effective_gait_rows=len(df_gait),
            preview_effective_movement=df_eff.head(req.head_rows).to_dict(orient="records"),
            preview_effective_gait=df_gait.head(req.head_rows).to_dict(orient="records"),
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gait detection failed: {e}")
