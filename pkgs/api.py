from fastapi import FastAPI, HTTPException, Query, Request, Security, Depends, status
from fastapi.security import APIKeyHeader
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import List, Optional, Literal, Dict, Any
from datetime import datetime, timedelta
import os
from pathlib import Path
import logging
from msTools.i18n import _

# Configuración del Logger para la API
logger = logging.getLogger("msGait.API")

app = FastAPI(
    title="MS Monitoring API",
    version="0.1.0",
    description="API to display activity windows and gait detection using existing logic.",
    root_path="/msGait"
)

# --- SEGURIDAD Y CONTROL DE ACCESO ---
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=True)

def get_api_key(api_key: str = Security(api_key_header)) -> str:
    """Verifica la autenticación mediante token estático/entorno para proteger la API."""
    expected_key = os.getenv("MSGAIT_API_KEY", "DEV_TOKEN_SEGURIDAD_UPM")
    if api_key != expected_key:
        logger.warning(_("WARN_AUTH_FAILED: Intento de acceso denegado a la API con token inválido."))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=_("ERR_UNAUTHORIZED: Credenciales inválidas"),
        )
    return api_key

# --- RESTRICCIÓN DE DIRECTORIO (Prevención de Path Traversal) ---
SECURE_EXPORT_DIR = Path("data/exports").resolve()
SECURE_EXPORT_DIR.mkdir(parents=True, exist_ok=True)

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
    _dm = DataManager(config_path=None)

    now = datetime.utcnow()
    fstart = (now - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
    fend   = now.strftime("%Y-%m-%d %H:%M:%S")

    _detector = MovementDetector(
        config_file=None,
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
    start: Optional[datetime] = Query(None, description="YYYY-MM-DD HH:MM:SS"),
    end:   Optional[datetime] = Query(None, description="YYYY-MM-DD HH:MM:SS"),
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
    api_key: str = Depends(get_api_key),
):
    """
    Returns raw sensor data (DataFrame -> JSON) for a codeid/foot in a range.
    Requiere autenticación mediante X-API-Key.
    """
    assert _detector is not None
    try:
        df = _detector.fetch_sensor_data(start, end, codeid_id, foot)
        return df.to_dict(orient="records")
    except Exception as e:
        logger.error(_("ERR_SENSOR_READ: Fallo al leer sensores: {e}").format(e=e))
        raise HTTPException(500, detail=f"Error retrieving sensor data: {e}")

@app.post("/gait/detect", response_model=GaitResponse)
def detect_gait(req: GaitRequest, api_key: str = Depends(get_api_key)):
    """
    Run the detection on a list of activity_all IDs.
    Requiere autenticación mediante X-API-Key y restringe la carpeta de salida.
    """
    assert _dm is not None and _detector is not None
    try:
        # --- PROTECCIÓN: Sanitización de ruta de exportación ---
        seguro_nomf = None
        if req.output_xlsx:
            # Extraemos solo el nombre del archivo, purgando rutas maliciosas (ej. ../../etc/)
            safe_filename = os.path.basename(req.output_xlsx)
            if not safe_filename.endswith(".xlsx"):
                safe_filename += ".xlsx"
            seguro_nomf = str(SECURE_EXPORT_DIR / safe_filename)
            logger.info(_("INFO_SECURE_EXPORT: Ruta restingida a {p}").format(p=seguro_nomf))

        # 1) Base windows from activity_all by IDs
        base_windows = _dm.segments_retrieval(
            fstart=None, fend=None, ids=req.ids, verbose=req.verbose
        )

        # 2) Transform into rows by leg (add columns "codeid_id" and "foot")
        df_legs = _dm.recover_activity_all(base_windows, vb=req.verbose)

        # 3) Effective movement (Usando la ruta segura obligatoria)
        df_eff = _detector.detect_effective_movement(
            activity_windows=df_legs,
            nomf=seguro_nomf,
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
        logger.error(_("ERR_GAIT_DETECT: Fallo en la detección de marcha: {e}").format(e=e))
        raise HTTPException(status_code=500, detail=f"Gait detection failed: {e}")
