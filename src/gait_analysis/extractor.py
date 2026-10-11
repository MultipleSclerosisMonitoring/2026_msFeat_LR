import logging
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable

import pandas as pd
from influxdb_client import InfluxDBClient

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'pkgs'))
from msTools.timeutils import influx_wallclock_z
from msConfig import influx_client_options, load_config
from msTools.i18n import _

from .logging_utils import log_summary
from .query import fetch_trial_frame, split_series
from .registry import (
    STATUS_EMPTY, STATUS_OK, STATUS_QUERY_ERROR, STATUS_SKIPPED, STATUS_WRITE_ERROR,
    Trial, TrialResult, load_registry,
)
from .storage import Manifest, SignalStore, StorageError, audit_store, manifest_path_for

# Configuración del Logger para el Extractor
logger = logging.getLogger("msGait.Extractor")


def acquire_trial(
    client: Any,
    bucket: str,
    measurement: str,
    store: SignalStore,
    manifest: Manifest,
    trial: Trial,
    run_id: str,
    completed: Dict[str, list[str]] | None = None,
    load_stored: bool = False,
) -> tuple[TrialResult, Dict[str, pd.DataFrame]]:
    """Obtiene, normaliza y persiste las señales de un evento.

    Es el único camino de adquisición, compartido por el extractor y por el
    pipeline directo. Nunca lanza excepciones por un evento: el fallo queda
    descrito en el resultado y en el manifest.

    Args:
        client (Any): Cliente ``InfluxDBClient`` ya conectado.
        bucket (str): Bucket de InfluxDB.
        measurement (str): Valor de ``_measurement`` a seleccionar.
        store (SignalStore): Almacén HDF5 abierto para escritura.
        manifest (Manifest): Manifest donde se anota el resultado.
        trial (Trial): Evento validado del registro.
        run_id (str): Identificador de la ejecución en curso.
        completed (Dict[str, list[str]] | None): Eventos ya completados según el
            manifest. Si se indica, los que conservan sus claves no se consultan.
        load_stored (bool): Con ``completed``, devolver las señales almacenadas
            en lugar de marcar el evento como ``SKIPPED``.

    Returns:
        tuple[TrialResult, Dict[str, pd.DataFrame]]: Resultado del evento y
        señales por pie (vacío si no hay datos o el evento se omitió).
    """
    started = time.perf_counter()
    result = TrialResult.from_trial(trial)
    frames: Dict[str, pd.DataFrame] = {}

    stored_keys = (completed or {}).get(trial.trial_id, [])
    if stored_keys and all(store.has(key) for key in stored_keys):
        if not load_stored:
            result.status = STATUS_SKIPPED
            result.detail = "ya completado según el manifest"
            logger.debug(_("DEBUG_TRIAL_SKIPPED: Evento {0} ya completado; no se consulta").format(trial.trial_id))
            return result, frames
        try:
            for key in stored_keys:
                side = key.rsplit("/", 1)[-1]
                frames[side] = store.read(key)
                result.feet[side] = {
                    "key": key, "rows": len(frames[side]), "device": store.meta(key).get("device", ""),
                }
            result.source = "hdf5"
            result.elapsed_s = time.perf_counter() - started
            logger.debug(_("DEBUG_TRIAL_REUSED: Evento {0} leído del HDF5; no se consulta").format(trial.trial_id))
            return result, frames
        except Exception as error:
            logger.warning(_("WARN_STORED_UNREADABLE: Evento {0} no legible en HDF5 ({1}); se vuelve a consultar").format(trial.trial_id, error))
            frames, result.feet = {}, {}

    try:
        raw = fetch_trial_frame(client, bucket, measurement, trial)
        series = split_series(raw, trial.trial_id)
    except Exception as error:
        result.status, result.detail = STATUS_QUERY_ERROR, str(error)
        logger.error(_("ERR_FLUX_QUERY: [!] Error consultando {p}: {err}").format(p=trial.codeid, err=error))
        series = {}
    else:
        if not series:
            result.status, result.detail = STATUS_EMPTY, "sin datos en el rango"
            logger.warning(_("ERR_TRIAL_EMPTY: [-] {p}: Sin datos en el rango {s}").format(
                p=trial.codeid, s=trial.start_iso
            ))

    payload: Dict[str, tuple[pd.DataFrame, Dict[str, Any]]] = {}
    for side, foot in series.items():
        frames[side] = foot.frame
        result.feet[side] = foot.report()
        result.signal_mem_mb += foot.frame.memory_usage(deep=True).sum() / 1e6
        payload[side] = (foot.frame, {
            "id": trial.trial_id, "codeid": trial.codeid, "t_code": trial.test_type,
            "d_from": trial.d_from, "d_until": trial.d_until, "foot": side, "device": foot.device,
        })
    try:
        # Publicación atómica: o quedan todos los pies del evento o no cambia nada.
        for side, key in store.write_event(trial, payload).items():
            result.feet[side]["key"] = key
            logger.info(_("INFO_HDF5_SAVED: [+] Paciente {p} ({s}) guardado en HDF5: {k}").format(
                p=trial.codeid, s=side, k=key
            ))
    except StorageError as error:
        result.status, result.detail = STATUS_WRITE_ERROR, str(error)
        logger.error(_("ERR_HDF5_WRITE: Evento {0}: {1}").format(trial.trial_id, error))

    result.elapsed_s = time.perf_counter() - started
    manifest.append({"record": "event", "run_id": run_id, "ts": datetime.now().isoformat(timespec="seconds"), **result.to_record()})
    return result, frames


def start_run(manifest: Manifest, command: str, total: int) -> str:
    """Anota el inicio de una ejecución en el manifest.

    Args:
        manifest (Manifest): Manifest asociado al almacén.
        command (str): Nombre del comando que se ejecuta.
        total (int): Número de eventos seleccionados.

    Returns:
        str: Identificador de la ejecución.
    """
    run_id = uuid.uuid4().hex[:12]
    manifest.append({
        "record": "run_start", "run_id": run_id, "command": command, "total": total,
        "ts": datetime.now().isoformat(timespec="seconds"),
    })
    return run_id


def end_run(manifest: Manifest, run_id: str, results: Iterable[TrialResult], title: str, elapsed_s: float) -> Dict[str, int]:
    """Anota el fin de una ejecución y muestra el resumen por estado.

    Una ejecución con ``run_start`` y sin ``run_end`` quedó interrumpida.

    Args:
        manifest (Manifest): Manifest asociado al almacén.
        run_id (str): Identificador devuelto por ``start_run``.
        results (Iterable[TrialResult]): Resultados de todos los eventos.
        title (str): Texto que identifica la ejecución en el resumen.
        elapsed_s (float): Duración total de la ejecución.

    Returns:
        Dict[str, int]: Número de eventos por estado.
    """
    results = list(results)
    counts = log_summary(logger, results, title)
    manifest.append({
        "record": "run_end", "run_id": run_id, "counts": counts, "total": len(results),
        "elapsed_s": round(elapsed_s, 3), "ts": datetime.now().isoformat(timespec="seconds"),
    })
    return counts


class GaitDataExtractor:
    """Clase para la extracción masiva y tipada de datos biomecánicos.

    Se encarga de gestionar la conexión con InfluxDB, validar la configuración
    y de exportar los datos a formato jerárquico HDF5 para optimizar el almacenamiento masivo.

    Attributes:
        config (Dict[str, Any]): Configuración resuelta (YAML + entorno).
        _out_dir (str): Ruta absoluta al directorio de salida.
        _h5_database (str): Ruta del almacén HDF5.
        _client (InfluxDBClient): Cliente de conexión a la base de datos.
        _bucket (str): Nombre del bucket origen de los datos.
        _measurement (str): Valor de ``_measurement`` consultado.
    """
    def __init__(self, config_file: str | None = None, output_folder: str | None = None) -> None:
        """Carga la configuración y prepara cliente y rutas de salida.

        Args:
            config_file (str | None): YAML de configuración; ``None`` usa el predeterminado.
            output_folder (str | None): Carpeta de salida que sustituye a la configurada.

        Raises:
            ConfigError: Si la configuración no es válida.
        """
        self.config = self._load_config(config_file)
        self._out_dir = str(Path(output_folder or self.config['paths']['output_folder']).resolve())
        self._h5_database = self.config['paths']['h5_path']
        if output_folder is not None:
            self._h5_database = str(Path(self._out_dir) / 'gait_study_data.h5')
        Path(self._h5_database).parent.mkdir(parents=True, exist_ok=True)
        inf = self.config['influxdb']
        self._client = InfluxDBClient(**influx_client_options(self.config))
        self._bucket = inf['bucket']
        self._measurement = inf['measurement']

    def _load_config(self, config_path: str | None) -> Dict[str, Any]:
        """Carga la configuración compartida del proyecto.

        Args:
            config_path (str | None): Ruta del YAML o ``None``.

        Returns:
            Dict[str, Any]: Configuración validada.
        """
        return load_config(config_path)

    def _registry_path(self, csv_path: str | None) -> str:
        """Resuelve la ruta del registro: argumento o configuración."""
        return csv_path or self.config['paths']['registry_csv']

    def run_batch_extraction(
        self,
        csv_path: str | None = None,
        test_type: str | None = None,
        ids: Iterable[str] | None = None,
        missing_only: bool = False,
    ) -> list[TrialResult]:
        """Extrae a HDF5 los eventos seleccionados del registro.

        Valida el registro completo antes de conectar, procesa los eventos de
        forma secuencial y anota cada resultado en el manifest.

        Args:
            csv_path (str | None): CSV de eventos; ``None`` usa el configurado.
            test_type (str | None): Código del test (ej. '6MWT') o ``"all"``;
                ``None`` usa ``batch.test_type``.
            ids (Iterable[str] | None): Identificadores de evento a extraer.
            missing_only (bool): Omitir los eventos ya completados según el manifest.

        Returns:
            list[TrialResult]: Un resultado por fila seleccionada del registro.

        Raises:
            FileNotFoundError: Si el registro no existe.
            RegistryError: Si al registro le faltan columnas requeridas.
            StorageError: Si el almacén está bloqueado o tiene el esquema antiguo.
        """
        test_type = test_type or self.config['batch']['test_type']
        trials, results = load_registry(self._registry_path(csv_path), test_type, ids)
        manifest = Manifest(manifest_path_for(self._h5_database))
        completed = manifest.completed_keys() if missing_only else None

        logger.info(_("INFO_EXTRACTION_START: Extracción de {0} eventos (test: {1}) hacia {2}").format(len(trials), test_type, self._h5_database))
        started = time.perf_counter()
        with SignalStore(self._h5_database) as store:
            run_id = start_run(manifest, "extract", len(trials))
            for position, trial in enumerate(trials, start=1):
                logger.info(_("INFO_EXTRACT_TRIAL: [{0}/{1}] Evento {2} ({3}, {4})").format(position, len(trials), trial.trial_id, trial.codeid, trial.test_type))
                result, _frames = acquire_trial(
                    self._client, self._bucket, self._measurement, store, manifest, trial, run_id, completed
                )
                logger.debug(_("DEBUG_TRIAL_COST: Evento {0}: {1:.2f} s, {2:.2f} MB").format(trial.trial_id, result.elapsed_s, result.signal_mem_mb))
                results.append(result)
            end_run(manifest, run_id, results, "Extracción", time.perf_counter() - started)
        return results

    def audit(self, csv_path: str | None = None, test_type: str | None = None) -> Dict[str, list[str]]:
        """Compara las claves del HDF5 con el registro, sin consultar InfluxDB.

        Args:
            csv_path (str | None): CSV de eventos; ``None`` usa el configurado.
            test_type (str | None): Código del test o ``"all"``; ``None`` usa
                ``batch.test_type``.

        Returns:
            Dict[str, list[str]]: Eventos ausentes, claves huérfanas, claves del
            esquema antiguo y eventos sin confirmar en el manifest.

        Raises:
            FileNotFoundError: Si el registro no existe.
            RegistryError: Si al registro le faltan columnas requeridas.
        """
        test_type = test_type or self.config['batch']['test_type']
        trials, _invalid = load_registry(self._registry_path(csv_path), test_type)
        report = audit_store(self._h5_database, trials, Manifest(manifest_path_for(self._h5_database)))
        logger.info(
            _("INFO_AUDIT: {0} frente a {1} eventos del registro: {2} ausentes, {3} sin confirmar en el manifest, {4} claves huérfanas, {5} claves del esquema antiguo").format(self._h5_database, len(trials), len(report['missing']), len(report['unconfirmed']), len(report['orphan']), len(report['legacy']))
        )
        for name, values in report.items():
            if values:
                logger.debug(_("DEBUG_AUDIT_{0}: {1}").format(name.upper(), values))
        return report

    def close(self):
        """Cierra de forma segura la conexión con el cliente de InfluxDB."""
        self._client.close()
