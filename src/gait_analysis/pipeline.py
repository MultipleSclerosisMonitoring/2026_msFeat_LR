import sys
import os

import hashlib
import json
import subprocess
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Iterable

import pandas as pd
import numpy as np
import logging
from influxdb_client import InfluxDBClient
from gait_analysis.metrics import METRIC_STATE, REPORT_METRICS, SYMMETRY_METRIC, GaitMetricsCalculator, ProcessingConfig
from msConfig import influx_client_options, load_config
from msTools import i18n
from msTools.i18n import _
from msTools.timeutils import experiment_wallclock, influx_wallclock_z
from gait_analysis import __version__ as PIPELINE_VERSION
from gait_analysis.extractor import acquire_trial, end_run, fetch_with_retries, start_run
from gait_analysis.logging_utils import configure_logging
from gait_analysis.registry import STATUS_SKIPPED, RegistryError, Trial, TrialResult, load_registry
from gait_analysis.storage import (
    Manifest, SignalStore, StorageError, audit_store, manifest_path_for, publish_excel, set_aside, write_derived,
)

logger = logging.getLogger("msGait.Pipeline")


class NoDataError(RuntimeError):
    """Ninguna fila seleccionada del registro ha producido señales que analizar."""


SIDES = ("Left", "Right")
# Pruebas en las que no se informan métricas de marcha: la marcha estable es demasiado corta.
TESTS_WITHOUT_GAIT_METRICS = ("TUG",)
NOT_REPORTED = "NO_APLICA"
# Pruebas cuyos intentos del mismo paciente y día se juntan para alcanzar el mínimo de zancadas.
POOLED_TESTS = ("T25FW",)
POOLED_NOTE = ("zancadas de los intentos del mismo día juntas; resultado orientativo; asimetría: media de los índices "
               "de los días con cadencia en los dos pies (nunca se comparan pies de días distintos)")

# Alcance de cada hoja del Excel (se escribe en la hoja Leeme).
SHEET_GUIDE = (
    ("Leeme", "Alcance del informe y contenido de cada hoja.", "-"),
    ("Datos_Ensayos", "Todas las métricas de cada ensayo en una fila, con su fecha: es la tabla para seguir a un paciente en el tiempo. Columnas {test}_{pie}_{métrica} y su estado de confianza {test}_{pie}_estado_{grupo} (OK o WARNING).", "evento"),
    ("Datos_Pacientes", "Resumen por paciente: media de sus ensayos con valor, incluidos los que están en WARNING; {test}_ensayos_warning dice cuántos lo están y {test}_{pie}_estado_{grupo}_n_warning, cuántos en cada grupo de métricas. Columnas {test}_{pie}_{métrica}.", "paciente"),
    ("Calidad_Series", "Resultado de cada filtro de calidad por serie (presión, IMU, mínimo de zancadas) y motivo principal de lo que no se informa. Distingue lo observado de lo rellenado: cobertura_observada_* (fracción de filas con valor nuevo), cobertura_* (fracción utilizable tras rellenar), muestras_interpoladas, muestras_retenidas (último valor repetido) y muestras_repetidas_reposo (IMU repetida con el pie quieto, aceptada como medida).", "evento y pie"),
    ("Agregacion_Pacientes", "Ensayos que entran en cada paciente y test, cuántos son bilaterales y regla aplicada. En el T25FW, dias_bilaterales: días con cadencia en los dos pies, de los que sale la asimetría del paciente.", "paciente y test"),
    ("Ensayos", "Todas las métricas de cada ensayo y pie, con estado, motivo, cobertura y método de cada una.", "evento y pie"),
    ("Asimetria", "Índice de simetría de cadencia de cada ensayo; vacío si falta un pie (bilateral_available). Incluye la comparación del recuento de zancadas de los dos pies.", "evento"),
    ("Zancadas", "Cada zancada integrada entre dos pies planos: longitud, excursión vertical del tobillo o su causa de exclusión. La MTC queda vacía.", "zancada"),
    ("Estado_Eventos", "Resultado de cada fila del registro (descarga y número de métricas con valor).", "fila del registro"),
    ("Parametros", "Valores de procesado usados (sección processing).", "parámetro"),
    ("Procedencia", "Versión del código, huella de la configuración y del registro, fecha y recuentos.", "dato"),
    ("Estadisticas_Ensayos", "N total, N con valor, media, mediana y desviación típica de cada métrica entre ensayos.", "estadístico"),
    ("Estadisticas_Globales", "Los mismos estadísticos entre pacientes (tabla Datos_Pacientes).", "estadístico"),
)
REPORT_SCOPE = (
    "Único informe del proyecto. Métricas temporales por pie (zancadas, cadencia de zancada, fases y decaimiento en 6MWT), "
    "longitud de zancada y excursión vertical del tobillo por integración entre pies planos, estas dos sin validar con una "
    "referencia. La distancia mínima punta-suelo (MTC) no se calcula: el módulo va en el tobillo y falta trasladar la "
    "trayectoria a la punta. En TUG no se informan métricas de marcha (su métrica es la duración). En todos los ensayos se "
    "descarta la primera zancada de cada pie y, junto a cada giro, las zancadas de frenada y de arranque; si la IMU no es "
    "válida no se sabe dónde están los giros y esa exclusión no se aplica (columna giros_excluidos). En T25FW se juntan las "
    "zancadas de los intentos del mismo día y el resultado es orientativo; en cada intento se avisa si la suma de las "
    "longitudes de zancada no es coherente con los 7,62 m de la prueba (t25fw_distancia_coherente), sin que eso valide ni "
    "calibre la longitud. El decaimiento del 6MWT es la pendiente de la "
    "cadencia minuto a minuto. No hay eventos anotados ni otro dispositivo de referencia: la "
    "comprobación disponible es visual (Grafana) y de coherencia interna. Por eso siguen sin validar con una referencia "
    "la detección de giros y sus umbrales, la correspondencia de cada contacto de talón con un apoyo real, la polaridad de "
    "la presión (la lectura es relativa, no una presión absoluta), la longitud de zancada y la excursión del tobillo. Dentro de un tramo sin huecos se supone que no "
    "falta ninguna muestra; eso cubre las ráfagas y los retrasos de recepción, no las pérdidas pequeñas sin hueco detectable. Una celda vacía significa que la métrica no "
    "pudo calcularse; nunca se escribe cero en su lugar. Los valores con estado WARNING se conservan y entran siempre en "
    "las medias y en los gráficos; su estado de confianza acompaña a cada valor."
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

    def _analyze_foot(
        self, trial: Trial, side: str, df_side: pd.DataFrame
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[float]]]:
        """Calcula las métricas de un pie dentro de un evento.

        La serie se prepara una sola vez (tiempo, frecuencia efectiva, huecos) y
        todos los métodos usan la misma configuración de procesado.

        Args:
            trial (Trial): Evento al que pertenece la serie.
            side (str): ``Left`` o ``Right``.
            df_side (pd.DataFrame): Serie normalizada del pie.

        Returns:
            tuple[dict[str, Any], list[dict[str, Any]], dict[str, list[float]]]:
            Métricas del pie con su calidad y estado, un registro por zancada
            integrada y los valores por zancada que pueden juntarse entre intentos.
        """
        minimum = self._min_strides(trial.test_type)
        prepared = self.calculator.preparar(df_side)
        if trial.test_type in TESTS_WITHOUT_GAIT_METRICS or minimum is None:
            if minimum is None and trial.test_type not in TESTS_WITHOUT_GAIT_METRICS:
                reason = f"no se informa: la prueba {trial.test_type} no tiene mínimo de zancadas definido (processing.min_strides)"
            else:
                reason = f"no se informa en {trial.test_type}: la marcha estable es demasiado corta; su métrica es la duración"
            empty = {"status": NOT_REPORTED, "error_reason": reason, "eventos": []}
            cad_res = {**empty, "num_zancadas": np.nan, "cadencia_zancada": np.nan, "tiempo_zancada_seg": np.nan}
            fas_res = {**empty, "apoyo_prc": np.nan, "balanceo_prc": np.nan}
            stride_res = {**empty, "stride_length_cm": np.nan, "stride_length_std_cm": np.nan}
            height_res = {**empty, "altura_minima_cm": np.nan, "excursion_tobillo_cm": np.nan, "excursion_tobillo_std_cm": np.nan}
        else:
            # Arranque: se descarta la primera zancada completa de cada pie.
            skip = self.processing.skip_first_strides
            options = {"excluir_giros": True, "min_strides": minimum, "skip_first": skip}
            cad_res = self.calculator.calcular_cadencia(prepared, **options)
            fas_res = self.calculator.calcular_fases(prepared, **options)
            stride_res = self.calculator.calcular_longitud_zancada(prepared, **options)
            height_res = self.calculator.calcular_altura_zancada(prepared, **options)
        report = prepared.report

        coverage = report.get("cobertura", {})

        def covered(*channels: str) -> float:
            """Cobertura mínima de los canales que usa una métrica."""
            return min(coverage[c] for c in channels) if all(c in coverage for c in channels) else np.nan

        metrics: dict[str, Any] = {
            "muestras": len(df_side),
            "fs_efectiva": report.get("fs_efectiva", np.nan),
            "fs_desviada": report.get("fs_desviada", np.nan),
            "duracion_real_seg": report.get("duracion_real_seg", np.nan),
            "n_huecos": report.get("n_huecos", np.nan),
            "tiempo_huecos_seg": report.get("tiempo_huecos_seg", np.nan),
            "muestras_interpoladas": report.get("muestras_interpoladas", np.nan),
            "muestras_retenidas": report.get("muestras_retenidas", np.nan),
            "muestras_repetidas_reposo": report.get("muestras_repetidas_reposo", np.nan),
            "cobertura_observada_imu": report.get("cobertura_observada", {}).get("imu", np.nan),
            "cobertura_observada_presion": report.get("cobertura_observada", {}).get("presion", np.nan),
            "irregularidad_tiempo": report.get("irregularidad_tiempo", np.nan),
            "rejilla_tiempo": report.get("rejilla_tiempo", ""),
            "cobertura_imu": coverage.get("imu", np.nan),
            "cobertura_presion": coverage.get("presion", np.nan),
            "huecos_imu": report.get("huecos_por_grupo", {}).get("imu", np.nan),
            "huecos_presion": report.get("huecos_por_grupo", {}).get("presion", np.nan),
            "imu_valida": report.get("imu_valida", np.nan),
            "imu_estado": report.get("imu_estado", ""),
            "presion_activa_seg": report.get("presion_activa_seg", np.nan),
            "imu_repeticion_presion_activa_prc": report.get("imu_repeticion_presion_activa_prc", np.nan),
            "intervalos_recuperados": report.get("intervalos_recuperados", np.nan),
            "vertical_estimada": report.get("vertical_estimada", np.nan),
            "inclinacion_grados": report.get("inclinacion_grados", np.nan),
            "min_zancadas": minimum,
            "rango_talon": report.get("rango_talon", np.nan),
            "periodicidad_talon": report.get("periodicidad_talon", np.nan),
            "criterio_presion": report.get("criterio_presion", ""),
            "ventanas_ritmo": report.get("ventanas_ritmo", np.nan),
            "ventanas_con_ritmo_prc": report.get("ventanas_con_ritmo_prc", np.nan),
            "zancadas": cad_res["num_zancadas"],
            "cadencia_zancada": cad_res["cadencia_zancada"],
            "tiempo_zancada_seg": cad_res["tiempo_zancada_seg"],
            "tiempo_zancada_std_seg": cad_res.get("tiempo_zancada_std_seg", np.nan),
            "contactos_talon": cad_res.get("contactos_talon", np.nan),
            "zancadas_en_banda_prc": cad_res.get("zancadas_en_banda_prc", np.nan),
            "n_tramos": cad_res.get("n_tramos", np.nan),
            "tiempo_analizado_seg": cad_res.get("tiempo_analizado_seg", np.nan),
            "tiempo_giros_seg": cad_res.get("tiempo_giros_seg", np.nan),
            "n_giros": cad_res.get("n_giros", np.nan),
            "n_giros_descartados": cad_res.get("n_giros_descartados", np.nan),
            "angulo_giros_grados": cad_res.get("angulo_giros_grados", np.nan),
            # False: no se pudo localizar los giros (IMU no válida) y la cadencia incluye frenadas y arranques.
            "giros_excluidos": cad_res.get("giros_evaluados", np.nan),
            "zancadas_margen_giro": cad_res.get("zancadas_margen_giro", np.nan),
            "modalidad_zancadas": cad_res.get("modalidad", ""),
            "cobertura_cadencia": covered("S0", "S1", "S2"),
            "filtro_cadencia": cad_res.get("metodo_filtro", ""),
            "cobertura_fases": covered("S0", "S1", "S2"),
            "filtro_fases": fas_res.get("metodo_filtro", ""),
            "cobertura_zancada": covered("Ax", "Ay", "Az", "Gx", "Gy", "Gz", "S0", "S1", "S2"),
            "filtro_zancada": stride_res.get("metodo_filtro", ""),
            "cobertura_altura": covered("Av", "S0", "S1", "S2"),
            "filtro_altura": height_res.get("metodo_filtro", ""),
            "apoyo_prc": fas_res["apoyo_prc"],
            "balanceo_prc": fas_res["balanceo_prc"],
            "apoyo_std_prc": fas_res.get("apoyo_std_prc", np.nan),
            "n_ciclos": fas_res.get("n_ciclos", np.nan),
            "n_ciclos_excluidos": fas_res.get("n_ciclos_excluidos", np.nan),
            "stride_length_cm": stride_res["stride_length_cm"],
            "stride_length_std_cm": stride_res["stride_length_std_cm"],
            "n_zancadas_longitud": stride_res.get("n_zancadas_longitud", np.nan),
            "n_zancadas_longitud_excluidas": stride_res.get("n_zancadas_longitud_excluidas", np.nan),
            "metodo_longitud": stride_res.get("metodo", ""),
            **self._t25fw_distance_check(trial, side, height_res.get("eventos", [])),
            "excursion_tobillo_cm": height_res["excursion_tobillo_cm"],
            "excursion_tobillo_std_cm": height_res["excursion_tobillo_std_cm"],
            "altura_minima_cm": height_res["altura_minima_cm"],
            "motivo_mtc": height_res.get("motivo_mtc", ""),
            "n_zancadas_altura": height_res.get("n_zancadas_altura", np.nan),
            "n_zancadas_altura_excluidas": height_res.get("n_zancadas_altura_excluidas", np.nan),
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
            decay_res = self.calculator.calcular_decaimiento_6mwt(
                df_side, duracion_registro_s=trial.duration_s, inicio=experiment_wallclock(trial.d_from),
                skip_first=self.processing.skip_first_strides,
            )
            metrics["decaimiento_prc"] = decay_res["6mwt_decaimiento"]
            metrics["pendiente_cadencia"] = decay_res["pendiente_cadencia"]
            metrics["minutos_con_cadencia"] = decay_res.get("minutos_con_cadencia", np.nan)
            metrics["cadencias_minuto"] = str(decay_res.get("cadencias_minuto", "")) if decay_res.get("cadencias_minuto") else ""
            metrics["minutos_validos"] = decay_res.get("minutos_validos", np.nan)
            metrics["ensayo_6mwt_completo"] = decay_res.get("ensayo_completo", np.nan)
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
        events = height_res.get("eventos", [])
        pool = {
            "tiempos": cad_res.get("tiempos_zancada", []), "balanceos": fas_res.get("balanceos_ciclo_prc", []),
            "longitudes": [event["longitud_cm"] for event in events if not event["motivo_exclusion"]],
            "alturas": [event["excursion_tobillo_cm"] for event in events if np.isfinite(event["excursion_tobillo_cm"])],
            "contactos": cad_res.get("contactos_s", []), "giros": cad_res.get("giros", []),
            "despegues": cad_res.get("despegues_s", []), "tramos_excluidos": cad_res.get("tramos_excluidos", []),
        }
        if skip_first := self.processing.skip_first_strides:
            for name in ("longitudes", "alturas"):
                pool[name] = pool[name][skip_first:]
        return metrics, events, pool

    def _t25fw_distance_check(self, trial: Trial, side: str, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Compara la suma de longitudes de zancada de un intento de T25FW con la distancia de la prueba.

        Es un aviso de coherencia, no una validación ni una calibración: la
        longitud de zancada no se corrige ni cambia de estado. Un pie recorre
        la distancia de la prueba (``t25fw_distance_m``) en zancadas completas
        menos, como mucho, una zancada parcial al principio y otra al final.
        Por eso la suma de las zancadas integradas se considera coherente si no
        supera la distancia y no le faltan más de dos zancadas medias, más una
        por cada zancada detectada que no pudo integrarse.

        Args:
            trial (Trial): Evento analizado.
            side (str): ``Left`` o ``Right``.
            events (list[dict[str, Any]]): Zancadas integradas del pie, con su
                longitud y su causa de exclusión.

        Returns:
            dict[str, Any]: ``t25fw_suma_longitudes_m`` y
            ``t25fw_distancia_coherente``; vacío si la prueba no es T25FW.
        """
        if trial.test_type != "T25FW":
            return {}
        lengths = [event["longitud_cm"] / 100 for event in events if not event["motivo_exclusion"]]
        if not lengths:
            return {"t25fw_suma_longitudes_m": np.nan, "t25fw_distancia_coherente": np.nan}
        total, distance = float(np.sum(lengths)), self.processing.t25fw_distance_m
        lowest = max(0.0, distance - (len(events) - len(lengths) + 2) * float(np.mean(lengths)))
        coherent = bool(lowest <= total <= distance)
        if not coherent:
            logger.warning(_("WARN_T25FW_DISTANCE: Evento {0} ({1}): las {2} zancadas integradas suman {3:.2f} m; para los {4:g} m del T25FW se espera entre {5:.2f} y {4:g} m. Es un aviso: la longitud de zancada no está validada con una referencia.").format(
                trial.trial_id, side, len(lengths), total, distance, lowest))
        return {"t25fw_suma_longitudes_m": round(total, 2), "t25fw_distancia_coherente": coherent}

    def _save_derived(self, trial: Trial, strides: list[dict[str, Any]], pools: list[dict[str, Any]], config_hash: str) -> None:
        """Guarda lo detectado en un ensayo para poder reconstruir de dónde sale cada cifra.

        En la carpeta de resultados (``paths.results_folder``), separada de los
        datos en bruto, se escribe un archivo por ensayo con una tabla de
        eventos por pie: contactos de talón, despegues, giros y tramos
        excluidos con su motivo, cada uno con su instante en el reloj de las
        señales. Lleva la versión del código, la huella de la configuración y
        la fecha de la ejecución. El Excel sigue siendo solo el resumen. Un
        fallo al guardar se avisa pero no detiene el análisis.

        Args:
            trial (Trial): Evento analizado.
            strides (list[dict[str, Any]]): Filas por zancada (hoja ``Zancadas``).
            pools (list[dict[str, Any]]): Por pie: inicio de la serie y eventos detectados.
            config_hash (str): Huella de la configuración de procesado.
        """
        tables: dict[str, pd.DataFrame] = {}
        for row in pools:
            start = row["inicio_serie"]
            events = [{"tipo": "contacto_talon", "inicio_s": t, "fin_s": t, "motivo": "", "angulo_grados": np.nan} for t in row["contactos"]]
            events += [{"tipo": "despegue", "inicio_s": t, "fin_s": t, "motivo": "", "angulo_grados": np.nan} for t in row["despegues"]]
            events += [{"tipo": "giro", "inicio_s": turn["inicio_s"], "fin_s": turn["fin_s"], "motivo": "",
                        "angulo_grados": turn["angulo_grados"]} for turn in row["giros"]]
            events += [{"tipo": "tramo_excluido", "inicio_s": item["inicio_s"], "fin_s": item["fin_s"], "motivo": item["motivo"],
                        "angulo_grados": np.nan} for item in row["tramos_excluidos"]]
            table = pd.DataFrame(events)
            if not table.empty:
                table = table.sort_values(["inicio_s", "tipo"], kind="stable").reset_index(drop=True)
                table["instante"] = start + pd.to_timedelta(table["inicio_s"], unit="s")
                tables[f"{row['Foot']}/eventos"] = table
            foot_strides = pd.DataFrame([
                {**{k: v for k, v in item.items() if k not in ("TrialID", "CodeID", "test_type", "Foot")},
                 "instante": start + pd.to_timedelta(item["inicio_s"], unit="s")}
                for item in strides if item["Foot"] == row["Foot"]
            ])
            if not foot_strides.empty:
                tables[f"{row['Foot']}/zancadas"] = foot_strides
        meta = {
            "id": trial.trial_id, "codeid": trial.codeid, "t_code": trial.test_type, "d_from": trial.d_from,
            "d_until": trial.d_until, "version_pipeline": PIPELINE_VERSION, "huella_config_processing": config_hash,
            "fecha_ejecucion": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            write_derived(self.config["paths"]["results_folder"], trial, tables, meta)
        except StorageError as error:
            logger.warning(_("WARN_DERIVED_NOT_SAVED: Evento {0}: no se guardaron los eventos detectados ({1})").format(trial.trial_id, error))

    def _min_strides(self, test_type: str) -> int | None:
        """Devuelve el mínimo de zancadas válidas que exige cada prueba.

        Args:
            test_type (str): Código del test.

        Returns:
            int | None: Mínimo de la prueba, o ``None`` si no tiene ninguno
            definido, en cuyo caso no se informan sus métricas de marcha.
        """
        return self.processing.min_strides.get(test_type)

    def _analyze_trial(
        self, trial: Trial, result: TrialResult, frames: dict[str, pd.DataFrame]
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        """Calcula las métricas de un evento usando solo sus propias señales.

        Args:
            trial (Trial): Evento del registro.
            result (TrialResult): Resultado de la adquisición del evento.
            frames (dict[str, pd.DataFrame]): Series normalizadas por pie.

        Returns:
            tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
            Filas por pie, fila de asimetría del evento, fila ancha del evento,
            filas por zancada y valores por zancada para juntar intentos.
        """
        tt = trial.test_type
        identity = {"TrialID": trial.trial_id, "CodeID": trial.codeid, "test_type": tt}
        wide: dict[str, Any] = {**identity, "d_from": trial.d_from, "d_until": trial.d_until, f"{tt}_duracion_seg": trial.duration_s}
        foot_rows: list[dict[str, Any]] = []
        cadences: dict[str, float] = {}
        strides: dict[str, tuple[float, float]] = {}
        swing_rows: list[dict[str, Any]] = []
        pool_rows: list[dict[str, Any]] = []
        day = str(experiment_wallclock(trial.d_from).date())

        for side in SIDES:
            df_side = frames.get(side)
            if df_side is None or df_side.empty:
                continue
            metrics, swings, pool = self._analyze_foot(trial, side, df_side)
            swing_rows.extend({**identity, "Foot": side, **swing} for swing in swings)
            pool_rows.append({**identity, "Foot": side, "dia": day, "inicio_serie": pd.to_datetime(df_side["_time"]).min(), **pool})
            cadences[side] = metrics["cadencia_zancada"]
            strides[side] = (metrics["zancadas"], metrics["n_tramos"])
            foot_rows.append({
                **identity, "Foot": side, "d_from": trial.d_from, "d_until": trial.d_until,
                "duracion_seg": trial.duration_s,
                "dispositivo": result.feet.get(side, {}).get("device", ""), **metrics,
            })
            for name in REPORT_METRICS:
                if name in metrics:
                    wide[f"{tt}_{side}_{name}"] = metrics[name]
            # Estado de confianza de cada grupo de métricas, como un campo más del registro.
            for state in dict.fromkeys(METRIC_STATE[name] for name in REPORT_METRICS if name in metrics):
                wide[f"{tt}_{side}_{state}"] = metrics.get(state, "")

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
        # Validación sin etiquetas: los dos pies deben contar casi las mismas zancadas
        # (como mucho una de diferencia por tramo continuo de marcha).
        n_left, runs_left = strides.get("Left", (np.nan, np.nan))
        n_right, runs_right = strides.get("Right", (np.nan, np.nan))
        difference = abs(n_left - n_right) if np.isfinite(n_left) and np.isfinite(n_right) else np.nan
        allowed = np.nanmax([runs_left, runs_right, 1.0]) if np.isfinite(difference) else np.nan
        consistent = bool(difference <= allowed) if np.isfinite(difference) else np.nan
        if consistent is False:
            logger.debug(_("DEBUG_FEET_MISMATCH: Evento {0}: {1} zancadas en el pie izquierdo y {2} en el derecho (diferencia admitida: {3:.0f})").format(trial.trial_id, n_left, n_right, allowed))
        asymmetry = {
            **identity, "pies_con_datos": ", ".join(sorted(cadences)),
            "bilateral_available": bool(cad_l > 0 and cad_r > 0),
            "cadencia_Left": cadences.get("Left", np.nan), "cadencia_Right": cadences.get("Right", np.nan),
            "symmetry_index": symmetry_index,
            "zancadas_Left": n_left, "zancadas_Right": n_right, "diferencia_zancadas": difference,
            "diferencia_admitida": allowed, "zancadas_coherentes": consistent,
        }
        return foot_rows, asymmetry, wide, swing_rows, pool_rows

    def _aggregate_patients(
        self, wide_rows: list[dict[str, Any]], asymmetry_rows: list[dict[str, Any]],
        pool_rows: list[dict[str, Any]] | None = None,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Agrega los eventos por paciente con la regla ``AGGREGATION_RULE``.

        Ningún ensayo se descarta: todos los eventos válidos del paciente entran
        en la media de cada métrica. La asimetría se promedia solo entre los
        ensayos con cadencia válida en ambos pies.

        En las pruebas de ``POOLED_TESTS`` (T25FW) un solo intento no alcanza el
        mínimo de zancadas: se juntan las zancadas de los intentos del mismo
        paciente y día, se calcula la métrica si alcanzan el mínimo y el valor
        del paciente es la media de sus días. El resultado es orientativo. La
        asimetría se calcula día a día, solo en los días en que los dos pies
        alcanzan el mínimo, y el valor del paciente es la media de esos índices
        diarios: nunca se compara la cadencia de un pie de un día con la del
        otro pie de otro día.

        Args:
            wide_rows (list[dict[str, Any]]): Una fila ancha por evento.
            asymmetry_rows (list[dict[str, Any]]): Una fila de asimetría por evento.
            pool_rows (list[dict[str, Any]] | None): Valores por zancada de cada
                evento y pie, con el día del ensayo.

        Returns:
            tuple[pd.DataFrame, pd.DataFrame]: Tabla con una fila por paciente
            (columnas con prefijo del test) y tabla de trazabilidad con una fila
            por paciente y test (ensayos usados, bilaterales y regla).
        """
        events = pd.DataFrame(wide_rows).drop(columns=["TrialID", "test_type", "d_from", "d_until"])
        states = [column for column in events.columns if any(column.endswith(f"_{state}") for state in set(METRIC_STATE.values()))]
        metrics = events.drop(columns=["CodeID", *states]).apply(lambda x: pd.to_numeric(x, errors="coerce"))
        patients = metrics.groupby(events["CodeID"], sort=False).mean()
        # Los WARNING entran en la media; se cuenta cuántos ensayos de cada prueba lo están.
        for test in sorted({row["test_type"] for row in wide_rows}):
            own = [column for column in states if column.startswith(f"{test}_")]
            if own:
                warned = events[own].eq("WARNING").any(axis=1).groupby(events["CodeID"], sort=False).sum()
                done = events[f"{test}_duracion_seg"].notna().groupby(events["CodeID"], sort=False).any()
                patients[f"{test}_ensayos_warning"] = warned.where(done).reindex(patients.index)
                # Y por pie y grupo de métricas, para poder distinguirlos en cada gráfico.
                for column in own:
                    count = events[column].eq("WARNING").groupby(events["CodeID"], sort=False).sum()
                    patients[f"{column}_n_warning"] = count.where(done).reindex(patients.index)
        pooled_counts: dict[tuple[str, str], int] = defaultdict(int)
        bilateral_days: dict[tuple[str, str], int] = defaultdict(int)
        for test in POOLED_TESTS:
            minimum = self._min_strides(test)
            if minimum is None:
                continue
            grouped: dict[tuple[str, str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
            for row in pool_rows or []:
                if row["test_type"] == test:
                    for name in ("tiempos", "balanceos", "longitudes", "alturas"):
                        grouped[(row["CodeID"], row["Foot"], row["dia"])][name].extend(row[name])
            per_day: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
            daily_cadence: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
            for (code, side, day), values in grouped.items():
                target = per_day[(code, side)]
                if len(values["tiempos"]) >= minimum:
                    daily_cadence[(code, day)][side] = 60.0 / float(np.mean(values["tiempos"]))
                    target["cadencia_zancada"].append(daily_cadence[(code, day)][side])
                    target["zancadas"].append(len(values["tiempos"]))
                    pooled_counts[(code, test)] += len(values["tiempos"])
                if len(values["balanceos"]) >= minimum:
                    target["balanceo_prc"].append(float(np.mean(values["balanceos"])))
                    target["apoyo_prc"].append(100.0 - float(np.mean(values["balanceos"])))
                if len(values["longitudes"]) >= minimum:
                    target["stride_length_cm"].append(float(np.mean(values["longitudes"])))
                if len(values["alturas"]) >= minimum:
                    target["excursion_tobillo_cm"].append(float(np.mean(values["alturas"])))
            # Asimetría día a día: solo los días con cadencia en los dos pies.
            daily_symmetry: dict[str, list[float]] = defaultdict(list)
            for (code, _day), sides in daily_cadence.items():
                left, right = sides.get("Left", 0.0), sides.get("Right", 0.0)
                if left > 0 and right > 0:
                    daily_symmetry[code].append(abs(left - right) / (0.5 * (left + right)) * 100)
                    bilateral_days[(code, test)] += 1
            for code in patients.index:
                for side in SIDES:
                    for name in ("zancadas", "cadencia_zancada", "apoyo_prc", "balanceo_prc", "stride_length_cm", "excursion_tobillo_cm"):
                        column = f"{test}_{side}_{name}"
                        if column in patients.columns and pd.notna(patients.at[code, f"{test}_duracion_seg"]):
                            days = per_day.get((code, side), {}).get(name, [])
                            patients.at[code, column] = float(np.mean(days)) if days else np.nan
                column = f"{test}_{SYMMETRY_METRIC}"
                if column in patients.columns and pd.notna(patients.at[code, f"{test}_duracion_seg"]):
                    values = daily_symmetry.get(code, [])
                    patients.at[code, column] = float(np.mean(values)) if values else np.nan
        patients = patients.round(2).reset_index()

        trace = pd.DataFrame(asymmetry_rows).groupby(["CodeID", "test_type"], sort=False).agg(
            n_ensayos=("TrialID", "size"),
            n_bilaterales=("symmetry_index", "count"),
            eventos=("TrialID", lambda ids: ", ".join(str(i) for i in ids)),
            symmetry_index_media=("symmetry_index", "mean"),
        ).reset_index()
        trace["symmetry_index_media"] = trace["symmetry_index_media"].round(2)
        trace["regla"] = AGGREGATION_RULE
        pooled = trace["test_type"].isin(POOLED_TESTS)
        trace.loc[pooled, "regla"] = AGGREGATION_RULE + "; " + POOLED_NOTE
        trace["zancadas_juntadas"] = [
            pooled_counts.get((code, test), 0) if test in POOLED_TESTS else np.nan
            for code, test in zip(trace["CodeID"], trace["test_type"])
        ]
        # En las pruebas juntadas, la asimetría del paciente sale de estos días, no de los intentos sueltos.
        trace["dias_bilaterales"] = [
            bilateral_days.get((code, test), 0) if test in POOLED_TESTS else np.nan
            for code, test in zip(trace["CodeID"], trace["test_type"])
        ]

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
        workers: int = 1,
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
        ``Zancadas`` (una fila por zancada integrada, con su longitud y alturas
        o su causa de exclusión), ``Estado_Eventos`` (resultado de cada fila del registro),
        ``Datos_Ensayos`` (una fila por evento con su fecha), ``Calidad_Series``
        (resultado de cada filtro por serie),
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
            workers (int): Consultas a InfluxDB simultáneas. La escritura y el
                análisis siguen en un único hilo, en el orden del registro.

        Returns:
            list[TrialResult]: Un resultado por fila seleccionada del registro.

        Raises:
            FileNotFoundError: Si el archivo de registro CSV no existe.
            RegistryError: Si al registro le faltan columnas requeridas.
            StorageError: Si el almacén está bloqueado, tiene el esquema antiguo
                o el Excel no puede publicarse.
            NoDataError: Si ningún evento tiene señales. El Excel anterior, si
                existe, se aparta (:func:`set_aside`) para que no parezca el
                resultado de esta ejecución.
        """
        config = self.config
        csv_path = csv_path or config["paths"]["registry_csv"]
        trials, results = load_registry(csv_path, test_type, ids, config.get("registry", {}).get("exclude_codeids"))
        provenance_hash = hashlib.sha256(json.dumps(self.processing.to_record(), sort_keys=True).encode()).hexdigest()[:16]

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
        pool_rows: list[dict[str, Any]] = []
        workers = max(1, int(workers))
        retry_waits = () if self.client is None else config["influxdb"].get("retry_waits_s", ())
        executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 and self.client is not None else None
        if executor is not None:
            logger.info(_("INFO_PARALLEL_QUERIES: {0} consultas simultáneas a InfluxDB; escritura y análisis en un único hilo").format(workers))

        started = time.perf_counter()
        with SignalStore(self.h5_path) as store:
            run_id = start_run(manifest, "pipeline", len(trials))
            position = 0
            for chunk_start in range(0, len(trials), workers):
                chunk = trials[chunk_start:chunk_start + workers]
                # Solo las consultas van en paralelo, y de trozo en trozo para no acumular señales en memoria.
                futures = {
                    trial.trial_id: executor.submit(
                        fetch_with_retries, self.client, self.bucket, self.measurement, trial, retry_waits
                    )
                    for trial in chunk if executor is not None and trial.trial_id not in (completed or {})
                }
                for trial in chunk:
                    position += 1
                    logger.info(_("INFO_INGEST_TRIAL: [{cid}] - Ingestando evento {tid} ({tt}) desde InfluxDB...").format(
                        cid=trial.codeid, tid=trial.trial_id, tt=trial.test_type
                    ) + _(" [{0}/{1}]").format(position, len(trials)))

                    # --- BLOQUE 1: INGESTA Y PERSISTENCIA (contrato compartido con el extractor) ---
                    result, frames = acquire_trial(
                        self.client, self.bucket, self.measurement, store, manifest, trial, run_id,
                        completed, load_stored=True, retry_waits=retry_waits, prefetched=futures.get(trial.trial_id),
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
                    rows, asymmetry, wide, swings, pools = self._analyze_trial(trial, result, frames)
                    foot_rows.extend(rows)
                    swing_rows.extend(swings)
                    pool_rows.extend(pools)
                    self._save_derived(trial, swings, pools, provenance_hash)
                    asymmetry_rows.append(asymmetry)
                    wide_rows.append(wide)
            if executor is not None:
                executor.shutdown(wait=True)

        output = config["paths"]["output_dataset"]
        if not wide_rows:
            # Un Excel anterior parecería el resultado de esta ejecución: se aparta.
            previous = set_aside(output)
            detail = _("ERR_NO_DATA_PROCESSED: No se generaron datos para ningún ensayo clínico.")
            if previous is not None:
                detail += " " + _("El Excel anterior se ha apartado como {0}.").format(previous.name)
            end_run(manifest, run_id, results, "Pipeline", time.perf_counter() - started,
                    excel={"estado": "no_publicado", "detalle": detail})
            raise NoDataError(detail)

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
        df_patients, df_trace = self._aggregate_patients(wide_rows, asymmetry_rows, pool_rows)
        df_final = df_patients.astype(object).fillna("N/A")
        tests = sorted(df_events["test_type"].unique())
        quality_columns = [
            "TrialID", "CodeID", "test_type", "Foot", "criterio_presion", "rango_talon", "periodicidad_talon", "ventanas_ritmo",
            "ventanas_con_ritmo_prc",
            "cobertura_observada_presion", "cobertura_presion", "huecos_presion", "muestras_interpoladas", "muestras_retenidas",
            "cobertura_observada_imu", "cobertura_imu", "muestras_repetidas_reposo", "imu_estado", "presion_activa_seg", "imu_repeticion_presion_activa_prc",
            "giros_excluidos", "zancadas_margen_giro", "n_huecos",
            "intervalos_recuperados", "min_zancadas", "contactos_talon", "zancadas", "zancadas_en_banda_prc",
            "estado_cadencia", "motivo_cadencia", "estado_zancada", "motivo_zancada",
            "t25fw_suma_longitudes_m", "t25fw_distancia_coherente",
        ]
        df_feet = pd.DataFrame(foot_rows)
        df_quality = df_feet[[column for column in quality_columns if column in df_feet.columns]].copy()
        if not df_quality.empty:
            # Resumen en nivel 1: cuánto se pierde y por qué.
            lost = df_quality.loc[df_quality["estado_cadencia"] != "OK", "motivo_cadencia"].astype(str).str.split(":").str[0]
            logger.info(_("INFO_QUALITY_SUMMARY: {0} de {1} series con cadencia; sin cadencia por motivo: {2}").format(
                int((df_quality["estado_cadencia"] == "OK").sum()), len(df_quality), dict(lost.value_counts())))
        sheets = {
            "Leeme": pd.DataFrame(
                [{"hoja": "(informe)", "contenido": REPORT_SCOPE, "una_fila_por": "-"}]
                + [{"hoja": name, "contenido": content, "una_fila_por": unit} for name, content, unit in SHEET_GUIDE]
            ),
            "Datos_Ensayos": df_events,
            "Datos_Pacientes": df_final,
            "Calidad_Series": df_quality,
            "Agregacion_Pacientes": df_trace,
            "Ensayos": pd.DataFrame(foot_rows),
            "Asimetria": pd.DataFrame(asymmetry_rows),
            "Zancadas": pd.DataFrame(swing_rows),
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
            logger.info(
                _("INFO_FS_RATES: {0} de {1} series tienen una frecuencia efectiva (~{2} Hz) distinta de la nominal processing.fs={3:g} Hz. Es esperable, porque depende del sensor; las métricas usan la frecuencia medida de cada serie.").format(len(deviated), len(foot_rows), rates, self.processing.fs)
            )
        if not (sheets["Estadisticas_Globales"].loc["n_validos"] > 0).any():
            logger.warning(_("WARN_NO_NUMERIC_STATS: No hay suficientes datos numéricos para Estadísticas Globales."))

        # La ejecución se cierra en el manifest después de publicar, con el resultado de la publicación.
        try:
            publish_excel(sheets, output, index_sheets=("Estadisticas_Ensayos", "Estadisticas_Globales"))
        except StorageError as error:
            end_run(manifest, run_id, results, "Pipeline", time.perf_counter() - started,
                    excel={"estado": "error", "detalle": str(error)})
            raise
        end_run(manifest, run_id, results, "Pipeline", time.perf_counter() - started,
                excel={"estado": "publicado", "ruta": str(output)})
        logger.info(_("INFO_PIPELINE_COMPLETED: Pipeline finalizado. Excel generado ({out}).").format(out=output))
        return results


def main(argv: list[str] | None = None) -> None:
    """Punto de entrada de la orden ``msfeat-pipeline`` (y de ``pipeline_test.py``).

    Args:
        argv (list[str] | None): Argumentos; por defecto los de la línea de órdenes.
    """
    import argparse
    from msConfig import ConfigError
    parser = argparse.ArgumentParser(description="Pipeline único InfluxDB -> HDF5 -> Excel")
    parser.add_argument('--config', default=None, help='YAML; por defecto config/config.yaml')
    parser.add_argument('--csv', default=None, help='Sustituye paths.registry_csv')
    parser.add_argument('--ids', default=None, help='Identificadores de evento separados por comas')
    parser.add_argument('--test-type', default=None, help='Código de test a procesar; por defecto todos')
    parser.add_argument('--missing-only', action='store_true', help='Reutilizar del HDF5 los eventos ya completados')
    parser.add_argument('--stored-only', action='store_true', help='Recalcular solo lo ya guardado, sin consultar InfluxDB')
    parser.add_argument('--workers', type=int, default=1, help='Consultas simultáneas a InfluxDB (por defecto 1)')
    parser.add_argument('--audit', action='store_true', help='Comparar el almacén con el registro, sin consultar InfluxDB')
    parser.add_argument('--check-config', action='store_true', help='Validar YAML/.env sin acceder a bases de datos')
    parser.add_argument('--verbose', type=int, choices=(0, 1, 2), default=1, help='0 avisos, 1 información, 2 depuración')
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        if args.check_config:
            # Valida también todos los parámetros del análisis, no solo el YAML y las credenciales.
            ProcessingConfig.from_mapping(load_config(args.config).get("processing"))
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
            pipeline.run_from_csv(args.csv, ids, args.missing_only, args.test_type, args.stored_only, args.workers)
    except (FileNotFoundError, RegistryError, StorageError, NoDataError) as error:
        sys.exit(f"ERROR: {error}")
    finally:
        if pipeline.client is not None:
            pipeline.client.close()


if __name__ == "__main__":
    main()
