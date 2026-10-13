"""Contrato único de consulta a InfluxDB y normalización de las señales."""
import logging
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from msTools.flux import flux_range, flux_string
from msTools.i18n import _

from .registry import Trial

logger = logging.getLogger("msGait.Query")

# Columnas internas de Flux sin significado para el evento; el resto de metadatos se conserva.
_FLUX_INTERNAL_COLUMNS = ("result", "table", "_start", "_stop")
_FOOT_COLUMNS = ("Foot", "foot")
_DEVICE_COLUMNS = ("mac", "DeviceName")
UNKNOWN_SIDE = "Unknown"


@dataclass
class FootSeries:
    """Serie física de un pie dentro de un evento, ya normalizada.

    Attributes:
        side (str): ``Left``, ``Right`` o ``Unknown``.
        frame (pd.DataFrame): Muestras ordenadas por ``_time`` y sin filas idénticas.
        device (str): Dispositivos usados, en orden de tiempo (cadena vacía si
            no hay etiqueta).
        devices_found (dict[str, int]): Muestras recibidas por dispositivo.
        rows_received (int): Muestras del pie antes de deduplicar.
        exact_duplicates (int): Filas idénticas eliminadas.
        time_conflicts (int): Filas que repiten ``_time`` con otros valores; se
            conservan (son muestras reales recibidas a la vez).
        overlap_dropped (int): Filas descartadas donde dos dispositivos se
            solapan en el tiempo.
        was_sorted (bool): Si las muestras llegaron ya ordenadas por tiempo.
    """
    side: str
    frame: pd.DataFrame
    device: str = ""
    devices_found: dict[str, int] = field(default_factory=dict)
    rows_received: int = 0
    exact_duplicates: int = 0
    time_conflicts: int = 0
    overlap_dropped: int = 0
    was_sorted: bool = True

    def report(self) -> dict[str, Any]:
        """Resume la normalización aplicada para el manifest.

        Returns:
            dict[str, Any]: Filas finales, dispositivo y recuentos de limpieza.
        """
        return {
            "rows": len(self.frame), "rows_received": self.rows_received, "device": self.device,
            "devices_found": self.devices_found, "exact_duplicates": self.exact_duplicates,
            "time_conflicts": self.time_conflicts, "overlap_dropped": self.overlap_dropped, "was_sorted": self.was_sorted,
        }


def build_trial_query(bucket: str, measurement: str, trial: Trial) -> str:
    """Construye la consulta Flux de un evento.

    Es la única definición de selección: mismo ``_measurement``, paciente y rango
    para el extractor y para el pipeline directo.

    Args:
        bucket (str): Bucket de InfluxDB.
        measurement (str): Valor de ``_measurement`` a seleccionar.
        trial (Trial): Evento validado del registro.

    Returns:
        str: Consulta Flux con las señales pivotadas en columnas. Todos los
        valores se insertan como literales escapados.

    Raises:
        ValueError: Si un valor contiene caracteres de control o el rango no es válido.
    """
    return f'''
    from(bucket: {flux_string(bucket)})
      |> range({flux_range(trial.start_iso, trial.stop_iso)})
      |> filter(fn: (r) => r["_measurement"] == {flux_string(measurement)})
      |> filter(fn: (r) => r["CodeID"] == {flux_string(trial.codeid)})
      |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
    '''


def normalize_query_result(result: Any) -> pd.DataFrame:
    """Unifica la respuesta de ``query_data_frame`` en un único DataFrame.

    El cliente devuelve un DataFrame o una lista de ellos (una tabla por serie).

    Args:
        result (Any): DataFrame, lista de DataFrames o ``None``.

    Returns:
        pd.DataFrame: Señales con ``_time`` sin zona horaria y sin columnas
        internas de Flux. Vacío si no hay datos.

    Raises:
        TypeError: Si la respuesta no es de un tipo previsto.
        ValueError: Si hay datos pero no contienen la columna ``_time``.
    """
    if result is None:
        return pd.DataFrame()
    if isinstance(result, pd.DataFrame):
        frames = [result]
    elif isinstance(result, (list, tuple)):
        if any(not isinstance(item, pd.DataFrame) for item in result):
            raise TypeError(_("La respuesta de InfluxDB contiene elementos que no son DataFrame"))
        frames = list(result)
    else:
        raise TypeError(_("Respuesta de InfluxDB de tipo no previsto: {0}").format(type(result).__name__))

    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    df = frames[0] if len(frames) == 1 else pd.concat(frames, ignore_index=True, sort=False)
    logger.debug(_("DEBUG_QUERY_TABLES: Respuesta con {0} tabla(s) y {1} filas").format(len(frames), len(df)))
    if "_time" not in df.columns:
        raise ValueError(_("La respuesta de InfluxDB no contiene la columna _time"))

    df = df.drop(columns=[column for column in _FLUX_INTERNAL_COLUMNS if column in df.columns])
    df["_time"] = pd.to_datetime(df["_time"])
    if df["_time"].dt.tz is not None:
        df["_time"] = df["_time"].dt.tz_localize(None)
    return df.reset_index(drop=True)


def fetch_trial_frame(client: Any, bucket: str, measurement: str, trial: Trial) -> pd.DataFrame:
    """Consulta las señales de un evento y las devuelve normalizadas.

    Args:
        client (Any): Cliente ``InfluxDBClient`` ya conectado.
        bucket (str): Bucket de InfluxDB.
        measurement (str): Valor de ``_measurement`` a seleccionar.
        trial (Trial): Evento validado del registro.

    Returns:
        pd.DataFrame: Señales del evento; vacío si no hay datos.

    Raises:
        Exception: Los errores del cliente o de normalización se propagan para
            que quien llama los registre como resultado del evento.
    """
    query = build_trial_query(bucket, measurement, trial)
    return normalize_query_result(client.query_api().query_data_frame(query))


def _normalize_series(side: str, frame: pd.DataFrame, trial_id: str) -> FootSeries:
    """Une los dispositivos de un pie, ordena y quita las filas idénticas.

    Un pie puede tener más de un dispositivo en un ensayo: si se agota una
    batería se cambia el calcetín, así que en cada instante hay un único
    registro y los tramos de todos los dispositivos se unen por tiempo. Si dos
    dispositivos se solapan, en el tramo común se conserva el que tiene más
    muestras en él. Dos filas con la misma marca de tiempo y valores distintos
    se conservan las dos: la marca es la de recepción y son muestras reales.
    """
    series = FootSeries(side, frame, rows_received=len(frame))
    # Identidad del dispositivo: la primera etiqueta disponible que tenga valores.
    labels = {
        column: frame[column].fillna("").astype(str).str.strip()
        for column in _DEVICE_COLUMNS if column in frame.columns
    }
    devices = next((values for values in labels.values() if values.ne("").any()), None)
    if devices is not None:
        counts = devices.value_counts()
        series.devices_found = {str(name): int(n) for name, n in counts.items()}
        spans = frame.groupby(devices)["_time"].agg(["min", "max"]).sort_values("min")
        series.device = ", ".join(str(name) for name in spans.index)
        if len(counts) > 1:
            keep = pd.Series(True, index=frame.index)
            names = list(spans.index)
            for position, first in enumerate(names):
                for second in names[position + 1:]:
                    start, stop = max(spans.at[first, "min"], spans.at[second, "min"]), min(spans.at[first, "max"], spans.at[second, "max"])
                    if start > stop:
                        continue
                    inside = (frame["_time"] >= start) & (frame["_time"] <= stop) & keep
                    in_first, in_second = int((inside & (devices == first)).sum()), int((inside & (devices == second)).sum())
                    loser = second if in_first >= in_second else first    # a igualdad, se conserva el que empezó antes
                    dropped = inside & (devices == loser)
                    keep &= ~dropped
                    series.overlap_dropped += int(dropped.sum())
                    logger.warning(
                        _("WARN_DEVICES_OVERLAP: Evento {0} ({1}): los dispositivos '{2}' y '{3}' se solapan entre {4} y {5}; en ese tramo se descartan {6} filas de '{7}'").format(
                            trial_id, side, first, second, start, stop, int(dropped.sum()), loser)
                    )
            frame = frame[keep]
            logger.info(
                _("INFO_MULTIPLE_DEVICES: Evento {0} ({1}) con {2} dispositivos {3}; sus tramos se unen por tiempo").format(trial_id, side, len(counts), series.devices_found)
            )

    series.was_sorted = bool(frame["_time"].is_monotonic_increasing)
    frame = frame.sort_values("_time", kind="stable")
    exact = frame.duplicated(keep="first")
    series.exact_duplicates = int(exact.sum())
    frame = frame[~exact].reset_index(drop=True)
    series.time_conflicts = int(frame.duplicated(subset="_time", keep="first").sum())

    if series.exact_duplicates or series.time_conflicts:
        logger.info(
            _("INFO_DUPLICATE_SAMPLES: Evento {0} ({1}): eliminadas {2} filas idénticas; {3} filas repiten _time con valores distintos y se conservan").format(trial_id, side, series.exact_duplicates, series.time_conflicts)
        )
    if not series.was_sorted:
        logger.debug(_("DEBUG_SERIES_SORTED: Evento {0} ({1}) llegó desordenado y se ordenó por _time").format(trial_id, side))
    if not frame["_time"].is_monotonic_increasing:
        raise ValueError(_("La serie {0} del evento {1} no es creciente en _time").format(side, trial_id))
    series.frame = frame
    return series


def split_series(df: pd.DataFrame, trial_id: str = "") -> dict[str, FootSeries]:
    """Separa las señales de un evento en una serie física por pie.

    Dentro de cada pie une los tramos de sus dispositivos, ordena por
    ``_time`` y elimina las filas idénticas.

    Args:
        df (pd.DataFrame): Señales normalizadas de un evento.
        trial_id (str): Identificador del evento, usado en los mensajes.

    Returns:
        dict[str, FootSeries]: Series por pie. Sin etiqueta de pie, una única
        serie ``Unknown``.

    Raises:
        ValueError: Si una serie no queda creciente en ``_time``.
    """
    if df.empty:
        return {}
    foot_column = next((column for column in _FOOT_COLUMNS if column in df.columns), None)
    if foot_column is None:
        logger.warning(_("WARN_MISSING_FOOT_TAG: Evento {0} sin etiqueta de pie; se trata como '{1}'").format(trial_id, UNKNOWN_SIDE))
        return {UNKNOWN_SIDE: _normalize_series(UNKNOWN_SIDE, df, trial_id)}

    sides = df[foot_column].fillna("").astype(str).str.strip().str.capitalize()
    sides = sides.where(~sides.isin(["", "Nan", "None"]), UNKNOWN_SIDE)
    return {
        side: _normalize_series(side, df[sides == side], trial_id)
        for side in sorted(sides.unique())
    }
