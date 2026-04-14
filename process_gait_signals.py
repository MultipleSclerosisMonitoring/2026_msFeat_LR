#Graficar datos de los sensores antes de aplicar los filrtos matemáticos 
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from scipy.signal import butter, filtfilt, find_peaks 

#Funcion del Filtro Butterworth
def butter_lowpass_filter(data, cutoff, fs, order = 4):
    nyq = 0.5 * fs
    normal_cutoff = cutoff / nyq
    b, a = butter(order, normal_cutoff, btype = 'low', analog=False)
    y = filtfilt(b, a, data)
    return y

#Parámetros técnicos 
FS = 100 
CUTOFF = 5.0
GIRO_THRESHOLD = 50.0 # Grados/s. Si supera esto, consideramos que está girando

#Cargar uno de los archivos de 6 minutos obtenidos mediantes el batch extractor
ruta = r"C:\Users\teres\TFG\2026_msFeat_TEA\test_InfluxDB\out\EM_01912299X-118_6MWT_9.xlsx"
try:
    df = pd.read_excel(ruta)

    # Convertir la columna de tiempo a formato fecha real 
    df['_time'] = pd.to_datetime(df['_time'])
    
    # Limpiar filas con datos vacíos 
    df = df.dropna(subset=['S0', 'Gz', 'Az'])

    #Procesamiento: Filtrado y Detección
    df['S0_filt'] = butter_lowpass_filter(df['S0'], CUTOFF, FS)
    df['Gz_filt'] = butter_lowpass_filter(df['Gz'], 2.0, FS) #filtrado más estricto para giro

    #Identificar zonas de giro (Giro > Threshold)
    df['is_turning'] = df['Gz_filt'].abs() > GIRO_THRESHOLD

    #Detección de pasos
    #Utilización de los datos previamente filtrados 
    #Parámetros para detección de picos (Heel Strikes)
    # 'distance' evita detectar dos pasos demasiado cercanos (100 muestras/segundo)
    # 'height' es el humbral de presión para considerar que hay apoyo
    distancia_minima = 50 
    altura_minima = 200 

    # Detectar Heel Strikes (HS) solo donde NO hay giro
    # Copia de S0 poniendo a 0 las zonas de giro
    s0_clean = df['S0_filt'].copy()
    s0_clean[df['is_turning']] = 0

    peaks, _ = find_peaks(s0_clean, distance=distancia_minima, height=altura_minima) 
    
    #Cálculo de métricas
    if len(peaks) > 1:
        tiempos_pasos = df['_time'].iloc[peaks].values
        stride_times = np.diff(tiempos_pasos) / np.timedelta64(1, 's')
        mean_stride = np.mean(stride_times)
    else:
        mean_stride = 0

    #CVisualización UNIFICADA
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(12, 12), sharex=True)
    
    #Gráfico 1: Presión con sombreado en los giros
    ax1.plot(df['_time'], df['S0_filt'], label='Presión Talón (S0) Filtrada', color='blue')
    # Pintamos de rojo las zonas donde el paciente está girando
    ax1.fill_between(df['_time'], 0, df['S0_filt'].max(), where=df['is_turning'], 
                    color='red', alpha=0.3, label='Zona de Giro (Excluida)')
    ax1.set_title('Segmentación de Marcha Estable (Steady-State)')
    ax1.legend()

    # Gráfico 2: Giroscopio y Umbral
    ax2.plot(df['_time'], df['Gz_filt'], label='Giroscopio (Gz)', color='orange')
    ax2.axhline(y=GIRO_THRESHOLD, color='red', linestyle='--', label='Umbral de Giro')
    ax2.axhline(y=-GIRO_THRESHOLD, color='red', linestyle='--')
    ax2.set_title('Detección de Giros mediante Giroscopio Z')
    ax2.legend()
    
    # Gráfico 3: Detección de Pasos
    ax3.plot(df['_time'], df['S0_filt'], label='Presión S0', color='green', alpha=0.6)
    ax3.plot(df['_time'].iloc[peaks], df['S0_filt'].iloc[peaks], "x", color='black', label='Heel Strike')
    ax3.set_title(f'Eventos Detectados: {len(peaks)} pasos | Stride Time Medio: {mean_stride:.2f}s')
    ax3.legend()

    plt.tight_layout()
    print(f"Éxito: {len(peaks)} pasos detectados.")
    plt.show()
except Exception as e:
    print(f"Error al ejecutar: {e}")