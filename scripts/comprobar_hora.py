"""Comprueba con datos reales qué criterio horario alinea el registro con InfluxDB (duda D14).

Uso:
    poetry run python scripts/comprobar_hora.py [--n-por-estrato 2] [--semilla 2026]

Para una muestra de eventos del registro, estratificada por prueba y mes, pide a
InfluxDB solo agregados por segundo del sensor del talón (número de filas y
amplitud de la presión), nunca las señales, en una ventana amplia alrededor
del evento: desde tres horas antes hasta una hora después de la hora civil.
Con esos agregados compara dos criterios:

- ``civil``: el que usa hoy el pipeline (hora de reloj del registro, sin zona).
- ``utc``: el instante real del registro convertido a UTC (una hora antes en
  invierno y dos en verano).

Para cada evento informa la fracción de segundos con datos y con actividad de
marcha dentro de la ventana con cada criterio, el desplazamiento que mejor
encaja (buscado minuto a minuto entre -180 y +60 min) y el bloque continuo de
datos más cercano (inicio respecto a la hora civil y duración frente a la
registrada). Encontrar datos no basta para acreditar el criterio: lo que lo
acredita es que el bloque empiece donde dice el criterio y dure lo que dura la
prueba.

El detalle por evento se guarda en ``data/comprobacion_hora/`` (fuera de Git)
sin el código del paciente; por pantalla solo salen recuentos agregados.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pkgs"), str(ROOT / "src")]

from influxdb_client import InfluxDBClient  # noqa: E402
from influxdb_client.client.warnings import MissingPivotFunction  # noqa: E402

from gait_analysis.query import normalize_query_result  # noqa: E402
from gait_analysis.registry import Trial, load_registry  # noqa: E402
from msConfig import influx_client_options, load_config  # noqa: E402
from msTools.flux import flux_string  # noqa: E402
from msTools.timeutils import experiment_wallclock  # noqa: E402

BEFORE = pd.Timedelta(hours=3)
AFTER = pd.Timedelta(hours=1)
SHIFTS_MIN = np.arange(-180, 61)
# Un segundo es activo si la presión del talón varía al menos esta fracción del p95 de la serie.
ACTIVE_FRACTION = 0.25
# Un bloque de datos termina cuando faltan datos más de estos segundos.
BLOCK_GAP_S = 5
# Tolerancia, en minutos, para decir que el mejor desplazamiento coincide con un criterio.
MATCH_MIN = 2
# Segundos antes y después de la ventana en los que se mide si la actividad cae (bordes de la prueba).
EDGE_S = 60
OUTPUT = ROOT / "data" / "comprobacion_hora"
# El aviso de la librería imprime la consulta entera, con el código del paciente: se silencia.
warnings.simplefilter("ignore", MissingPivotFunction)


def sample_trials(trials: list[Trial], per_stratum: int, seed: int) -> list[Trial]:
    """Elige hasta ``per_stratum`` eventos al azar por prueba y mes.

    Args:
        trials (list[Trial]): Eventos válidos del registro.
        per_stratum (int): Máximo de eventos por combinación de prueba y mes.
        seed (int): Semilla, para que la muestra sea reproducible.

    Returns:
        list[Trial]: Eventos elegidos, ordenados por fecha.
    """
    frame = pd.DataFrame({"trial": trials, "test": [t.test_type for t in trials],
                          "month": [experiment_wallclock(t.d_from).strftime("%Y-%m") for t in trials]})
    chosen = frame.groupby(["test", "month"], group_keys=False).apply(
        lambda group: group.sample(min(per_stratum, len(group)), random_state=seed))
    return sorted(chosen["trial"], key=lambda t: experiment_wallclock(t.d_from))


def per_second(client: InfluxDBClient, bucket: str, measurement: str, trial: Trial,
               start: pd.Timestamp, stop: pd.Timestamp) -> pd.DataFrame:
    """Pide filas y amplitud por segundo del sensor del talón, por pie.

    Args:
        client (InfluxDBClient): Cliente conectado.
        bucket (str): Bucket de InfluxDB.
        measurement (str): ``_measurement`` de las señales.
        trial (Trial): Evento (para el código del paciente).
        start (pd.Timestamp): Inicio del rango, en la escala de InfluxDB.
        stop (pd.Timestamp): Fin del rango.

    Returns:
        pd.DataFrame: Columnas ``_time``, ``Foot``, ``filas`` y ``amplitud``.
    """
    rng = f"start: {start.strftime('%Y-%m-%dT%H:%M:%SZ')}, stop: {stop.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    base = f'''
    from(bucket: {flux_string(bucket)})
      |> range({rng})
      |> filter(fn: (r) => r["_measurement"] == {flux_string(measurement)})
      |> filter(fn: (r) => r["CodeID"] == {flux_string(trial.codeid)})
      |> filter(fn: (r) => r["_field"] == "S2")
      |> group(columns: ["Foot"])
    '''
    api = client.query_api()
    counts = normalize_query_result(api.query_data_frame(base + '|> aggregateWindow(every: 1s, fn: count, createEmpty: false)'))
    spreads = normalize_query_result(api.query_data_frame(base + '|> aggregateWindow(every: 1s, fn: spread, createEmpty: false)'))
    if counts.empty:
        return pd.DataFrame(columns=["_time", "Foot", "filas", "amplitud"])
    counts = counts.rename(columns={"_value": "filas"})[["_time", "Foot", "filas"]]
    spreads = spreads.rename(columns={"_value": "amplitud"})[["_time", "Foot", "amplitud"]]
    return counts.merge(spreads, on=["_time", "Foot"], how="left")


def analyse(trial: Trial, seconds: pd.DataFrame) -> dict:
    """Mide, con cada criterio y con cada desplazamiento, datos y actividad en la ventana.

    Args:
        trial (Trial): Evento del registro.
        seconds (pd.DataFrame): Agregados por segundo de :func:`per_second`.

    Returns:
        dict: Resultado del evento, sin el código del paciente.
    """
    civil_start = experiment_wallclock(trial.d_from)
    duration = pd.Timedelta(seconds=trial.duration_s)
    offset_h = (pd.Timestamp(trial.d_from).utcoffset() or pd.Timedelta(0)) / pd.Timedelta(hours=1)
    row = {"trial_id": trial.trial_id, "prueba": trial.test_type, "mes": civil_start.strftime("%Y-%m"),
           "desfase_registro_h": offset_h, "duracion_registro_s": trial.duration_s}
    origin = civil_start - BEFORE
    n = int((BEFORE + duration + AFTER).total_seconds()) + 1
    data = np.zeros(n, dtype=bool)
    active = np.zeros(n, dtype=bool)
    if not seconds.empty:
        for _foot, foot in seconds.groupby("Foot"):
            index = ((pd.to_datetime(foot["_time"]) - origin).dt.total_seconds() - 1).round().astype(int).to_numpy()
            keep = (index >= 0) & (index < n)
            data[index[keep]] = True
            spread = foot["amplitud"].to_numpy(dtype=float)
            p95 = np.nanpercentile(spread, 95) if np.isfinite(spread).any() else np.nan
            if np.isfinite(p95) and p95 > 0:
                active[index[keep & (spread >= ACTIVE_FRACTION * p95)]] = True
    length = max(1, int(duration.total_seconds()))

    def window(shift_min: float) -> tuple[float, float]:
        first = int(BEFORE.total_seconds() + shift_min * 60)
        part = slice(max(0, first), max(0, first + length))
        return float(data[part].mean()) if data[part].size else 0.0, float(active[part].mean()) if active[part].size else 0.0

    row["datos_civil"], row["actividad_civil"] = window(0)
    row["datos_utc"], row["actividad_utc"] = window(-60 * offset_h)

    def edges(shift_min: float) -> tuple[float, float]:
        """Actividad en los EDGE_S segundos anteriores y posteriores a la ventana."""
        first = int(BEFORE.total_seconds() + shift_min * 60)
        before = active[max(0, first - EDGE_S):max(0, first)]
        after = active[max(0, first + length):max(0, first + length + EDGE_S)]
        return (float(before.mean()) if before.size else np.nan, float(after.mean()) if after.size else np.nan)

    # Si la ventana es la prueba, la actividad cae justo antes de empezar y justo después de terminar.
    row["actividad_antes_utc"], row["actividad_despues_utc"] = edges(-60 * offset_h)
    row["actividad_antes_civil"], row["actividad_despues_civil"] = edges(0)
    scores = np.array([window(shift) for shift in SHIFTS_MIN])
    if scores[:, 0].max() > 0:
        best = SHIFTS_MIN[np.lexsort((-np.abs(SHIFTS_MIN), scores[:, 1], scores[:, 0]))[-1]]
        row["mejor_desplazamiento_min"] = int(best)
        row["coincide"] = ("civil" if abs(best) <= MATCH_MIN else
                           "utc" if abs(best + 60 * offset_h) <= MATCH_MIN else "otro")
    else:
        row["mejor_desplazamiento_min"], row["coincide"] = np.nan, "sin_datos"
    # Bloques continuos de datos: el más cercano a cualquiera de los dos criterios.
    on = np.flatnonzero(data)
    if on.size:
        cuts = np.flatnonzero(np.diff(on) > BLOCK_GAP_S)
        starts, stops = on[np.r_[0, cuts + 1]], on[np.r_[cuts, on.size - 1]]
        references = (BEFORE.total_seconds(), BEFORE.total_seconds() - 3600 * offset_h)
        nearest = int(np.argmin([min(abs(s - r) for r in references) for s in starts]))
        row["bloques"] = len(starts)
        row["bloque_inicio_min"] = round((starts[nearest] - BEFORE.total_seconds()) / 60, 2)
        row["bloque_duracion_s"] = int(stops[nearest] - starts[nearest] + 1)
    else:
        row["bloques"], row["bloque_inicio_min"], row["bloque_duracion_s"] = 0, np.nan, np.nan
    return row


def main(argv: list[str] | None = None) -> pd.DataFrame:
    """Ejecuta la comprobación y guarda el detalle por evento.

    Args:
        argv (list[str] | None): Argumentos; por defecto los de la línea de órdenes.

    Returns:
        pd.DataFrame: Una fila por evento analizado.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None)
    parser.add_argument("--n-por-estrato", type=int, default=2)
    parser.add_argument("--semilla", type=int, default=2026)
    parser.add_argument("--prueba", default=None, help="Solo este código de prueba (por ejemplo 6MWT)")
    parser.add_argument("--ids", default=None, help="Identificadores de evento separados por comas, en lugar de la muestra")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    wanted = [i.strip() for i in args.ids.split(",") if i.strip()] if args.ids else None
    trials, _invalid = load_registry(config["paths"]["registry_csv"], args.prueba, wanted,
                                     config.get("registry", {}).get("exclude_codeids"))
    chosen = trials if wanted else sample_trials(trials, args.n_por_estrato, args.semilla)
    print(f"{len(chosen)} eventos elegidos de {len(trials)} (hasta {args.n_por_estrato} por prueba y mes, semilla {args.semilla})")
    rows = []
    with InfluxDBClient(**influx_client_options(config)) as client:
        for position, trial in enumerate(chosen, 1):
            civil_start = experiment_wallclock(trial.d_from)
            stop = civil_start + pd.Timedelta(seconds=trial.duration_s) + AFTER
            try:
                seconds = per_second(client, config["influxdb"]["bucket"], config["influxdb"]["measurement"],
                                     trial, civil_start - BEFORE, stop)
                rows.append(analyse(trial, seconds))
            except Exception as error:  # se informa y se sigue con el siguiente evento
                rows.append({"trial_id": trial.trial_id, "prueba": trial.test_type, "error": str(error)[:200]})
            print(f"  [{position}/{len(chosen)}] evento {trial.trial_id} ({trial.test_type})", flush=True)
    result = pd.DataFrame(rows)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    path = OUTPUT / (f"comprobacion_hora_ids_{len(chosen)}.csv" if wanted else f"comprobacion_hora_{args.prueba or 'todas'}_semilla{args.semilla}.csv")
    result.to_csv(path, index=False)
    print(f"Detalle por evento: {path}")
    if "coincide" in result:
        result["periodo"] = np.where(result["mes"] < "2025-11", "antes de 2025-11", "desde 2025-11")
        print("\nMejor desplazamiento por prueba y periodo:")
        print(pd.crosstab([result["prueba"], result["periodo"]], result["coincide"], margins=True).to_string())
        print("\nEventos con algún dato en la ventana:",
              f"civil {int((result['datos_civil'] > 0).sum())}, utc {int((result['datos_utc'] > 0).sum())} de {len(result)}")
    return result


if __name__ == "__main__":
    main()
