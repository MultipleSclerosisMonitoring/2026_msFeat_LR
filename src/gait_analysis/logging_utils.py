"""Política de información (verbose) por niveles compartida por los comandos."""
import logging
from collections import Counter
from typing import Iterable
from msTools.i18n import _

ROOT_LOGGER = "msGait"

# 0 = solo avisos y errores, 1 = progreso por evento y resumen, 2 = detalle de depuración.
VERBOSITY_LEVELS = {0: logging.WARNING, 1: logging.INFO, 2: logging.DEBUG}


def configure_logging(verbose: int = 1) -> int:
    """Configura la salida de los loggers ``msGait.*`` según el nivel pedido.

    Las bibliotecas externas se mantienen en WARNING para que el nivel 2 muestre
    solo el detalle del proyecto.

    Args:
        verbose (int): 0 (avisos y errores), 1 (información) o 2 (depuración).
            Valores fuera de rango se ajustan al extremo más cercano.

    Returns:
        int: Nivel de ``logging`` aplicado a los loggers del proyecto.
    """
    level = VERBOSITY_LEVELS[max(0, min(2, int(verbose)))]
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s", force=True)
    logging.getLogger(ROOT_LOGGER).setLevel(level)
    return level


def log_summary(logger: logging.Logger, results: Iterable, title: str) -> dict[str, int]:
    """Registra en nivel INFO el recuento de eventos por estado.

    Args:
        logger (logging.Logger): Logger del comando que termina.
        results (Iterable): Resultados por evento con atributo ``status``.
        title (str): Texto que identifica la ejecución resumida.

    Returns:
        dict[str, int]: Número de eventos por estado.
    """
    counts = dict(Counter(result.status for result in results))
    total = sum(counts.values())
    detail = ", ".join(f"{status}={n}" for status, n in sorted(counts.items())) or "sin eventos"
    logger.info(_("INFO_RUN_SUMMARY: {0}: {1} eventos ({2})").format(title, total, detail))
    return counts
