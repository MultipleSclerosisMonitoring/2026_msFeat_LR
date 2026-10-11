"""Migra el HDF5 antiguo (un solo archivo) al almacén actual, sin consultar InfluxDB.

El archivo antiguo identifica cada serie por la posición de la fila en el CSV
(``/p_{codeid}/{test}/trial_{posición}``). Una serie solo se migra si la fila de
esa posición coincide en paciente y test y todas sus muestras caen dentro de la
ventana ``d_from``–``d_until`` del registro actual. Las series migradas pasan
por la misma normalización que una descarga (pie, dispositivo, orden y
duplicados) y se publican de forma atómica. El archivo antiguo no se modifica.

Uso:
    python scripts/migrar_hdf5_antiguo.py --dry-run
    python scripts/migrar_hdf5_antiguo.py --source data/raw/gait_study_data_antiguo.h5
"""
import argparse
import logging
import sys
import time
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "pkgs"), str(ROOT / "src"), str(ROOT)]

from msConfig import ConfigError, load_config
from msTools.timeutils import experiment_wallclock
from msTools.i18n import _

from gait_analysis.extractor import end_run, start_run
from gait_analysis.logging_utils import configure_logging
from gait_analysis.query import normalize_query_result, split_series
from gait_analysis.registry import RegistryError, Trial, TrialResult, load_registry
from gait_analysis.storage import Manifest, SignalStore, StorageError, list_keys, manifest_path_for, trial_key

logger = logging.getLogger("msGait.Migration")

STATUS_MIGRATED = "OK"
STATUS_PRESENT = "SKIPPED"
STATUS_REJECTED = "INVALID_ROW"


def match_trial(key: str, table: pd.DataFrame, trials: dict[str, Trial]) -> tuple[Trial | None, str]:
    """Localiza el evento del registro al que corresponde una clave antigua.

    Args:
        key (str): Clave ``/p_{codeid}/{test}/trial_{posición}`` del archivo antiguo.
        table (pd.DataFrame): Registro CSV leído en su orden original.
        trials (dict[str, Trial]): Eventos válidos del registro por identificador.

    Returns:
        tuple[Trial | None, str]: Evento correspondiente, o ``None`` y el motivo
        por el que la clave no puede asociarse a ninguno.
    """
    parts = key.strip("/").split("/")
    if len(parts) != 3 or not parts[2].startswith("trial_") or not parts[2][6:].isdigit():
        return None, "clave con formato no reconocido"
    position = int(parts[2][6:])
    if position >= len(table):
        return None, f"la posición {position} no existe en el registro"
    row = table.iloc[position]
    if f"p_{row['codeid']}" != parts[0] or row["t_code"] != parts[1]:
        return None, f"la fila {position} del registro es de otro paciente o test"
    trial = trials.get(row["id"])
    if trial is None:
        return None, f"la fila {position} del registro no es válida"
    return trial, ""


def migrate(source: Path, h5_path: str, csv_path: str, dry_run: bool = False, overwrite: bool = False) -> list[TrialResult]:
    """Migra las series verificables del archivo antiguo al almacén actual.

    Args:
        source (Path): Archivo HDF5 del formato anterior.
        h5_path (str): Valor de ``paths.h5_path`` del almacén actual.
        csv_path (str): Registro CSV de eventos.
        dry_run (bool): Solo comprobar qué se migraría, sin escribir.
        overwrite (bool): Reemplazar eventos que ya están en el almacén.

    Returns:
        list[TrialResult]: Un resultado por clave del archivo antiguo.

    Raises:
        FileNotFoundError: Si no existe el archivo antiguo o el registro.
        RegistryError: Si al registro le faltan columnas requeridas.
        StorageError: Si el almacén está bloqueado por otra ejecución.
    """
    if not source.is_file():
        raise FileNotFoundError(_("No se encuentra el archivo antiguo {0}").format(source))
    valid, _invalid = load_registry(csv_path)
    trials = {trial.trial_id: trial for trial in valid}
    table = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    for column in ("id", "codeid", "t_code"):
        table[column] = table[column].str.strip()
    present = {key.rsplit("/", 1)[0] for key in list_keys(h5_path)}
    manifest = Manifest(manifest_path_for(h5_path))
    results: list[TrialResult] = []
    reasons: Counter[str] = Counter()
    started = time.perf_counter()

    with warnings.catch_warnings(), SignalStore(h5_path) as store, pd.HDFStore(str(source), mode="r") as old:
        warnings.simplefilter("ignore")
        keys = old.keys()
        run_id = "" if dry_run else start_run(manifest, "migrate", len(keys))
        for position, key in enumerate(keys, start=1):
            trial, problem = match_trial(key, table, trials)
            result = TrialResult.from_trial(trial) if trial else TrialResult(key)
            result.source = "hdf5_antiguo"
            event_started = time.perf_counter()
            if trial and not problem and not overwrite and trial_key(trial, "").rstrip("/") in present:
                result.status, result.detail = STATUS_PRESENT, "el evento ya está en el almacén"
            elif trial and not problem:
                frame = normalize_query_result(old.get(key))
                low, high = experiment_wallclock(trial.d_from), experiment_wallclock(trial.d_until)
                if frame.empty:
                    problem = "serie vacía"
                elif frame["_time"].min() < low or frame["_time"].max() > high:
                    problem = "muestras fuera de la ventana del registro (hay que volver a descargar)"
                else:
                    series = split_series(frame, trial.trial_id)
                    payload = {}
                    for side, foot in series.items():
                        result.feet[side] = {**foot.report(), "key": trial_key(trial, side)}
                        payload[side] = (foot.frame, {
                            "id": trial.trial_id, "codeid": trial.codeid, "t_code": trial.test_type,
                            "d_from": trial.d_from, "d_until": trial.d_until, "foot": side, "device": foot.device,
                        })
                    if not dry_run:
                        store.write_event(trial, payload)
            if problem:
                result.status, result.detail = STATUS_REJECTED, problem
                logger.warning(_("WARN_MIGRATION_REJECTED: {0}: {1}").format(key, problem))
            reasons[result.detail or "migrado"] += 1
            result.elapsed_s = time.perf_counter() - event_started
            logger.debug(_("DEBUG_MIGRATION_KEY: [{0}/{1}] {2} → {3} {4}").format(position, len(keys), key, result.status, result.detail))
            if position % 25 == 0:
                logger.info(_("INFO_MIGRATION_PROGRESS: {0}/{1} claves revisadas").format(position, len(keys)))
            if not dry_run and result.status == STATUS_MIGRATED:
                manifest.append({
                    "record": "event", "run_id": run_id, "ts": datetime.now().isoformat(timespec="seconds"),
                    **result.to_record(),
                })
            results.append(result)
        if not dry_run:
            end_run(manifest, run_id, results, "Migración", time.perf_counter() - started)

    mode = "simulación, no se ha escrito nada" if dry_run else "escritura completada"
    logger.info(_("INFO_MIGRATION_SUMMARY: {0} claves del archivo antiguo ({1}):").format(len(results), mode))
    for reason, count in reasons.most_common():
        logger.info(_("INFO_MIGRATION_SUMMARY:   {0:4d}  {1}").format(count, reason))
    return results


def main() -> None:
    """Punto de entrada de la línea de órdenes."""
    parser = argparse.ArgumentParser(description="Migrar el HDF5 antiguo al almacén por evento")
    parser.add_argument("--config", default=None, help="YAML; por defecto config/config.yaml")
    parser.add_argument("--source", default=None, help="Archivo antiguo; por defecto <h5_path>_antiguo.h5")
    parser.add_argument("--csv", default=None, help="Sustituye paths.registry_csv")
    parser.add_argument("--dry-run", action="store_true", help="Solo comprobar, sin escribir")
    parser.add_argument("--overwrite", action="store_true", help="Reemplazar eventos ya presentes en el almacén")
    parser.add_argument("--verbose", type=int, choices=(0, 1, 2), default=1, help="0 avisos, 1 información, 2 depuración")
    args = parser.parse_args()
    configure_logging(args.verbose)
    try:
        paths = load_config(args.config)["paths"]
        h5_path = Path(paths["h5_path"])
        source = Path(args.source) if args.source else h5_path.with_name(f"{h5_path.stem}_antiguo{h5_path.suffix}")
        migrate(source, str(h5_path), args.csv or paths["registry_csv"], args.dry_run, args.overwrite)
    except (ConfigError, FileNotFoundError, RegistryError, StorageError) as error:
        sys.exit(f"ERROR: {error}")


if __name__ == "__main__":
    main()
