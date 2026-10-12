"""Contratos offline de los algoritmos (LR-12 a LR-29) con las decisiones ya tomadas sobre sensores."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pkgs'), str(ROOT / 'src'), str(ROOT)]
from gait_analysis.metrics import GaitMetricsCalculator, ProcessingConfig
from msConfig import ConfigError, load_config


def walk(seconds=20.0, fs=100.0, step_hz=1.0, gy=0.0, start='2025-01-01 10:00:00', stance=0.6,
         forefoot_shift=0.0, tilt_deg=0.0, accel_amp=0.3, stride_m=0.0, clearance_cm=None):
    """Marcha sintética físicamente coherente.

    Talón (S2) cargado (presión baja) durante ``stance`` del ciclo; antepié (S0, S1) igual pero
    desplazado ``forefoot_shift`` ciclos. Aceleración vertical de 1 g más una oscilación por
    zancada, con el módulo inclinado ``tilt_deg`` respecto a Ay. ``gy`` es la velocidad de giro
    en torno a la vertical. ``stride_m`` es el avance del pie en cada balanceo, generado con una
    aceleración horizontal (eje Az) que arranca y frena. Con ``clearance_cm`` el pie sigue en cada
    balanceo una trayectoria vertical con dos máximos y un mínimo intermedio de esa altura (el
    máximo es 1,5625 veces mayor). Las lecturas cambian en cada muestra, como en los sensores reales.
    """
    n = int(round(seconds * fs))
    t = np.arange(n) / fs
    flicker = np.arange(n) % 2
    heel = np.where((t * step_hz) % 1.0 < stance, 100.0, 300.0) + flicker
    fore = np.where((t * step_hz - forefoot_shift) % 1.0 < stance, 100.0, 300.0) + flicker
    vertical = 1.0 + accel_amp * np.sin(2 * np.pi * step_hz * t)
    tilt = np.radians(tilt_deg)
    turn = np.broadcast_to(np.asarray(gy, dtype=float), (n,)) + 0.01 * flicker
    phase = (t * step_hz) % 1.0
    swing_s = (1.0 - stance) / step_hz
    amplitude = stride_m * 2 * np.pi / (9.81 * swing_s ** 2) if swing_s > 0 else 0.0   # en g
    progress = np.clip((phase - stance) / max(1.0 - stance, 1e-9), 0.0, 1.0)
    forward = np.where(phase >= stance, amplitude * np.sin(2 * np.pi * progress), 0.0)
    if clearance_cm is not None:
        u = np.sin(np.pi * progress) ** 2
        height = (clearance_cm / 100 / 0.2) * u * (1 - 0.8 * u)            # metros; mínimo intermedio = clearance_cm
        vertical = 1.0 + np.gradient(np.gradient(height, 1 / fs), 1 / fs) / 9.81
    return pd.DataFrame({
        '_time': pd.Timestamp(start) + pd.to_timedelta(t, unit='s'),
        'Ax': vertical * np.sin(tilt), 'Ay': vertical * np.cos(tilt), 'Az': forward,
        'Gx': turn * np.sin(tilt), 'Gy': turn * np.cos(tilt), 'Gz': 0.0,
        'S0': fore, 'S1': fore, 'S2': heel,
    })


def seconds_of(frame):
    return (frame['_time'] - frame['_time'].iloc[0]).dt.total_seconds()


CALC = GaitMetricsCalculator()


# --- LR-12: el tiempo real manda, no el número de muestras ------------------------------

def test_cadence_uses_real_time_not_sample_count():
    assert CALC.calcular_cadencia(walk(fs=100.0))['cadencia_zancada'] == pytest.approx(60, abs=1)
    # Misma marcha muestreada a 50 Hz: mismas zancadas por minuto.
    half = CALC.calcular_cadencia(walk(fs=50.0))
    assert half['cadencia_zancada'] == pytest.approx(60, abs=1) and half['fs_efectiva'] == pytest.approx(50)
    # Mismas muestras con el doble de duración real: la cadencia se reduce a la mitad.
    slow = walk(fs=100.0)
    slow['_time'] = slow['_time'].iloc[0] + (slow['_time'] - slow['_time'].iloc[0]) * 2
    result = CALC.calcular_cadencia(slow)
    assert result['cadencia_zancada'] == pytest.approx(30, abs=1) and result['duracion_real_seg'] == pytest.approx(40, abs=0.1)
    assert result['tiempo_zancada_seg'] == pytest.approx(2.0, abs=0.03)


def test_missing_time_column_is_an_error():
    for method in (CALC.calcular_cadencia, CALC.calcular_fases, CALC.calcular_longitud_zancada, CALC.calcular_altura_zancada):
        result = method(walk().drop(columns=['_time']))
        assert result['status'] == 'ERROR' and result['error_reason'] == 'ERR_MISSING_TIME'


def test_gaps_split_segments_and_are_not_counted_as_walking():
    frame = walk(seconds=30)
    gapped = frame[(seconds_of(frame) < 10) | (seconds_of(frame) >= 20)]
    prepared = CALC.preparar(gapped)
    assert prepared.report['n_huecos'] == 1 and prepared.report['n_segmentos'] == 2
    assert prepared.report['tiempo_huecos_seg'] == pytest.approx(10, abs=0.05)
    result = CALC.calcular_cadencia(prepared)
    assert result['tiempo_analizado_seg'] == pytest.approx(20, abs=0.1) and result['n_tramos'] == 2
    assert result['cadencia_zancada'] == pytest.approx(60, abs=1)
    # Ninguna zancada cruza el hueco: 9 contactos y 8 zancadas por tramo.
    assert result['num_zancadas'] == 16 and result['zancadas_en_banda_prc'] == 100.0


def test_bursty_timestamps_do_not_create_gaps_or_a_false_rate():
    # Datos reales: dos muestras casi juntas cada 20 ms (tasa media de 100 Hz).
    frame = walk(fs=100.0)
    offsets = np.where(np.arange(len(frame)) % 2 == 1, -8, 0)
    frame['_time'] = frame['_time'] + pd.to_timedelta(offsets, unit='ms')
    prepared = CALC.preparar(frame)
    assert prepared.report['fs_efectiva'] == pytest.approx(100, abs=0.5) and prepared.report['n_huecos'] == 0
    assert prepared.report['irregularidad_tiempo'] > 0.9 and prepared.report['fs_desviada']   # nominal: 50 Hz
    assert CALC.calcular_cadencia(prepared)['cadencia_zancada'] == pytest.approx(60, abs=1)
    # Rejilla uniforme: mismas filas y valores, instantes a paso constante (marcas de recepción corregidas).
    assert prepared.report['rejilla_tiempo'] == 'uniform' and len(prepared.t) == len(frame)
    assert np.allclose(np.diff(prepared.t), 0.01, atol=1e-4) and np.allclose(prepared.data['Ay'], frame['Ay'])
    assert prepared.report['duracion_real_seg'] == pytest.approx(20, abs=0.02)
    received = GaitMetricsCalculator(ProcessingConfig(time_grid='received')).preparar(frame)
    assert received.report['rejilla_tiempo'] == 'received' and np.diff(received.t).min() < 0.005
    # La integración usa la rejilla: con marcas en ráfagas la longitud es la misma que con marcas regulares.
    bursty = walk(fs=100.0, stride_m=1.0)
    bursty['_time'] = bursty['_time'] + pd.to_timedelta(offsets, unit='ms')
    regular = CALC.calcular_longitud_zancada(walk(fs=100.0, stride_m=1.0))['stride_length_cm']
    assert CALC.calcular_longitud_zancada(bursty)['stride_length_cm'] == pytest.approx(regular, rel=0.02)
    assert not CALC.preparar(walk(fs=50.0)).report['fs_desviada']


def test_uniform_grid_keeps_gaps_and_never_overlaps_segments():
    frame = walk(seconds=30, fs=50.0)
    gapped = frame[(seconds_of(frame) < 10) | (seconds_of(frame) >= 20)]
    prepared = CALC.preparar(gapped)
    assert prepared.report['n_huecos'] == 1 and prepared.report['tiempo_huecos_seg'] == pytest.approx(10, abs=0.05)
    assert np.all(np.diff(prepared.t) > 0) and prepared.t[-1] == pytest.approx(29.98, abs=0.03)


# --- LR-13 / LR-14 / LR-29: configuración tipada y transmitida --------------------------

def test_integer_values_from_yaml_work_in_every_method(tmp_path):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({
        'influxdb': {'url': 'https://example.test:8086', 'org': 'o', 'bucket': 'b', 'token': 't'},
        'processing': {'fs': 50, 'turn_threshold': 30, 'sixmwt_duration_s': 360, 'cycle_max_s': 3},
    }))
    config = ProcessingConfig.from_mapping(load_config(path, sections=())['processing'])
    assert isinstance(config.fs, float) and config.fs == 50.0 and config.turn_threshold == 30.0 and isinstance(config.cycle_max_s, float)
    calculator = GaitMetricsCalculator(config)
    assert np.isfinite(calculator.calcular_longitud_zancada(walk(fs=50.0, stride_m=1.0))['stride_length_cm'])
    assert calculator.seconds_to_samples(2.0, 50.0) == 100 and isinstance(calculator.seconds_to_samples(0.4, 99.7), int)
    assert config.to_record()['sixmwt_duration_s'] == 360.0 and config.to_record()['heel_channel'] == 'S2'


@pytest.mark.parametrize('section', [
    {'accel_cutoff_hz': 60.0}, {'fs': 5.0}, {'filter_order': 0}, {'min_coverage': 1.5}, {'cadence_min': 300.0},
    {'decay_block_s': 100.0}, {'desconocido': 1}, {'turn_axis': 'Qz'}, {'filter_order': 2.5}, {'time_grid': 'otra'},
    {'heel_channel': 'Ay'}, {'max_interpolation_s': 0.5}, {'swing_max_prc': 120.0}, {'min_valid_strides': 1.5},
    {'cycle_min_s': 3.0}, {'swing_min_s': 1.0}, {'rest_gyro_quantile': 2.0}, {'max_gap_s': 0.01},
    {'turn_extend_threshold': 40.0}, {'sixmwt_min_valid_minutes': 7}, {'stride_k': 50.0}, {'min_valid_strides': 2},
    {'min_strides': {'6MWT': 0}}, {'min_strides': [30]}, {'heel_min_periodicity': 1.5}, {'mtc_ok_min_cm': 9.0},
])
def test_processing_config_rejects_invalid_or_contradictory_values(section):
    with pytest.raises(ConfigError):
        ProcessingConfig.from_mapping(section)


def test_cutoff_above_effective_nyquist_is_reported():
    wide_gaps = GaitMetricsCalculator(ProcessingConfig(max_gap_s=2.0))
    result = wide_gaps.calcular_cadencia(walk(seconds=200, fs=0.8))      # Nyquist de 0,4 Hz: por debajo del suavizado de giros
    assert result['status'] == 'ERROR' and result['error_reason'] == 'ERR_CUTOFF_ABOVE_NYQUIST'


# --- LR-15: huecos por grupo de sensores, medidos sobre valores nuevos ------------------

IMU = ['Ax', 'Ay', 'Az', 'Gx', 'Gy', 'Gz']


def test_pressure_gaps_are_measured_on_new_values():
    frame = walk()
    pressure = ['S0', 'S1', 'S2']
    frame.loc[101:105, pressure] = frame.loc[100, pressure].to_numpy()      # 0,05 s sin valor nuevo: se interpola
    frame.loc[501:549, pressure] = frame.loc[500, pressure].to_numpy()      # 0,5 s sin valor nuevo: hueco
    prepared = CALC.preparar(frame)
    report = prepared.report
    assert report['huecos_por_grupo']['presion'] == 1 and report['cobertura']['presion'] == pytest.approx(1 - 49 / 2000, abs=1e-3)
    assert prepared.data['S2'].iloc[501:550].isna().all() and report['muestras_interpoladas'] >= 5
    expected = np.interp(prepared.t[101:106], prepared.t[[100, 106]], prepared.data['S2'].iloc[[100, 106]])
    assert np.allclose(prepared.data['S2'].iloc[101:106], expected)
    # La zancada que atraviesa el hueco queda fuera.
    assert CALC.calcular_cadencia(prepared)['num_zancadas'] == 16


def test_repeated_imu_with_the_foot_still_is_a_valid_measurement():
    frame = walk()
    frame.loc[515:545, IMU] = frame.loc[514, IMU].to_numpy()                # 0,3 s repetidos en pleno apoyo
    prepared = CALC.preparar(frame)
    assert prepared.report['huecos_por_grupo']['imu'] == 0 and prepared.report['cobertura']['imu'] == 1.0
    assert prepared.report['imu_valida'] and prepared.report['imu_repeticion_en_movimiento_prc'] == 0.0
    assert not prepared.data['Ay'].isna().any()


def test_repeated_imu_while_the_foot_moves_is_a_gap_and_too_many_invalidate_the_imu():
    frame = walk(stride_m=1.0)
    frame['Gx'] = np.where((seconds_of(frame) % 1.0) >= 0.6, 200.0, 0.0) + 0.01 * (np.arange(len(frame)) % 2)   # el pie gira deprisa en el balanceo
    few = frame.copy()
    few.loc[666:690, IMU] = few.loc[665, IMU].to_numpy()                   # 0,25 s repetidos en un balanceo
    prepared = CALC.preparar(few)
    assert prepared.report['huecos_por_grupo']['imu'] == 1 and prepared.data['Ay'].iloc[666:691].isna().all()
    assert prepared.report['imu_valida'] and 0 < prepared.report['imu_repeticion_en_movimiento_prc'] < 10
    # La zancada con el hueco no se integra; las demás sí.
    assert CALC.calcular_longitud_zancada(prepared)['n_zancadas_longitud'] == 18
    # Si la IMU se repite en más del 10 % de las filas en movimiento, no vale para nada que dependa de ella.
    many = frame.copy()
    for start in range(60, 2000, 100):
        many.loc[start + 1:start + 12, IMU] = many.loc[start, IMU].to_numpy()
    prepared = CALC.preparar(many)
    assert not prepared.report['imu_valida'] and prepared.report['imu_repeticion_en_movimiento_prc'] > 10
    for method in (CALC.calcular_longitud_zancada, CALC.calcular_altura_zancada):
        assert method(prepared)['error_reason'] == 'ERR_IMU_STALE'
    # La cadencia sale de la presión y no se ve afectada.
    assert CALC.calcular_cadencia(prepared)['cadencia_zancada'] == pytest.approx(60, abs=1)


def test_delayed_reception_is_recovered_and_real_gaps_are_not():
    frame = walk(seconds=30)
    seconds = seconds_of(frame)
    # El móvil se queda 0,5 s sin recibir y luego llegan todas las muestras juntas: no falta ninguna.
    late = frame.copy()
    stalled = (seconds > 10.0) & (seconds <= 10.5)
    late.loc[stalled, '_time'] = late['_time'].iloc[0] + pd.to_timedelta(10.5 + (seconds[stalled] - 10.0) * 0.02, unit='s')
    late = late.sort_values('_time', kind='stable').reset_index(drop=True)
    prepared = CALC.preparar(late)
    assert prepared.report['intervalos_recuperados'] == 1 and prepared.report['n_huecos'] == 0 and prepared.report['n_segmentos'] == 1
    assert np.allclose(np.diff(prepared.t), 0.01, atol=1e-3)
    assert CALC.calcular_cadencia(prepared)['num_zancadas'] == 28
    # Si en ese medio segundo las muestras se pierden, es un hueco real.
    lost = frame[~stalled]
    prepared = CALC.preparar(lost)
    assert prepared.report['intervalos_recuperados'] == 0 and prepared.report['n_huecos'] == 1
    assert CALC.calcular_cadencia(prepared)['num_zancadas'] == 26


def test_missing_or_text_values_never_become_zero_and_low_coverage_is_rejected():
    frame = walk()
    frame['S2'] = frame['S2'].astype(object)
    frame.loc[100:104, 'S2'] = 'texto'                                # 0,05 s: se interpola
    prepared = CALC.preparar(frame)
    assert prepared.report['cobertura']['presion'] == 1.0 and not (prepared.data['S2'] == 0).any()

    poor = walk()
    poor.loc[400:999, ['S0', 'S1', 'S2']] = np.nan                    # 6 s de 20 sin presión: 70 % de cobertura
    result = CALC.calcular_cadencia(poor)
    assert result['status'] == 'ERROR' and 'ERR_LOW_COVERAGE' in result['error_reason'] and np.isnan(result['cadencia_zancada'])
    assert CALC.preparar(poor).report['cobertura']['presion'] == pytest.approx(0.7, abs=0.001)
    # La longitud necesita los apoyos, que salen de la presión: tampoco se informa.
    assert 'ERR_LOW_COVERAGE' in CALC.calcular_longitud_zancada(poor)['error_reason']
    absent = walk()
    absent[['Ax', 'Ay', 'Az']] = np.nan
    assert np.isnan(CALC.calcular_longitud_zancada(absent)['stride_length_cm'])


# --- Orientación: vertical estimada con la gravedad en reposo ---------------------------

def test_vertical_is_estimated_from_gravity_whatever_the_sock_tilt():
    straight, tilted = CALC.preparar(walk()), CALC.preparar(walk(tilt_deg=45))
    assert straight.report['vertical_estimada'] and straight.report['inclinacion_grados'] == pytest.approx(0, abs=1)
    assert tilted.report['inclinacion_grados'] == pytest.approx(45, abs=1)
    assert np.allclose(tilted.data['Av'], straight.data['Av'], atol=1e-6)      # misma aceleración vertical
    assert not np.allclose(tilted.data['Ay'], straight.data['Ay'], atol=0.05)  # Ay sola mezcla componentes
    same = CALC.calcular_longitud_zancada(walk(stride_m=1.0))['stride_length_cm']
    assert CALC.calcular_longitud_zancada(walk(stride_m=1.0, tilt_deg=45))['stride_length_cm'] == pytest.approx(same, rel=1e-3)
    assert CALC.calcular_altura_zancada(walk(stride_m=1.0, clearance_cm=3.0, tilt_deg=45))['excursion_tobillo_cm'] == pytest.approx(
        CALC.calcular_altura_zancada(walk(stride_m=1.0, clearance_cm=3.0))['excursion_tobillo_cm'], rel=1e-3)
    # Sin reposo utilizable (módulo lejos de 1 g) se usan Ay y Gy y se declara.
    shaken = walk()
    shaken['Ay'] = shaken['Ay'] + 3.0
    prepared = CALC.preparar(shaken)
    assert not prepared.report['vertical_estimada'] and np.allclose(prepared.data['Av'], prepared.data['Ay'])


# --- LR-16 / LR-17 / LR-18: giros por ángulo acumulado ----------------------------------

def test_turn_is_detected_by_accumulated_angle_and_removed_without_compacting():
    frame = walk(seconds=30)
    t = seconds_of(frame).to_numpy()
    frame['Gy'] = np.where((t >= 12) & (t < 15), 60.0, 0.0) + 0.01 * (np.arange(len(frame)) % 2)   # 180° en 3 s
    result = CALC.calcular_cadencia(frame)
    assert result['n_giros'] == 1 and result['angulo_giros_grados'] == pytest.approx(180, abs=15)
    # El episodio se extiende hasta que el giro baja de 10 grados/s: dura algo más que su núcleo.
    assert result['tiempo_giros_seg'] == pytest.approx(3.5, abs=0.6) and result['n_tramos'] == 2
    assert result['tiempo_analizado_seg'] == pytest.approx(26.5, abs=0.6)
    assert result['cadencia_zancada'] == pytest.approx(60, abs=1)
    # Sin excluir giros (TUG) se analiza todo y no se informa exposición a giros.
    full = CALC.calcular_cadencia(frame, excluir_giros=False)
    assert full['tiempo_giros_seg'] == 0.0 and full['tiempo_analizado_seg'] == pytest.approx(30, abs=0.1)


def test_swing_oscillation_and_small_heading_changes_are_not_turns():
    frame = walk(seconds=30)
    t = seconds_of(frame).to_numpy()
    flicker = 0.01 * (np.arange(len(frame)) % 2)
    # El balanceo hace oscilar el giro en cada zancada con picos altos, pero sin cambio neto de rumbo.
    frame['Gy'] = 150.0 * np.sin(2 * np.pi * 1.0 * t) + flicker
    swinging = CALC.calcular_cadencia(frame)
    assert swinging['n_giros'] == 0 and swinging['tiempo_giros_seg'] == 0.0 and swinging['cadencia_zancada'] == pytest.approx(60, abs=1)
    # Un cambio de rumbo de 40° supera el umbral de velocidad pero no el de ángulo.
    frame['Gy'] = np.where((t >= 10) & (t < 11), 40.0, 0.0) + flicker
    small = CALC.calcular_cadencia(frame)
    assert small['n_giros'] == 0 and small['n_giros_descartados'] == 1


def test_trial_made_only_of_turns_reports_no_steady_gait():
    result = CALC.calcular_cadencia(walk(gy=60.0))
    assert result['status'] == 'WARNING' and result['error_reason'] == 'ERR_NO_STEADY_GAIT'
    assert np.isnan(result['cadencia_zancada']) and np.isnan(result['num_zancadas'])
    assert result['tiempo_giros_seg'] == pytest.approx(20, abs=0.1) and result['n_giros'] == 1


def test_turn_axis_follows_the_vertical_when_the_sock_is_tilted():
    frame = walk(seconds=30, tilt_deg=45)
    t = seconds_of(frame).to_numpy()
    turn = np.where((t >= 12) & (t < 15), 60.0, 0.0) + 0.01 * (np.arange(len(frame)) % 2)
    frame['Gx'], frame['Gy'] = turn * np.sin(np.radians(45)), turn * np.cos(np.radians(45))
    assert CALC.calcular_cadencia(frame)['angulo_giros_grados'] == pytest.approx(180, abs=15)
    only_gy = GaitMetricsCalculator(ProcessingConfig(turn_axis='Gy')).calcular_cadencia(frame)
    assert only_gy['angulo_giros_grados'] == pytest.approx(127, abs=15)          # 180 · cos 45°: se queda corto


# --- LR-19 / LR-20 / LR-21: contacto de talón por S2 ------------------------------------

def test_heel_contacts_define_strides_within_the_valid_band():
    result = CALC.calcular_cadencia(walk())
    assert result['modalidad'] == 'S2' and result['contactos_talon'] == 19 and result['num_zancadas'] == 18
    assert result['tiempo_zancada_seg'] == pytest.approx(1.0, abs=0.01) and result['status'] == 'OK'
    # Dos contactos nunca están a menos de 0,8 s: a 2 ciclos por segundo solo se acepta uno de cada dos.
    fast = CALC.calcular_cadencia(walk(step_hz=2.0))
    assert fast['contactos_talon'] <= 20 and fast['tiempo_zancada_seg'] >= 0.8 - 0.005
    # Un ritmo fuera de la banda de zancada (ciclos de 3,3 s) no es modulación de marcha.
    slow = CALC.calcular_cadencia(walk(seconds=40, step_hz=0.3))
    assert slow['error_reason'].startswith('ERR_NO_GAIT_MODULATION') and np.isnan(slow['cadencia_zancada'])
    # El mínimo de zancadas lo fija quien llama (cada prueba tiene el suyo); sin mínimo se informa con una.
    short = CALC.calcular_cadencia(walk(seconds=2.5), min_strides=2)
    assert short['error_reason'] == 'ERR_TOO_FEW_STRIDES' and short['num_zancadas'] == 1
    assert CALC.calcular_cadencia(walk(seconds=2.5))['status'] == 'OK'
    # Arranque: se descartan las primeras zancadas válidas que se pidan.
    assert CALC.calcular_cadencia(walk(), skip_first=1)['num_zancadas'] == 17
    assert CALC.calcular_fases(walk(), skip_first=1)['n_ciclos'] == 17


def test_signal_quality_is_checked_before_detecting_and_never_switches_modality():
    rng = np.random.default_rng(0)
    flat = walk()
    flat['S2'] = 200.0 + rng.integers(0, 3, len(flat))                 # talón sin modulación: solo ruido
    result = CALC.calcular_cadencia(flat)
    # La IMU es perfecta, pero el talón no sirve: no se cuenta con otra señal.
    assert result['error_reason'] == 'ERR_NO_GAIT_MODULATION: presión sin modulación de marcha'
    assert np.isnan(result['num_zancadas']) and result['modalidad'] == 'S2'
    report = CALC.preparar(flat)
    CALC._contact(report)
    assert report.report['criterio_presion'] == 'periodicidad' and report.report['periodicidad_talon'] < 0.3
    # El criterio no depende de la escala: una señal pequeña con ritmo claro sirve; una grande sin ritmo, no.
    small = walk()
    small[['S0', 'S1', 'S2']] = 100.0 + (small[['S0', 'S1', 'S2']] - 100.0) * 0.15      # rango de 30 unidades
    assert CALC.calcular_cadencia(small)['cadencia_zancada'] == pytest.approx(60, abs=1)
    big_noise = walk()
    big_noise['S2'] = 200.0 + rng.integers(0, 60, len(big_noise))
    assert CALC.calcular_cadencia(big_noise)['error_reason'].startswith('ERR_NO_GAIT_MODULATION')
    # En una ventana demasiado corta para medir el ritmo decide el rango mínimo de 40.
    tiny = walk(seconds=4)
    tiny[['S0', 'S1', 'S2']] = 100.0 + (tiny[['S0', 'S1', 'S2']] - 100.0) * 0.15
    assert CALC.calcular_cadencia(tiny)['error_reason'].startswith('ERR_SHORT_WINDOW')
    assert CALC.calcular_cadencia(walk(seconds=4))['status'] == 'OK'
    missing = CALC.calcular_cadencia(walk().drop(columns=['S2']))
    assert missing['status'] == 'ERROR' and missing['error_reason'] == 'ERR_MISSING_S2'
    # Sin acelerómetro pero con presión válida se cuenta igual.
    no_imu = CALC.calcular_cadencia(walk().drop(columns=['Ax', 'Ay', 'Az', 'Gx', 'Gy', 'Gz']))
    assert no_imu['status'] == 'OK' and no_imu['num_zancadas'] == 18


# --- LR-22 / LR-23: fases por ciclo -----------------------------------------------------

def test_stance_is_measured_per_valid_cycle_and_excludes_rest():
    result = CALC.calcular_fases(walk())
    assert result['apoyo_prc'] == pytest.approx(60, abs=2) and result['n_ciclos'] == 18
    # Diez segundos de reposo en apoyo no cambian el porcentaje por ciclo.
    rest = walk(seconds=10, start='2025-01-01 10:00:20', stance=1.0)
    with_rest = CALC.calcular_fases(pd.concat([walk(), rest], ignore_index=True))
    assert with_rest['apoyo_prc'] == pytest.approx(60, abs=2)
    # El pie está en el suelo si el talón O el antepié están cargados.
    rolling = CALC.calcular_fases(walk(stance=0.5, forefoot_shift=0.2))
    assert rolling['apoyo_prc'] == pytest.approx(70, abs=2)


def test_cycles_outside_the_swing_limits_are_excluded():
    # Balanceo del 10 % del ciclo (0,1 s): fuera de 0,25-0,8 s y de 20-55 %.
    result = CALC.calcular_fases(walk(stance=0.9))
    assert result['error_reason'] == 'ERR_NO_VALID_CYCLES' and result['n_ciclos_excluidos'] == 18 and np.isnan(result['apoyo_prc'])
    # La polaridad es un parámetro explícito: invertida, el balanceo pasa a ser el 60 % y los ciclos no valen.
    inverted = GaitMetricsCalculator(ProcessingConfig(pressure_stance_is_low=False))
    assert inverted.calcular_fases(walk())['error_reason'] == 'ERR_NO_VALID_CYCLES'
    assert inverted.calcular_fases(walk(stance=0.45))['apoyo_prc'] == pytest.approx(55, abs=2)


# --- LR-24: decaimiento -----------------------------------------------------------------

def six_minute_walk(first_hz=1.0, second_hz=0.8):
    fast = walk(seconds=180, step_hz=first_hz)
    slow = walk(seconds=180, step_hz=second_hz, start='2025-01-01 10:03:00')
    return pd.concat([fast, slow], ignore_index=True)


def test_decay_needs_a_complete_six_minute_walk():
    frame = six_minute_walk()
    result = CALC.calcular_decaimiento_6mwt(frame, duracion_registro_s=360.0, inicio='2025-01-01 10:00:00')
    assert result['status'] == 'OK' and result['minutos_validos'] == 6 and result['ensayo_completo'] is True
    assert result['6mwt_decaimiento'] == pytest.approx(20, abs=1)
    assert result['cadencia_inicial'] == pytest.approx(60, abs=1) and result['cadencia_final'] == pytest.approx(48, abs=1)

    # La ventana del registro debe durar 360 ± 5 s.
    for registered in (120.0, 354.0, 366.0):
        short = CALC.calcular_decaimiento_6mwt(frame, duracion_registro_s=registered)
        assert np.isnan(short['6mwt_decaimiento']) and short['error_reason'].startswith('ERR_6MWT_VENTANA') and not short['ensayo_completo']
    assert CALC.calcular_decaimiento_6mwt(frame, duracion_registro_s=364.0)['status'] == 'OK'

    seconds = seconds_of(frame)
    pressure = ['S0', 'S1', 'S2']
    # Un minuto intermedio sin presión: quedan 5 minutos válidos con el primero y el último: se calcula.
    five = frame.copy()
    five.loc[(seconds >= 120) & (seconds < 180), pressure] = np.nan
    result = CALC.calcular_decaimiento_6mwt(five, duracion_registro_s=360.0)
    assert result['status'] == 'OK' and result['minutos_validos'] == 5 and result['6mwt_decaimiento'] == pytest.approx(20, abs=1)
    # Dos minutos sin presión: 6MWT incompleto, decaimiento vacío con su motivo.
    four = five.copy()
    four.loc[(seconds >= 180) & (seconds < 240), pressure] = np.nan
    result = CALC.calcular_decaimiento_6mwt(four, duracion_registro_s=360.0)
    assert np.isnan(result['6mwt_decaimiento']) and result['error_reason'] == 'ERR_6MWT_INCOMPLETO: 6MWT incompleto: 4 minutos válidos'
    assert result['minutos_validos'] == 4 and result['ensayo_completo'] is False
    # Cinco minutos válidos, pero falta el último: no se convierte en un decaimiento del 100 %.
    no_last = frame.copy()
    no_last.loc[seconds >= 300, pressure] = np.nan
    result = CALC.calcular_decaimiento_6mwt(no_last, duracion_registro_s=360.0)
    assert np.isnan(result['6mwt_decaimiento']) and 'falta el último' in result['error_reason'] and result['minutos_validos'] == 5
    # Los datos empiezan un minuto después del inicio del registro: falta el primero.
    late = CALC.calcular_decaimiento_6mwt(frame[seconds >= 60], duracion_registro_s=360.0, inicio='2025-01-01 10:00:00')
    assert 'falta el primero' in late['error_reason'] and late['minutos_validos'] == 5
    # Datos válidos pero el paciente se detiene en el último minuto: sin cadencia, sin decaimiento.
    stopped = frame.copy()
    for column in pressure:
        stopped.loc[seconds >= 300, column] = 100.0 + (np.arange(len(frame)) % 2)[(seconds >= 300).to_numpy()]
    result = CALC.calcular_decaimiento_6mwt(stopped, duracion_registro_s=360.0)
    assert result['minutos_validos'] == 6 and result['error_reason'].startswith('ERR_6MWT_SIN_CADENCIA') and np.isnan(result['6mwt_decaimiento'])


# --- LR-25: longitud de zancada por integración entre apoyos ----------------------------

def test_stride_length_comes_from_integration_between_stances_without_constants():
    result = CALC.calcular_longitud_zancada(walk(stride_m=1.0))
    assert result['stride_length_cm'] == pytest.approx(100, rel=0.03) and result['status'] == 'OK'
    assert result['metodo'] == 'integracion_entre_apoyos' and result['n_zancadas_longitud'] == 19
    assert result['stride_length_std_cm'] < 2
    # Directamente en metros: el doble de avance da el doble de longitud, a 50 o a 100 Hz.
    assert CALC.calcular_longitud_zancada(walk(stride_m=0.5))['stride_length_cm'] == pytest.approx(50, rel=0.03)
    assert CALC.calcular_longitud_zancada(walk(stride_m=1.0, fs=50.0))['stride_length_cm'] == pytest.approx(100, rel=0.05)
    # Fuera de la banda plausible se conserva el valor (no hay recorte) con estado WARNING.
    long = CALC.calcular_longitud_zancada(walk(stride_m=2.0))
    assert long['stride_length_cm'] == pytest.approx(200, rel=0.03) and long['status'] == 'WARNING'


def test_stride_length_only_integrates_complete_valid_strides():
    frame = walk(stride_m=1.0)
    frame.loc[640:700, ['S0', 'S1', 'S2']] = np.nan                   # hueco de presión dentro de una zancada
    result = CALC.calcular_longitud_zancada(frame)
    assert result['stride_length_cm'] == pytest.approx(100, rel=0.03) and result['n_zancadas_longitud'] < 19
    # Cada prueba fija su mínimo de zancadas.
    assert CALC.calcular_longitud_zancada(walk(stride_m=1.0), min_strides=30)['error_reason'] == 'ERR_TOO_FEW_STRIDES'
    assert CALC.calcular_cadencia(walk(), min_strides=30)['error_reason'] == 'ERR_TOO_FEW_STRIDES'
    assert CALC.calcular_cadencia(walk(), min_strides=30)['tiempos_zancada'] and CALC.calcular_fases(walk(), min_strides=30)['balanceos_ciclo_prc']
    short = CALC.calcular_longitud_zancada(walk(seconds=1.9, stride_m=1.0), min_strides=2)
    assert short['error_reason'] == 'ERR_TOO_FEW_STRIDES' and np.isnan(short['stride_length_cm'])
    assert CALC.calcular_longitud_zancada(walk(stride_m=1.0).drop(columns=['S2']))['error_reason'] == 'ERR_MISSING_S2'
    # Las zancadas en giro no se integran.
    turning = walk(seconds=30, stride_m=1.0)
    t = seconds_of(turning).to_numpy()
    turning['Gy'] = np.where((t >= 12) & (t < 15), 60.0, 0.0) + 0.01 * (np.arange(len(turning)) % 2)
    assert CALC.calcular_longitud_zancada(turning)['n_zancadas_longitud_excluidas'] >= 3
    assert CALC.calcular_longitud_zancada(turning, excluir_giros=False)['n_zancadas_longitud_excluidas'] == 0


# --- LR-27 / LR-28: altura por balanceo y filtro declarado ------------------------------

def test_ankle_excursion_is_reported_and_mtc_stays_empty_with_its_reason():
    result = CALC.calcular_altura_zancada(walk(stride_m=1.0, clearance_cm=3.0))
    assert result['status'] == 'OK' and result['metodo'] == 'integracion_entre_apoyos'
    # Máximo de la trayectoria vertical del sensor en el balanceo (1,5625 × 3 cm en esta señal).
    assert result['excursion_tobillo_cm'] == pytest.approx(3.0 * 1.5625, rel=0.15)
    assert result['excursion_tobillo_std_cm'] < 0.5 and result['n_zancadas_altura'] == 19
    # La MTC no se calcula con el módulo en el tobillo: vacía y con su motivo, sin aplicarle ninguna banda.
    assert np.isnan(result['altura_minima_cm']) and 'sensor en el tobillo' in result['motivo_mtc']
    events = pd.DataFrame(result['eventos'])
    assert {'inicio_s', 'duracion_s', 'longitud_cm', 'excursion_tobillo_cm', 'altura_minima_cm', 'motivo_exclusion', 'motivo_altura'} <= set(events.columns)
    assert events['altura_minima_cm'].isna().all() and set(events['motivo_altura']) == {result['motivo_mtc']}
    assert events['longitud_cm'].mean() == pytest.approx(100, rel=0.03) and events['duracion_s'].iloc[0] == pytest.approx(1.0, abs=0.02)
    # Sale en centímetros sin constantes: el doble de despeje da el doble de excursión.
    double = CALC.calcular_altura_zancada(walk(stride_m=1.0, clearance_cm=6.0))['excursion_tobillo_cm']
    assert double == pytest.approx(2 * result['excursion_tobillo_cm'], rel=0.05)
    assert CALC.config.mtc_ok_min_cm == 0.4 and CALC.config.mtc_ok_max_cm == 5.5 and CALC.config.mtc_stride_max_cm == 6.0
    assert CALC.calcular_altura_zancada(walk(stride_m=1.0, clearance_cm=3.0), min_strides=30)['error_reason'] == 'ERR_TOO_FEW_STRIDES'


def test_short_segment_filter_fallback_is_declared_and_windows_are_in_seconds():
    assert CALC._lowpass(np.ones(10), 3.0, 100.0)[1] == 'media_movil'
    assert CALC._lowpass(np.ones(200), 3.0, 100.0)[1] == 'butterworth'
    # La ventana de presión dura lo mismo a 50 y a 100 Hz.
    assert CALC.seconds_to_samples(CALC.config.pressure_window_s, 100) == 15
    assert CALC.seconds_to_samples(CALC.config.pressure_window_s, 50) == 8
