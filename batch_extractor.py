#Este archivo hace de batch extractor para extraer datos de multiples archivos de influxDB
#Automatiza la extraccion de series temporales de alta frecuencia desde influxDB
import pandas as pd
import subprocess

#1. Cargar el archivo de marcas de tiempo
test_df = pd.read_csv('tests.csv')

#2. Filtrar solo los tests de 6 minutos (6MWT) de los pacientes
tests_6mwt = test_df[test_df['t_code'] == '6MWT']

for index, row in tests_6mwt.iterrows():
    patient_id = row['codeid']
    start_time = row['d_from'].replace(" ", "T").split(".")[0] + "Z"
    end_time = row['d_until'].replace(" ", "T").split(".")[0] + "Z"

    #Crear nombre archivo 
    output_name = f"test_InfluxDB/out/EM_{patient_id}_6MWT_{index}.xlsx"

    print(f"--- Procesando {patient_id} (6MWT) ---")

    # 3. Construir el comando para llamar a extract_data.py
    comando = [
        "python", "-m", "test_InfluxDB.extract_data",
        "-f", start_time,
        "-u", end_time,
        "-q", patient_id,
        "-l", "Left",
        "-p", "InfluxDBms/.config_db.yaml",
        "-o", output_name,
        "-m", "Ax,Ay,Az,Gx,Gy,Gz,S0,S1,S2,lat,lng" # Todas las métricas necesarias
    ]
    
    # 4. Ejecutar el comando automáticamente
    subprocess.run(comando)

print("¡Procesamiento en lote finalizado!")