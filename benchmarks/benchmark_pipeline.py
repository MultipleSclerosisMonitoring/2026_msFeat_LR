"""Benchmark de tiempo y memoria por evento del pipeline (LR-11).

Mide cada etapa por separado (consulta, normalización, escritura HDF5, lectura y
métricas) para eventos de distinto tamaño. Por defecto usa señales sintéticas y
no accede a ninguna base de datos; con ``--server`` mide eventos reales del
registro contra InfluxDB. Nunca escribe en el almacén configurado: usa un
directorio temporal.

Ejemplos:
    python benchmarks/benchmark_pipeline.py
    python benchmarks/benchmark_pipeline.py --durations 30,360,1800 --repeats 5
    python benchmarks/benchmark_pipeline.py --server --ids 58,60
"""
import argparse
import logging
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pkgs"), str(ROOT / "src"), str(ROOT)]

from GaitMetricsCalculator import GaitMetricsCalculator
from gait_analysis.logging_utils import configure_logging
from msTools.i18n import _
from gait_analysis.query import build_trial_query, normalize_query_result, split_series
from gait_analysis.registry import Trial, load_registry
from gait_analysis.storage import SignalStore, trial_key

logger = logging.getLogger("msGait.Benchmark")

STAGES = ("consulta", "normalizacion", "escritura", "lectura", "metricas")


def synthetic_response(seconds: int, fs: float) -> list[pd.DataFrame]:
    """Genera la respuesta de un evento sintético con dos pies.

    Args:
        seconds (int): Duración del evento.
        fs (float): Frecuencia de muestreo en Hz.

    Returns:
        list[pd.DataFrame]: Una tabla por pie, con la forma que devuelve
        ``query_data_frame`` (columnas internas de Flux, etiquetas y señales).
    """
    n = int(seconds * fs)
    t = np.arange(n) / fs
    walking = np.sin(2 * np.pi * 1.0 * t)
    start = pd.Timestamp("2025-01-01 10:00:00", tz="UTC")
    frames = []
    for table, side in enumerate(("Left", "Right")):
        frames.append(pd.DataFrame({
            "result": "_result", "table": table, "_start": start, "_stop": start,
            "_time": start + pd.to_timedelta(t, unit="s"),
            "_measurement": "Gait", "CodeID": "BENCH-1", "DeviceName": f"Sock{side}", "Foot": side,
            "mac": f"AA:{side}", "lat": 40.4, "lng": -3.7,
            "Ax": 0.0, "Ay": walking, "Az": 1.0, "Gx": 0.0, "Gy": 0.0, "Gz": 0.0,
            "S0": 500 + 400 * walking, "S1": 500 + 400 * walking, "S2": 500 + 400 * walking,
        }))
    return frames


def measure(function: Callable[[], Any], trace_memory: bool) -> tuple[Any, float, float]:
    """Ejecuta una etapa midiendo su tiempo o su pico de memoria.

    ``tracemalloc`` ralentiza la ejecución, así que las dos magnitudes se miden
    en repeticiones distintas para no falsear los tiempos.

    Args:
        function (Callable[[], Any]): Etapa a ejecutar, sin argumentos.
        trace_memory (bool): Medir el pico de memoria en lugar del tiempo.

    Returns:
        tuple[Any, float, float]: Resultado, segundos empleados y pico de
        memoria en MB (según ``tracemalloc``). La magnitud no medida es ``NaN``.
    """
    if trace_memory:
        tracemalloc.start()
        result = function()
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return result, float("nan"), peak / 1e6
    started = time.perf_counter()
    result = function()
    return result, time.perf_counter() - started, float("nan")


def run_metrics(calculator: GaitMetricsCalculator, frames: dict[str, pd.DataFrame], fs: float) -> None:
    """Calcula las métricas del pipeline sobre cada pie del evento.

    Args:
        calculator (GaitMetricsCalculator): Motor de cálculo.
        frames (dict[str, pd.DataFrame]): Series normalizadas por pie.
        fs (float): Frecuencia de muestreo en Hz.
    """
    for frame in frames.values():
        prepared = calculator.preparar(frame)
        calculator.calcular_cadencia(prepared, excluir_giros=True)
        calculator.calcular_fases(prepared)
        calculator.calcular_longitud_zancada(prepared)
        calculator.calcular_altura_zancada(prepared)


def benchmark_event(
    label: str, trial: Trial, fetch: Callable[[], Any], store: SignalStore, fs: float, repeat: int
) -> dict[str, Any]:
    """Mide todas las etapas de un evento.

    Args:
        label (str): Texto que identifica el caso en la tabla.
        trial (Trial): Evento medido.
        fetch (Callable[[], Any]): Obtiene la respuesta en bruto (consulta real
            o generación sintética).
        store (SignalStore): Almacén temporal abierto para escritura.
        fs (float): Frecuencia de muestreo en Hz.
        repeat (int): Número de repetición. La primera mide memoria; el
            resto, tiempos.

    Returns:
        dict[str, Any]: Filas, tamaño en disco, y tiempo y pico de memoria por etapa.
    """
    calculator = GaitMetricsCalculator()
    memory = repeat == 1
    raw, t_query, m_query = measure(fetch, memory)
    series, t_norm, m_norm = measure(lambda: split_series(normalize_query_result(raw), trial.trial_id), memory)
    payload = {side: (foot.frame, {"id": trial.trial_id, "foot": side}) for side, foot in series.items()}
    keys, t_write, m_write = measure(lambda: store.write_event(trial, payload), memory)
    frames, t_read, m_read = measure(lambda: {side: store.read(key) for side, key in keys.items()}, memory)
    _none, t_metrics, m_metrics = measure(lambda: run_metrics(calculator, frames, fs), memory)

    rows = sum(len(frame) for frame in frames.values())
    size_mb = 0.0
    if keys:
        event_file = store.root / f"p_{trial.codeid}" / trial.test_type / f"trial_{trial.trial_id}.h5"
        size_mb = event_file.stat().st_size / 1e6
    record = {
        "caso": label, "repeticion": repeat, "pies": len(frames), "filas": rows, "hdf5_mb": round(size_mb, 3),
    }
    for stage, seconds, peak in zip(
        STAGES, (t_query, t_norm, t_write, t_read, t_metrics), (m_query, m_norm, m_write, m_read, m_metrics)
    ):
        record[f"{stage}_s"] = round(seconds, 4)
        record[f"{stage}_pico_mb"] = round(peak, 2)
    record["total_s"] = round(t_query + t_norm + t_write + t_read + t_metrics, 4)
    record["filas_por_s"] = round(rows / record["total_s"]) if record["total_s"] > 0 else float("nan")
    logger.debug(_("DEBUG_BENCH_EVENT: {0}").format(record))
    return record


def summarize(records: list[dict[str, Any]]) -> pd.DataFrame:
    """Resume las repeticiones de cada caso con la mediana.

    Args:
        records (list[dict[str, Any]]): Una medida por caso y repetición.

    Returns:
        pd.DataFrame: Una fila por caso con la mediana de cada magnitud.
    """
    table = pd.DataFrame(records).drop(columns=["repeticion"])
    return table.groupby("caso", sort=False).median(numeric_only=True).round(4).reset_index()


def main() -> None:
    """Punto de entrada del benchmark."""
    parser = argparse.ArgumentParser(description="Benchmark de tiempo y memoria por evento")
    parser.add_argument("--durations", default="30,120,360,1800", help="Duraciones sintéticas en segundos")
    parser.add_argument("--fs", type=float, default=100.0, help="Frecuencia de muestreo en Hz")
    parser.add_argument("--repeats", type=int, default=4, help="Repeticiones por caso: la primera mide memoria, el resto tiempos (mediana)")
    parser.add_argument("--server", action="store_true", help="Medir eventos reales del registro contra InfluxDB")
    parser.add_argument("--config", default=None, help="YAML; por defecto config/config.yaml (solo con --server)")
    parser.add_argument("--csv", default=None, help="Sustituye paths.registry_csv (solo con --server)")
    parser.add_argument("--ids", default=None, help="Identificadores de evento separados por comas (solo con --server)")
    parser.add_argument("--output", default=None, help="CSV de resultados; por defecto benchmarks/resultados_<modo>.csv")
    parser.add_argument("--verbose", type=int, choices=(0, 1, 2), default=1, help="0 avisos, 1 información, 2 depuración")
    args = parser.parse_args()
    configure_logging(args.verbose)
    if args.repeats < 2:
        sys.exit("ERROR: --repeats debe ser al menos 2 (una repetición mide memoria y otra tiempos).")

    records: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as folder, SignalStore(Path(folder) / "benchmark.h5") as store:
        if args.server:
            if not args.ids:
                sys.exit("ERROR: --server requiere --ids para acotar los eventos medidos.")
            from influxdb_client import InfluxDBClient
            from msConfig import influx_client_options, load_config
            config = load_config(args.config, sections=("influxdb",))
            inf = config["influxdb"]
            ids = [i.strip() for i in args.ids.split(",") if i.strip()]
            trials, _invalid = load_registry(args.csv or config["paths"]["registry_csv"], None, ids)
            client = InfluxDBClient(**influx_client_options(config))
            try:
                for trial in trials:
                    query = build_trial_query(inf["bucket"], inf["measurement"], trial)
                    label = f"{trial.trial_id} ({trial.test_type}, {trial.duration_s:.0f} s)"
                    for repeat in range(1, args.repeats + 1):
                        logger.info(_("INFO_BENCH_CASE: Evento {0}, repetición {1}/{2}").format(label, repeat, args.repeats))
                        records.append(benchmark_event(
                            label, trial, lambda: client.query_api().query_data_frame(query), store, args.fs, repeat
                        ))
            finally:
                client.close()
        else:
            for seconds in (int(value) for value in args.durations.split(",") if value.strip()):
                trial = Trial(f"bench{seconds}", "BENCH-1", "SYN", "", "", "", "", float(seconds))
                label = f"sintético {seconds} s"
                for repeat in range(1, args.repeats + 1):
                    logger.info(_("INFO_BENCH_CASE: Caso {0}, repetición {1}/{2}").format(label, repeat, args.repeats))
                    records.append(benchmark_event(
                        label, trial, lambda: synthetic_response(seconds, args.fs), store, args.fs, repeat
                    ))

    if not records:
        sys.exit("ERROR: No se midió ningún evento.")
    mode = "servidor" if args.server else "sintetico"
    output = Path(args.output) if args.output else ROOT / "benchmarks" / f"resultados_{mode}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(records).to_csv(output, index=False)
    summary = summarize(records)
    columns = ["caso", "filas", "hdf5_mb", "total_s", "filas_por_s"] + [f"{stage}_s" for stage in STAGES]
    logger.info(_('INFO_BENCH_SUMMARY: Mediana por caso (tiempos en segundos):\n') + summary[columns].to_string(index=False))
    peaks = ["caso"] + [f"{stage}_pico_mb" for stage in STAGES]
    logger.info(_('INFO_BENCH_MEMORY: Pico de memoria por etapa (MB):\n') + summary[peaks].to_string(index=False))
    logger.info(_("INFO_BENCH_SAVED: Medidas completas en {0}").format(output))


if __name__ == "__main__":
    main()
