"""Mide el tiempo de zancada junto a los giros en el 6MWT (Tabla 4.1 de la memoria, R105).

Uso:
    poetry run python scripts/medir_margen_giro.py

Lee los eventos que guarda el pipeline (``eventos/*/6MWT/*.h5`` en la carpeta de
resultados) y, para cada giro, toma las 5 zancadas válidas anteriores y las 5
posteriores de cada pie. Una zancada son dos contactos de talón consecutivos sin
un tramo excluido entre ellos (giro, hueco de presión o sin ritmo) y con una
duración dentro de la banda del código. El nivel estable de cada serie es la
mediana de sus zancadas a más de 5 de cualquier giro, sin la primera. Las
zancadas que duran 1,6 veces el nivel estable o más se cuentan aparte como
probablemente dobles (un contacto perdido) y no entran en la media. La
variación de cada posición es su media respecto a la de las zancadas alejadas
de los giros, en %, así que la fila de referencia vale 0. Solo salen agregados
por pantalla.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pkgs"), str(ROOT / "src")]

from gait_analysis.metrics import ProcessingConfig  # noqa: E402
from msConfig import load_config  # noqa: E402

N_STRIDES = 5
DOUBLE_FACTOR = 1.6
CUT_REASONS = ("giro", "hueco_presion", "sin_ritmo")


def _foot_ratios(events: pd.DataFrame, cfg: ProcessingConfig) -> tuple[dict, list[float]] | None:
    """Calcula los cocientes de una serie (ensayo y pie).

    Args:
        events (pd.DataFrame): Tabla ``{pie}/eventos`` de un ensayo.
        cfg (ProcessingConfig): Configuración con la banda de duración de la zancada.

    Returns:
        tuple[dict, list[float]] | None: Cocientes por posición (``("antes", n)``
        y ``("despues", n)``) y cocientes de las zancadas alejadas de los giros;
        ``None`` si la serie no tiene giros o zancadas suficientes.
    """
    turns = events[events.tipo == "giro"][["inicio_s", "fin_s"]].to_numpy()
    if len(turns) == 0:
        return None
    cuts = events[(events.tipo == "tramo_excluido") & events.motivo.isin(CUT_REASONS)][["inicio_s", "fin_s"]].to_numpy()
    contacts = np.sort(events[events.tipo == "contacto_talon"].inicio_s.to_numpy())
    start, stop = contacts[:-1], contacts[1:]
    duration = stop - start
    valid = (duration >= cfg.cycle_min_s) & (duration <= cfg.cycle_max_s)
    for first, last in cuts:
        valid &= ~((start < last) & (stop > first))
    if valid.sum() < 30:
        return None
    index = np.flatnonzero(valid)
    near = np.zeros(len(start), dtype=bool)
    groups = []
    for k, (turn_start, turn_stop) in enumerate(turns):
        previous = turns[k - 1][1] if k else -np.inf
        following = turns[k + 1][0] if k + 1 < len(turns) else np.inf
        before = [i for i in index if stop[i] <= turn_start and start[i] >= previous][-N_STRIDES:][::-1]
        after = [i for i in index if start[i] >= turn_stop and stop[i] <= following][:N_STRIDES]
        near[before] = near[after] = True
        groups.append((before, after))
    far = duration[valid & ~near][1:]
    if len(far) <= 10:
        return None
    steady = float(np.median(far))
    ratios: dict = {}
    for before, after in groups:
        for side, strides in (("antes", before), ("despues", after)):
            for n, i in enumerate(strides, 1):
                ratios.setdefault((side, n), []).append(duration[i] / steady)
    return ratios, (far / steady).tolist()


def _summary(values: list[float]) -> tuple[float, float]:
    """Devuelve la media sin zancadas dobles y el porcentaje de dobles."""
    array = np.asarray(values)
    return float(array[array < DOUBLE_FACTOR].mean()), float((array >= DOUBLE_FACTOR).mean() * 100)


def main() -> pd.DataFrame:
    """Recorre los eventos del 6MWT y resume el tiempo de zancada junto a los giros.

    Returns:
        pd.DataFrame: Una fila por posición de la zancada respecto al giro, con
        el cociente medio, su variación en % respecto a las zancadas alejadas
        de los giros y el porcentaje de dobles.
    """
    config = load_config(None)
    cfg = ProcessingConfig.from_mapping(config.get("processing"))
    results = Path(config["paths"].get("results_folder") or ROOT / "data" / "results")
    if not results.is_absolute():
        results = (ROOT / "config" / results).resolve()
    ratios: dict = {}
    far: list[float] = []
    series = turns = 0
    for path in sorted(results.glob("eventos/*/6MWT/*.h5")):
        for foot in ("Left", "Right"):
            try:
                events = pd.read_hdf(path, f"{foot}/eventos")
            except (KeyError, FileNotFoundError):
                continue
            result = _foot_ratios(events, cfg)
            if result is None:
                continue
            series += 1
            turns += int((events.tipo == "giro").sum())
            for key, values in result[0].items():
                ratios.setdefault(key, []).extend(values)
            far.extend(result[1])
    rows = []
    for n in range(1, N_STRIDES + 1):
        before, after = _summary(ratios.get(("antes", n), [np.nan])), _summary(ratios.get(("despues", n), [np.nan]))
        rows.append({"zancada": n, "antes": before[0], "antes_dobles_prc": before[1],
                     "despues": after[0], "despues_dobles_prc": after[1]})
    reference = _summary(far)
    rows.append({"zancada": "lejos", "antes": reference[0], "antes_dobles_prc": reference[1],
                 "despues": reference[0], "despues_dobles_prc": reference[1]})
    table = pd.DataFrame(rows)
    for side in ("antes", "despues"):
        table.insert(table.columns.get_loc(side) + 1, f"{side}_var_prc", (table[side] / reference[0] - 1) * 100)
    print(f"Series de 6MWT con giros: {series}; giros: {turns}")
    print(table.round(3).to_string(index=False))
    return table


if __name__ == "__main__":
    main()
