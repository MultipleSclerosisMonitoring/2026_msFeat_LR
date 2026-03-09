import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import os

# CONFIGURACIÓN
archivo_h5 = 'data/raw/gait_study_data.h5'

if not os.path.exists(archivo_h5): exit()

with pd.HDFStore(archivo_h5, mode='r') as store:
    clave = store.keys()[0]
    df = store.get(clave)

# 1. Ordenar
df = df.sort_values(by='_time').reset_index(drop=True)

# 2. Suma de Presiones RAW (Cruda y Ruidosa)
df['Presion_Raw'] = df['S0'] + df['S1'] + df['S2']


# 3. FILTRADO (SUAVIZADO)

# Usamos una ventana de 15 muestras para eliminar el ruido de "sierra".
# Esto convertirá la mancha negra en una línea limpia.
df['Presion_Smooth'] = df['Presion_Raw'].rolling(window=15, center=True).mean().bfill().ffill()


# 4. CÁLCULO DE UMBRAL DINÁMICO

# Calculamos los máximos y mínimos sobre la señal YA FILTRADA
val_aire = np.percentile(df['Presion_Smooth'], 95)
val_suelo = np.percentile(df['Presion_Smooth'], 5)

# Umbral al 50% entre suelo y aire
umbral_dinamico = (val_aire + val_suelo) / 2

# 5. Clasificación (Binarización)
# Usamos la señal SUAVE para decidir
df['Is_Stance'] = np.where(df['Presion_Smooth'] < umbral_dinamico, 1, 0)

# 6. Cálculo de Porcentajes
total_muestras = len(df)
muestras_apoyo = df['Is_Stance'].sum()
muestras_balanceo = total_muestras - muestras_apoyo

porcentaje_apoyo = (muestras_apoyo / total_muestras) * 100
porcentaje_balanceo = (muestras_balanceo / total_muestras) * 100

print(f"\nANÁLISIS DE FASES (CON FILTRADO DE RUIDO)")
print(f"---------------------------------------------")
print(f"Calibración: Aire={val_aire:.0f} | Suelo={val_suelo:.0f} | Umbral={umbral_dinamico:.0f}")
print(f"Fase de Apoyo (Stance):   {porcentaje_apoyo:.2f}%  (Normal: ~60%)")
print(f"Fase de Balanceo (Swing): {porcentaje_balanceo:.2f}%  (Normal: ~40%)")
print(f"---------------------------------------------")

# 7. GRÁFICA COMPARATIVA
plt.figure(figsize=(14, 7))

tiempo = df['_time']
apoyo = df['Is_Stance']

# A. Pintamos la señal Ruidosa (Fina y transparente)
plt.plot(tiempo, df['Presion_Raw'], color='gray', alpha=0.3, linewidth=0.5, label='Señal Raw (Ruidosa)')

# B. Pintamos la señal FILTRADA (Gruesa y negra)
plt.plot(tiempo, df['Presion_Smooth'], color='black', linewidth=2, label='Señal Filtrada')

# C. Umbral
plt.axhline(y=umbral_dinamico, color='blue', linestyle='--', label='Umbral Dinámico')

# D. Relleno de Fases
plt.fill_between(tiempo, 0, 3070, where=(apoyo==1), color='green', alpha=0.2, label='Fase de Apoyo')
plt.fill_between(tiempo, 0, 3070, where=(apoyo==0), color='red', alpha=0.1, label='Fase de Balanceo')

plt.title(f'Análisis Final: Apoyo ({porcentaje_apoyo:.1f}%) vs Balanceo ({porcentaje_balanceo:.1f}%)')
plt.ylabel('Suma Sensores')
plt.legend(loc='upper right')
plt.grid(True)

# Zoom centrado
mid = len(df) // 2
plt.xlim(tiempo.iloc[mid], tiempo.iloc[mid + 600])
plt.ylim(val_suelo - 100, val_aire + 100)

plt.tight_layout()
plt.show()