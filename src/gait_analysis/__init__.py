"""Paquete de extracción y preparación de señales de marcha."""
import sys
from pathlib import Path

# Los paquetes heredados (msTools, msConfig) viven en pkgs/ cuando se ejecuta desde el checkout.
_PKGS = Path(__file__).resolve().parents[2] / "pkgs"
if _PKGS.is_dir() and str(_PKGS) not in sys.path:
    sys.path.append(str(_PKGS))

# Versión del pipeline; se escribe en la hoja Procedencia. Debe coincidir con pyproject.toml.
__version__ = "0.1.0"
