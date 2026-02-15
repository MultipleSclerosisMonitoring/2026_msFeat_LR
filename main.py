import os
import sys
# Truco para que Python encuentre tus carpetas 'src' sin problemas
sys.path.append(os.path.abspath("src"))

from gait_analysis.extractor import GaitDataExtractor
import pandas as pd

# ---------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------
ARCHIVO_CSV = 'tests.csv'   # El nombre de tu archivo
TIPO_TEST = '6MWT'          # Lo que pusiste en la columna t_code
CARPETA_SALIDA = 'data/raw' # Donde guardará el HDF5

print("🚀 Iniciando el Motor de Extracción...")

# 1. Instanciamos el Extractor Profesional
try:
    extractor = GaitDataExtractor(config_file="config_db.yaml", output_folder=CARPETA_SALIDA)
    
    # 2. Ejecutamos la extracción masiva leyendo tu CSV
    extractor.run_batch_extraction(csv_path=ARCHIVO_CSV, test_type=TIPO_TEST)
    
    # 3. Cerramos conexión
    extractor.close()
    print("\n✅ Proceso finalizado. Datos guardados en formato HDF5.")

except Exception as e:
    print(f"\n❌ ERROR CRÍTICO: {e}")
    print("Consejo: Revisa que 'config_db.yaml' esté en la carpeta InfluxDBms y tu CSV en la raíz.")

# ---------------------------------------------------------
# VERIFICACIÓN RÁPIDA (Leemos lo que acabamos de guardar)
# ---------------------------------------------------------
archivo_h5 = os.path.join(CARPETA_SALIDA, "gait_study_data.h5")

if os.path.exists(archivo_h5):
    print(f"\n📂 Abriendo archivo de datos: {archivo_h5}")
    try:
        with pd.HDFStore(archivo_h5, mode='r') as store:
            claves = store.keys()
            print(f"   Pacientes encontrados en el archivo: {len(claves)}")
            for k in claves:
                print(f"   - {k}")
                
            if len(claves) > 0:
                df = store.get(claves[0])
                print(f"\n📊 Primer paciente cargado con {len(df)} filas.")
                print(df.head())
    except Exception as e:
        print(f"❌ Error leyendo el HDF5: {e}")
else:
    print("⚠️ No se generó el archivo .h5 (revisa si el CSV tenía las fechas correctas).")