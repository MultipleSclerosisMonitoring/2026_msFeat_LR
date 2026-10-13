"""Resume lo que detecta el pipeline en los ensayos de referencia, para compararlo con Grafana.

Lee las señales ya descargadas (no consulta InfluxDB) y, por ensayo y pie,
lista los filtros de calidad, los contactos de talón, las zancadas válidas y
cuántas se excluyen por cada motivo. Con ``--detalle`` escribe además los
instantes de cada evento detectado en un CSV, para localizarlos en Grafana.

Uso:
    poetry run python scripts/revisar_referencias.py
    poetry run python scripts/revisar_referencias.py --ids 68,261 --detalle referencias.csv
"""
import argparse
import sys
from collections import Counter

import pandas as pd

from gait_analysis.logging_utils import configure_logging
from gait_analysis.metrics import GaitMetricsCalculator, ProcessingConfig
from gait_analysis.registry import load_registry
from gait_analysis.storage import SignalStore, trial_key
from msConfig import ConfigError, load_config

# Ensayos que se saben bien grabados (232: pie izquierdo con presión algo débil).
REFERENCE_IDS = ("68", "261", "167", "89", "108", "291", "232")
SIDES = ("Left", "Right")


def review(config_path: str | None, ids: list[str]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Analiza los ensayos pedidos con la configuración del proyecto.

    Args:
        config_path (str | None): YAML de configuración; ``None`` usa el predeterminado.
        ids (list[str]): Identificadores de evento del registro.

    Returns:
        tuple[pd.DataFrame, pd.DataFrame]: Resumen por ensayo y pie, y un evento
        detectado por fila (contactos, despegues, giros y tramos excluidos).

    Raises:
        ConfigError: Si la configuración no es válida.
        FileNotFoundError: Si no existe el registro.
    """
    config = load_config(config_path, sections=())
    processing = ProcessingConfig.from_mapping(config.get("processing"))
    calculator = GaitMetricsCalculator(processing)
    trials, _invalid = load_registry(config["paths"]["registry_csv"], "all", ids)
    summary: list[dict] = []
    detail: list[dict] = []
    with SignalStore(config["paths"]["h5_path"]) as store:
        for trial in trials:
            for side in SIDES:
                key = trial_key(trial, side)
                if not store.has(key):
                    summary.append({"id": trial.trial_id, "prueba": trial.test_type, "pie": side, "motivo": "sin señales guardadas"})
                    continue
                frame = store.read(key)
                start = pd.to_datetime(frame["_time"]).min()
                prepared = calculator.preparar(frame)
                result = calculator.calcular_cadencia(
                    prepared, min_strides=processing.min_strides.get(trial.test_type), skip_first=processing.skip_first_strides)
                report = prepared.report
                excluded = Counter(item["motivo"] for item in result.get("tramos_excluidos", []))
                summary.append({
                    "id": trial.trial_id, "prueba": trial.test_type, "pie": side,
                    "fs": report.get("fs_efectiva"), "duracion_s": report.get("duracion_real_seg"),
                    "rango_talon": report.get("rango_talon"), "ritmo_talon": report.get("periodicidad_talon"),
                    "imu": report.get("imu_estado"), "giros": result.get("n_giros"),
                    "contactos": result.get("contactos_talon"), "zancadas_validas": result.get("num_zancadas"),
                    "cadencia": result.get("cadencia_zancada"),
                    "fuera_de_banda": excluded.get("duracion_fuera_de_banda", 0), "margen_giro": excluded.get("margen_giro", 0),
                    "huecos_presion": excluded.get("hueco_presion", 0), "motivo": result.get("error_reason", ""),
                })
                events = [("contacto_talon", t, t, "") for t in result.get("contactos_s", [])]
                events += [("despegue", t, t, "") for t in result.get("despegues_s", [])]
                events += [("giro", turn["inicio_s"], turn["fin_s"], "") for turn in result.get("giros", [])]
                events += [("tramo_excluido", item["inicio_s"], item["fin_s"], item["motivo"]) for item in result.get("tramos_excluidos", [])]
                detail += [
                    {"id": trial.trial_id, "pie": side, "tipo": kind, "instante": start + pd.to_timedelta(first, unit="s"),
                     "inicio_s": first, "fin_s": last, "motivo": reason}
                    for kind, first, last, reason in sorted(events, key=lambda event: event[1])
                ]
    return pd.DataFrame(summary), pd.DataFrame(detail)


def main(argv: list[str] | None = None) -> None:
    """Punto de entrada del script.

    Args:
        argv (list[str] | None): Argumentos; por defecto los de la línea de órdenes.
    """
    parser = argparse.ArgumentParser(description="Revisión de los ensayos de referencia frente a Grafana")
    parser.add_argument("--config", default=None, help="YAML; por defecto config/config.yaml")
    parser.add_argument("--ids", default=",".join(REFERENCE_IDS), help="Identificadores de evento separados por comas")
    parser.add_argument("--detalle", default=None, help="CSV donde escribir cada evento detectado con su instante")
    parser.add_argument("--verbose", type=int, choices=(0, 1, 2), default=0, help="0 avisos, 1 información, 2 depuración")
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        summary, detail = review(args.config, [item.strip() for item in args.ids.split(",") if item.strip()])
    except (ConfigError, FileNotFoundError) as error:
        sys.exit(str(error))
    with pd.option_context("display.width", 250, "display.max_columns", None, "display.max_rows", None):
        print(summary.to_string(index=False))
    if args.detalle:
        detail.to_csv(args.detalle, index=False)
        print(f"Eventos detectados: {len(detail)} filas en {args.detalle}")


if __name__ == "__main__":
    main()
