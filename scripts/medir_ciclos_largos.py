"""Sensibilidad de la cadencia a los ciclos largos respecto al ritmo local (D04).

Uso:
    poetry run python scripts/medir_ciclos_largos.py

La revisión con Grafana mostró que, si el detector no ve un contacto de talón,
la zancada resultante dura el doble y puede entrar en la banda de duración
(0,8-2,5 s) como si fuera válida. Sin una referencia anotada no puede saberse
qué ciclos largos son zancadas dobles, así que este script no corrige nada:
marca como ``ciclo_largo_sospechoso`` cada zancada válida que dura más de
``k`` veces la mediana de las ``LOCAL_WINDOW`` zancadas válidas anteriores y
posteriores (el ritmo local, sin contar la propia zancada) y mide cómo cambian
la cadencia, el índice de simetría y la coherencia del número de zancadas entre
pies si se quitan, para varios factores ``k`` próximos a 1,6.

No modifica el pipeline ni el DataSet Maestro. Para cada serie del almacén HDF5
vuelve a calcular las zancadas válidas con :meth:`calcular_cadencia` y la
configuración del proyecto, y comprueba que la cadencia sin quitar nada es la
del DataSet Maestro (la fila «sin_exclusion» del resumen debe dar 0 series
distintas). El detalle se guarda en ``data/medicion_ciclos_largos/`` (fuera de
Git), con el número de ensayo y sin códigos de paciente; por pantalla solo
salen resúmenes agregados.
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

OUTPUT = ROOT / "data" / "medicion_ciclos_largos"
MAESTRO = ROOT / "resultados_excel" / "DatasetMaestro.xlsx"
FACTORS = (1.4, 1.5, 1.6, 1.7, 1.8)  # en torno al 1,6 usado como indicador en la revisión (D04)
LOCAL_WINDOW = 5  # zancadas válidas a cada lado que definen el ritmo local
MIN_NEIGHBOURS = 4  # vecinas necesarias para juzgar una zancada
SYMMETRY_LIMIT = 20.0  # índice de simetría a partir del cual el pipeline avisa (%)


def suspicious(times: np.ndarray, factor: float) -> np.ndarray:
    """Marca las zancadas largas respecto a su ritmo local.

    Args:
        times (np.ndarray): Duración de las zancadas válidas de una serie, en
            orden temporal y en segundos.
        factor (float): Múltiplo de la mediana local a partir del cual una
            zancada se marca.

    Returns:
        np.ndarray: Máscara booleana, ``True`` en los ciclos largos sospechosos.
        Una zancada con menos de ``MIN_NEIGHBOURS`` vecinas no se marca.
    """
    flags = np.zeros(len(times), dtype=bool)
    for index, value in enumerate(times):
        neighbours = np.concatenate([times[max(0, index - LOCAL_WINDOW):index], times[index + 1:index + 1 + LOCAL_WINDOW]])
        if len(neighbours) >= MIN_NEIGHBOURS:
            flags[index] = value > factor * float(np.median(neighbours))
    return flags


def measure(times: list[float], minimum: int) -> dict:
    """Calcula la cadencia de una serie sin quitar nada y quitando los ciclos sospechosos.

    Args:
        times (list[float]): Tiempos de las zancadas válidas de la serie.
        minimum (int): Mínimo de zancadas válidas para informar la cadencia.

    Returns:
        dict: Zancadas válidas y cadencia originales y, para cada factor, ciclos
        marcados, zancadas que quedan y cadencia sin ellos (``NaN`` si quedan
        menos que el mínimo).
    """
    values = np.asarray(times, dtype=float)
    row: dict = {"zancadas": len(values), "cadencia": round(60.0 / values.mean(), 2) if len(values) >= minimum else np.nan}
    for factor in FACTORS:
        flags = suspicious(values, factor)
        kept = values[~flags]
        label = f"k{factor:.1f}".replace(".", "_")
        row[f"sospechosos_{label}"] = int(flags.sum())
        row[f"zancadas_{label}"] = len(kept)
        row[f"cadencia_{label}"] = round(60.0 / kept.mean(), 2) if len(kept) >= minimum else np.nan
    return row


def symmetry(left: float, right: float) -> float:
    """Índice de simetría de la cadencia, con la fórmula del pipeline (%)."""
    if np.isnan(left) or np.isnan(right):
        return np.nan
    return round(abs(left - right) / (0.5 * (left + right)) * 100, 2)


def main() -> pd.DataFrame:
    """Recalcula las zancadas válidas de cada serie y resume la sensibilidad.

    Returns:
        pd.DataFrame: Una fila por serie con las cadencias para cada factor.
    """
    config = load_config(None)
    processing = ProcessingConfig.from_mapping(config.get("processing"))
    calc = GaitMetricsCalculator(processing)
    h5 = config["paths"]["h5_path"]
    rows = []
    with SignalStore(h5) as store:
        keys = list_keys(h5)
        for position, key in enumerate(keys, 1):
            meta = store.meta(key)
            # Solo las series descargadas con el criterio horario vigente para su fecha (R80).
            if meta.get("hora_influx", "civil") != criterion_for(meta.get("d_from"), config["registry"]["influx_time"]):
                continue
            test = meta.get("t_code", "")
            minimum = processing.min_strides.get(test)
            if minimum is None:
                continue
            prepared = calc.preparar(store.read(key), test)
            result = calc.calcular_cadencia(prepared, excluir_giros=True, min_strides=minimum,
                                            skip_first=processing.skip_first_strides)
            rows.append({"TrialID": int(meta["id"]), "prueba": test, "pie": meta.get("foot", ""),
                         **measure(result.get("tiempos_zancada", []), minimum)})
            if position % 100 == 0:
                print(f"  {position}/{len(keys)} series", flush=True)
    detail = pd.DataFrame(rows)

    # Comprobación: sin quitar nada debe salir la cadencia del DataSet Maestro.
    maestro = pd.read_excel(MAESTRO, sheet_name="Ensayos")[["TrialID", "Foot", "cadencia_zancada"]]
    check = detail.merge(maestro, left_on=["TrialID", "pie"], right_on=["TrialID", "Foot"], how="left")
    # El pipeline no informa algunas series aunque tengan zancadas (p. ej., duración no plausible):
    # se resumen solo las que tienen cadencia en el DataSet Maestro.
    reported = check.cadencia_zancada.notna().to_numpy()
    same = (check.cadencia - check.cadencia_zancada).abs().le(0.011)
    print(f"Series con cadencia en el DataSet Maestro: {int(reported.sum())} de {len(detail)}; "
          f"cadencia recalculada distinta: {int((~same[reported]).sum())}")
    detail = detail[reported].reset_index(drop=True)

    OUTPUT.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUTPUT / "ciclos_largos_series.csv", index=False)

    # Resumen por prueba y factor: ciclos marcados y cambio de la cadencia.
    summary = []
    with_cadence = detail[detail.cadencia.notna()]
    for test, group in with_cadence.groupby("prueba"):
        for factor in FACTORS:
            label = f"k{factor:.1f}".replace(".", "_")
            change = group[f"cadencia_{label}"] - group.cadencia
            summary.append({
                "prueba": test, "factor": factor, "series": len(group),
                "zancadas_validas": int(group.zancadas.sum()),
                "ciclos_sospechosos": int(group[f"sospechosos_{label}"].sum()),
                "sospechosos_prc": round(100 * group[f"sospechosos_{label}"].sum() / group.zancadas.sum(), 2),
                "series_con_alguno": int((group[f"sospechosos_{label}"] > 0).sum()),
                "cambio_cadencia_mediana": round(float(change.median()), 2),
                "cambio_cadencia_p90": round(float(change.quantile(0.9)), 2),
                "series_cambio_mas_1": int((change > 1).sum()),
                "series_sin_cadencia": int(group[f"cadencia_{label}"].isna().sum()),
            })
    summary = pd.DataFrame(summary)
    summary.to_csv(OUTPUT / "ciclos_largos_resumen.csv", index=False)
    print("\nCiclos largos sospechosos y cambio de la cadencia (zancadas/min) al quitarlos:")
    print(summary.to_string(index=False))

    # Efecto en la simetría de los ensayos con los dos pies.
    wide = with_cadence.pivot_table(index=["TrialID", "prueba"], columns="pie", aggfunc="first")
    wide = wide.dropna(subset=[("cadencia", "Left"), ("cadencia", "Right")])
    bilateral = []
    for (trial, test), row in wide.iterrows():
        entry = {"TrialID": trial, "prueba": test,
                 "simetria": symmetry(row[("cadencia", "Left")], row[("cadencia", "Right")])}
        for factor in FACTORS:
            label = f"k{factor:.1f}".replace(".", "_")
            entry[f"simetria_{label}"] = symmetry(row[(f"cadencia_{label}", "Left")], row[(f"cadencia_{label}", "Right")])
        bilateral.append(entry)
    bilateral = pd.DataFrame(bilateral)
    bilateral.to_csv(OUTPUT / "ciclos_largos_simetria.csv", index=False)
    print("\nÍndice de simetría de la cadencia (%), mediana y ensayos por encima del 20 %:")
    columns = ["simetria"] + [f"simetria_k{f:.1f}".replace(".", "_") for f in FACTORS]
    for test, group in bilateral.groupby("prueba"):
        medians = " ".join(f"{column.removeprefix('simetria').strip('_') or 'original'}={group[column].median():.2f}" for column in columns)
        above = " ".join(f"{int((group[column] > SYMMETRY_LIMIT).sum())}" for column in columns)
        print(f"  {test} ({len(group)} ensayos): {medians} | >20 %: {above}")
    print(f"\nDetalle: {OUTPUT}")
    return detail


if __name__ == "__main__":
    main()
