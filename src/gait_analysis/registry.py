"""Validación del registro CSV de eventos y resultado por fila."""
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import pandas as pd

from msTools.timeutils import experiment_wallclock, influx_wallclock_z
from msTools.i18n import _

logger = logging.getLogger("msGait.Registry")

REQUIRED_COLUMNS = ("id", "codeid", "d_from", "d_until", "t_code")

# Estados posibles de un evento.
STATUS_OK = "OK"
STATUS_EMPTY = "EMPTY"
STATUS_INVALID_ROW = "INVALID_ROW"
STATUS_QUERY_ERROR = "QUERY_ERROR"
STATUS_WRITE_ERROR = "WRITE_ERROR"
STATUS_SKIPPED = "SKIPPED"
STATUS_EXCLUDED = "EXCLUDED"

# Los valores se interpolan en Flux y en claves HDF5: solo caracteres seguros.
_SAFE_VALUE = re.compile(r"[A-Za-z0-9_.\-]+")


class RegistryError(ValueError):
    """El registro no puede usarse como conjunto (por ejemplo, faltan columnas)."""


@dataclass(frozen=True)
class Trial:
    """Evento validado del registro.

    Attributes:
        trial_id (str): Identificador estable del evento (columna ``id``).
        codeid (str): Identificador del paciente.
        test_type (str): Código del test (columna ``t_code``).
        d_from (str): Inicio tal como figura en el registro.
        d_until (str): Fin tal como figura en el registro.
        start_iso (str): Inicio en el formato que espera ``range()`` de Flux.
        stop_iso (str): Fin en el formato que espera ``range()`` de Flux.
        duration_s (float): Duración del evento en segundos.
    """
    trial_id: str
    codeid: str
    test_type: str
    d_from: str
    d_until: str
    start_iso: str
    stop_iso: str
    duration_s: float


@dataclass
class TrialResult:
    """Resultado de procesar una fila del registro.

    Attributes:
        trial_id (str): Identificador del evento, o posición si no es utilizable.
        codeid (str): Identificador del paciente.
        test_type (str): Código del test.
        status (str): Uno de los estados ``STATUS_*``.
        detail (str): Motivo legible cuando el estado no es ``OK``.
        d_from (str): Inicio tal como figura en el registro.
        d_until (str): Fin tal como figura en el registro.
        source (str): Origen de las señales (``influx`` o ``hdf5``).
        feet (dict): Por pie: clave HDF5, filas, dispositivo y duplicados eliminados.
        elapsed_s (float): Tiempo empleado en el evento.
        signal_mem_mb (float): Memoria ocupada por las señales del evento.
        attempts (int): Consultas realizadas hasta obtener respuesta o agotar los reintentos.
    """
    trial_id: str
    codeid: str = ""
    test_type: str = ""
    status: str = STATUS_OK
    detail: str = ""
    d_from: str = ""
    d_until: str = ""
    source: str = "influx"
    feet: dict[str, dict[str, Any]] = field(default_factory=dict)
    elapsed_s: float = 0.0
    signal_mem_mb: float = 0.0
    attempts: int = 1

    @classmethod
    def from_trial(cls, trial: Trial) -> "TrialResult":
        """Crea un resultado en estado ``OK`` para un evento validado.

        Args:
            trial (Trial): Evento del registro.

        Returns:
            TrialResult: Resultado inicial del evento.
        """
        return cls(trial.trial_id, trial.codeid, trial.test_type, d_from=trial.d_from, d_until=trial.d_until)

    def to_record(self) -> dict[str, Any]:
        """Convierte el resultado en un diccionario serializable.

        Returns:
            dict[str, Any]: Campos del resultado.
        """
        return {
            "trial_id": self.trial_id, "codeid": self.codeid, "test_type": self.test_type,
            "status": self.status, "detail": self.detail, "d_from": self.d_from,
            "d_until": self.d_until, "source": self.source, "feet": self.feet,
            "elapsed_s": round(self.elapsed_s, 3), "signal_mem_mb": round(self.signal_mem_mb, 3),
            "attempts": self.attempts,
        }


def _row_problem(row: pd.Series, duplicated_ids: set[str]) -> str:
    """Devuelve el motivo por el que una fila no es válida, o cadena vacía."""
    if not _SAFE_VALUE.fullmatch(row["id"]):
        return "id vacío o con caracteres no admitidos"
    if row["id"] in duplicated_ids:
        return "id repetido en el registro"
    for column in ("codeid", "t_code"):
        if not _SAFE_VALUE.fullmatch(row[column]):
            return f"{column} vacío o con caracteres no admitidos"
    try:
        start = experiment_wallclock(row["d_from"])
        stop = experiment_wallclock(row["d_until"])
    except (ValueError, TypeError, OverflowError):
        return "d_from o d_until no son fechas interpretables"
    if pd.isna(start) or pd.isna(stop):
        return "d_from o d_until vacíos"
    if start >= stop:
        return "rango invertido o vacío (d_from >= d_until)"
    return ""


def load_registry(
    csv_path: str,
    test_type: str | None = None,
    ids: Iterable[str] | None = None,
    exclude_codeids: Iterable[str] | None = None,
) -> tuple[list[Trial], list[TrialResult]]:
    """Lee y valida el registro completo antes de acceder a ninguna base de datos.

    Los eventos de los pacientes de ``exclude_codeids`` (por ejemplo, pruebas
    del desarrollador sin los calcetines puestos) no se procesan: se devuelven
    con estado ``EXCLUDED`` y el registro no se modifica.

    Se validan todas las filas, también las que quedan fuera de la selección
    (estas solo se informan). Una fila inválida no aborta el lote: se devuelve
    con estado ``INVALID_ROW``.

    Args:
        csv_path (str): Ruta al CSV con columnas ``id``, ``codeid``, ``d_from``,
            ``d_until`` y ``t_code``.
        test_type (str | None): Código de test a seleccionar. ``None`` o ``"all"``
            seleccionan todos los tipos.
        ids (Iterable[str] | None): Identificadores de evento a seleccionar.
            ``None`` selecciona todos.
        exclude_codeids (Iterable[str] | None): Pacientes cuyos eventos se excluyen.

    Returns:
        tuple[list[Trial], list[TrialResult]]: Eventos válidos seleccionados y
        resultados ``INVALID_ROW`` o ``EXCLUDED`` de las filas seleccionadas que no se procesan.

    Raises:
        FileNotFoundError: Si el CSV no existe.
        RegistryError: Si el CSV no puede leerse o le faltan columnas requeridas.
    """
    if not os.path.exists(csv_path):
        raise FileNotFoundError(_("No se encuentra el registro {0}").format(csv_path))
    try:
        table = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeError) as error:
        raise RegistryError(_("El registro {0} no es un CSV legible: {1}").format(csv_path, error)) from None
    missing = [column for column in REQUIRED_COLUMNS if column not in table.columns]
    if missing:
        raise RegistryError(_("Al registro le faltan columnas requeridas: {0}").format(', '.join(missing)))

    for column in REQUIRED_COLUMNS:
        table[column] = table[column].str.strip()
    duplicated_ids = set(table.loc[table["id"].duplicated(keep=False) & (table["id"] != ""), "id"])
    wanted_ids = None if ids is None else {str(i).strip() for i in ids}
    all_types = test_type is None or test_type.lower() == "all"

    trials: list[Trial] = []
    invalid: list[TrialResult] = []
    unselected_invalid = 0
    excluded_codes = {str(code).strip() for code in (exclude_codeids or ())}
    for position, row in table.iterrows():
        # Se valida el registro completo; la selección solo decide qué se devuelve.
        problem = _row_problem(row, duplicated_ids)
        selected = (wanted_ids is None or row["id"] in wanted_ids) and (all_types or row["t_code"] in ("", test_type))
        if not selected:
            if problem:
                unselected_invalid += 1
                logger.info(_("INFO_INVALID_ROW_UNSELECTED: Fila {0} del registro (id='{1}'), fuera de la selección, no es válida: {2}").format(position + 2, row['id'], problem))
            continue
        if row["codeid"] in excluded_codes:
            invalid.append(TrialResult(
                row["id"] or f"fila_{position + 2}", row["codeid"], row["t_code"], status=STATUS_EXCLUDED,
                detail="paciente excluido en la configuración (registry.exclude_codeids)",
                d_from=row["d_from"], d_until=row["d_until"],
            ))
            continue
        if problem:
            logger.warning(_("WARN_INVALID_ROW: Fila {0} del registro (id='{1}') descartada: {2}").format(position + 2, row['id'], problem))
            invalid.append(TrialResult(
                row["id"] or f"fila_{position + 2}", row["codeid"], row["t_code"],
                status=STATUS_INVALID_ROW, detail=problem, d_from=row["d_from"], d_until=row["d_until"],
            ))
            continue
        start = experiment_wallclock(row["d_from"])
        stop = experiment_wallclock(row["d_until"])
        trials.append(Trial(
            row["id"], row["codeid"], row["t_code"], row["d_from"], row["d_until"],
            influx_wallclock_z(row["d_from"]), influx_wallclock_z(row["d_until"]),
            round((stop - start).total_seconds(), 2),
        ))

    if wanted_ids is not None:
        absent = wanted_ids - set(table["id"])
        if absent:
            logger.warning(_("WARN_IDS_NOT_FOUND: Identificadores pedidos que no están en el registro: {0}").format(sorted(absent)))
    logger.info(
        _("INFO_REGISTRY_LOADED: Registro validado ({0} filas): {1} eventos válidos seleccionados, {2} filas inválidas seleccionadas, {3} inválidas fuera de la selección").format(len(table), len(trials), len(invalid), unselected_invalid)
    )
    return trials, invalid
