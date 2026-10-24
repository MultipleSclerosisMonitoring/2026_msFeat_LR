import os
import sys
import logging
from itertools import combinations
from pathlib import Path
from typing import Dict, List, Optional

import matplotlib
matplotlib.use("Agg")  # solo se escriben archivos; no se abre ninguna ventana
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from scipy.stats import spearmanr

from gait_analysis.metrics import METRIC_STATE, REPORT_METRICS, SYMMETRY_METRIC
from gait_analysis.logging_utils import configure_logging
from msConfig import PROJECT_ROOT, ConfigError, load_config
from msTools import i18n
from msTools.i18n import _

# Logger estructurado; el nivel lo fija la política de información (--verbose)
logger = logging.getLogger("msGait.GraphicsDynamic")

REQUIRED_SHEETS = ("Datos_Pacientes", "Ensayos", "Procedencia")
# Tamaño de muestra mínimo: gráficos descriptivos y matrices de correlación.
MIN_N_DESCRIPTIVE = 3
MIN_N_CORRELATION = 10
# Nivel del control de la tasa de falsos descubrimientos (Benjamini-Hochberg).
FDR_ALPHA = 0.05
# El índice de simetría implementado toma valores entre 0 y 200 %.
SYMMETRY_RANGE = (0.0, 200.0)
# Métricas que no entran en las matrices de correlación: el balanceo es 100 menos
# el apoyo, así que cada correlación suya repetiría la del apoyo con el signo cambiado.
CORRELATION_OMITTED = ("balanceo_prc",)
FOOT_COLORS = {"Left": "#1f77b4", "Right": "#d95f02"}


def fdr_bh(p_values: np.ndarray) -> np.ndarray:
    """Ajusta valores p con el procedimiento de Benjamini-Hochberg.

    Args:
        p_values (np.ndarray): Valores p de una familia de contrastes.

    Returns:
        np.ndarray: Valores p ajustados (q), en el mismo orden.
    """
    p_values = np.asarray(p_values, dtype=float)
    count = len(p_values)
    if count == 0:
        return p_values
    order = np.argsort(p_values)
    scaled = p_values[order] * count / np.arange(1, count + 1)
    adjusted = np.minimum.accumulate(scaled[::-1])[::-1].clip(max=1.0)
    result = np.empty(count)
    result[order] = adjusted
    return result


def related_by_construction(first: str, second: str) -> bool:
    """Indica si dos métricas están relacionadas por construcción.

    Son pares que no pueden informar de una asociación real: apoyo y balanceo
    del mismo pie (suman 100), zancadas y cadencia de zancada del mismo pie
    (una sale de la otra y de la duración), decaimiento y pendiente de la
    cadencia del mismo pie (salen de la misma recta) y el índice de simetría
    con la cadencia de cada pie (se calcula a partir de ellas).

    Args:
        first (str): Nombre de una columna ``{test}_{pie}_{métrica}``.
        second (str): Nombre de la otra columna.

    Returns:
        bool: ``True`` si el par debe quedar fuera de las correlaciones.
    """
    def parts(name: str) -> tuple[str, str]:
        for foot in FOOT_COLORS:
            if f"_{foot}_" in name:
                return foot, name.split(f"_{foot}_", 1)[1]
        return "", name.rsplit("_", 2)[-2] + "_" + name.rsplit("_", 1)[-1] if name.endswith(SYMMETRY_METRIC) else name

    (foot_a, metric_a), (foot_b, metric_b) = parts(first), parts(second)
    metrics = {metric_a, metric_b}
    same_foot_pairs = ({"apoyo_prc", "balanceo_prc"}, {"zancadas", "cadencia_zancada"}, {"decaimiento_prc", "pendiente_cadencia"})
    if foot_a and foot_a == foot_b and metrics in same_foot_pairs:
        return True
    return SYMMETRY_METRIC in metrics and "cadencia_zancada" in metrics


def spearman_pairs(frame: pd.DataFrame, min_n: int = MIN_N_CORRELATION) -> pd.DataFrame:
    """Calcula la correlación de Spearman por pares, cada par con sus propios casos.

    La población de un par no depende de las demás columnas: añadir una métrica
    poco disponible no cambia el resto de correlaciones. Los pares con menos de
    ``min_n`` casos, con una variable constante o relacionados por construcción
    (:func:`related_by_construction`) no se calculan y quedan con su causa de
    exclusión. Los valores p de los pares calculados se ajustan por FDR
    (Benjamini-Hochberg) dentro de la tabla.

    Args:
        frame (pd.DataFrame): Una fila por paciente y una columna por métrica.
        min_n (int): Número mínimo de casos completos del par.

    Returns:
        pd.DataFrame: Una fila por par con ``variable_x``, ``variable_y``, ``n``,
        ``rho``, ``p``, ``p_fdr``, ``significativo_fdr`` y ``motivo_exclusion``.
    """
    rows = []
    for first, second in combinations(frame.columns, 2):
        pair = frame[[first, second]].dropna()
        row = {
            "variable_x": first, "variable_y": second, "n": len(pair),
            "rho": np.nan, "p": np.nan, "p_fdr": np.nan, "significativo_fdr": False, "motivo_exclusion": "",
        }
        if related_by_construction(first, second):
            row["motivo_exclusion"] = "relacionados por construcción"
        elif len(pair) < min_n:
            row["motivo_exclusion"] = f"n < {min_n}"
        elif pair[first].nunique() < 2 or pair[second].nunique() < 2:
            row["motivo_exclusion"] = "variable constante"
        else:
            rho, p_value = spearmanr(pair[first], pair[second])
            row["rho"], row["p"] = round(float(rho), 4), float(p_value)
        rows.append(row)
    table = pd.DataFrame(rows)
    if not table.empty:
        computed = table["p"].notna()
        table.loc[computed, "p_fdr"] = fdr_bh(table.loc[computed, "p"].to_numpy())
        table["significativo_fdr"] = table["p_fdr"] < FDR_ALPHA
    return table


class GaitVisualizer:
    """Clase encargada de la generación automatizada y dinámica de gráficos.

    Lee el Excel del pipeline y genera visualizaciones exploratorias (300 dpi).
    Por defecto cada figura lleva al pie su procedencia y su tamaño de muestra;
    para la memoria del TFG se pueden generar sin ese pie, porque allí esa
    información va en el texto y en el pie de la figura.

    Attributes:
        data_path (str): Ruta al archivo Excel maestro.
        out_dir (str): Directorio principal de exportación.
        df (pd.DataFrame): Tabla por paciente con las métricas en numérico.
        trials (pd.DataFrame): Tabla por ensayo y pie, con estados.
        tests (List[str]): Códigos de test presentes.
        provenance (Dict[str, str]): Procedencia del Excel (fecha, commit, huellas).
        provenance_footer (bool): Si se escribe al pie de cada figura su
            tamaño de muestra y su procedencia.
    """

    def __init__(self, data_path: Optional[str] = None, out_dir: Optional[str] = None, config_path: Optional[str] = None,
                 provenance_footer: bool = True) -> None:
        """Inicializa el visualizador y prepara el entorno gráfico.

        Args:
            data_path (str, optional): Ruta al Excel. Por defecto, ``paths.output_dataset``
                de la configuración.
            out_dir (str, optional): Carpeta de exportación. Por defecto,
                ``resultados_graficos`` en la raíz del proyecto.
            config_path (str, optional): YAML de configuración; solo se usa si
                no se indica ``data_path``.
            provenance_footer (bool): ``False`` genera las figuras sin el pie de
                tamaño de muestra y procedencia (figuras para la memoria).

        Raises:
            ConfigError: Si hace falta la configuración y no es válida.
        """
        # Las gráficas no usan ninguna base de datos: se carga la configuración sin conexiones.
        try:
            config = load_config(config_path, sections=())
        except ConfigError:
            if data_path is None:
                raise
            config = {"project": {"language": "es"}}
        self.data_path = str(data_path or config["paths"]["output_dataset"])
        self.out_dir = str(out_dir or PROJECT_ROOT / "resultados_graficos")
        self.df: Optional[pd.DataFrame] = None
        self.trials: Optional[pd.DataFrame] = None
        self.tests: List[str] = []
        self.provenance: Dict[str, str] = {}
        self.provenance_footer = provenance_footer

        i18n.init_translation(config["project"]["language"])
        sns.set_theme(style="whitegrid", palette="muted")

        if not os.path.exists(self.out_dir):
            os.makedirs(self.out_dir)
            logger.debug(_("DEBUG_DIR_CREATED: Directorio base creado en {d}").format(d=self.out_dir))

    def load_data(self) -> bool:
        """Carga el Excel y convierte a numérico solo las métricas del informe.

        Las columnas se seleccionan por su nombre (lista explícita de métricas),
        de modo que un identificador con aspecto numérico nunca entra como dato.

        Returns:
            bool: True si la carga es exitosa, False en caso contrario.
        """
        try:
            book = pd.read_excel(self.data_path, sheet_name=None)
        except Exception as e:
            logger.error(_("ERR_DATA_LOAD: Fallo al cargar datos - {err}").format(err=str(e)))
            return False
        missing = [sheet for sheet in REQUIRED_SHEETS if sheet not in book]
        if missing:
            logger.error(
                _("ERR_DATA_FORMAT: A {0} le faltan las hojas {1}. Regenere el Excel con pipeline_test.py.").format(self.data_path, ', '.join(missing))
            )
            return False

        self.df = book["Datos_Pacientes"].copy()
        suffixes = tuple(f"_{name}" for name in (*REPORT_METRICS, SYMMETRY_METRIC, "duracion_seg"))
        for col in self.df.columns:
            if col.endswith(suffixes):
                self.df[col] = pd.to_numeric(self.df[col].replace("N/A", np.nan), errors="coerce")
        self.trials = book["Ensayos"]
        self.tests = sorted(self.trials["test_type"].dropna().astype(str).unique(), key=len, reverse=True)
        self.provenance = dict(zip(book["Procedencia"]["clave"].astype(str), book["Procedencia"]["valor"].astype(str)))

        logger.info(_("INFO_DATA_LOADED: Datos cargados. Columnas detectadas: {c}").format(c=len(self.df.columns)))
        logger.debug(_("DEBUG_DATA_SCOPE: {0} pacientes, {1} series de ensayo, tests {2}").format(len(self.df), len(self.trials), self.tests))
        return True

    def _test_of(self, column: str) -> str:
        """Devuelve el test al que pertenece una columna ``{test}_...``."""
        return next((test for test in self.tests if column.startswith(f"{test}_")), "")

    def _metric_columns(self, test: str) -> List[str]:
        """Columnas de métricas de un test en la tabla por paciente (sin duraciones)."""
        suffixes = tuple(f"_{name}" for name in (*REPORT_METRICS, SYMMETRY_METRIC))
        return [c for c in self.df.columns if c.startswith(f"{test}_") and c.endswith(suffixes)]

    def _save(self, out_file: str, note: str) -> None:
        """Añade el pie de procedencia (si está activado), guarda la figura actual y la cierra.

        Args:
            out_file (str): Ruta del PNG.
            note (str): Tamaño de muestra y criterio de inclusión de la figura.
        """
        figure = plt.gcf()
        if not self.provenance_footer:
            figure.tight_layout()
            figure.savefig(out_file, dpi=300)
            plt.close(figure)
            logger.debug(_("DEBUG_PLOT_SAVED: {f}").format(f=out_file))
            return
        dirty = " (con cambios sin commit)" if self.provenance.get("cambios_sin_commit") == "True" else ""
        origin = (
            f"Origen: {os.path.basename(self.data_path)} · generado {self.provenance.get('generado', '?')} · "
            f"commit {self.provenance.get('commit_git', '?')[:10]}{dirty} · config {self.provenance.get('huella_config_processing', '?')}"
        )
        figure.tight_layout(rect=(0, 0.05, 1, 1))
        figure.text(0.01, 0.012, f"{note}\n{origin}", fontsize=6.5, color="#52606d", ha="left", va="bottom")
        figure.savefig(out_file, dpi=300)
        plt.close(figure)
        logger.debug(_("DEBUG_PLOT_SAVED: {f}").format(f=out_file))

    def plot_dynamic_boxplots(self) -> None:
        """Genera Boxplots dinámicos agrupando métricas por tipo (ej. cadencia, zancada).

        Sobre cada caja se dibuja el valor de cada paciente: relleno si todos
        sus ensayos tienen esa métrica en OK y hueco si alguno la tiene en
        WARNING (columna ``{test}_{pie}_estado_{grupo}_n_warning``). Los
        WARNING entran siempre.
        """
        if self.df is None or self.df.empty:
            return

        # Definir familias de métricas a buscar en las columnas
        familias = {
            "Cadencia": "cadencia_zancada",
            "Longitud_Zancada_cm": "stride_length_cm",
            "Excursion_Tobillo_cm": "excursion_tobillo_cm",
            "Fase_Apoyo_Pct": "apoyo_prc",
            "Pendiente_Cadencia": "pendiente_cadencia",
        }

        out_subfolder = os.path.join(self.out_dir, "boxplots")
        os.makedirs(out_subfolder, exist_ok=True)

        for titulo, sufijo in familias.items():
            # Columnas de la métrica con muestra descriptiva mínima
            counts = {c: int(self.df[c].notna().sum()) for c in self.df.columns if c.endswith(f"_{sufijo}")}
            cols_metrica = [c for c, n in counts.items() if n >= MIN_N_DESCRIPTIVE]
            omitted = sorted(set(counts) - set(cols_metrica))
            if omitted:
                logger.debug(_("DEBUG_PLOT_SKIP: Boxplot de {0}: columnas con n < {1} omitidas: {2}").format(titulo, MIN_N_DESCRIPTIVE, omitted))
            if not cols_metrica:
                continue

            plt.figure(figsize=(10, 6))
            data = self.df[cols_metrica].rename(columns={c: f"{c[:-len(sufijo) - 1]}\n(n={counts[c]})" for c in cols_metrica})
            sns.boxplot(data=data, width=0.6, color="#dfe7ee", showfliers=False)
            rng = np.random.default_rng(0)
            warned_total = 0
            for position, column in enumerate(cols_metrica):
                values = self.df[column]
                flag = f"{column[:-len(sufijo)]}{METRIC_STATE[sufijo]}_n_warning"
                warned = (pd.to_numeric(self.df[flag], errors="coerce").fillna(0) > 0) if flag in self.df.columns else pd.Series(False, index=self.df.index)
                for mask, style in ((values.notna() & ~warned, {"color": "#1f77b4"}),
                                    (values.notna() & warned, {"facecolors": "none", "edgecolors": "#1f77b4", "linewidths": 1.1})):
                    x = position + rng.uniform(-0.12, 0.12, int(mask.sum()))
                    plt.scatter(x, values[mask], s=22, alpha=0.8, zorder=3, **style)
                warned_total += int((values.notna() & warned).sum())
            plt.title(_("Distribución de {t} entre Pruebas Clínicas").format(t=titulo))
            plt.ylabel(titulo.replace("_", " "))
            plt.xticks(rotation=45, ha='right')

            out_file = os.path.join(out_subfolder, f"boxplot_{titulo.lower()}.png")
            self._save(out_file, f"Un valor por paciente (media de sus ensayos con valor). Columnas con n >= {MIN_N_DESCRIPTIVE}. "
                                 f"Punto hueco: algún ensayo del paciente con esta métrica en WARNING ({warned_total} valores).")

    def plot_dynamic_asymmetry(self) -> None:
        """Dibuja el índice de simetría de cada paciente como un gráfico de puntos.

        Con pocos pacientes un histograma o una curva de densidad no describen
        nada, así que se dibuja un punto por paciente, ordenados de menor a
        mayor y sin su identificador, con la mediana como línea de referencia.
        El eje se ajusta a los datos (desde 0 hasta algo más que el máximo)
        dentro del rango del índice implementado (0-200 %), de modo que ningún
        valor queda fuera. No se dibuja ningún umbral: no hay referencia
        bibliográfica para la simetría de la cadencia de zancada. Se anota
        cuántos pacientes no tienen asimetría calculable.
        """
        if self.df is None:
            return

        cols_asim = [c for c in self.df.columns if c.endswith(f"_{SYMMETRY_METRIC}")]
        out_subfolder = os.path.join(self.out_dir, "asimetria")
        os.makedirs(out_subfolder, exist_ok=True)

        for col in cols_asim:
            test_name = col[:-len(SYMMETRY_METRIC) - 1]
            data_clean = self.df[col].dropna()
            duration = self.df.get(f"{test_name}_duracion_seg")
            with_test = int(duration.notna().sum()) if duration is not None else len(self.df)
            not_computable = with_test - len(data_clean)

            # Filtro QA: Muestra estadística mínima
            if len(data_clean) < MIN_N_DESCRIPTIVE:
                logger.debug(_("DEBUG_PLOT_SKIP: Omitiendo la asimetría de {c} por falta de datos (n={n})").format(c=col, n=len(data_clean)))
                continue

            values = np.sort(data_clean.to_numpy(dtype=float))
            positions = np.arange(1, len(values) + 1)
            median = float(np.median(values))
            # La altura crece con los pacientes, con un tope para que la figura quepa en una página.
            plt.figure(figsize=(8, min(9.0, max(3.5, 0.45 * len(values) + 1.5))))
            plt.hlines(positions, SYMMETRY_RANGE[0], values, color="#c9d3dd", linewidth=1)
            plt.scatter(values, positions, s=45 if len(values) <= 30 else 18, color="#1f77b4", zorder=3)
            # Separador decimal del idioma activo: coma en español, punto en el catálogo inglés.
            shown = f"{median:.1f}".replace(".", _(","))
            plt.axvline(x=median, color='#52606d', linestyle=':', label=_("Mediana ({0} %)").format(shown))
            plt.title(_("Índice de simetría por paciente - {t}").format(t=test_name))
            plt.xlabel(_("Índice de Simetría (%)"))
            plt.ylabel(_("Pacientes, de menor a mayor índice"))
            plt.yticks([])
            plt.ylim(0.4, len(values) + 0.6)
            plt.xlim(SYMMETRY_RANGE[0], min(SYMMETRY_RANGE[1], max(5.0, 1.15 * float(values.max()))))
            plt.legend(loc="lower right")

            out_file = os.path.join(out_subfolder, f"asimetria_{test_name}.png")
            self._save(
                out_file,
                f"Un valor por paciente. n = {len(data_clean)} con asimetría calculada; {not_computable} de {with_test} "
                "pacientes con este test sin asimetría calculable (falta un pie o su cadencia).",
            )

    def plot_dynamic_correlation_matrices(self) -> None:
        """Genera matrices de correlación exploratorias (Spearman por pares, con FDR).

        Cada par usa sus propios casos completos y solo se calcula con al menos
        ``MIN_N_CORRELATION`` pacientes. La unidad es el paciente (una fila por
        sujeto), por lo que no hay medidas repetidas del mismo sujeto dentro de
        una correlación. Las métricas de ``CORRELATION_OMITTED`` no entran, para
        no repetir contrastes en la familia del FDR. Junto a cada figura se
        exporta un CSV con N, rho, p, p ajustado por FDR y la causa de exclusión
        de cada par.
        """
        if self.df is None:
            return

        out_subfolder = os.path.join(self.out_dir, "correlaciones")
        os.makedirs(out_subfolder, exist_ok=True)

        omitted = tuple(f"_{name}" for name in CORRELATION_OMITTED)
        for test in self.tests:
            cols_test = [c for c in self._metric_columns(test) if not c.endswith(omitted)]
            if len(cols_test) < 2:
                continue
            table = spearman_pairs(self.df[cols_test], MIN_N_CORRELATION)
            table.insert(0, "test", test)
            table.to_csv(os.path.join(out_subfolder, f"correlaciones_{test}.csv"), index=False)
            computed = table[table["rho"].notna()]
            logger.debug(
                _("DEBUG_CORRELATION_PAIRS: {0}: {1} de {2} pares calculados (n >= {3}); {4} con FDR < {5}").format(test, len(computed), len(table), MIN_N_CORRELATION, int(computed['significativo_fdr'].sum()), FDR_ALPHA)
            )

            # Filtro QA: sin ningún par con muestra suficiente no hay matriz
            if computed.empty:
                logger.debug(_("DEBUG_PLOT_SKIP: Omitiendo correlación para {t} por falta de datos completos (n={n})").format(t=test, n=int(table["n"].max())))
                continue

            names = [c[len(test) + 1:] for c in cols_test]
            corr = pd.DataFrame(np.nan, index=names, columns=names)
            marks = pd.DataFrame("", index=names, columns=names)
            for row in computed.itertuples():
                x, y = row.variable_x[len(test) + 1:], row.variable_y[len(test) + 1:]
                corr.loc[y, x] = row.rho
                marks.loc[y, x] = f"{row.rho:.2f}{'*' if row.significativo_fdr else ''}\nn={row.n}"
            mask = corr.isna()

            plt.figure(figsize=(12, 10))
            sns.heatmap(corr, mask=mask, annot=marks, fmt="", annot_kws={"fontsize": 6}, cmap="coolwarm", vmin=-1, vmax=1, square=True)
            plt.title(_("Matriz de Correlación: {t}").format(t=test) + " (Spearman, exploratoria)")

            out_file = os.path.join(out_subfolder, f"correlacion_{test}.png")
            self._save(
                out_file,
                f"Análisis exploratorio. Un valor por paciente; cada par con sus casos completos y n >= {MIN_N_CORRELATION} "
                f"({len(computed)} de {len(table)} pares). * p ajustado por FDR (Benjamini-Hochberg) < {FDR_ALPHA}.",
            )

    def plot_dynamic_scatter(self) -> None:
        """Genera gráficos de dispersión (Scatter) dinámicos para cada test.

        Visualiza la relación entre la cadencia (eje X) y el porcentaje de la fase
        de apoyo (eje Y) de cada ensayo, con los dos pies en el mismo gráfico y
        un color por pie. Entran todos los ensayos con valor en ambas métricas;
        los que tienen alguna de las dos en estado WARNING se dibujan huecos.
        """
        if self.trials is None:
            return

        out_subfolder = os.path.join(self.out_dir, "dispersion")
        os.makedirs(out_subfolder, exist_ok=True)

        for test in sorted(self.tests):
            df_test = self.trials[self.trials["test_type"].astype(str) == test].dropna(subset=["cadencia_zancada", "apoyo_prc"])
            df_test = df_test[df_test["Foot"].isin(FOOT_COLORS)]

            # Filtro QA: muestra descriptiva mínima
            if len(df_test) < MIN_N_DESCRIPTIVE:
                logger.debug(_("DEBUG_PLOT_SKIP: Omitiendo Scatter para {t} por falta de datos coherentes").format(t=test))
                continue

            valid = (df_test["estado_cadencia"] == "OK") & (df_test["estado_fases"] == "OK")
            plt.figure(figsize=(8, 6))
            for foot, color in FOOT_COLORS.items():
                ok = df_test[valid & (df_test["Foot"] == foot)]
                warned = df_test[~valid & (df_test["Foot"] == foot)]
                plt.scatter(ok["cadencia_zancada"], ok["apoyo_prc"], s=70, alpha=0.75, color=color, label=f"{foot} (n={len(ok)})")
                if len(warned):
                    plt.scatter(warned["cadencia_zancada"], warned["apoyo_prc"], s=70, facecolors="none", edgecolors=color,
                                linewidths=1.2, label=f"{foot}, WARNING (n={len(warned)})")
            plt.title(_("Cadencia frente a Fase de Apoyo ({t})").format(t=test))
            plt.xlabel(_("Cadencia (zancadas por minuto)"))
            plt.ylabel(_("Porcentaje Fase de Apoyo (%)"))
            plt.legend(title=_("Pie"))

            out_file = os.path.join(out_subfolder, f"scatter_{test}.png")
            self._save(
                out_file,
                f"Un punto por ensayo y pie con valor en ambas métricas (n = {len(df_test)}). "
                "Hueco: cadencia o fases en estado WARNING.",
            )

    def generate_all_reports(self) -> None:
        """Orquesta la extracción masiva de gráficos por familias."""
        logger.info(_("INFO_START_PLOTTING: Generando batería dinámica de gráficas..."))
        if self.load_data():
            self.plot_dynamic_boxplots()
            self.plot_dynamic_asymmetry()
            self.plot_dynamic_correlation_matrices()
            self.plot_dynamic_scatter()
            logger.info(_("INFO_END_PLOTTING: Proceso finalizado. Gráficas estructuradas por carpetas."))


def main(argv: list[str] | None = None) -> None:
    """Punto de entrada de la orden ``msfeat-graficas`` (y de ``graficas_tfg.py``).

    Args:
        argv (list[str] | None): Argumentos; por defecto los de la línea de órdenes.
    """
    import argparse
    parser = argparse.ArgumentParser(description="Gráficas a partir del Excel del pipeline")
    parser.add_argument('--config', default=None, help='YAML; por defecto config/config.yaml')
    parser.add_argument('--data', default=None, help='Excel de entrada; por defecto paths.output_dataset')
    parser.add_argument('--out', default=None, help='Carpeta de salida; por defecto resultados_graficos')
    parser.add_argument('--verbose', type=int, choices=(0, 1, 2), default=1, help='0 avisos, 1 información, 2 depuración')
    parser.add_argument('--sin-procedencia', action='store_true',
                        help='Sin el pie de tamaño de muestra y procedencia (figuras para la memoria)')
    args = parser.parse_args(argv)
    configure_logging(args.verbose)
    try:
        visualizer = GaitVisualizer(args.data, args.out, args.config, provenance_footer=not args.sin_procedencia)
    except ConfigError as error:
        sys.exit(str(error))
    visualizer.generate_all_reports()


if __name__ == "__main__":
    main()
