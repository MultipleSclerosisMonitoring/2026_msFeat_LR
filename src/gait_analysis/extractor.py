import pandas as pd
import yaml
import urllib3
import os
from typing import Dict, Any
from pathlib import Path
from pydantic import BaseModel, Field
from influxdb_client import InfluxDBClient

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

class GaitDataExtractor:
    """Clase para la extracción masiva y tipada de datos biomecánicos.

    Se encarga de gestionar la conexión con InfluxDB, validar la configuración
    y de exportar los datos a formato jerárquico HDF5 para optimizar el almacenamiento masivo.

    Attributes:
        __out_dir (str): Ruta absoluta al directorio de salida.
        __client (InfluxDBClient): Cliente de conexión a la base de datos.
        __bucket (str): Nombre del bucket origen de los datos.
    """
    def __init__(self, config_file: str = "config_db.yaml", output_folder: str = 'data/raw') -> None:
        """Inicializa el extractor con validación de rutas y configuración.

        Args:
            config_file (str): Nombre del archivo YAML de configuración.
            output_folder (str): Carpeta relativa donde se guardarán los resultados.

        Raises:
            FileNotFoundError: Si no se encuentra el archivo de configuración.
            ValidationError: Si el archivo YAML no cumple con el esquema InfluxConfig.
        """
        base_path = os.path.dirname(os.path.abspath(__file__))
        
        # Atributo privado: Directorio de salida configurable
        self._out_dir = os.path.join(base_path, '..', '..', output_folder)
        os.makedirs(self._out_dir, exist_ok=True)

        # DEFINIR LA RUTA DEL ARCHIVO HDF5
        # Este será el único archivo que contendrá TODOS los datos organizados
        self._h5_database: str = os.path.join(self._out_dir, "gait_study_data.h5")


        # Carga de configuración privada
        config_path = os.path.join(base_path, '..','..', "InfluxDBms", config_file)
        raw_config = self._load_config(config_path)

        validated_config = InfluxConfig(**raw_config['influxdb'])
        # Cliente InfluxDB encapsulado (Atributo privado)
        self._client = InfluxDBClient(
            url=validated_config.url,
            token=validated_config.token,
            org=validated_config.org,
            timeout=30000,
            verify_ssl=False
        )
        self._bucket = validated_config.bucket

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Carga y parsea el archivo de configuración YAML.

        Args:
            config_path (str): Ruta absoluta al archivo YAML.

        Returns:
            Dict[str, Any]: Contenido del archivo YAML como diccionario.

        Raises:
            FileNotFoundError: Si la ruta especificada no existe.
        """
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuración no encontrada en: {config_path}")
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)

    def run_batch_extraction(self, csv_path: str = 'tests.csv', test_type: str = '6MWT') -> None:
        """Ejecuta el proceso de extracción para todos los pacientes que coincidan con el test.

        Args:
            csv_path (str): Ruta al CSV que contiene el registro de pacientes.
            test_type (str): Código del test (ej. '6MWT') para filtrar la extracción.

        Returns:
            None
        """
        base_path = os.path.dirname(os.path.abspath(__file__))
        full_csv_path = os.path.join(base_path, '..', '..', csv_path)
        
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

        Este método es privado y gestiona la lógica de la consulta Flux y el 
        formateo final de los datos para análisis posterior.

        Args:
            row (pd.Series): Fila con 'codeid', 'd_from' y 'd_until'.
            idx (int): Índice secuencial para el nombre del archivo.
            test_type (str): Tipo de prueba realizada.

        Returns:
            None
        """
        p_id: str = str(row['codeid'])
        
        # Conversión de fechas a formato ISO 8601 para InfluxDB
        start_iso = pd.to_datetime(row['d_from']).tz_convert('UTC').strftime('%Y-%m-%dT%H:%M:%SZ')
        stop_iso = pd.to_datetime(row['d_until']).tz_convert('UTC').strftime('%Y-%m-%dT%H:%M:%SZ')
        
        query = f'''
        from(bucket: "{self._bucket}")
          |> range(start: {start_iso}, stop: {stop_iso})
          |> filter(fn: (r) => r["_measurement"] == "Gait")
          |> filter(fn: (r) => r["CodeID"] == "{p_id}")
          |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
          |> drop(columns: ["_start", "_stop", "_measurement", "result", "table", "CodeID", 
                            "DeviceName", "Foot", "app", "lat", "lng", "mac", "type"])
        '''
        
        try:
            df = self._client.query_api().query_data_frame(query)
            
            if isinstance(df, pd.DataFrame) and not df.empty:
                df["_time"] = df["_time"].dt.tz_localize(None)
                
                # Crear la ruta gerárquica 
                hdf_key: str = f"p_{p_id}/{test_type}/trial_{idx}"
                
                # Guardar en el almacén HDF5
                # 'append=False' para sobreescribir si el intento es el mismo
                df.to_hdf(self._h5_database, key=hdf_key, mode='a', format='table')

                print(f"  [+] {p_id} guardado en HDF5: {hdf_key}")
            else:
                print(f"  [-] {p_id}: Sin datos en el rango {start_iso}")
                
        except Exception as e:
            print(f"  [!] Error consultando {p_id}: {e}")

    def close(self):
        """Cierra de forma segura la conexión con el cliente de InfluxDB."""
        self._client.close()