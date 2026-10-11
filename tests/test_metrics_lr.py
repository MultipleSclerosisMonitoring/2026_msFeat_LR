"""Contratos offline de los puntos de algoritmo LR-12 a LR-29 ya implementados."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pkgs'), str(ROOT / 'src'), str(ROOT)]
from GaitMetricsCalculator import GaitMetricsCalculator, ProcessingConfig
from msConfig import ConfigError, load_config


def walk(seconds=20.0, fs=100.0, step_hz=1.0, gz=0.0, start='2025-01-01 10:00:00'):
    """Marcha sintética: un impacto por ciclo en Ay y presión baja durante el 60 % del ciclo."""
    t = np.arange(int(round(seconds * fs))) / fs
    phase = (t * step_hz) % 1.0
    pressure = np.where(phase < 0.6, 100.0, 300.0)
    return pd.DataFrame({
        '_time': pd.Timestamp(start) + pd.to_timedelta(t, unit='s'),
        'Ay': np.sin(2 * np.pi * step_hz * t), 'Gz': gz,
        'S0': pressure, 'S1': pressure, 'S2': pressure,
    })


CALC = GaitMetricsCalculator()


# --- LR-12: el tiempo real manda, no el número de muestras ------------------------------

def test_cadence_uses_real_time_not_sample_count():
    assert CALC.calcular_cadencia(walk(fs=100.0))['cadencia'] == pytest.approx(60, abs=1)
    # Misma marcha muestreada a 50 Hz: mismas zancadas por minuto.
    half = CALC.calcular_cadencia(walk(fs=50.0))
    assert half['cadencia'] == pytest.approx(60, abs=1) and half['fs_efectiva'] == pytest.approx(50)
    # Mismas muestras con el doble de duración real: la cadencia se reduce a la mitad.
    slow = walk(fs=100.0)
    slow['_time'] = slow['_time'].iloc[0] + (slow['_time'] - slow['_time'].iloc[0]) * 2
    result = CALC.calcular_cadencia(slow)
    assert result['cadencia'] == pytest.approx(30, abs=1) and result['duracion_real_seg'] == pytest.approx(40, abs=0.1)


def test_missing_time_column_is_an_error():
    for method in (CALC.calcular_cadencia, CALC.calcular_fases, CALC.calcular_longitud_zancada, CALC.calcular_altura_zancada):
        result = method(walk().drop(columns=['_time']))
        assert result['status'] == 'ERROR' and result['error_reason'] == 'ERR_MISSING_TIME'


def test_gaps_split_segments_and_are_not_counted_as_walking():
    frame = walk(seconds=30)
    seconds = (frame['_time'] - frame['_time'].iloc[0]).dt.total_seconds()
    gapped = frame[(seconds < 10) | (seconds >= 20)]
    prepared = CALC.preparar(gapped)
    assert prepared.report['n_huecos'] == 1 and prepared.report['n_segmentos'] == 2
    assert prepared.report['tiempo_huecos_seg'] == pytest.approx(10, abs=0.05)
    result = CALC.calcular_cadencia(prepared)
    assert result['tiempo_analizado_seg'] == pytest.approx(20, abs=0.1)
    assert result['cadencia'] == pytest.approx(60, abs=4)


def test_bursty_timestamps_do_not_create_gaps_or_a_false_rate():
    # Datos reales: dos muestras casi juntas cada 20 ms (tasa media de 100 Hz).
    frame = walk(fs=100.0)
    offsets = np.where(np.arange(len(frame)) % 2 == 1, pd.Timedelta(milliseconds=-8), pd.Timedelta(0))
    frame['_time'] = frame['_time'] + offsets
    prepared = CALC.preparar(frame)
    assert prepared.report['fs_efectiva'] == pytest.approx(100, abs=0.5) and prepared.report['n_huecos'] == 0
    assert prepared.report['irregularidad_tiempo'] > 0.9 and prepared.report['fs_desviada']   # nominal: 50 Hz
    assert CALC.calcular_cadencia(prepared)['cadencia'] == pytest.approx(60, abs=1)
    # Rejilla uniforme: mismas filas y valores, instantes a paso constante (marcas de recepción corregidas).
    assert prepared.report['rejilla_tiempo'] == 'uniform' and len(prepared.t) == len(frame)
    assert np.allclose(np.diff(prepared.t), 0.01, atol=1e-4) and prepared.data['Ay'].equals(frame['Ay'].reset_index(drop=True))
    assert prepared.report['duracion_real_seg'] == pytest.approx(20, abs=0.02)
    received = GaitMetricsCalculator(ProcessingConfig(time_grid='received')).preparar(frame)
    assert received.report['rejilla_tiempo'] == 'received' and np.diff(received.t).min() < 0.005
    # La altura se integra sobre la rejilla: con marcas en ráfagas da lo mismo que con marcas regulares.
    regular = CALC.calcular_altura_zancada(walk(fs=100.0))['step_height_cm']
    assert CALC.calcular_altura_zancada(prepared)['step_height_cm'] == pytest.approx(regular, rel=0.02)
    assert not CALC.preparar(walk(fs=50.0)).report['fs_desviada']


def test_uniform_grid_keeps_gaps_and_never_overlaps_segments():
    frame = walk(seconds=30, fs=50.0)
    seconds = (frame['_time'] - frame['_time'].iloc[0]).dt.total_seconds()
    gapped = frame[(seconds < 10) | (seconds >= 20)]
    prepared = CALC.preparar(gapped)
    assert prepared.report['n_huecos'] == 1 and prepared.report['tiempo_huecos_seg'] == pytest.approx(10, abs=0.05)
    assert np.all(np.diff(prepared.t) > 0) and prepared.t[-1] == pytest.approx(29.98, abs=0.03)
    with pytest.raises(ConfigError):
        ProcessingConfig.from_mapping({'time_grid': 'otra'})


# --- LR-13 / LR-14 / LR-29: configuración tipada y transmitida --------------------------

def test_float_fs_from_yaml_works_in_every_method(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({
        'influxdb': {'url': 'https://example.test:8086', 'org': 'o', 'bucket': 'b', 'token': 't'},
        'processing': {'fs': 50, 'stride_k': 40.0},
    }))
    config = ProcessingConfig.from_mapping(load_config(path)['processing'])
    assert isinstance(config.fs, float) and config.fs == 50.0 and config.stride_k == 40.0
    calculator = GaitMetricsCalculator(config)
    frame = walk(fs=50.0)
    assert calculator.calcular_longitud_zancada(frame)['status'] in ('OK', 'WARNING')
    assert np.isfinite(calculator.calcular_longitud_zancada(frame)['stride_length_raw_cm'])
    assert calculator.seconds_to_samples(2.0, 50.0) == 100 and isinstance(calculator.seconds_to_samples(0.4, 99.7), int)
    assert config.to_record()['stride_k'] == 40.0


@pytest.mark.parametrize('section', [
    {'accel_cutoff_hz': 60.0}, {'fs': 8.0, 'gyro_cutoff_hz': 5.0}, {'filter_order': 0}, {'min_coverage': 1.5},
    {'cadence_min': 300.0}, {'decay_block_s': 100.0}, {'desconocido': 1}, {'step_modality': 'otra'},
    {'turn_axis': 'Qz'}, {'filter_order': 2.5},
])
def test_processing_config_rejects_invalid_or_contradictory_values(section):
    with pytest.raises(ConfigError):
        ProcessingConfig.from_mapping(section)


def test_cutoff_above_effective_nyquist_is_reported():
    result = CALC.calcular_cadencia(walk(fs=8.0))
    assert result['status'] == 'ERROR' and result['error_reason'] == 'ERR_CUTOFF_ABOVE_NYQUIST'


# --- LR-15: valores ausentes -----------------------------------------------------------

def test_missing_values_are_measured_not_replaced_by_zero():
    frame = walk()
    frame['Ay'] = frame['Ay'].astype(object)
    frame.loc[100:104, 'Ay'] = 'texto'        # 0,05 s: se interpola
    frame.loc[500:799, 'Ay'] = np.nan          # 3 s: corta el segmento
    prepared = CALC.preparar(frame)
    assert prepared.report['muestras_interpoladas'] == 5
    assert prepared.report['cobertura']['Ay'] == pytest.approx(1 - 305 / 2000)
    assert prepared.data['Ay'].isna().sum() == 300 and not (prepared.data['Ay'] == 0).iloc[500:800].any()
    result = CALC.calcular_cadencia(prepared)
    assert result['tiempo_analizado_seg'] == pytest.approx(17, abs=0.1)

    absent = walk()
    absent['Ay'] = np.nan
    absent['S0'] = np.nan
    result = CALC.calcular_cadencia(absent)
    assert result['status'] == 'ERROR' and 'LOW_COVERAGE' in result['error_reason'] and np.isnan(result['cadencia'])
    assert np.isnan(CALC.calcular_longitud_zancada(absent)['stride_length_cm'])


# --- LR-16 / LR-17: giros ---------------------------------------------------------------

def test_trial_made_only_of_turns_reports_no_steady_gait():
    result = CALC.calcular_cadencia(walk(gz=80.0))
    assert result['status'] == 'WARNING' and result['error_reason'] == 'ERR_NO_STEADY_GAIT'
    assert np.isnan(result['cadencia']) and np.isnan(result['num_pasos'])
    assert result['tiempo_giros_seg'] == pytest.approx(20, abs=0.1) and result['n_giros'] == 1


def test_turns_are_removed_without_compacting_the_signal():
    frame = walk(seconds=30)
    seconds = (frame['_time'] - frame['_time'].iloc[0]).dt.total_seconds()
    frame.loc[(seconds >= 10) & (seconds < 20), 'Gz'] = 80.0
    result = CALC.calcular_cadencia(frame)
    assert result['n_giros'] == 1 and result['tiempo_giros_seg'] == pytest.approx(10, abs=0.3)
    assert result['tiempo_analizado_seg'] == pytest.approx(20, abs=0.3)
    assert result['cadencia'] == pytest.approx(60, abs=4)
    # Sin excluir giros (TUG) se analiza todo y no se informa exposición a giros.
    full = CALC.calcular_cadencia(frame, excluir_giros=False)
    assert full['tiempo_giros_seg'] == 0.0 and full['tiempo_analizado_seg'] == pytest.approx(30, abs=0.1)


def test_short_gyro_bursts_are_not_turns():
    # El balanceo supera el umbral durante 0,3 s en cada ciclo: no es un giro.
    frame = walk(seconds=30)
    phase = ((frame['_time'] - frame['_time'].iloc[0]).dt.total_seconds()) % 1.0
    frame.loc[phase < 0.3, 'Gz'] = 120.0
    result = CALC.calcular_cadencia(frame)
    assert result['n_giros'] == 0 and result['n_giros_descartados'] == 30 and result['tiempo_giros_seg'] == 0.0
    assert result['cadencia'] == pytest.approx(60, abs=1)
    legacy = GaitMetricsCalculator(ProcessingConfig(turn_min_duration_s=0.0)).calcular_cadencia(frame)
    assert legacy['n_giros'] == 30 and legacy['tiempo_giros_seg'] > 5


# --- LR-20 / LR-21: detección y modalidad -----------------------------------------------

def test_step_distance_is_in_seconds_and_modality_is_reported():
    fast = walk(step_hz=2.0)
    assert CALC.calcular_cadencia(fast)['cadencia'] == pytest.approx(120, abs=3)
    result = CALC.calcular_cadencia(walk())
    assert result['modalidad'] == 'accel' and result['pasos_accel'] == result['num_pasos']
    assert result['metodo_filtro'] == 'butterworth'

    pressure_only = walk().drop(columns=['Ay'])
    pressure_only['S0'] = 300 + 250 * walk()['Ay']  # un pico suave de 550 > 200 por ciclo
    result = CALC.calcular_cadencia(pressure_only)
    assert result['status'] == 'OK' and result['modalidad'] == 'pressure'
    assert result['cadencia'] == pytest.approx(60, abs=4) and np.isnan(result['pasos_accel'])

    # Regla única de rescate, sin depender de la duración: aceleración sin picos y presión con ellos.
    weak = walk()
    weak['S0'] = 300 + 250 * weak['Ay']
    weak['Ay'] *= 0.01
    result = CALC.calcular_cadencia(weak)
    assert result['modalidad'] == 'pressure' and result['pasos_accel'] == 0 and result['num_pasos'] == result['pasos_presion']
    forced = GaitMetricsCalculator(ProcessingConfig(step_modality='accel')).calcular_cadencia(weak)
    assert forced['modalidad'] == 'accel' and forced['num_pasos'] == 0


# --- LR-23: fases por ciclo -------------------------------------------------------------

def test_stance_is_measured_per_complete_cycle_and_excludes_rest():
    result = CALC.calcular_fases(walk())
    assert result['apoyo_prc'] == pytest.approx(60, abs=2) and result['n_ciclos'] >= 17
    # Diez segundos de reposo en apoyo no cambian el porcentaje por ciclo.
    rest = walk(seconds=10, step_hz=1.0, start='2025-01-01 10:00:20')
    rest[['S0', 'S1', 'S2']] = 100.0
    rest['Ay'] = 0.0
    with_rest = CALC.calcular_fases(pd.concat([walk(), rest], ignore_index=True))
    assert with_rest['apoyo_prc'] == pytest.approx(60, abs=2)
    # La polaridad es un parámetro explícito.
    inverted = GaitMetricsCalculator(ProcessingConfig(pressure_stance_is_low=False))
    assert inverted.calcular_fases(walk())['apoyo_prc'] == pytest.approx(40, abs=2)
    flat = walk()
    flat[['S0', 'S1', 'S2']] = 100.0
    assert CALC.calcular_fases(flat)['error_reason'] == 'ERR_FLAT_PRESSURE'


# --- LR-24: decaimiento -----------------------------------------------------------------

def test_decay_compares_time_blocks_and_requires_duration():
    short = CALC.calcular_decaimiento_6mwt(walk(seconds=10))
    assert short['status'] == 'WARNING' and short['error_reason'] == 'ERR_TOO_SHORT_FOR_DECAY'
    assert np.isnan(short['6mwt_decaimiento'])

    fast = walk(seconds=120, step_hz=1.0)
    slow = walk(seconds=120, step_hz=0.8, start='2025-01-01 10:02:00')
    result = CALC.calcular_decaimiento_6mwt(pd.concat([fast, slow], ignore_index=True))
    assert result['status'] == 'OK' and result['6mwt_decaimiento'] == pytest.approx(20, abs=3)
    assert result['cadencia_inicial'] == pytest.approx(60, abs=2) and result['cadencia_final'] == pytest.approx(48, abs=2)

    # Un último bloque sin señal válida no se convierte en un decaimiento del 100 %.
    broken = pd.concat([fast, slow], ignore_index=True)
    broken.loc[broken.index[-7000:], ['Ay', 'S0']] = np.nan
    result = CALC.calcular_decaimiento_6mwt(broken)
    assert result['status'] == 'WARNING' and 'ERR_INVALID_BLOCK_FINAL' in result['error_reason']
    assert np.isnan(result['6mwt_decaimiento'])


# --- LR-25: longitud sin recorte --------------------------------------------------------

def test_stride_length_keeps_unclipped_value_and_is_flagged_uncalibrated():
    strong = walk()
    strong['Ay'] *= 50
    result = CALC.calcular_longitud_zancada(strong)
    assert result['stride_length_cm'] == 160.0 and result['stride_length_raw_cm'] > 160.0
    assert result['calibrado'] is False and result['status'] == 'WARNING'


# --- LR-27 / LR-28: altura por balanceo y filtro declarado ------------------------------

def test_height_keeps_every_swing_with_its_exclusion_reason():
    frame = walk(seconds=20.75)       # el registro termina en mitad de un balanceo
    frame['Ay'] = frame['Ay'].astype(str)
    result = CALC.calcular_altura_zancada(frame)
    events = pd.DataFrame(result['eventos'])
    assert result['n_balanceos'] == 20 and result['n_balanceos_excluidos'] == 1
    assert events['motivo_exclusion'].iloc[-1] == 'balanceo_incompleto' and np.isnan(events['altura_cm'].iloc[-1])
    assert events['duracion_s'].iloc[0] == pytest.approx(0.4, abs=0.02)
    assert np.isfinite(result['step_height_cm'])

    slow = CALC.calcular_altura_zancada(walk(step_hz=0.2))  # balanceos de 2 s
    assert slow['error_reason'] == 'ERR_NO_VALID_SWING_PHASES'
    assert 'duracion_fuera_de_rango' in set(pd.DataFrame(slow['eventos'])['motivo_exclusion'])
    assert slow['n_balanceos'] == 0


def test_short_segment_filter_fallback_is_declared():
    result = CALC.calcular_cadencia(walk(seconds=0.1))
    assert result['metodo_filtro'] == 'media_movil'
    # La ventana de presión dura lo mismo a 50 y a 100 Hz.
    assert CALC.seconds_to_samples(CALC.config.pressure_window_s, 100) == 15
    assert CALC.seconds_to_samples(CALC.config.pressure_window_s, 50) == 8
