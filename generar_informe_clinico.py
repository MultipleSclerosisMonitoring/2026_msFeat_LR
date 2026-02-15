import pandas as pd
import numpy as np
import os
import warnings

# Ignoramos avisos de compatibilidad futura de pandas para mantener la salida limpia
warnings.simplefilter(action='ignore', category=FutureWarning)

# ---------------------------------------------------------
# CONFIGURACIÓN
# ---------------------------------------------------------
ARCHIVO_INPUT = 'data/raw/gait_study_data.h5'
ARCHIVO_OUTPUT = 'INFORME_CLINICO_FINAL.xlsx'

# ---------------------------------------------------------
# FUNCIONES DE ANÁLISIS (MOTORES DE CÁLCULO)
# ---------------------------------------------------------

def calcular_cadencia(df):
    """Calcula la cadencia (pasos/min) usando el acelerómetro vertical."""
    try:
        # 1. Suavizado (Media móvil)
        signal = df['Ay'].rolling(window=15, center=True).mean().fillna(0)
        signal = signal - signal.mean() # Centrar en 0
        
        # 2. Detección de picos con "Debouncing" (Tiempo muerto)
        umbral_impacto = 0.3
        distancia_minima_seg = 0.4
        
        times = df['_time']
        ultimo_paso_time = -1.0
        pasos = 0
        
        # Recorrido manual optimizado
        signal_values = signal.values
        time_values = times.values
        t_stamps = [t.timestamp() for t in time_values] # Pre-cálculo para velocidad
        
        for i in range(1, len(signal)-1):
            actual = signal_values[i]
            
            # Condición de Pico
            if actual > signal_values[i-1] and actual > signal_values[i+1] and actual > umbral_impacto:
                t_actual = t_stamps[i]
                if (ultimo_paso_time == -1) or ((t_actual - ultimo_paso_time) > distancia_minima_seg):
                    pasos += 1
                    ultimo_paso_time = t_actual
        
        # 3. Cálculo final
        duracion_min = (times.max() - times.min()).total_seconds() / 60
        if duracion_min == 0: return 0, 0
        
        cadencia = pasos / duracion_min
        return pasos, cadencia

    except Exception as e:
        return 0, 0

def calcular_fases(df):
    """Calcula % de Apoyo (Stance) y Balanceo (Swing) usando presión."""
    try:
        # 1. Suma y Suavizado
        presion_raw = df['S0'] + df['S1'] + df['S2']
        # Usamos bfill().ffill() para pandas moderno
        presion_smooth = presion_raw.rolling(window=15, center=True).mean().bfill().ffill()
        
        # 2. Umbral Dinámico
        val_aire = np.percentile(presion_smooth, 95)
        val_suelo = np.percentile(presion_smooth, 5)
        umbral = (val_aire + val_suelo) / 2
        
        # 3. Clasificación
        # Valor < Umbral = PISANDO (Stance)
        is_stance = np.where(presion_smooth < umbral, 1, 0)
        
        # 4. Porcentajes
        total = len(df)
        apoyo = is_stance.sum()
        balanceo = total - apoyo
        
        pct_apoyo = (apoyo / total) * 100
        pct_balanceo = (balanceo / total) * 100
        
        return pct_apoyo, pct_balanceo
        
    except Exception as e:
        return 0, 0

# ---------------------------------------------------------
# BLOQUE PRINCIPAL (MAIN LOOP)
# ---------------------------------------------------------
def main():
    if not os.path.exists(ARCHIVO_INPUT):
        print("❌ Error: No se encuentra el archivo HDF5.")
        return

    print(f"🚀 Iniciando Análisis Masivo sobre: {ARCHIVO_INPUT}")
    
    resultados_globales = []
    
    with pd.HDFStore(ARCHIVO_INPUT, mode='r') as store:
        keys = store.keys()
        total_pacientes = len(keys)
        print(f"📂 Pacientes encontrados: {total_pacientes}\n")
        
        for i, key in enumerate(keys):
            try:
                # Extraer ID del paciente de la ruta (ej: /p_2025-CY1.../6MWT/...)
                partes = key.split('/')
                # partes[0] es vacío, partes[1] es p_ID
                paciente_id = partes[1].replace('p_', '') 
                test_type = partes[2]
                
                print(f"   [{i+1}/{total_pacientes}] Procesando: {paciente_id}...", end=" ")
                
                df = store.get(key)
                df = df.sort_values(by='_time').reset_index(drop=True)
                
                # Duración
                duracion_seg = (df['_time'].max() - df['_time'].min()).total_seconds()
                
                # --- CÁLCULOS ---
                pasos, cadencia = calcular_cadencia(df)
                pct_apoyo, pct_swing = calcular_fases(df)
                
                print(f"✅ (Cad: {cadencia:.1f} | Apoyo: {pct_apoyo:.1f}%)")
                
                # Guardar fila de resultados
                resultados_globales.append({
                    "ID_Paciente": paciente_id,
                    "Tipo_Test": test_type,
                    "Duracion_Seg": round(duracion_seg, 2),
                    "Num_Pasos": pasos,
                    "Cadencia_PPM": round(cadencia, 2),
                    "Fase_Apoyo_Pct": round(pct_apoyo, 2),
                    "Fase_Balanceo_Pct": round(pct_swing, 2),
                    "Estado": "OK"
                })
                
            except Exception as e:
                print(f"❌ ERROR: {e}")
                resultados_globales.append({
                    "ID_Paciente": key,
                    "Estado": f"ERROR: {str(e)}"
                })

    # ---------------------------------------------------------
    # EXPORTACIÓN A EXCEL
    # ---------------------------------------------------------
    if resultados_globales:
        df_final = pd.DataFrame(resultados_globales)
        df_final.to_excel(ARCHIVO_OUTPUT, index=False)
        print(f"\n🎉 ¡PROCESO TERMINADO!")
        print(f"📄 Informe guardado en: {os.path.abspath(ARCHIVO_OUTPUT)}")
        print(f"📊 Total procesados: {len(df_final)}")
    else:
        print("⚠️ No se generaron resultados.")

if __name__ == "__main__":
    main()