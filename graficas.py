import pandas as pd
import matplotlib.pyplot as plt
import numpy as np

# 1. Cargar los datos del informe final
archivo = 'INFORME_CLINICO_FINAL.xlsx'
try:
    df = pd.read_excel(archivo)
    # Filtramos para asegurarnos de usar solo los que se procesaron bien
    df = df[df['Estado'] == 'OK'] 
except FileNotFoundError:
    print("Error: No se encuentra el archivo Excel. Ejecuta el análisis masivo primero.")
    exit()

print(f"Generando gráficas para {len(df)} pacientes...")

# Configuración visual para que parezca de artículo científico
plt.rcParams.update({'font.size': 12, 'font.family': 'sans-serif'})

# 2. Crear un lienzo con 3 sub-gráficas (1 fila, 3 columnas)
fig, axs = plt.subplots(1, 3, figsize=(16, 6))

# --- GRÁFICA A: Distribución de la Cadencia ---
axs[0].boxplot(df['Cadencia_PPM'], patch_artist=True, 
               boxprops=dict(facecolor='#a1c9f4', color='black'),
               medianprops=dict(color='red', linewidth=2))
axs[0].set_title('A. Distribución de Cadencia\n(Prueba 6MWT)')
axs[0].set_ylabel('Cadencia (Pasos por Minuto)')
axs[0].set_xticks([1])
axs[0].set_xticklabels([f'Pacientes (n={len(df)})'])
axs[0].grid(axis='y', linestyle='--', alpha=0.7)

# --- GRÁFICA B: Fases del Ciclo de Marcha ---
datos_fases = [df['Fase_Apoyo_Pct'], df['Fase_Balanceo_Pct']]
bp = axs[1].boxplot(datos_fases, patch_artist=True, labels=['Apoyo (Stance)', 'Balanceo (Swing)'],
                    medianprops=dict(color='black', linewidth=2))

# Colorear cajas (Verde para apoyo, Rojo pastel para balanceo)
colores = ['#8de5a1', '#ffb55a']
for patch, color in zip(bp['boxes'], colores):
    patch.set_facecolor(color)

axs[1].set_title('B. Proporción del Ciclo de Marcha')
axs[1].set_ylabel('Porcentaje del Ciclo (%)')
axs[1].grid(axis='y', linestyle='--', alpha=0.7)

# --- GRÁFICA C: Dispersión y Tendencia Biomecánica ---
axs[2].scatter(df['Cadencia_PPM'], df['Fase_Apoyo_Pct'], color='#4a4a4a', s=50, alpha=0.8)
axs[2].set_title('C. Correlación: Cadencia vs Apoyo')
axs[2].set_xlabel('Cadencia (PPM)')
axs[2].set_ylabel('Fase de Apoyo (%)')

# Añadir línea de tendencia lineal
z = np.polyfit(df['Cadencia_PPM'], df['Fase_Apoyo_Pct'], 1)
p = np.poly1d(z)
axs[2].plot(df['Cadencia_PPM'], p(df['Cadencia_PPM']), "r--", linewidth=2, label='Tendencia')
axs[2].legend()
axs[2].grid(True, linestyle='--', alpha=0.7)

# 3. Ajustar márgenes y guardar imagen en Alta Resolución (300 dpi)
plt.tight_layout()
nombre_imagen = 'graficas_resultados_TFG.png'
plt.savefig(nombre_imagen, dpi=300, bbox_inches='tight')

print(f"Imagen guardada como: {nombre_imagen}")
plt.show()