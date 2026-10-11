"""Contratos offline de los gráficos (LR-37 a LR-39) sobre un Excel sintético."""
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pkgs'), str(ROOT / 'src'), str(ROOT)]
from graficas_tfg import MIN_N_CORRELATION, GaitVisualizer, fdr_bh, spearman_pairs


def write_book(path, patients=14, sheets=('Datos_Pacientes', 'Ensayos', 'Procedencia')):
    rng = np.random.default_rng(0)
    base = np.arange(patients, dtype=float)
    wide = pd.DataFrame({
        'CodeID': [str(1000 + i) for i in range(patients)],        # identificador con aspecto numérico
        'TUG_duracion_seg': 10.0,
        'TUG_Left_cadencia': 60 + base, 'TUG_Right_cadencia': 61 + base + rng.normal(0, 0.1, patients),
        'TUG_Left_apoyo_prc': 60 - base, 'TUG_Right_apoyo_prc': rng.normal(60, 5, patients),
        'TUG_Left_step_height_cm': [5.0 + i if i < 5 else np.nan for i in range(patients)],  # poco disponible
        'TUG_symmetry_index': [150.0 if i == 0 else float(i) if i < patients - 2 else np.nan for i in range(patients)],
    }).astype(object).fillna('N/A')
    trials = pd.DataFrame([
        {'TrialID': i, 'CodeID': str(1000 + i), 'test_type': 'TUG', 'Foot': foot, 'cadencia': 60.0 + i,
         'apoyo_prc': 55.0 + i, 'estado_cadencia': 'WARNING' if i == 0 else 'OK', 'estado_fases': 'OK'}
        for i in range(patients) for foot in ('Left', 'Right')
    ])
    trials.loc[1, 'apoyo_prc'] = np.nan
    provenance = pd.DataFrame({'clave': ['generado', 'commit_git', 'huella_config_processing'],
                               'valor': ['2026-10-10T10:00:00', 'abcdef1234567890', '0123456789abcdef']})
    book = {'Datos_Pacientes': wide, 'Ensayos': trials, 'Procedencia': provenance}
    with pd.ExcelWriter(path) as writer:
        for name in sheets:
            book[name].to_excel(writer, sheet_name=name, index=False)
    return path


def test_fdr_bh_matches_the_reference_procedure():
    assert fdr_bh(np.array([0.01, 0.02, 0.03, 0.04])) == pytest.approx([0.04, 0.04, 0.04, 0.04])
    assert fdr_bh(np.array([0.04, 0.001, 0.5])) == pytest.approx([0.06, 0.003, 0.5])
    assert len(fdr_bh(np.array([]))) == 0


def test_pairwise_population_does_not_depend_on_other_columns():
    frame = pd.DataFrame({'a': np.arange(12.0), 'b': np.arange(12.0) ** 2, 'c': [1.0] * 5 + [np.nan] * 7})
    with_sparse = spearman_pairs(frame).set_index(['variable_x', 'variable_y'])
    alone = spearman_pairs(frame[['a', 'b']]).set_index(['variable_x', 'variable_y'])
    assert with_sparse.loc[('a', 'b'), 'n'] == alone.loc[('a', 'b'), 'n'] == 12
    assert with_sparse.loc[('a', 'b'), 'rho'] == alone.loc[('a', 'b'), 'rho'] == 1.0
    assert with_sparse.loc[('a', 'c'), 'motivo_exclusion'] == f'n < {MIN_N_CORRELATION}' and np.isnan(with_sparse.loc[('a', 'c'), 'rho'])
    constant = spearman_pairs(pd.DataFrame({'a': np.arange(12.0), 'k': 3.0}))
    assert constant['motivo_exclusion'].tolist() == ['variable constante']


def test_figures_and_correlation_table_are_generated(tmp_path, caplog):
    out = tmp_path / 'figuras'
    visualizer = GaitVisualizer(str(write_book(tmp_path / 'dataset.xlsx')), str(out))
    with caplog.at_level(logging.DEBUG, logger='msGait'):
        visualizer.generate_all_reports()

    # El identificador no se convierte en dato aunque parezca numérico.
    assert visualizer.df['TUG_Left_cadencia'].dtype.kind in 'if' and 'CodeID' not in visualizer._metric_columns('TUG')
    assert visualizer.df['TUG_Left_step_height_cm'].isna().sum() == 9      # 'N/A' pasa a ausente, no a cero
    assert 'TUG_duracion_seg' not in visualizer._metric_columns('TUG')

    table = pd.read_csv(out / 'correlaciones' / 'correlaciones_TUG.csv')
    assert {'test', 'variable_x', 'variable_y', 'n', 'rho', 'p', 'p_fdr', 'significativo_fdr', 'motivo_exclusion'} <= set(table.columns)
    sparse = table[table['variable_x'].str.contains('step_height') | table['variable_y'].str.contains('step_height')]
    assert sparse['rho'].isna().all() and (sparse['n'] < MIN_N_CORRELATION).all()
    strong = table[(table['variable_x'] == 'TUG_Left_cadencia') & (table['variable_y'] == 'TUG_Left_apoyo_prc')].iloc[0]
    assert strong['n'] == 14 and strong['rho'] == -1.0 and bool(strong['significativo_fdr'])
    assert table.loc[table['rho'].notna(), 'p_fdr'].ge(table.loc[table['rho'].notna(), 'p'] - 1e-12).all()

    for name in ('correlaciones/correlacion_TUG.png', 'asimetria_kde/kde_asimetria_TUG.png',
                 'dispersion/scatter_TUG.png', 'boxplots/boxplot_cadencia.png'):
        assert (out / name).stat().st_size > 10_000
    # La altura solo tiene 5 pacientes: se dibuja en el boxplot (n >= 3) pero no en correlaciones.
    assert (out / 'boxplots/boxplot_altura_paso_cm.png').exists()
    assert 'DEBUG_CORRELATION_PAIRS' in caplog.text


def test_old_format_workbook_is_rejected_with_a_clear_message(tmp_path, caplog):
    path = write_book(tmp_path / 'viejo.xlsx', sheets=('Datos_Pacientes',))
    visualizer = GaitVisualizer(str(path), str(tmp_path / 'figuras'))
    with caplog.at_level(logging.ERROR, logger='msGait'):
        assert visualizer.load_data() is False
    assert 'ERR_DATA_FORMAT' in caplog.text and 'Ensayos' in caplog.text
