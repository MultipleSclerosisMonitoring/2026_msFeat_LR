"""Mide la quietud del sensor en los pies planos de las zancadas integradas (dudas D18 y R81).

Uso:
    poetry run python scripts/medir_quietud.py

Para cada serie guardada con el criterio horario vigente (6MWT, 2MWT y T25FW)
prepara la señal, integra las zancadas como el pipeline y recoge, de cada
zancada integrada (no excluida por giros, márgenes ni duración), los
indicadores de quietud de sus dos pies planos: desvío del módulo de la
aceleración respecto a 1 g, giro máximo en la ventana, giro en el pie plano y
valores nuevos de la IMU. No excluye nada: sirve para elegir el umbral del giro
y medir su impacto. El detalle por zancada se guarda en ``data/medicion_quietud/``
(fuera de Git) y por pantalla solo salen distribuciones agregadas.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pkgs"), str(ROOT / "src")]

from gait_analysis.metrics import GaitMetricsCalculator, ProcessingConfig  # noqa: E402
from gait_analysis.storage import SignalStore, list_keys  # noqa: E402
from msConfig import load_config  # noqa: E402
from msTools.timeutils import criterion_for  # noqa: E402

OUTPUT = ROOT / "data" / "medicion_quietud"
TESTS = ("6MWT", "2MWT", "T25FW")
FIELDS = ("giro_min_apoyo_dps", "quietud_acc_g", "quietud_giro_dps", "quietud_valores_nuevos", "quieto_acc")


def main() -> pd.DataFrame:
    """Recoge los indicadores de quietud de todas las zancadas integradas.

    Returns:
        pd.DataFrame: Una fila por zancada integrada.
    """
    config = load_config(None)
    processing = ProcessingConfig.from_mapping(config.get("processing"))
    calc = GaitMetricsCalculator(processing)
    h5 = config["paths"]["h5_path"]
    rows = []
    series = {"total": 0, "con_zancadas": 0}
    with SignalStore(h5) as store:
        for key in list_keys(h5):
            meta = store.meta(key)
            test = meta.get("t_code", "")
            if test not in TESTS:
                continue
            if meta.get("hora_influx", "civil") != criterion_for(meta.get("d_from"), config["registry"]["influx_time"]):
                continue
            series["total"] += 1
            prepared = calc.preparar(store.read(key), test)
            rate = prepared.report.get("imu_nuevos_por_s")
            events, status, _reason = calc._integrated_strides(prepared, True)
            integrated = [event for event in events if not event["motivo_exclusion"]] if status == "OK" else []
            series["con_zancadas"] += bool(integrated)
            observed = prepared.cache.get("imu_nuevo")
            new_per_s = float(observed.sum() / max(prepared.t[-1] - prepared.t[0], 1e-9)) if observed is not None and len(prepared.t) > 1 else np.nan
            for event in integrated:
                rows.append({"prueba": test, "imu_nuevos_por_s": round(new_per_s, 1), **{name: event.get(name) for name in FIELDS}})
    detail = pd.DataFrame(rows)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUTPUT / "medicion_quietud.csv", index=False)
    print(f"{series['total']} series ({series['con_zancadas']} con zancadas integradas), {len(detail)} zancadas integradas")
    if detail.empty:
        return detail
    detail["imu"] = np.where(detail["imu_nuevos_por_s"] >= 30, "rapida", "lenta")
    print("\nZancadas con el sensor quieto según la aceleración (|a| a menos de "
          f"{processing.rest_accel_tolerance_g} g de 1 g en los dos pies planos):")
    print(detail.groupby(["prueba", "imu"])["quieto_acc"].agg(["size", "sum", "mean"]).round(3).to_string())
    percentiles = [0.1, 0.25, 0.5, 0.75, 0.9]
    for name in ("quietud_acc_g", "giro_min_apoyo_dps", "quietud_giro_dps", "quietud_valores_nuevos"):
        print(f"\n{name} por tipo de IMU:")
        print(detail.groupby("imu")[name].describe(percentiles=percentiles).round(3).to_string())
    return detail


if __name__ == "__main__":
    main()
