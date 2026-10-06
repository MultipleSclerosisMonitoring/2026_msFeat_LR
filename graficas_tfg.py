import os
import logging
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import seaborn as sns
from typing import List, Optional, Dict

import sys
sys.path.append(os.path.join(os.path.dirname(__file__), "pkgs"))
from msTools import i18n
from msTools.i18n import _

# Configuración del Logger estructurado
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger("msGait.GraphicsDynamic")

class GaitVisualizer:
    """Clase encargada de la generación automatizada y dinámica de gráficos.

    Escanea el dataset maestro para identificar pruebas y métricas disponibles,
    generando visualizaciones cruzadas de calidad científica (300 dpi).

    Attributes:
        data_path (str): Ruta al archivo Excel maestro.
        out_dir (str): Directorio principal de exportación.
        df (pd.DataFrame): Dataset cargado y limpiado en memoria.
    """

    def __init__(self, data_path: str = "DatasetMaestro.xlsx", out_dir: str = "resultados_graficos") -> None:
        """Inicializa el visualizador y prepara el entorno gráfico.

        Args:
            data_path (str, optional): Ruta al archivo de datos. Por defecto 'DatasetMaestro.xlsx'.
            out_dir (str, optional): Carpeta de exportación. Por defecto 'resultados_graficos'.
        """
        self.data_path = data_path
        self.out_dir = out_dir
        self.df: Optional[pd.DataFrame] = None
        
        i18n.init_translation("es")
        sns.set_theme(style="whitegrid", palette="muted")
        
        if not os.path.exists(self.out_dir):
            os.makedirs(self.out_dir)
            logger.debug(_("DEBUG_DIR_CREATED: Directorio base creado en {d}").format(d=self.out_dir))

    def load_data(self) -> bool:
        """Carga y limpia los datos, convirtiendo N/A a valores nulos calculables.

        Returns:
            bool: True si la carga es exitosa, False en caso contrario.
        """
        try:
            raw_df = pd.read_excel(self.data_path, sheet_name="Datos_Pacientes")
            self.df = raw_df.replace("N/A", np.nan)
            
            for col in self.df.columns:
                if col != "CodeID":
                    self.df[col] = pd.to_numeric(self.df[col], errors='coerce')
                    
            logger.info(_("INFO_DATA_LOADED: Datos cargados. Columnas detectadas: {c}").format(c=len(self.df.columns)))
            return True
        except Exception as e:
            logger.error(_("ERR_DATA_LOAD: Fallo al cargar datos - {err}").format(err=str(e)))
            return False

    def plot_dynamic_boxplots(self) -> None:
        """Genera Boxplots dinámicos agrupando métricas por tipo (ej. cadencia, zancada)."""
        if self.df is None or self.df.empty:
            return

        # Definir familias de métricas a buscar en las columnas
        familias = {
            "Cadencia": "cadencia",
            "Longitud_Zancada_cm": "stride_length_cm",
            "Altura_Paso_cm": "step_height_cm",
            "Fase_Apoyo_Pct": "apoyo_prc"
        }

        out_subfolder = os.path.join(self.out_dir, "boxplots")
        os.makedirs(out_subfolder, exist_ok=True)

        for titulo, sufijo in familias.items():
            # Buscar columnas que terminen en la métrica deseada
            cols_metrica = [c for c in self.df.columns if c.endswith(sufijo)]
            if not cols_metrica:
                continue

            plt.figure(figsize=(10, 6))
            sns.boxplot(data=self.df[cols_metrica], width=0.6)
            plt.title(_("Distribución de {t} entre Pruebas Clínicas").format(t=titulo))
            plt.ylabel(titulo.replace("_", " "))
            plt.xticks(rotation=45, ha='right')
            
            out_file = os.path.join(out_subfolder, f"boxplot_{titulo.lower()}.png")
            plt.tight_layout()
            plt.savefig(out_file, dpi=300)
            plt.close()
            logger.debug(_("DEBUG_PLOT_SAVED: {f}").format(f=out_file))

    def plot_dynamic_kde_asymmetry(self) -> None:
        """Genera histogramas KDE limitando el eje X y asegurando una muestra mínima."""
        if self.df is None:
            return

        cols_asim = [c for c in self.df.columns if 'symmetry_index' in c]
        out_subfolder = os.path.join(self.out_dir, "asimetria_kde")
        os.makedirs(out_subfolder, exist_ok=True)

        for col in cols_asim:
            data_clean = self.df[col].dropna()
            
            # Filtro QA: Muestra estadística mínima
            if len(data_clean) < 3:
                logger.debug(_("DEBUG_PLOT_SKIP: Omitiendo KDE para {c} por falta de datos (n={n})").format(c=col, n=len(data_clean)))
                continue

            test_name = col.split('_')[0]
            plt.figure(figsize=(8, 6))
            sns.histplot(data_clean, kde=True, bins=15, color="coral")
            plt.title(_("Distribución de Asimetría - {t}").format(t=test_name))
            plt.xlabel(_("Índice de Simetría (%)"))
            plt.ylabel(_("Frecuencia"))
            plt.axvline(x=10.0, color='red', linestyle='--', label=_("Umbral Patológico (10%)"))
            
            # Límite visual para que los outliers extremos (>100%) no aplasten la campana
            plt.xlim(0, max(100, data_clean.quantile(0.95))) 
            plt.legend()
            
            out_file = os.path.join(out_subfolder, f"kde_asimetria_{test_name}.png")
            plt.tight_layout()
            plt.savefig(out_file, dpi=300)
            plt.close()

    def plot_dynamic_correlation_matrices(self) -> None:
        """Genera matrices de correlación exigiendo un tamaño de muestra estadístico válido."""
        if self.df is None:
            return

        tests_detectados = set(c.split('_')[0] for c in self.df.columns if '_' in c and c != "CodeID")
        out_subfolder = os.path.join(self.out_dir, "correlaciones")
        os.makedirs(out_subfolder, exist_ok=True)

        for test in tests_detectados:
            cols_test = [c for c in self.df.columns if c.startswith(test)]
            df_test = self.df[cols_test].dropna(axis=0, how='any')
            
            # Filtro QA: Correlación imposible/inválida con menos de 3 pacientes completos
            if len(df_test) < 3 or len(df_test.columns) < 2:
                logger.debug(_("DEBUG_PLOT_SKIP: Omitiendo correlación para {t} por falta de datos completos (n={n})").format(t=test, n=len(df_test)))
                continue

            corr = df_test.corr(method='pearson')
            mask = np.triu(np.ones_like(corr, dtype=bool))
            
            plt.figure(figsize=(12, 10))
            sns.heatmap(corr, mask=mask, annot=False, cmap="coolwarm", vmin=-1, vmax=1, square=True)
            plt.title(_("Matriz de Correlación: {t}").format(t=test))
            
            out_file = os.path.join(out_subfolder, f"correlacion_{test}.png")
            plt.tight_layout()
            plt.savefig(out_file, dpi=300)
            plt.close()


    def plot_dynamic_scatter(self) -> None:
        """Genera gráficos de dispersión (Scatter) dinámicos para cada test.

        Visualiza la relación entre la cadencia (eje X) y el porcentaje de la fase 
        de apoyo (eje Y). Aplica un filtro de coherencia biomecánica para descartar
        artefactos donde la cadencia sea 0.
        """
        if self.df is None:
            return

        out_subfolder = os.path.join(self.out_dir, "dispersion")
        os.makedirs(out_subfolder, exist_ok=True)

        tests_detectados = set(c.split('_')[0] for c in self.df.columns if '_' in c and c != "CodeID")

        for test in tests_detectados:
            col_x = f"{test}_Left_cadencia"
            col_y = f"{test}_Left_apoyo_prc"
            
            if col_x in self.df.columns and col_y in self.df.columns:
                df_test = self.df[[col_x, col_y]].dropna()
                
                # FILTRO BIOMECÁNICO: Descartar artefactos de 0 pasos
                # Una cadencia menor a 5 PPM no representa un ciclo de marcha real
                df_test = df_test[df_test[col_x] > 5.0]
                
                # Filtro QA: Mínimo 3 pacientes válidos tras la limpieza
                if len(df_test) < 3:
                    logger.debug(_("DEBUG_PLOT_SKIP: Omitiendo Scatter para {t} por falta de datos coherentes").format(t=test))
                    continue

                plt.figure(figsize=(8, 6))
                sns.scatterplot(data=df_test, x=col_x, y=col_y, s=100, alpha=0.7, color="teal")
                plt.title(_("Correlación: Velocidad vs Fase de Apoyo ({t})").format(t=test))
                plt.xlabel(_("Cadencia Izquierda (PPM)"))
                plt.ylabel(_("Porcentaje Fase de Apoyo (%)"))
                
                out_file = os.path.join(out_subfolder, f"scatter_{test}.png")
                plt.tight_layout()
                plt.savefig(out_file, dpi=300)
                plt.close()

    def generate_all_reports(self) -> None:
        """Orquesta la extracción masiva de gráficos por familias."""
        logger.info(_("INFO_START_PLOTTING: Generando batería dinámica de gráficas..."))
        if self.load_data():
            self.plot_dynamic_boxplots()
            self.plot_dynamic_kde_asymmetry()
            self.plot_dynamic_correlation_matrices()
            self.plot_dynamic_scatter()
            logger.info(_("INFO_END_PLOTTING: Proceso finalizado. Gráficas estructuradas por carpetas."))

if __name__ == "__main__":
    visualizer = GaitVisualizer()
    visualizer.generate_all_reports()