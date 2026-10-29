"""Clasifica por sus indicios los 6MWT con zancadas incoherentes entre pies (D01).

Uso:
    poetry run python scripts/clasificar_zancadas_incoherentes.py

Anota la columna ``observaciones`` de ``resultados_excel/revision_zancadas_6MWT.xlsx``
(la lista de :mod:`listar_zancadas_incoherentes`) con los indicios que apuntan
las mediciones, sin revisar cada ensayo a mano ni atribuirle una causa:

- **Ciclos largos sospechosos** (D04): el pie con menos zancadas tiene al menos
  ``MIN_LONG`` ciclos largos respecto a su ritmo local (factor 1,6 de
  :mod:`medir_ciclos_largos`) y más que el otro pie.
- **Detección de giros sospechosa** (D05): marca de
  :mod:`medir_concordancia_giros` (un pie con la mitad o más de su tiempo de
  giro sin cubrir por los giros del otro).
- **Giroscopio saturado** (D06): el pie con menos zancadas satura según
  :mod:`medir_saturacion`.

Los indicios no son excluyentes; si no se cumple ninguno, «sin indicio». Los
ensayos revisados en Grafana (apartado 5.7 de la memoria) se marcan además en
``revisado``. Hay que ejecutar antes los tres scripts de medición. Por pantalla
solo salen recuentos.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ROOT / "resultados_excel" / "revision_zancadas_6MWT.xlsx"
LONG = ROOT / "data" / "medicion_ciclos_largos" / "ciclos_largos_series.csv"
TURNS = ROOT / "data" / "medicion_concordancia_giros" / "concordancia_giros_ensayos.csv"
SATURATION = ROOT / "data" / "medicion_saturacion" / "medicion_saturacion.csv"
LONG_COLUMN = "sospechosos_k1_6"
MIN_LONG = 3  # ciclos largos sospechosos para contar el indicio (provisional)
REVIEWED = {321: "sí, en Grafana (V-03)", 295: "sí, en Grafana (V-04)", 496: "sí, en Grafana (V-05)"}
FOOT = {"Left": "I", "Right": "D"}


def classify(row: pd.Series, long: dict, turns: dict, saturated: set) -> list[str]:
    """Devuelve los indicios de un ensayo de la lista.

    Args:
        row (pd.Series): Fila de la hoja ``Revision``.
        long (dict): Ciclos largos sospechosos por ``(ensayo, pie)``.
        turns (dict): Por ensayo, peor porcentaje de tiempo de giro sin
            confirmar y si la detección de giros es sospechosa.
        saturated (set): Pares ``(ensayo, pie)`` con el giroscopio saturado.

    Returns:
        list[str]: Indicios en texto, vacía si no hay ninguno.
    """
    trial, fewer = int(row.TrialID), row.pie_con_menos
    other = "Right" if fewer == "Left" else "Left"
    signs = []
    mine, theirs = long.get((trial, fewer), 0), long.get((trial, other), 0)
    if mine >= MIN_LONG and mine > theirs:
        signs.append(f"ciclos largos sospechosos ({FOOT[fewer]}: {mine}; {FOOT[other]}: {theirs})")
    worst, suspicious = turns.get(trial, (float("nan"), False))
    if suspicious:
        signs.append(f"detección de giros sospechosa ({worst:.0f} % del tiempo de giro de un pie sin confirmar)")
    if (trial, fewer) in saturated:
        signs.append(f"giroscopio saturado ({FOOT[fewer]})")
    return signs


def main() -> pd.DataFrame:
    """Anota la hoja de revisión y muestra los recuentos por indicio.

    Returns:
        pd.DataFrame: Un ensayo por fila con sus indicios.
    """
    review = pd.read_excel(REVIEW, sheet_name="Revision")
    long_table = pd.read_csv(LONG)
    long = {(int(trial), foot): int(count) for trial, foot, count in long_table[["TrialID", "pie", LONG_COLUMN]].to_numpy()}
    turn_table = pd.read_csv(TURNS)
    turns = {int(r.TrialID): (r.peor_prc, bool(r.deteccion_giros_sospechosa)) for r in turn_table.itertuples()}
    saturation = pd.read_csv(SATURATION)
    saturated = {(int(r.ensayo), r.pie) for r in saturation[saturation.satura].itertuples()}

    rows = []
    for row in review.itertuples(index=False):
        signs = classify(pd.Series(row._asdict()), long, turns, saturated)
        rows.append({"TrialID": int(row.TrialID), "indicios": signs})
    result = pd.DataFrame(rows)

    book = load_workbook(REVIEW)
    sheet = book["Revision"]
    header = [cell.value for cell in sheet[1]]
    trial_col, review_col, note_col = (header.index(name) + 1 for name in ("TrialID", "revisado", "observaciones"))
    notes = {r.TrialID: r.indicios for r in result.itertuples()}
    for line in range(2, sheet.max_row + 1):
        trial = int(sheet.cell(line, trial_col).value)
        signs = notes.get(trial, [])
        text = "; ".join(signs) if signs else "sin indicio"
        sheet.cell(line, note_col).value = f"Clasificación automática, no revisada a mano: {text}."
        if trial in REVIEWED:
            sheet.cell(line, review_col).value = REVIEWED[trial]
    book.save(REVIEW)

    total = len(result)
    pending = result[~result.TrialID.isin(REVIEWED)]
    print(f"Ensayos de la lista: {total}; sin revisar en Grafana: {len(pending)}")
    for label in ("ciclos largos sospechosos", "detección de giros sospechosa", "giroscopio saturado"):
        print(f"  {label}: {int(pending.indicios.map(lambda s: any(x.startswith(label) for x in s)).sum())}")
    print(f"  con más de un indicio: {int(pending.indicios.map(len).gt(1).sum())}")
    print(f"  sin indicio: {int(pending.indicios.map(len).eq(0).sum())}")
    for trial in REVIEWED:
        print(f"  revisado {trial}: {notes.get(trial)}")
    print(f"Anotado en {REVIEW}")
    return result


if __name__ == "__main__":
    main()
