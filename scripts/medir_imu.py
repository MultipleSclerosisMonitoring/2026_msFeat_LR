"""Mide en las series guardadas la tasa real de la IMU y la presión activa (dudas D03 y D20).

Uso:
    poetry run python scripts/medir_imu.py

Para cada serie del almacén HDF5 calcula, sin modificar nada:

- Filas por segundo y valores nuevos de la IMU por segundo (una fila aporta un
  valor nuevo si cambia alguno de ``Ax`` a ``Gz``).
- Distribución de los intervalos entre valores nuevos de la IMU (mediana,
  percentil 95 y fracción de intervalos de más de 100 ms).
- Segundos de presión activa y, dentro de ellos, la fracción del tiempo que
  queda fuera de huecos de la IMU (la regla propuesta en D03: válida si llega
  a ``min_coverage``) y la fracción de filas que repiten valor (la regla actual).

El calcetín se identifica con una etiqueta anónima (``calcetin_01``...) en
lugar de su dirección MAC. El detalle se guarda en ``data/medicion_imu/``
(fuera de Git) y por pantalla solo salen resúmenes agregados.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pkgs"), str(ROOT / "src")]

from gait_analysis.metrics import IMU_CHANNELS, GaitMetricsCalculator, ProcessingConfig  # noqa: E402
from gait_analysis.storage import SignalStore, list_keys  # noqa: E402
from msConfig import load_config  # noqa: E402
from msTools.timeutils import criterion_for  # noqa: E402

OUTPUT = ROOT / "data" / "medicion_imu"


def imu_new_values(frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """Devuelve los instantes (s) de cada fila y la máscara de filas con valor nuevo de la IMU."""
    t = (pd.to_datetime(frame["_time"]) - pd.to_datetime(frame["_time"]).iloc[0]).dt.total_seconds().to_numpy()
    values = frame[list(IMU_CHANNELS)].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    new = np.ones(len(values), dtype=bool)
    new[1:] = (np.nan_to_num(values[1:], nan=np.inf) != np.nan_to_num(values[:-1], nan=np.inf)).any(axis=1)
    new &= ~np.isnan(values).any(axis=1)
    return t, new


def measure(frame: pd.DataFrame, calc: GaitMetricsCalculator, gap_s: float) -> dict:
    """Mide una serie.

    Args:
        frame (pd.DataFrame): Serie de un pie tal como está en el almacén.
        calc (GaitMetricsCalculator): Calculador con la configuración actual.
        gap_s (float): Tiempo sin valor nuevo a partir del cual hay hueco.

    Returns:
        dict: Medidas de la serie.
    """
    t, new = imu_new_values(frame)
    duration = float(t[-1] - t[0]) if len(t) > 1 else np.nan
    times = t[new]
    intervals = np.diff(times)
    prepared = calc.preparar(frame)
    active = calc._pressure_active(prepared.data, prepared.fs)
    tp = prepared.t
    # Tiempo desde el último valor nuevo de la IMU en cada muestra preparada.
    last = np.full(len(tp), np.nan)
    if times.size:
        position = np.searchsorted(times, tp, side="right") - 1
        ok = position >= 0
        last[ok] = tp[ok] - times[position[ok]]
    in_gap = ~(last <= gap_s)
    active_s = float(active.sum() * prepared.dt)
    return {
        "duracion_s": round(duration, 1),
        "filas_por_s": round(len(t) / duration, 2) if duration else np.nan,
        "imu_nuevos_por_s": round(new.sum() / duration, 2) if duration else np.nan,
        "intervalo_mediana_ms": round(float(np.median(intervals)) * 1000, 1) if intervals.size else np.nan,
        "intervalo_p95_ms": round(float(np.percentile(intervals, 95)) * 1000, 1) if intervals.size else np.nan,
        "intervalos_mas_100ms_prc": round(float((intervals > gap_s).mean()) * 100, 1) if intervals.size else np.nan,
        "presion_activa_s": round(active_s, 1),
        "imu_fuera_huecos_activa_prc": round(float((~in_gap[active]).mean()) * 100, 1) if active.any() else np.nan,
        "imu_repeticion_activa_prc": prepared.report.get("imu_repeticion_presion_activa_prc", np.nan),
        "imu_estado_actual": prepared.report.get("imu_estado", ""),
    }


def main() -> pd.DataFrame:
    """Mide todas las series y guarda el detalle.

    Returns:
        pd.DataFrame: Una fila por serie.
    """
    config = load_config(None)
    processing = ProcessingConfig.from_mapping(config.get("processing"))
    calc = GaitMetricsCalculator(processing)
    h5 = config["paths"]["h5_path"]
    rows, labels = [], {}
    with SignalStore(h5) as store:
        keys = list_keys(h5)
        for position, key in enumerate(keys, 1):
            meta = store.meta(key)
            # Solo las series descargadas con el criterio horario vigente para su fecha (R80).
            if meta.get("hora_influx", "civil") != criterion_for(meta.get("d_from"), config["registry"]["influx_time"]):
                continue
            frame = store.read(key)
            device = str(frame["mac"].iloc[0]) if "mac" in frame and len(frame) else "desconocido"
            labels.setdefault(device, f"calcetin_{len(labels) + 1:02d}")
            try:
                row = measure(frame, calc, processing.max_interpolation_s)
            except Exception as error:  # se informa y se sigue
                row = {"error": str(error)[:120]}
            rows.append({"prueba": meta.get("t_code", ""), "pie": meta.get("foot", ""), "calcetin": labels[device], **row})
            if position % 50 == 0:
                print(f"  {position}/{len(keys)} series", flush=True)
    result = pd.DataFrame(rows)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT / "medicion_imu.csv", index=False)
    print(f"{len(result)} series de {len(labels)} calcetines. Detalle: {OUTPUT / 'medicion_imu.csv'}")
    numeric = ["filas_por_s", "imu_nuevos_por_s", "intervalo_mediana_ms", "intervalo_p95_ms", "intervalos_mas_100ms_prc",
               "presion_activa_s", "imu_fuera_huecos_activa_prc", "imu_repeticion_activa_prc"]
    print("\nMediana por prueba:")
    print(result.groupby("prueba")[numeric].median().round(1).to_string())
    print("\nMediana por calcetín (series):")
    by_sock = result.groupby("calcetin")[numeric[:5]].median().round(1)
    by_sock.insert(0, "series", result.groupby("calcetin").size())
    print(by_sock.to_string())
    return result


if __name__ == "__main__":
    main()
