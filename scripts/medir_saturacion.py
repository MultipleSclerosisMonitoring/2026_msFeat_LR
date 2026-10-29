"""Cuenta en las series guardadas las muestras del giroscopio que tocan el tope de su calcetín.

Uso:
    poetry run python scripts/medir_saturacion.py

En la revisión visual con Grafana (V-02, ensayo 232) el giroscopio del pie
izquierdo se queda plano en -286,72 °/s durante miles de filas, mientras que
otros calcetines llegan a ±700. Para cada serie del almacén HDF5 calcula, sin
modificar nada y para cada eje de ``Gx`` a ``Gz``:

- El valor absoluto máximo del eje en la serie.
- Los episodios (grupos de filas seguidas) con el eje en el tope ``TOP_DPS``
  y el porcentaje de filas en él. No basta con contar filas: cuando la IMU
  repite filas, un pico normal sale varias veces con el mismo valor. La serie
  se marca como saturada en un eje si su máximo es ``TOP_DPS`` y lo alcanza en
  al menos ``MIN_EPISODES`` episodios.
- Si la serie tiene el rango corto: ningún eje pasa de ``TOP_DPS``.

El calcetín se identifica con una etiqueta anónima (``calcetin_01``...) en
lugar de su dirección MAC, y el ensayo por su número. El detalle se guarda en
``data/medicion_saturacion/`` (fuera de Git) y por pantalla solo salen
resúmenes agregados.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pkgs"), str(ROOT / "src")]

from gait_analysis.storage import SignalStore, list_keys  # noqa: E402
from msConfig import load_config  # noqa: E402
from msTools.timeutils import criterion_for  # noqa: E402

OUTPUT = ROOT / "data" / "medicion_saturacion"
GYRO_CHANNELS = ("Gx", "Gy", "Gz")
TOP_DPS = 286.72  # tope observado en V-02 (ensayo 232, pie izquierdo)
TOP_TOLERANCE = 0.6  # el tope sale como 286,72 o 286,16 según el eje
MIN_EPISODES = 3  # episodios en el tope a partir de los cuales se considera saturación


def measure(frame: pd.DataFrame) -> dict:
    """Mide una serie.

    Args:
        frame (pd.DataFrame): Serie de un pie tal como está en el almacén.

    Returns:
        dict: Por eje, máximo absoluto, episodios en el tope y porcentaje de
        filas en el tope; además, si algún eje satura y si el rango es corto.
    """
    row: dict = {"filas": len(frame)}
    saturated = []
    for channel in GYRO_CHANNELS:
        values = pd.to_numeric(frame[channel], errors="coerce").round(2).dropna().to_numpy()
        if not values.size:
            row.update({f"{channel}_max": np.nan, f"{channel}_episodios_tope": 0, f"{channel}_tope_prc": np.nan})
            continue
        top = float(np.abs(values).max())
        at_top = np.abs(np.abs(values) - TOP_DPS) <= TOP_TOLERANCE
        episodes = int((at_top & ~np.r_[False, at_top[:-1]]).sum())
        row.update({
            f"{channel}_max": top,
            f"{channel}_episodios_tope": episodes,
            f"{channel}_tope_prc": round(float(at_top.mean()) * 100, 2),
        })
        if top <= TOP_DPS + TOP_TOLERANCE and episodes >= MIN_EPISODES:
            saturated.append(channel)
    row["ejes_saturados"] = ",".join(saturated)
    row["satura"] = bool(saturated)
    row["rango_corto"] = bool(np.nanmax([row[f"{c}_max"] for c in GYRO_CHANNELS]) <= TOP_DPS + TOP_TOLERANCE)
    return row


def main() -> pd.DataFrame:
    """Mide todas las series y guarda el detalle.

    Returns:
        pd.DataFrame: Una fila por serie.
    """
    config = load_config(None)
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
            trial = key.rstrip("/").split("/")[-2].removeprefix("trial_")
            try:
                row = measure(frame)
            except Exception as error:  # se informa y se sigue
                row = {"error": str(error)[:120]}
            rows.append({"ensayo": trial, "prueba": meta.get("t_code", ""), "pie": meta.get("foot", ""),
                         "calcetin": labels[device], **row})
            if position % 50 == 0:
                print(f"  {position}/{len(keys)} series", flush=True)
    result = pd.DataFrame(rows)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    result.to_csv(OUTPUT / "medicion_saturacion.csv", index=False)
    print(f"{len(result)} series de {len(labels)} calcetines. Detalle: {OUTPUT / 'medicion_saturacion.csv'}")
    print(f"\nSeries con algún eje saturado en {TOP_DPS} °/s (>= {MIN_EPISODES} episodios): "
          f"{int(result['satura'].sum())} de {len(result)}; con el rango corto: {int(result['rango_corto'].sum())}")
    print("\nPor prueba:")
    print(result.groupby("prueba").agg(series=("satura", "size"), saturadas=("satura", "sum"),
                                       rango_corto=("rango_corto", "sum")).to_string())
    saturated = result[result["satura"]]
    print("\nPorcentaje de filas en el tope en las series saturadas (mediana y máximo):")
    print(saturated[[f"{c}_tope_prc" for c in GYRO_CHANNELS]].agg(["median", "max"]).round(2).to_string())
    print("\nEjes saturados (series):")
    print(saturated["ejes_saturados"].value_counts().to_string())
    by_sock = result.groupby("calcetin").agg(series=("satura", "size"), saturadas=("satura", "sum"))
    print(f"\nCalcetines: {len(by_sock)}; con alguna serie saturada: {int((by_sock['saturadas'] > 0).sum())}; "
          f"con todas: {int((by_sock['saturadas'] == by_sock['series']).sum())}")
    return result


if __name__ == "__main__":
    main()
