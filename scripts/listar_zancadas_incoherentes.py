"""Lista los 6MWT con zancadas incoherentes entre pies, para revisarlos en Grafana.

Lee el DataSet Maestro (no consulta InfluxDB ni las señales guardadas) y, de
la hoja Asimetria, toma los ensayos con zancadas en los dos pies cuya
diferencia supera la admitida (``zancadas_coherentes`` falso). Para cada uno
da la ventana del registro en su hora local y en UTC, que es la hora con la
que están guardadas las señales desde noviembre de 2025, y, por pie, el
dispositivo, la frecuencia efectiva, los contactos de talón, las zancadas
válidas y los tramos continuos. Se ordenan por el exceso sobre la diferencia
admitida, de mayor a menor, y llevan dos columnas vacías para anotar la
revisión. Si el Excel de salida ya existe no se sobrescribe, para no perder
esas anotaciones, salvo con ``--sobrescribir``.

Uso:
    poetry run python scripts/listar_zancadas_incoherentes.py
    poetry run python scripts/listar_zancadas_incoherentes.py --prueba T25FW --salida revision.xlsx
    poetry run python scripts/listar_zancadas_incoherentes.py --sobrescribir
"""
import argparse
import os
import sys

import pandas as pd

from gait_analysis.logging_utils import configure_logging
from msConfig import ConfigError, load_config

SIDES = ("Left", "Right")
FOOT_COLUMNS = ("dispositivo", "fs_efectiva", "contactos_talon", "zancadas", "n_tramos", "cadencia_zancada")
DEFAULT_OUTPUT = os.path.join("resultados_excel", "revision_zancadas_{test}.xlsx")


def list_incoherent(dataset: str, test: str) -> pd.DataFrame:
    """Reúne los ensayos de una prueba con zancadas incoherentes entre pies.

    Args:
        dataset (str): Ruta del DataSet Maestro.
        test (str): Prueba (``test_type``), por ejemplo ``6MWT``.

    Returns:
        pd.DataFrame: Un ensayo por fila, ordenados por el exceso sobre la
        diferencia admitida.

    Raises:
        FileNotFoundError: Si no existe el Excel.
    """
    asymmetry = pd.read_excel(dataset, sheet_name="Asimetria")
    trials = pd.read_excel(dataset, sheet_name="Ensayos", usecols=["TrialID", "test_type", "Foot", "d_from", "d_until", *FOOT_COLUMNS])
    selected = asymmetry[(asymmetry["test_type"] == test) & (asymmetry["zancadas_coherentes"] == 0)]
    selected = selected[["TrialID", "CodeID", "zancadas_Left", "zancadas_Right", "diferencia_zancadas", "diferencia_admitida"]].copy()
    selected["exceso"] = selected["diferencia_zancadas"] - selected["diferencia_admitida"]
    selected["pie_con_menos"] = selected["zancadas_Left"].lt(selected["zancadas_Right"]).map({True: "Left", False: "Right"})

    trials = trials[trials["test_type"] == test]
    window = trials.drop_duplicates("TrialID")[["TrialID", "d_from", "d_until"]].copy()
    for column in ("d_from", "d_until"):
        window[f"{column}_utc"] = pd.to_datetime(window[column], utc=True, format="ISO8601").dt.strftime("%Y-%m-%d %H:%M:%S")
    per_foot = trials.pivot(index="TrialID", columns="Foot", values=list(FOOT_COLUMNS))
    per_foot.columns = [f"{metric}_{foot}" for metric, foot in per_foot.columns]
    ordered = [f"{metric}_{foot}" for foot in SIDES for metric in FOOT_COLUMNS if metric != "zancadas"]

    table = selected.merge(window, on="TrialID", how="left").merge(per_foot[ordered].reset_index(), on="TrialID", how="left")
    table = table.sort_values(["exceso", "TrialID"], ascending=[False, True])
    table["revisado"] = ""
    table["observaciones"] = ""
    return table


def main(argv: list[str] | None = None) -> None:
    """Punto de entrada del script.

    Args:
        argv (list[str] | None): Argumentos; por defecto los de la línea de órdenes.
    """
    parser = argparse.ArgumentParser(description="Ensayos con zancadas incoherentes entre pies, para revisar en Grafana")
    parser.add_argument("--config", default=None, help="YAML; por defecto config/config.yaml")
    parser.add_argument("--data", default=None, help="Excel de entrada; por defecto paths.output_dataset")
    parser.add_argument("--prueba", default="6MWT", help="Prueba que se lista; por defecto 6MWT")
    parser.add_argument("--salida", default=None, help="Excel de salida; por defecto resultados_excel/revision_zancadas_<prueba>.xlsx")
    parser.add_argument("--sobrescribir", action="store_true", help="Sustituye el Excel de salida si ya existe (se pierden sus anotaciones)")
    parser.add_argument("--verbose", type=int, choices=(0, 1, 2), default=0, help="0 avisos, 1 información, 2 depuración")
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    output = args.salida or DEFAULT_OUTPUT.format(test=args.prueba)
    if os.path.exists(output) and not args.sobrescribir:
        sys.exit(f"{output} ya existe y puede tener anotaciones de la revisión: no se sobrescribe. "
                 "Usa --salida con otro nombre o --sobrescribir para sustituirlo.")
    try:
        dataset = args.data or load_config(args.config, sections=())["paths"]["output_dataset"]
        table = list_incoherent(dataset, args.prueba)
    except (ConfigError, FileNotFoundError) as error:
        sys.exit(str(error))
    table.to_excel(output, index=False, sheet_name="Revision")
    print(f"{len(table)} ensayos de {args.prueba} con zancadas incoherentes entre pies en {output}")


if __name__ == "__main__":
    main()
