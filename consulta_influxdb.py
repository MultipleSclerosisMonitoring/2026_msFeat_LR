# consulta_influxdb.py
from InfluxDBms.cInfluxDB import cInfluxDB
from datetime import datetime
import matplotlib.pyplot as plt
import numpy as np

# -----------------------------
# 1️⃣ Inicializa conexión
# -----------------------------
# Cambia la ruta a tu archivo config_db.yaml si es necesario
config_path = "InfluxDBms/config_db.yaml"
db = cInfluxDB(config_path=config_path)

# -----------------------------
# 2️⃣ Parámetros de consulta
# -----------------------------
from_date = datetime(2024, 1, 1, 0, 0, 0)
to_date   = datetime.now()
qtok      = "2025-CY1-SJ1-33"  # Código de sesión / paciente
pie       = "Left"             # Pie: 'Right' o 'Left'
metrics   = None               # Si quieres todas, deja None

print("🔹 Campos disponibles en el bucket:")
db.debug_fields()

# -----------------------------
# 3️⃣ Mostrar columnas disponibles (opcional)
# -----------------------------
print("🔹 Columnas disponibles en el bucket:")
db.debug_fields()

# -----------------------------
# 4️⃣ Mostrar un sample rápido (opcional)
# -----------------------------
print("\n🔹 Sample de 5 registros:")
db.show_raw_sample(from_date, to_date, qtok, pie)

# -----------------------------
# 5️⃣ Consulta completa y DataFrame
# -----------------------------
print("\n🔹 Consulta completa:")
df = db.query_data(from_date, to_date, qtok, pie, metrics)

# -----------------------------
# 6️⃣ Mostrar resultados
# -----------------------------
if df.empty:
    print("⚠️ La consulta no devolvió datos. Revisa parámetros (fechas, CodeID, pie).")
else:
    print(df.head())  # Muestra solo las primeras 5 filas
    print(f"\nTotal registros obtenidos: {len(df)}")

# -----------------------------
# 7️⃣ Guardar en Excel (opcional)
# -----------------------------
# df.to_excel("resultados_influx.xlsx", index=False)
# print("✅ Guardado en resultados_influx.xlsx")

# -----------------------------
# 8️⃣ BÚSQUEDA POR "AGITACIÓN" (VARIANZA)
# -----------------------------
print("🔍 Buscando dónde hay más movimiento (pasos reales)...")

# Calculamos la "agitación" (desviación estándar) en ventanas de 100 muestras
# Esto nos dirá dónde la señal deja de ser una línea plana
rolling_std = df['Ax'].rolling(window=100).std()

# Buscamos el punto con mayor agitación
indice_max_movimiento = rolling_std.idxmax()

if np.isnan(indice_max_movimiento):
    print("⚠️ No se detectó movimiento significativo.")
    inicio = 0
else:
    print(f"✅ ¡Movimiento fuerte detectado alrededor del índice: {indice_max_movimiento}!")
    # Centramos la vista en ese momento
    inicio = max(0, int(indice_max_movimiento) - 300)

fin = inicio + 600 # 6 segundos de visualización

# Asegurar límites
if fin > len(df): fin = len(df)
subset = df.iloc[inicio:fin]

# -----------------------------
# 9️⃣ GRAFICAR
# -----------------------------
plt.figure(figsize=(12, 10))

# 1. Acelerometría (¡Aquí esperamos ver ruido/pasos!)
plt.subplot(2, 1, 1)
plt.plot(subset['_time'], subset['Ax'], label='Ax', alpha=0.8)
plt.plot(subset['_time'], subset['Ay'], label='Ay', alpha=0.8)
plt.plot(subset['_time'], subset['Az'], label='Az', alpha=0.8)
plt.title(f'Zona de mayor movimiento - Índice {inicio}')
plt.ylabel('Fuerza G')
plt.legend()
plt.grid(True)

# 2. Presión
plt.subplot(2, 1, 2)
plt.plot(subset['_time'], subset['S0'], label='S0', color='red')
plt.plot(subset['_time'], subset['S1'], label='S1', color='green')
plt.plot(subset['_time'], subset['S2'], label='S2', color='blue')
plt.axhline(y=1023, color='gray', linestyle='--')
plt.title('Sensores de Presión correspondientes')
plt.ylabel('Valor Raw')
plt.legend()
plt.grid(True)

plt.tight_layout()
plt.show()