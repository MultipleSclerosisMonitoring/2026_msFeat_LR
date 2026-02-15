import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os

# CONFIGURACIÓN
archivo_h5 = 'data/raw/gait_study_data.h5'

print(f"📂 Abriendo almacén de datos: {archivo_h5}")

if not os.path.exists(archivo_h5):
    print("❌ ERROR: No existe el archivo .h5.")
    exit()

try:
    with pd.HDFStore(archivo_h5, mode='r') as store:
        keys = store.keys()
        if len(keys) == 0: exit()
        
        clave_paciente = keys[0]
        print(f"👤 Analizando paciente: {clave_paciente}")
        df = store.get(clave_paciente)

    # --- 1️⃣ LIMPIEZA DE DATOS (CRÍTICO) ---
    # Ordenamos por tiempo para evitar el "rayado" en la gráfica
    df = df.sort_values(by='_time').reset_index(drop=True)
    
    # --- 2️⃣ SUAVIZADO DE SEÑAL (FILTRO) ---
    # Usamos una ventana de 10 muestras para quitar el ruido eléctrico
    # Esto convierte la "hierba" naranja en una línea más suave
    df['Ay_smooth'] = df['Ay'].rolling(window=15, center=True).mean().fillna(0)

    # --- 3️⃣ ALGORITMO DE DETECCIÓN CON "TIEMPO MUERTO" ---
    
    # Centramos la señal en 0 restando la media
    signal = df['Ay_smooth'] - df['Ay_smooth'].mean()
    times = df['_time']
    
    umbral_impacto = 0.3      # Altura mínima del pico
    distancia_minima_seg = 0.4 # Un humano no da pasos más rápido que esto (400ms)
    
    indices_pasos = []
    ultimo_paso_time = -1.0 # Para guardar cuándo fue el último paso
    
    # Recorremos la señal buscando picos
    # (Lo hacemos manual para no obligarte a instalar scipy)
    for i in range(1, len(signal)-1):
        actual = signal.iloc[i]
        anterior = signal.iloc[i-1]
        siguiente = signal.iloc[i+1]
        t_actual = times.iloc[i].timestamp()
        
        # ¿Es un pico? (Mayor que sus vecinos y mayor que el umbral)
        if actual > anterior and actual > siguiente and actual > umbral_impacto:
            
            # ¿Ha pasado suficiente tiempo desde el último paso?
            if (ultimo_paso_time == -1) or ((t_actual - ultimo_paso_time) > distancia_minima_seg):
                indices_pasos.append(i)
                ultimo_paso_time = t_actual # Actualizamos el reloj

    num_pasos = len(indices_pasos)
    
    # Cálculo de tiempos
    tiempo_total_seg = (df['_time'].max() - df['_time'].min()).total_seconds()
    cadencia = num_pasos / (tiempo_total_seg / 60)

    print(f"\n📊 RESULTADOS MEJORADOS:")
    print(f"   ---------------------------")
    print(f"   👣 Pasos reales (aprox):   {num_pasos}")
    print(f"   ⏱️ Duración prueba:       {tiempo_total_seg:.2f} s")
    print(f"   🏃 CADENCIA CORREGIDA:    {cadencia:.2f} pasos/min")
    print(f"   ---------------------------")

    # --- 4️⃣ GRÁFICA DE VERIFICACIÓN ---
    plt.figure(figsize=(14, 6))
    
    # Pintamos la señal original flojita
    plt.plot(df['_time'], df['Ay'] - df['Ay'].mean(), label='Señal Original (Ruidosa)', color='orange', alpha=0.3)
    
    # Pintamos la señal FILTRADA más fuerte
    plt.plot(df['_time'], signal, label='Señal Suavizada', color='darkorange', linewidth=2)
    
    # Pintamos los pasos detectados
    plt.plot(df['_time'].iloc[indices_pasos], signal.iloc[indices_pasos], 'ro', label='Pasos Validados', markersize=8)
    
    plt.title(f'Análisis de Marcha - Cadencia: {cadencia:.1f} ppm')
    plt.ylabel('Aceleración Vertical (G)')
    plt.legend()
    plt.grid(True)
    
    # Hacemos zoom a 10 segundos interesantes (por ejemplo del segundo 30 al 40)
    mid = len(df) // 2
    plt.xlim(df['_time'].iloc[mid], df['_time'].iloc[mid + 800]) 
    
    plt.tight_layout()
    plt.show()

except Exception as e:
    print(f"❌ Error: {e}")