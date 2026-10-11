import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "pkgs")) # Parche de rutas (directorio de paquetes)
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

import hashlib
import json
import subprocess
import time
from datetime import datetime
from typing import Any, Iterable

import pandas as pd
import numpy as np
import logging
from influxdb_client import InfluxDBClient
from GaitMetricsCalculator import REPORT_METRICS, SYMMETRY_METRIC, GaitMetricsCalculator, ProcessingConfig
from msConfig import influx_client_options, load_config
from msTools import i18n
from msTools.i18n import _
from msTools.timeutils import influx_wallclock_z
from gait_analysis import __version__ as PIPELINE_VERSION
from gait_analysis.extractor import acquire_trial, end_run, start_run
from gait_analysis.logging_utils import configure_logging
from gait_analysis.registry import STATUS_SKIPPED, RegistryError, Trial, TrialResult, load_registry
from gait_analysis.storage import Manifest, SignalStore, StorageError, audit_store, manifest_path_for, publish_excel

logger = logging.getLogger("msGait.Pipeline")


SIDES = ("Left", "Right")

# Alcance de cada hoja del Excel (se escribe en la hoja Leeme).
SHEET_GUIDE = (
    ("Leeme", "Alcance del informe y contenido de cada hoja.", "-"),
    ("Datos_Pacientes", "Valor de cada métrica por paciente: media de sus ensayos con valor. Columnas {test}_{pie}_{métrica}.", "paciente"),
    ("Agregacion_Pacientes", "Ensayos que entran en cada paciente y test, cuántos son bilaterales y regla aplicada.", "paciente y test"),
    ("Ensayos", "Todas las métricas de cada ensayo y pie, con estado, motivo, cobertura y método de cada una.", "evento y pie"),
    ("Asimetria", "Índice de simetría de cadencia de cada ensayo; vacío si falta un pie (bilateral_available).", "evento"),
    ("Balanceos", "Cada balanceo detectado, con su altura o su causa de exclusión.", "balanceo"),
    ("Estado_Eventos", "Resultado de cada fila del registro (descarga y número de métricas con valor).", "fila del registro"),
    ("Parametros", "Valores de procesado usados (sección processing).", "parámetro"),
    ("Procedencia", "Versión del código, huella de la configuración y del registro, fecha y recuentos.", "dato"),
    ("Estadisticas_Ensayos", "N total, N con valor, media, mediana y desviación típica de cada métrica entre ensayos.", "estadístico"),
    ("Estadisticas_Globales", "Los mismos estadísticos entre pacientes (tabla Datos_Pacientes).", "estadístico"),
)
REPORT_SCOPE = (
    "Único informe del proyecto. Métricas temporales por pie (pasos, cadencia, fases y decaimiento en 6MWT) y dos "
    "estimadores no calibrados (longitud de zancada y altura del paso). Una celda vacía significa que la métrica no "
    "pudo calcularse; nunca se escribe cero en su lugar. Los valores con estado WARNING se conservan y entran en las "
    "medias."
)

# Regla explícita de agregación por paciente (se escribe también en el Excel).
AGGREGATION_RULE = "media de todos los ensayos válidos del paciente; asimetría solo con ensayos bilaterales"


def madrid_wallclock_as_influx_z(value: str) -> str:
    """Influx stores Madrid wall-clock timestamps as if they were UTC."""
    return influx_wallclock_z(value)


class GaitPipeline:
    """Pipeline único InfluxDB → HDF5 por evento → métricas de marcha → Excel.

    Attributes:
        config (dict): Configuración resuelta (YAML + entorno).
        client (InfluxDBClient | None): Cliente de conexión a InfluxDB; ``None`` sin conexión.
        bucket (str): Bucket origen de las señales.
        measurement (str): Valor de ``_measurement`` consultado.
        h5_path (str): Almacén HDF5 compartido con el extractor y el informe.
        processing (ProcessingConfig): Parámetros de procesado usados y exportados.
        calculator (GaitMetricsCalculator): Motor de cálculo de métricas.
    """

    def __init__(self, config_path: str | None = None, offline: bool = False):
        """Inicializa la conexión directa SOLO a InfluxDB, evitando el error de PostgreSQL.

        Args:
            config_path (str | None): YAML de configuración; ``None`` usa el predeterminado.
            offline (bool): No conectar con InfluxDB ni exigir sus credenciales
                (para recalcular lo almacenado o auditar el almacén).

        Raises:
            ConfigError: Si la configuración no es válida.
        """
        # Cada tarea carga solo las conexiones que usa: sin conexión no se exige ninguna credencial.
        config = load_config(config_path, sections=() if offline else ("influxdb",))
        self.config = config
        self.client, self.bucket, self.measurement = None, None, None
        if not offline:
            # Conectar Cliente InfluxDB nativo (verificación TLS según la configuración)
            self.client = InfluxDBClient(**influx_client_options(config))
            self.bucket = config["influxdb"]["bucket"]
            self.measurement = config["influxdb"]["measurement"]
        self.h5_path = config["paths"]["h5_path"]

        # Herramientas
        self.processing = ProcessingConfig.from_mapping(config.get("processing"))
        self.calculator = GaitMetricsCalculator(self.processing)
        i18n.init_translation(config["project"]["language"])

    def _analyze_foot(self, trial: Trial, side: str, df_side: pd.DataFrame) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Calcula las métricas de un pie dentro de un evento.

        La serie se prepara una sola vez (tiempo, frecuencia efectiva, huecos) y
        todos los métodos usan la misma configuración de procesado.

        Args:
            trial (Trial): Evento al que pertenece la serie.
            side (str): ``Left`` o ``Right``.
            df_side (pd.DataFrame): Serie normalizada del pie.

        Returns:
            tuple[dict[str, Any], list[dict[str, Any]]]: Métricas del pie con su
            calidad y estado, y un registro por balanceo detectado.
        """
        excluir_giros = trial.test_type != "TUG"
        prepared = self.calculator.preparar(df_side)
        cad_res = self.calculator.calcular_cadencia(prepared, excluir_giros=excluir_giros)
        fas_res = self.calculator.calcular_fases(prepared, excluir_giros=excluir_giros)
        stride_res = self.calculator.calcular_longitud_zancada(prepared)
        height_res = self.calculator.calcular_altura_zancada(prepared)
        report = prepared.report

        coverage = report.get("cobertura", {})

        def covered(*channels: str) -> float:
            """Cobertura mínima de los canales que usa una métrica."""
            return min(coverage[c] for c in channels) if all(c in coverage for c in channels) else np.nan

        step_channel = "S0" if cad_res.get("modalidad") == "pressure" else "Ay"
        metrics: dict[str, Any] = {
            "muestras": len(df_side),
            "fs_efectiva": report.get("fs_efectiva", np.nan),
            "fs_desviada": report.get("fs_desviada", np.nan),
            "duracion_real_seg": report.get("duracion_real_seg", np.nan),
            "n_huecos": report.get("n_huecos", np.nan),
            "tiempo_huecos_seg": report.get("tiempo_huecos_seg", np.nan),
            "muestras_interpoladas": report.get("muestras_interpoladas", np.nan),
            "irregularidad_tiempo": report.get("irregularidad_tiempo", np.nan),
            "cobertura_min": min(report["cobertura"].values()) if report.get("cobertura") else np.nan,
            "pasos": cad_res["num_pasos"],
            "cadencia": cad_res["cadencia"],
            "tiempo_analizado_seg": cad_res.get("tiempo_analizado_seg", np.nan),
            "tiempo_giros_seg": cad_res.get("tiempo_giros_seg", np.nan),
            "n_giros": cad_res.get("n_giros", np.nan),
            "n_giros_descartados": cad_res.get("n_giros_descartados", np.nan),
            "modalidad_pasos": cad_res.get("modalidad", ""),
            "pasos_accel": cad_res.get("pasos_accel", np.nan),
            "pasos_presion": cad_res.get("pasos_presion", np.nan),
            "cobertura_cadencia": covered(step_channel),
            "filtro_cadencia": cad_res.get("metodo_filtro", ""),
            "cobertura_fases": covered("S0", "S1", "S2"),
            "filtro_fases": fas_res.get("metodo_filtro", ""),
            "cobertura_zancada": covered("Ay"),
            "filtro_zancada": stride_res.get("metodo_filtro", ""),
            "cobertura_altura": covered("Ay", "S0", "S1", "S2"),
            "filtro_altura": height_res.get("metodo_filtro", ""),
            "apoyo_prc": fas_res["apoyo_prc"],
            "balanceo_prc": fas_res["balanceo_prc"],
            "n_ciclos": fas_res.get("n_ciclos", np.nan),
            "n_ciclos_excluidos": fas_res.get("n_ciclos_excluidos", np.nan),
            "stride_length_cm": stride_res["stride_length_cm"],
            "stride_length_raw_cm": stride_res["stride_length_raw_cm"],
            "stride_calibrado": stride_res.get("calibrado", False),
            "step_height_cm": height_res["step_height_cm"],
            "n_balanceos": height_res.get("n_balanceos", np.nan),
            "n_balanceos_excluidos": height_res.get("n_balanceos_excluidos", np.nan),
            "estado_cadencia": cad_res.get("status", ""),
            "motivo_cadencia": cad_res.get("error_reason", ""),
            "estado_fases": fas_res.get("status", ""),
            "motivo_fases": fas_res.get("error_reason", ""),
            "estado_zancada": stride_res.get("status", ""),
            "motivo_zancada": stride_res.get("error_reason", ""),
            "estado_altura": height_res.get("status", ""),
            "motivo_altura": height_res.get("error_reason", ""),
        }
        if trial.test_type == "6MWT":
            decay_res = self.calculator.calcular_decaimiento_6mwt(df_side)
            metrics["decaimiento_prc"] = decay_res["6mwt_decaimiento"]
            metrics["estado_decaimiento"] = decay_res.get("status", "")
            metrics["motivo_decaimiento"] = decay_res.get("error_reason", "")

        if stride_res.get("status") == "WARNING":
            logger.debug(_("DEBUG_GEOMETRY_WARN: Excursión inusualmente baja en paciente {p} ({s}) evento {tid}").format(
                p=trial.codeid, s=side, tid=trial.trial_id
            ))
        if height_res.get("status") == "WARNING":
            logger.debug(_("DEBUG_GEOMETRY_WARN: Altura atípica en paciente {p} ({s}) evento {tid}").format(
                p=trial.codeid, s=side, tid=trial.trial_id
            ))
        for name in ("cadencia", "fases", "zancada", "altura", "decaimiento"):
            if metrics.get(f"motivo_{name}"):
                logger.debug(
                    _("DEBUG_METRIC_STATUS: Evento {0} ({1}) {2}: {3} ({4})").format(trial.trial_id, side, name, metrics[f'estado_{name}'], metrics[f'motivo_{name}'])
                )
        return metrics, height_res.get("eventos", [])

    def _analyze_trial(
        self, trial: Trial, result: TrialResult, frames: dict[str, pd.DataFrame]
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Calcula las métricas de un evento usando solo sus propias señales.

        Args:
            trial (Trial): Evento del registro.
            result (TrialResult): Resultado de la adquisición del evento.
            frames (dict[str, pd.DataFrame]): Series normalizadas por pie.

        Returns:
            tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
            Filas por pie, fila de asimetría del evento, fila ancha del evento y
            filas por balanceo.
        """
        tt = trial.test_type
        identity = {"TrialID": trial.trial_id, "CodeID": trial.codeid, "test_type": tt}
        wide: dict[str, Any] = {**identity, f"{tt}_duracion_seg": trial.duration_s}
        foot_rows: list[dict[str, Any]] = []
        cadences: dict[str, float] = {}
        swing_rows: list[dict[str, Any]] = []

        for side in SIDES:
            df_side = frames.get(side)
            if df_side is None or df_side.empty:
                continue
            metrics, swings = self._analyze_foot(trial, side, df_side)
            swing_rows.extend({**identity, "Foot": side, **swing} for swing in swings)
            cadences[side] = metrics["cadencia"]
            foot_rows.append({
                **identity, "Foot": side, "d_from": trial.d_from, "d_until": trial.d_until,
                "duracion_seg": trial.duration_s,
                "dispositivo": result.feet.get(side, {}).get("device", ""), **metrics,
            })
            for name in REPORT_METRICS:
                if name in metrics:
                    wide[f"{tt}_{side}_{name}"] = metrics[name]

        # --- Cálculo de Asimetría Bilateral: EXCLUSIVO del evento actual ---
        cad_l, cad_r = cadences.get("Left", 0.0), cadences.get("Right", 0.0)
        symmetry_index = np.nan
        if cad_l > 0 and cad_r > 0:
            symmetry_index = round((abs(cad_l - cad_r) / (0.5 * (cad_l + cad_r))) * 100, 2)
            if symmetry_index > 20.0:
                logger.debug(_("DEBUG_ASYMMETRY_DETECTED: Alta asimetría en evento {tid} paciente {p} ({si}%)").format(
                    tid=trial.trial_id, p=trial.codeid, si=symmetry_index
                ))
        else:
            # Si en este ensayo falta un pie, no se puede calcular bilateralidad
            logger.debug(_("DEBUG_NO_BILATERAL: Evento {0} sin cadencia válida en ambos pies; asimetría no calculada").format(trial.trial_id))
        wide[f"{tt}_{SYMMETRY_METRIC}"] = symmetry_index
        asymmetry = {
            **identity, "pies_con_datos": ", ".join(sorted(cadences)),
            "bilateral_available": bool(cad_l > 0 and cad_r > 0),
            "cadencia_Left": cadences.get("Left", np.nan), "cadencia_Right": cadences.get("Right", np.nan),
            "symmetry_index": symmetry_index,
        }
        return foot_rows, asymmetry, wide, swing_rows

    def _aggregate_patients(
        self, wide_rows: list[dict[str, Any]], asymmetry_rows: list[dict[str, Any]]
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Agrega los eventos por paciente con la regla ``AGGREGATION_RULE``.

        Ningún ensayo se descarta: todos los eventos válidos del paciente entran
        en la media de cada métrica. La asimetría se promedia solo entre los
        ensayos con cadencia válida en ambos pies.

        Args:
            wide_rows (list[dict[str, Any]]): Una fila ancha por evento.
            asymmetry_rows (list[dict[str, Any]]): Una fila de asimetría por evento.

        Returns:
            tuple[pd.DataFrame, pd.DataFrame]: Tabla con una fila por paciente
            (columnas con prefijo del test) y tabla de trazabilidad con una fila
            por paciente y test (ensayos usados, bilaterales y regla).
        """
        events = pd.DataFrame(wide_rows).drop(columns=["TrialID", "test_type"])
        metrics = events.drop(columns=["CodeID"]).apply(lambda x: pd.to_numeric(x, errors="coerce"))
        patients = metrics.groupby(events["CodeID"], sort=False).mean().round(2).reset_index()

        trace = pd.DataFrame(asymmetry_rows).groupby(["CodeID", "test_type"], sort=False).agg(
            n_ensayos=("TrialID", "size"),
            n_bilaterales=("symmetry_index", "count"),
            eventos=("TrialID", lambda ids: ", ".join(str(i) for i in ids)),
            symmetry_index_media=("symmetry_index", "mean"),
        ).reset_index()
        trace["symmetry_index_media"] = trace["symmetry_index_media"].round(2)
        trace["regla"] = AGGREGATION_RULE

        repeated = trace[trace["n_ensayos"] > 1]
        logger.info(
            _("INFO_PATIENT_AGGREGATION: {0} pacientes agregados; {1} combinaciones paciente-test con ensayos repetidos ({2})").format(len(patients), len(repeated), AGGREGATION_RULE)
        )
        for row in repeated.itertuples():
            logger.debug(
                _("DEBUG_REPEATED_TRIALS: Paciente {0} ({1}): eventos {2}; {3} bilaterales").format(row.CodeID, row.test_type, row.eventos, row.n_bilaterales)
            )
        return patients, trace

    @staticmethod
    def _metric_columns(table: pd.DataFrame) -> list[str]:
        """Selecciona de forma explícita las columnas de métricas de una tabla ancha."""
        suffixes = tuple(f"_{name}" for name in (*REPORT_METRICS, SYMMETRY_METRIC))
        return [column for column in table.columns if column.endswith(suffixes)]

    def _statistics(self, table: pd.DataFrame, tests: Iterable[str]) -> pd.DataFrame:
        """Resume cada métrica con su N total, N con valor y estadísticos.

        Solo entran las métricas de la lista explícita ``REPORT_METRICS`` y el
        índice de simetría; identificadores y duraciones del registro quedan
        fuera aunque sean numéricos.

        Args:
            table (pd.DataFrame): Tabla ancha (una fila por ensayo o por paciente)
                con columnas ``{test}_{pie}_{métrica}`` y ``{test}_duracion_seg``.
            tests (Iterable[str]): Códigos de test presentes.

        Returns:
            pd.DataFrame: Filas ``n_total`` (filas que tienen ese test),
            ``n_validos``, ``n_sin_valor``, ``media``, ``mediana`` y
            ``desv_tipica``; una columna por métrica.
        """
        columns = self._metric_columns(table)
        numeric = table[columns].apply(lambda x: pd.to_numeric(x, errors="coerce"))
        tests = sorted(tests, key=len, reverse=True)
        totals = {}
        for column in columns:
            test = next((t for t in tests if column.startswith(f"{t}_")), None)
            present = table.get(f"{test}_duracion_seg")
            totals[column] = int(pd.to_numeric(present, errors="coerce").notna().sum()) if present is not None else len(table)
        total = pd.Series(totals, dtype=float)
        return pd.DataFrame({
            "n_total": total, "n_validos": numeric.count(), "n_sin_valor": total - numeric.count(),
            "media": numeric.mean().round(3), "mediana": numeric.median().round(3), "desv_tipica": numeric.std().round(3),
        }).T

    def _provenance(self, csv_path: str, counts: dict[str, Any]) -> pd.DataFrame:
        """Reúne lo necesario para saber con qué código y datos se obtuvo el Excel.

        Args:
            csv_path (str): Registro de eventos usado.
            counts (dict[str, Any]): Recuentos de la ejecución.

        Returns:
            pd.DataFrame: Tabla ``clave``/``valor`` con versión del código,
            huellas de la configuración y del registro, fecha y recuentos.
        """
        root = os.path.dirname(os.path.abspath(__file__))

        def git(*args: str) -> str:
            try:
                return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=20).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                return ""

        commit = git("rev-parse", "HEAD") or "desconocido"
        config_hash = hashlib.sha256(json.dumps(self.processing.to_record(), sort_keys=True).encode()).hexdigest()[:16]
        with open(csv_path, "rb") as handle:
            registry_hash = hashlib.sha256(handle.read()).hexdigest()[:16]
        record = {
            "generado": datetime.now().isoformat(timespec="seconds"),
            "version_pipeline": PIPELINE_VERSION,
            "commit_git": commit,
            "cambios_sin_commit": bool(git("status", "--porcelain")) if commit != "desconocido" else "desconocido",
            "huella_config_processing": config_hash,
            "registro": os.path.abspath(csv_path),
            "huella_registro": registry_hash,
            "almacen": self.h5_path,
            "medicion_influx": self.measurement,
            "regla_agregacion": AGGREGATION_RULE,
            "python": sys.version.split()[0], "pandas": pd.__version__, "numpy": np.__version__,
            **counts,
        }
        logger.debug(_("DEBUG_PROVENANCE: commit {0}, configuración {1}, registro {2}").format(commit[:12], config_hash, registry_hash))
        return pd.DataFrame([{"clave": key, "valor": str(value)} for key, value in record.items()])

    def audit(self, csv_path: str | None = None, test_type: str | None = None) -> dict[str, list[str]]:
        """Compara el almacén con el registro, sin consultar InfluxDB.

        Args:
            csv_path (str | None): CSV de eventos; ``None`` usa el configurado.
            test_type (str | None): Código del test; ``None`` o ``"all"`` los revisa todos.

        Returns:
            dict[str, list[str]]: Eventos ausentes, claves huérfanas, almacén del
            formato anterior y eventos sin confirmar en el manifest.

        Raises:
            FileNotFoundError: Si el registro no existe.
            RegistryError: Si al registro le faltan columnas requeridas.
        """
        trials, _invalid = load_registry(csv_path or self.config["paths"]["registry_csv"], test_type)
        report = audit_store(self.h5_path, trials, Manifest(manifest_path_for(self.h5_path)))
        logger.info(
            _("INFO_AUDIT: {0} frente a {1} eventos del registro: {2} ausentes, {3} sin confirmar en el manifest, {4} claves huérfanas, {5} almacenes del formato anterior").format(self.h5_path, len(trials), len(report['missing']), len(report['unconfirmed']), len(report['orphan']), len(report['legacy']))
        )
        for name, values in report.items():
            if values:
                logger.debug(_("DEBUG_AUDIT_{0}: {1}").format(name.upper(), values))
        return report

    def run_from_csv(
        self,
        csv_path: str | None = None,
        ids: Iterable[str] | None = None,
        missing_only: bool = False,
        test_type: str | None = None,
        stored_only: bool = False,
    ) -> list[TrialResult]:
        """Ejecuta el procesamiento masivo de ensayos de marcha desde un registro CSV.

        Cada evento del registro se procesa de forma aislada: sus métricas y su
        asimetría bilateral se calculan solo con las señales de ese evento, de
        modo que ensayos repetidos nunca se sobrescriben ni se mezclan. Después
        se agrega por paciente con la regla ``AGGREGATION_RULE``.

        Hojas generadas: ``Datos_Pacientes`` (una fila por paciente, columnas
        con prefijo del test), ``Agregacion_Pacientes`` (ensayos usados por
        paciente y test, y regla aplicada), ``Ensayos`` (una fila por evento y
        pie: todos los ensayos), ``Asimetria`` (una fila por evento),
        ``Balanceos`` (una fila por balanceo, con su altura o su causa de
        exclusión), ``Estado_Eventos`` (resultado de cada fila del registro),
        ``Parametros`` (valores de procesado usados), ``Procedencia`` (versión
        del código, huellas de configuración y registro), ``Estadisticas_Ensayos``
        y ``Estadisticas_Globales`` (N total, N con valor y estadísticos de las
        métricas explícitas, entre ensayos y entre pacientes) y ``Leeme``
        (alcance del informe y de cada hoja).

        Args:
            csv_path (str | None): Ruta al archivo CSV con el registro de ensayos.
                Si no se especifica, se toma de la configuración cargada.
            ids (Iterable[str] | None): Identificadores de evento a procesar.
            missing_only (bool): Reutilizar las señales ya guardadas en HDF5 de
                los eventos completados y consultar solo los que faltan.
            test_type (str | None): Código del test a procesar; ``None`` o
                ``"all"`` procesan todos.
            stored_only (bool): Recalcular solo los eventos ya guardados, sin
                ninguna consulta a InfluxDB; el resto queda como ``SKIPPED``.

        Returns:
            list[TrialResult]: Un resultado por fila seleccionada del registro.

        Raises:
            FileNotFoundError: Si el archivo de registro CSV no existe.
            RegistryError: Si al registro le faltan columnas requeridas.
            StorageError: Si el almacén está bloqueado, tiene el esquema antiguo
                o el Excel no puede publicarse.
        """
        config = self.config
        csv_path = csv_path or config["paths"]["registry_csv"]
        trials, results = load_registry(csv_path, test_type, ids)

        manifest = Manifest(manifest_path_for(self.h5_path))
        completed = manifest.completed_keys() if (missing_only or stored_only) else None
        if stored_only:
            pending = [trial for trial in trials if trial.trial_id not in completed]
            trials = [trial for trial in trials if trial.trial_id in completed]
            for trial in pending:
                skipped = TrialResult.from_trial(trial)
                skipped.status, skipped.detail = STATUS_SKIPPED, "no almacenado; no se consulta (--stored-only)"
                results.append(skipped)
            logger.info(
                _("INFO_STORED_ONLY: {0} eventos se recalculan desde el almacén; {1} no están almacenados y se omiten sin consultar").format(len(trials), len(pending))
            )
        foot_rows: list[dict[str, Any]] = []
        asymmetry_rows: list[dict[str, Any]] = []
        wide_rows: list[dict[str, Any]] = []
        swing_rows: list[dict[str, Any]] = []

        started = time.perf_counter()
        with SignalStore(self.h5_path) as store:
            run_id = start_run(manifest, "pipeline", len(trials))
            for position, trial in enumerate(trials, start=1):
                logger.info(_("INFO_INGEST_TRIAL: [{cid}] - Ingestando evento {tid} ({tt}) desde InfluxDB...").format(
                    cid=trial.codeid, tid=trial.trial_id, tt=trial.test_type
                ) + _(" [{0}/{1}]").format(position, len(trials)))

                # --- BLOQUE 1: INGESTA Y PERSISTENCIA (contrato compartido con el extractor) ---
                result, frames = acquire_trial(
                    self.client, self.bucket, self.measurement, store, manifest, trial, run_id,
                    completed, load_stored=True,
                )
                results.append(result)
                logger.debug(_("DEBUG_TRIAL_COST: Evento {0}: {1:.2f} s, {2:.2f} MB").format(trial.trial_id, result.elapsed_s, result.signal_mem_mb))
                if not frames:
                    continue
                if not any(side in frames for side in SIDES):
                    logger.warning(_("WARN_MISSING_FOOT_TAG: [{cid}] - No se encontró la etiqueta de pierna en evento {tid}").format(
                        cid=trial.codeid, tid=trial.trial_id
                    ))
                    continue

                # --- BLOQUE 2 y 3: ANÁLISIS ---
                rows, asymmetry, wide, swings = self._analyze_trial(trial, result, frames)
                foot_rows.extend(rows)
                swing_rows.extend(swings)
                asymmetry_rows.append(asymmetry)
                wide_rows.append(wide)
            end_run(manifest, run_id, results, "Pipeline", time.perf_counter() - started)

        if not wide_rows:
            logger.error(_("ERR_NO_DATA_PROCESSED: No se generaron datos para ningún ensayo clínico."))
            return results

        # --- Ensayos realmente procesados: con al menos una métrica con valor ---
        df_events = pd.DataFrame(wide_rows)
        valid_metrics = df_events[self._metric_columns(df_events)].apply(lambda x: pd.to_numeric(x, errors="coerce")).notna().sum(axis=1)
        valid_by_trial = dict(zip(df_events["TrialID"], valid_metrics.astype(int)))
        processed = int((valid_metrics > 0).sum())
        logger.info(
            _("INFO_TRIALS_PROCESSED: {0} de {1} eventos con señales tienen al menos una métrica con valor ({2} filas del registro seleccionadas)").format(processed, len(wide_rows), len(results))
        )
        if processed == 0:
            logger.error(_("ERR_NO_VALID_METRICS: Ningún evento tiene métricas con valor; el Excel solo contiene estados."))

        # --- BLOQUE 4: AGREGACIÓN POR PACIENTE (posterior y con regla explícita) ---
        df_patients, df_trace = self._aggregate_patients(wide_rows, asymmetry_rows)
        df_final = df_patients.astype(object).fillna("N/A")
        tests = sorted(df_events["test_type"].unique())
        sheets = {
            "Leeme": pd.DataFrame(
                [{"hoja": "(informe)", "contenido": REPORT_SCOPE, "una_fila_por": "-"}]
                + [{"hoja": name, "contenido": content, "una_fila_por": unit} for name, content, unit in SHEET_GUIDE]
            ),
            "Datos_Pacientes": df_final,
            "Agregacion_Pacientes": df_trace,
            "Ensayos": pd.DataFrame(foot_rows),
            "Asimetria": pd.DataFrame(asymmetry_rows),
            "Balanceos": pd.DataFrame(swing_rows),
            "Parametros": pd.DataFrame(
                [{"parametro": name, "valor": value} for name, value in self.processing.to_record().items()]
            ),
            "Estado_Eventos": pd.DataFrame([
                {
                    **{key: value for key, value in result.to_record().items() if key != "feet"},
                    "n_metricas_validas": valid_by_trial.get(result.trial_id, 0),
                } for result in results
            ]),
            "Procedencia": self._provenance(csv_path, {
                "filas_registro_seleccionadas": len(results), "eventos_con_senales": len(wide_rows),
                "eventos_con_metricas_validas": processed, "pacientes": len(df_patients),
            }),
            "Estadisticas_Ensayos": self._statistics(df_events, tests),
            "Estadisticas_Globales": self._statistics(df_patients, tests),
        }
        deviated = [row for row in foot_rows if row.get("fs_desviada") is True]
        if deviated:
            rates = sorted({round(row["fs_efectiva"]) for row in deviated})
            logger.warning(
                _("WARN_FS_MISMATCH: {0} de {1} series tienen una frecuencia efectiva (~{2} Hz) alejada de la nominal processing.fs={3:g} Hz. Las métricas usan la frecuencia medida; revise la configuración.").format(len(deviated), len(foot_rows), rates, self.processing.fs)
            )
        if not (sheets["Estadisticas_Globales"].loc["n_validos"] > 0).any():
            logger.warning(_("WARN_NO_NUMERIC_STATS: No hay suficientes datos numéricos para Estadísticas Globales."))

        output = config["paths"]["output_dataset"]
        publish_excel(sheets, output, index_sheets=("Estadisticas_Ensayos", "Estadisticas_Globales"))
        logger.info(_("INFO_PIPELINE_COMPLETED: Pipeline finalizado. Excel generado ({out}).").format(out=output))
        return results

if __name__ == "__main__":
    import argparse
    from msConfig import ConfigError
    parser = argparse.ArgumentParser(description="Pipeline único InfluxDB → HDF5 → Excel")
    parser.add_argument('--config', default=None, help='YAML; por defecto config/config.yaml')
    parser.add_argument('--csv', default=None, help='Sustituye paths.registry_csv')
    parser.add_argument('--ids', default=None, help='Identificadores de evento separados por comas')
    parser.add_argument('--test-type', default=None, help='Código de test a procesar; por defecto todos')
    parser.add_argument('--missing-only', action='store_true', help='Reutilizar del HDF5 los eventos ya completados')
    parser.add_argument('--stored-only', action='store_true', help='Recalcular solo lo ya guardado, sin consultar InfluxDB')
    parser.add_argument('--audit', action='store_true', help='Comparar el almacén con el registro, sin consultar InfluxDB')
    parser.add_argument('--check-config', action='store_true', help='Validar YAML/.env sin acceder a bases de datos')
    parser.add_argument('--verbose', type=int, choices=(0, 1, 2), default=1, help='0 avisos, 1 información, 2 depuración')
    args = parser.parse_args()
    configure_logging(args.verbose)
    try:
        if args.check_config:
            load_config(args.config)
            print('Configuración válida; no se han realizado conexiones.')
            sys.exit(0)
        pipeline = GaitPipeline(args.config, offline=args.stored_only or args.audit)
    except ConfigError as error:
        sys.exit(str(error))
    ids = [i.strip() for i in args.ids.split(',') if i.strip()] if args.ids else None
    try:
        if args.audit:
            pipeline.audit(args.csv, args.test_type)
        else:
            pipeline.run_from_csv(args.csv, ids, args.missing_only, args.test_type, args.stored_only)
    except (FileNotFoundError, RegistryError, StorageError) as error:
        sys.exit(f"ERROR: {error}")
    finally:
        if pipeline.client is not None:
            pipeline.client.close()
