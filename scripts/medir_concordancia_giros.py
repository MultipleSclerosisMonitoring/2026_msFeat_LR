"""Concordancia entre los giros detectados en los dos pies (D05).

Uso:
    poetry run python scripts/medir_concordancia_giros.py

En la revisión con Grafana (V-03 y V-05) el código marcó en un solo pie giros
que no existían, por un sesgo de la velocidad de giro de ese sensor. En una
prueba los dos pies cambian de rumbo a la vez, así que un giro que el otro pie
no acompaña es dudoso; pero sin una referencia anotada no puede saberse qué
sensor acierta. Este script no cambia qué giros se detectan ni qué zancadas
entran en las métricas: solo marca.

- ``giro_no_confirmado``: giro de un pie que no se solapa con ningún giro del
  otro pie, con una tolerancia de ``TOLERANCE_S`` segundos a cada lado por si
  los relojes de los calcetines no están alineados.
- ``deteccion_giros_sospechosa``: ensayo en el que al menos uno de los pies
  tiene ``SUSPICIOUS_PRC`` % o más de su tiempo de giro sin cubrir por los
  giros del otro pie (ampliados con la misma tolerancia). Se mide en tiempo y
  no por giro porque un giro falso muy largo se solapa con todos los giros
  reales del otro pie y, contado por giro, parecería confirmado (V-05).

Los giros se sitúan en tiempo absoluto con la columna ``instante`` de los
eventos que guarda el pipeline (``eventos/*/<prueba>/*.h5``). Solo se miden los
ensayos con los dos pies. El detalle se guarda en
``data/medicion_concordancia_giros/`` (fuera de Git), con el número de ensayo y
sin códigos de paciente; por pantalla solo salen resúmenes agregados.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pkgs"), str(ROOT / "src")]

from msConfig import load_config  # noqa: E402

OUTPUT = ROOT / "data" / "medicion_concordancia_giros"
TOLERANCE_S = 1.0  # holgura por la posible falta de sincronía entre calcetines (provisional)
SUSPICIOUS_PRC = 50.0  # tiempo de giro sin confirmar a partir del cual el ensayo se marca (provisional)
SIDES = ("Left", "Right")


def turn_intervals(events: pd.DataFrame) -> np.ndarray:
    """Devuelve los giros de un pie como intervalos en tiempo absoluto.

    Args:
        events (pd.DataFrame): Tabla ``{pie}/eventos`` de un ensayo.

    Returns:
        np.ndarray: Filas ``(inicio, fin)`` en segundos desde el 1/1/1970.
    """
    turns = events[events.tipo == "giro"]
    if turns.empty:
        return np.empty((0, 2))
    start = pd.to_datetime(turns.instante).astype("int64").to_numpy() / 1e9
    return np.column_stack([start, start + (turns.fin_s - turns.inicio_s).to_numpy()])


def unconfirmed(own: np.ndarray, other: np.ndarray) -> np.ndarray:
    """Marca los giros de un pie que el otro pie no acompaña.

    Args:
        own (np.ndarray): Giros del pie evaluado.
        other (np.ndarray): Giros del otro pie.

    Returns:
        np.ndarray: Máscara booleana, ``True`` en los giros sin confirmar.
    """
    if not len(other):
        return np.ones(len(own), dtype=bool)
    return np.array([not np.any((other[:, 0] < stop + TOLERANCE_S) & (other[:, 1] > start - TOLERANCE_S))
                     for start, stop in own], dtype=bool)


def uncovered_time(own: np.ndarray, other: np.ndarray) -> float:
    """Mide el tiempo de giro de un pie que no cubren los giros del otro pie.

    Args:
        own (np.ndarray): Giros del pie evaluado.
        other (np.ndarray): Giros del otro pie, que se amplían ``TOLERANCE_S``
            segundos a cada lado.

    Returns:
        float: Segundos de giro del pie evaluado fuera de los giros del otro.
    """
    merged: list[list[float]] = []
    for start, stop in sorted((a - TOLERANCE_S, b + TOLERANCE_S) for a, b in other):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], stop)
        else:
            merged.append([start, stop])
    total = 0.0
    for start, stop in own:
        covered = sum(max(0.0, min(stop, b) - max(start, a)) for a, b in merged)
        total += (stop - start) - covered
    return total


def main() -> pd.DataFrame:
    """Compara los giros de los dos pies en cada ensayo y resume la concordancia.

    Returns:
        pd.DataFrame: Una fila por serie (ensayo y pie) con sus giros y los que
        no confirma el otro pie.
    """
    config = load_config(None)
    results = Path(config["paths"].get("results_folder") or ROOT / "data" / "results")
    if not results.is_absolute():
        results = (ROOT / "config" / results).resolve()
    rows = []
    for path in sorted(results.glob("eventos/*/*/*.h5")):
        try:
            turns = {side: turn_intervals(pd.read_hdf(path, f"{side}/eventos")) for side in SIDES}
        except KeyError:
            continue  # un solo pie: no hay con qué comparar
        trial, test = int(path.stem.removeprefix("trial_")), path.parent.name
        for side, other in zip(SIDES, SIDES[::-1]):
            own = turns[side]
            flags = unconfirmed(own, turns[other])
            total = float((own[:, 1] - own[:, 0]).sum()) if len(own) else 0.0
            lonely = uncovered_time(own, turns[other])
            rows.append({"TrialID": trial, "prueba": test, "pie": side, "giros": len(own),
                         "giros_no_confirmados": int(flags.sum()), "tiempo_giros_s": round(total, 1),
                         "tiempo_no_confirmado_s": round(lonely, 1),
                         "no_confirmado_prc": round(100 * lonely / total, 1) if total else np.nan})
    detail = pd.DataFrame(rows)
    trials = detail.groupby(["TrialID", "prueba"]).agg(
        giros=("giros", "sum"), no_confirmados=("giros_no_confirmados", "sum"),
        peor_prc=("no_confirmado_prc", "max")).reset_index()
    trials["deteccion_giros_sospechosa"] = trials.peor_prc >= SUSPICIOUS_PRC
    OUTPUT.mkdir(parents=True, exist_ok=True)
    detail.to_csv(OUTPUT / "concordancia_giros_series.csv", index=False)
    trials.to_csv(OUTPUT / "concordancia_giros_ensayos.csv", index=False)

    print(f"Ensayos con los dos pies: {len(trials)} (tolerancia +-{TOLERANCE_S} s; ensayo sospechoso si un pie "
          f"tiene >={SUSPICIOUS_PRC:.0f} % de su tiempo de giro sin confirmar)")
    summary = trials.groupby("prueba").agg(
        ensayos=("TrialID", "count"), con_giros=("giros", lambda s: int((s > 0).sum())),
        giros=("giros", "sum"), no_confirmados=("no_confirmados", "sum"),
        sospechosos=("deteccion_giros_sospechosa", "sum"))
    summary["no_confirmados_prc"] = (100 * summary.no_confirmados / summary.giros).round(1)
    print(summary.to_string())
    for limit in (30.0, 50.0, 70.0):
        counts = trials[trials.peor_prc >= limit].groupby("prueba").size().to_dict()
        print(f"  Ensayos con un pie >={limit:.0f} % sin confirmar: {counts}")
    print(f"\nDetalle: {OUTPUT}")
    return detail


if __name__ == "__main__":
    main()
