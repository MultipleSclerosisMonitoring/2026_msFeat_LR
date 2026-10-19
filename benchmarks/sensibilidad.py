"""Análisis de sensibilidad de la cadencia y de las fases a los parámetros (duda D17).

Uso:
    poetry run python benchmarks/sensibilidad.py [--prueba 6MWT] [--max-series N] [--semilla 2026]

Recalcula, sobre las series guardadas en el almacén HDF5 y sin modificarlas, la
cadencia y el porcentaje de apoyo de cada serie con la configuración actual y
con una perturbación cada vez:

- ``ciclo_min_-0.1`` y ``ciclo_min_+0.1``: límite inferior de la zancada válida
  (``cycle_min_s``) desplazado 0,1 s.
- ``perdida_10``: se elimina al azar el 10 % de las filas de la serie.
- ``presion_entre_10``: la lectura de presión se divide entre diez, para ver el
  efecto de los mínimos absolutos (presión activa y rango del talón).

Por pantalla y en ``data/sensibilidad/`` (fuera de Git) solo salen cambios
agregados: series con cadencia y con fases en cada caso, y cambio absoluto de
la cadencia (mediana y máximo) en las series que tienen valor en los dos casos.
Los resultados solo describen los datos si las señales se descargaron con el
criterio horario correcto (D14).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pkgs"), str(ROOT / "src")]

from gait_analysis.metrics import PRESSURE_CHANNELS, GaitMetricsCalculator, ProcessingConfig  # noqa: E402
from gait_analysis.storage import SignalStore, list_keys  # noqa: E402
from msConfig import load_config  # noqa: E402
from msTools.timeutils import criterion_for  # noqa: E402

OUTPUT = ROOT / "data" / "sensibilidad"


def evaluate(calc: GaitMetricsCalculator, frame: pd.DataFrame, test_type: str) -> tuple[float, float]:
    """Cadencia y porcentaje de apoyo de una serie con un calculador dado.

    Args:
        calc (GaitMetricsCalculator): Calculador con la configuración a probar.
        frame (pd.DataFrame): Serie de un pie.
        test_type (str): Código de la prueba (para el mínimo de zancadas).

    Returns:
        tuple[float, float]: Cadencia de zancada y porcentaje de apoyo (``NaN`` si no hay valor).
    """
    minimum = calc.config.min_strides.get(test_type)
    prepared = calc.preparar(frame, test_type)
    options = {"excluir_giros": True, "min_strides": minimum, "skip_first": calc.config.skip_first_strides}
    cadence = calc.calcular_cadencia(prepared, **options).get("cadencia_zancada", np.nan)
    stance = calc.calcular_fases(prepared, **options).get("apoyo_prc", np.nan)
    return float(cadence), float(stance)


def main(argv: list[str] | None = None) -> pd.DataFrame:
    """Ejecuta el análisis y guarda el detalle por serie y caso.

    Args:
        argv (list[str] | None): Argumentos; por defecto los de la línea de órdenes.

    Returns:
        pd.DataFrame: Resumen por caso.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--prueba", default="6MWT")
    parser.add_argument("--max-series", type=int, default=None)
    parser.add_argument("--semilla", type=int, default=2026)
    args = parser.parse_args(argv)
    config = load_config(None)
    base = ProcessingConfig.from_mapping(config.get("processing"))
    cases = {
        "base": (base, None),
        "ciclo_min_-0.1": (replace(base, cycle_min_s=base.cycle_min_s - 0.1), None),
        "ciclo_min_+0.1": (replace(base, cycle_min_s=base.cycle_min_s + 0.1), None),
        "perdida_10": (base, "perdida"),
        "presion_entre_10": (base, "escala"),
    }
    calcs = {name: GaitMetricsCalculator(cfg) for name, (cfg, _change) in cases.items()}
    rng = np.random.default_rng(args.semilla)
    rows = []
    h5 = config["paths"]["h5_path"]
    with SignalStore(h5) as store:
        # Solo las series descargadas con el criterio horario vigente para su fecha (R80).
        keys = [key for key in list_keys(h5) if f"/{args.prueba}/" in key
                and store.meta(key).get("hora_influx", "civil") == criterion_for(store.meta(key).get("d_from"), config["registry"]["influx_time"])]
        keys = keys[:args.max_series]
        for position, key in enumerate(keys, 1):
            frame = store.read(key)
            for name, (_cfg, change) in cases.items():
                data = frame
                if change == "perdida":
                    data = frame.iloc[np.sort(rng.choice(len(frame), int(round(len(frame) * 0.9)), replace=False))]
                elif change == "escala":
                    data = frame.copy()
                    for channel in PRESSURE_CHANNELS:
                        data[channel] = pd.to_numeric(data[channel], errors="coerce") / 10
                try:
                    cadence, stance = evaluate(calcs[name], data.reset_index(drop=True), args.prueba)
                except Exception:  # una serie que no se puede analizar cuenta como sin valor
                    cadence, stance = np.nan, np.nan
                rows.append({"serie": position, "caso": name, "cadencia": cadence, "apoyo_prc": stance})
            if position % 20 == 0:
                print(f"  {position}/{len(keys)} series", flush=True)
    detail = pd.DataFrame(rows)
    wide = detail.pivot(index="serie", columns="caso", values="cadencia")
    phases = detail.pivot(index="serie", columns="caso", values="apoyo_prc")
    summary = []
    for name in cases:
        both = wide["base"].notna() & wide[name].notna()
        change = (wide.loc[both, name] - wide.loc[both, "base"]).abs()
        summary.append({
            "caso": name, "series": len(wide), "con_cadencia": int(wide[name].notna().sum()),
            "con_fases": int(phases[name].notna().sum()), "comparables": int(both.sum()),
            "cambio_cadencia_mediana": round(float(change.median()), 2) if both.any() else np.nan,
            "cambio_cadencia_max": round(float(change.max()), 2) if both.any() else np.nan,
        })
    summary = pd.DataFrame(summary)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUTPUT / f"sensibilidad_{args.prueba}_detalle.csv", index=False)
    summary.to_csv(OUTPUT / f"sensibilidad_{args.prueba}.csv", index=False)
    print(summary.to_string(index=False))
    return summary


if __name__ == "__main__":
    main()
