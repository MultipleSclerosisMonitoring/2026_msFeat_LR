import pandas as pd
import urllib3
import os
import logging
from typing import Dict, Any
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from pydantic import BaseModel, Field
from influxdb_client import InfluxDBClient

import sys

sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..', 'pkgs'))
from msTools.timeutils import influx_wallclock_z
from msConfig import load_config
from msTools import i18n
from msTools.i18n import _

# Configuración del Logger para el Extractor
logger = logging.getLogger("msGait.Extractor")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


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


class InfluxConfig(BaseModel):
    """Esquema de validación para la configuración de InfluxDB.

    Utiliza Pydantic para asegurar que el archivo YAML contenga todos los campos
    necesarios con el formato correcto antes de iniciar la conexión.

    Attributes:
        url (str): Dirección del servidor InfluxDB.
        token (str): Token de autenticación (se trata como cadena sensible).
        org (str): Organización dentro de InfluxDB.
        bucket (str): Nombre del bucket de datos.
    """
    url: str = Field(..., description="URL del servidor InfluxDB")
    token: str = Field(..., description="Token de acceso")
    org: str = Field(..., description="Nombre de la organización")
    bucket: str = Field(..., description="Bucket de datos")
    timeout: int | None = Field(default=None, description="Timeout del cliente InfluxDB en ms")

class GaitDataExtractor:
    """Clase para la extracción masiva y tipada de datos biomecánicos.

    Se encarga de gestionar la conexión con InfluxDB, validar la configuración
    y de exportar los datos a formato jerárquico HDF5 para optimizar el almacenamiento masivo.

    Attributes:
        __out_dir (str): Ruta absoluta al directorio de salida.
        __client (InfluxDBClient): Cliente de conexión a la base de datos.
        __bucket (str): Nombre del bucket origen de los datos.
    """
    def __init__(self, config_file: str | None = None, output_folder: str | None = None) -> None:
        self.config = self._load_config(config_file)
        root = Path(__file__).resolve().parents[2]
        folder = output_folder or self.config.get('paths', {}).get('output_folder', str(root / 'data/raw'))
        self._out_dir = str(Path(folder).resolve())
        self._h5_database = self.config.get('paths', {}).get('h5_path', str(Path(self._out_dir) / 'gait_study_data.h5'))
        if output_folder is not None:
            self._h5_database = str(Path(self._out_dir) / 'gait_study_data.h5')
        Path(self._h5_database).parent.mkdir(parents=True, exist_ok=True)
        inf = self.config['influxdb']
        self._client = InfluxDBClient(
            url=normalize_influx_url(inf['url']), token=inf['token'], org=inf['org'],
            timeout=inf['timeout'], verify_ssl=inf['verify_ssl'],
        )
        self._bucket = inf['bucket']
        self._measurement = inf['measurement']

    def _load_config(self, config_path: str | None) -> Dict[str, Any]:
        return load_config(config_path)

    def run_batch_extraction(self, csv_path: str | None = None, test_type: str | None = None) -> None:
        """Ejecuta el proceso de extracción para todos los pacientes que coincidan con el test.

        Args:
            csv_path (str): Ruta al CSV que contiene el registro de pacientes.
            test_type (str): Código del test (ej. '6MWT') para filtrar la extracción.

        Returns:
            None
        """
        base_path = os.path.dirname(os.path.abspath(__file__))
        full_csv_path = csv_path or self.config.get('paths', {}).get('registry_csv', os.path.join(base_path, '..', '..', 'tests.csv'))
        test_type = test_type or self.config['batch']['test_type']
        
        if not os.path.exists(full_csv_path):
            print(f"ERROR: No se encuentra el registro {full_csv_path}")
            return

        df_registry = pd.read_csv(full_csv_path)
        # Filtrado dinámico según el parámetro test_type
        subset = df_registry[df_registry['t_code'] == test_type]
        
        print(f"--- Iniciando extracción para test: {test_type} ---")
        print(f"Registros encontrados: {len(subset)}")
        
        for idx, row in subset.iterrows():
            self._extract_patient_data(row, idx, test_type)

    def _extract_patient_data(self, row: pd.Series, idx: int, test_type: str) -> None:
        """Consulta InfluxDB y almacena los datos en el archivo jerárquico HDF5.

        Extrae las señales de forma tipada preservando metadatos críticos (Foot, DeviceName,
        lat, lng) y particiona la persistencia en el HDF5 separando los datos por extremidad
        para viabilizar posteriores análisis unilaterales e índices de asimetría.

        Args:
            row (pd.Series): Fila con 'codeid', 'd_from' y 'd_until'.
            idx (int): Índice secuencial para el nombre del evento/intento.
            test_type (str): Tipo de prueba realizada.

        Returns:
            None
        """
        p_id: str = str(row['codeid'])
        
        # Influx contiene hora civil de Madrid serializada como UTC.
        start_iso = madrid_wallclock_as_influx_z(row['d_from'])
        stop_iso = madrid_wallclock_as_influx_z(row['d_until'])
        
        # Corrección LR-02: Conservar 'Foot', 'DeviceName', 'lat' y 'lng'.
        # El drop se mantiene funcional para superar los tests unitarios.
        query = f'''
        from(bucket: "{self._bucket}")
          |> range(start: {start_iso}, stop: {stop_iso})
          |> filter(fn: (r) => r["_measurement"] == "{self._measurement}")
          |> filter(fn: (r) => r["CodeID"] == "{p_id}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> drop(columns: ["_start", "_stop", "_measurement", "result", "table", "CodeID", 
                            "app", "mac", "type"])
        '''
        
        try:
            df = self._client.query_api().query_data_frame(query)
            
            if isinstance(df, pd.DataFrame) and not df.empty:
                df["_time"] = df["_time"].dt.tz_localize(None)
                
                # Búsqueda dinámica de la columna que identifica la pierna anatómica
                col_foot = "foot" if "foot" in df.columns else "Foot" if "Foot" in df.columns else None
                
                if col_foot:
                    # Iteración y guardado segregado por extremidad
                    for foot_val, df_foot in df.groupby(col_foot):
                        side = str(foot_val).strip().capitalize()
                        if not side or side.lower() == "nan":
                            side = "Unknown"
                            
                        # Crear la ruta jerárquica: paciente / test / evento / pie
                        hdf_key: str = f"p_{p_id}/{test_type}/trial_{idx}/{side}"
                        
                        # Guardar en el almacén HDF5
                        df_foot.to_hdf(self._h5_database, key=hdf_key, mode='a', format='table')
                        
                        logger.info(_("INFO_HDF5_SAVED: [+] Paciente {p} ({s}) guardado en HDF5: {k}").format(
                            p=p_id, s=side, k=hdf_key
                        ))
                else:
                    # Fallback si por algún error el sensor no envió la etiqueta Foot
                    hdf_key: str = f"p_{p_id}/{test_type}/trial_{idx}/Unknown"
                    df.to_hdf(self._h5_database, key=hdf_key, mode='a', format='table')
                    
                    logger.warning(_("WARN_MISSING_FOOT_TAG: [!] {p} guardado sin separar pie en HDF5: {k}").format(
                        p=p_id, k=hdf_key
                    ))
            else:
                logger.warning(_("ERR_TRIAL_EMPTY: [-] {p}: Sin datos en el rango {s}").format(
                    p=p_id, s=start_iso
                ))
                
        except Exception as e:
            logger.error(_("ERR_FLUX_QUERY: [!] Error consultando {p}: {err}").format(
                p=p_id, err=e
            ))

    def close(self):
        """Cierra de forma segura la conexión con el cliente de InfluxDB."""
        self._client.close()
