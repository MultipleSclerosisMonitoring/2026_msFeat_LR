# consulta_influxdb.py
from InfluxDBms.cInfluxDB import cInfluxDB
from datetime import datetime

# -----------------------------
# 1️⃣ Inicializa conexión
# -----------------------------
# Cambia la ruta a tu archivo config_db.yaml si es necesario
config_path = "InfluxDBms/config_db.yaml"
db = cInfluxDB(config_path=config_path)

# -----------------------------
# 2️⃣ Parâmetros de consulta
# -----------------------------
from_date = datetime(2025, 1, 1, 0, 0, 0)
to_date   = datetime(2025, 1, 2, 0, 0, 0)
qtok      = "2025-CY1-SJ1-33"  # Código de sesión / paciente
pie       = "Right"            # Pie: 'Right' o 'Left'
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
