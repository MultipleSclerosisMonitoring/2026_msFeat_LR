import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), "pkgs")) # Parche de rutas



import pandas as pd
import numpy as np
import logging
import yaml
import urllib3
from urllib.parse import urlparse, urlunparse
from influxdb_client import InfluxDBClient
from GaitMetricsCalculator import GaitMetricsCalculator
from msTools import i18n
from msTools.timeutils import influx_wallclock_z

# Configuración del Logger
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("msGait.Pipeline")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


INFLUX_IP_HOST = "138.100.82.178"
INFLUX_CERT_HOST = "apiivm78.etsii.upm.es"


def normalize_influx_url(url: str) -> str:
    """Use the DNS name covered by the TLS certificate when the old IP is configured."""
    parsed = urlparse(url)
    if parsed.hostname != INFLUX_IP_HOST:
        return url

    netloc = INFLUX_CERT_HOST
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def madrid_wallclock_as_influx_z(value: str) -> str:
    """Influx stores Madrid wall-clock timestamps as if they were UTC."""
    return influx_wallclock_z(value)


class GaitPipeline:
    def __init__(self, config_path: str = "InfluxDBms/.config_db.yaml"):
        """
        Inicializa la conexión directa SOLO a InfluxDB, evitando el error de PostgreSQL.
        """
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"No se encuentra el config: {config_path}")
            
        # 1. Cargar Configuración Manualmente
        with open(config_path, "r", encoding="utf-8") as file:
            config = yaml.safe_load(file)
            
        inf_cfg = config["influxdb"]
        influx_url = normalize_influx_url(inf_cfg["url"])
        
        # 2. Conectar Cliente InfluxDB nativo
        self.client = InfluxDBClient(
            url=influx_url,
            token=inf_cfg["token"],
            org=inf_cfg["org"],
            timeout=inf_cfg.get("timeout") or 30000,
            verify_ssl=False,
        )
        self.bucket = inf_cfg["bucket"]
        
        # Herramientas
        self.calculator = GaitMetricsCalculator()
        i18n.init_translation("es")

    def fetch_data_flux(self, cid: str, start_time: str, end_time: str) -> pd.DataFrame:
        """
        CAPA 1: Ingesta. Extrae las señales crudas directamente desde InfluxDB.
        Realiza un pivot para transformar las series temporales en columnas estructuradas.
        """
        # Consulta Flux adaptada a la estructura de alta frecuencia
        query = f'''
        from(bucket: "{self.bucket}")
            |> range(start: {start_time}, stop: {end_time})
            |> filter(fn: (r) => r["CodeID"] == "{cid}")
            |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
        '''

        
        try:
            df = self.client.query_api().query_data_frame(query)
            logger.info(f"[{cid}] - ...")
            # query_data_frame puede devolver una lista de DataFrames si hay varios sensores/etiquetas
            if isinstance(df, list):
                if len(df) > 0:
                    df = pd.concat(df, ignore_index=True)
                else:
                    return pd.DataFrame()
                    
            if not df.empty and '_time' in df.columns:
                # Ordenar por tiempo (Crítico para que el cálculo de cadencia no se raye)
                df = df.sort_values(by='_time').reset_index(drop=True)
                
            return df
        except Exception as e:
            logger.error(f"[{cid}] - Error en consulta Flux: {e}")
            return pd.DataFrame()

    def run_from_csv(self, csv_path: str = "tests.csv"):
        tests_df = pd.read_csv(csv_path)
        pacientes_dict = {}

        for index, row in tests_df.iterrows():
            cid = row['codeid']
            test_type = row['t_code'] 
            
            if cid not in pacientes_dict:
                pacientes_dict[cid] = {"CodeID": cid}
            
            # Influx contiene hora civil de Madrid serializada como UTC.
            start_time = madrid_wallclock_as_influx_z(row['d_from'])
            end_time = madrid_wallclock_as_influx_z(row['d_until'])
            
            logger.info(f"[{cid}] - Ingestando {test_type} desde InfluxDB...")
            
            # --- BLOQUE 1: INGESTA FLUX ---
            raw_df = self.fetch_data_flux(cid, start_time, end_time)
            
            if raw_df.empty:
                logger.error(f"[{cid}] - {test_type} - ERR_TRIAL_EMPTY (Sin datos en InfluxDB)")
                continue

            # Duración del test
            duracion_total = (pd.to_datetime(end_time) - pd.to_datetime(start_time)).total_seconds()
            pacientes_dict[cid][f"{test_type}_duracion_seg"] = round(duracion_total, 2)

            # --- BLOQUE 2 y 3: ANÁLISIS ---
            # Las etiquetas en InfluxDB suelen venir como columnas tras el 'pivot'. 
            # Aseguramos que existe la columna 'foot' o 'Foot'
            col_foot = 'foot' if 'foot' in raw_df.columns else 'Foot' if 'Foot' in raw_df.columns else None

            if not col_foot:
                logger.warning(f"[{cid}] - No se encontró la etiqueta de pierna (Foot) en los datos.")
                continue

            for side in ["Left", "Right"]:
                df_side = raw_df[raw_df[col_foot] == side].copy()
                
                if df_side.empty:
                    continue

                # Control de giros (Excluimos en 6MWT y T25FW, incluimos en TUG)
                debe_excluir_giros = False if test_type == "TUG" else True

                # Llamada al motor de cálculo
                cad_res = self.calculator.calcular_cadencia(df_side, excluir_giros=debe_excluir_giros)
                fas_res = self.calculator.calcular_fases(df_side)

                prefix = f"{test_type}_{side}_"
                pacientes_dict[cid].update({
                    f"{prefix}pasos": cad_res["num_pasos"],
                    f"{prefix}cadencia": cad_res["cadencia"],
                    f"{prefix}apoyo_prc": fas_res.get("apoyo_prc", 0),
                    f"{prefix}balanceo_prc": fas_res.get("balanceo_prc", 0)
                })

                if test_type == "6MWT":
                    decay_res = self.calculator.calcular_decaimiento_6mwt(df_side)
                    pacientes_dict[cid].update({
                        f"{prefix}decaimiento_prc": decay_res.get("6mwt_decaimiento", 0)
                    })

    # --- EXPORTACIÓN BLINDADA ---
        if not pacientes_dict:
            logger.error("No se generaron datos para ningún paciente. Revisa la conexión a InfluxDB.")
            return

        dataset_aplanado = list(pacientes_dict.values())
        df_final = pd.DataFrame(dataset_aplanado).fillna("N/A")

        with pd.ExcelWriter("DatasetMaestro.xlsx") as writer:
            # 1. Guardar la hoja principal
            df_final.to_excel(writer, sheet_name="Datos_Pacientes", index=False)
            
            # 2. Calcular estadísticas de forma segura
            # Convertimos a numérico: lo que sea texto puro (como CodeID) se convertirá en NaN y será ignorado
            df_numeric = df_final.apply(lambda x: pd.to_numeric(x, errors='coerce'))
            
            # Eliminamos las columnas que sean 100% texto/NaN
            df_numeric = df_numeric.dropna(axis=1, how='all')
            
            # Solo generamos estadísticas si realmente hay datos numéricos que calcular
            if not df_numeric.empty:
                stats = df_numeric.describe().loc[['mean', '50%', 'std']]
                stats.to_excel(writer, sheet_name="Estadisticas_Globales")
            else:
                logger.warning("No hay suficientes datos numéricos para calcular las Estadísticas Globales.")
            
        logger.info("Pipeline Finalizado: Excel generado (DatasetMaestro.xlsx).")

if __name__ == "__main__":
    # ¡Asegúrate de que esta ruta apunta al archivo YAML que SÍ tiene tu URL real de InfluxDB!
    pipeline = GaitPipeline("InfluxDBms/.config_db.yaml") 
    pipeline.run_from_csv("tests.csv")
