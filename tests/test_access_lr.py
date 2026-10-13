"""Contratos offline del acceso a datos (LR-41 a LR-54). Sin PostgreSQL, InfluxDB ni red."""
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pkgs'), str(ROOT / 'src'), str(ROOT)]
from msTools.flux import flux_range, flux_string


def write_config(tmp_path, postgresql=True):
    config = {'influxdb': {'url': 'https://example.test:8086', 'org': 'o', 'bucket': 'b', 'token': 't'},
              'movement': {'freq_band_min': 0.4, 'freq_band_max': 1.6, 'min_continuous_seconds': 10}}
    if postgresql:
        config['postgresql'] = {'host': 'h', 'user': 'u', 'password': 'p', 'database': 'd', 'port': 5432}
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config))
    return str(path)


@pytest.fixture
def manager(tmp_path):
    """DataManager con InfluxDB y PostgreSQL simulados."""
    from msTools import data_manager as module
    connection = MagicMock(closed=0)
    with patch.object(module, 'InfluxDBClient'), patch.object(module.psycopg2, 'connect', return_value=connection) as connect:
        dm = module.DataManager(write_config(tmp_path))
        dm.connect_mock, dm.connection_mock = connect, connection
        yield dm


# --- El pipeline no depende del paquete heredado ---------------------------------------

def test_pipeline_only_loads_config_messages_and_time_utilities_from_pkgs():
    code = (
        "import sys, os; sys.path[:0]=['.', 'pkgs', 'src']; import pipeline_test, graficas_tfg\n"
        "print(sorted(m for m, v in sys.modules.items() if getattr(v, '__file__', None) "
        "and os.sep + 'pkgs' + os.sep in v.__file__))"
    )
    done = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert eval(done.stdout.strip().splitlines()[-1]) == ['msConfig', 'msTools', 'msTools.flux', 'msTools.i18n', 'msTools.timeutils']


def test_no_credential_literals_in_versioned_code():
    api = (ROOT / 'pkgs' / 'api.py').read_text(encoding='utf-8')
    assert 'DEV_TOKEN' not in api and 'os.getenv(API_KEY_ENV, "")' in api


# --- LR-42: Flux ------------------------------------------------------------------------

def test_flux_values_are_escaped_and_ranges_validated(tmp_path):
    assert flux_string('A"B') == '"A\\"B"' and flux_string('a\\b') == '"a\\\\b"' and flux_string('${x}') == '"\\${x}"'
    for bad in ('', 'a\nb', 'a\x00'):
        with pytest.raises(ValueError):
            flux_string(bad)
    assert flux_range('2025-01-01T10:00:00Z', '2025-01-01T10:00:20Z') == 'start: 2025-01-01T10:00:00.000000Z, stop: 2025-01-01T10:00:20.000000Z'
    for start, stop in (('2025-01-02T00:00:00Z', '2025-01-01T00:00:00Z'), ('x', '2025-01-01T00:00:00Z')):
        with pytest.raises(ValueError):
            flux_range(start, stop)

    from gait_analysis.query import build_trial_query
    from gait_analysis.registry import Trial
    trial = Trial('1', 'PAT-1', 'TUG', 'a', 'b', '2025-01-01T10:00:00Z', '2025-01-01T10:00:20Z', 20.0)
    query = build_trial_query('bu"cket', 'Ga"it', trial)
    assert 'from(bucket: "bu\\"cket")' in query and 'r["_measurement"] == "Ga\\"it"' in query


# --- LR-41 / LR-53 / LR-54: DataManager -------------------------------------------------

def test_postgresql_is_connected_only_when_used_and_closing_is_safe(manager, tmp_path):
    assert manager.connect_mock.call_count == 0
    manager.close_all()                                   # sin conexión abierta no falla
    manager.pg_conn
    manager.pg_conn
    assert manager.connect_mock.call_count == 1
    manager.close_all()
    assert manager.connection_mock.close.called

    from msTools import data_manager as module
    with patch.object(module, 'InfluxDBClient'), patch.object(module.psycopg2, 'connect') as connect:
        influx_only = module.DataManager(write_config(tmp_path, postgresql=False))
        assert influx_only.bucket == 'b' and connect.call_count == 0
        with pytest.raises(ValueError, match='postgresql'):
            influx_only.pg_conn
    partial = object.__new__(module.DataManager)          # constructor que no llegó a terminar
    partial.__del__()


def test_connect_timeout_is_passed_only_when_configured(tmp_path):
    from msTools import data_manager as module
    with patch.object(module, 'InfluxDBClient'), patch.object(module.psycopg2, 'connect') as connect:
        dm = module.DataManager(write_config(tmp_path))
        dm.pg_conn
        assert 'connect_timeout' not in connect.call_args.kwargs
        dm._pg_conn = None
        dm.config['postgresql']['connect_timeout'] = 7
        dm.pg_conn
        assert connect.call_args.kwargs['connect_timeout'] == 7


def test_reads_are_parameterized_and_roll_back_on_error(manager):
    cursor = manager.connection_mock.cursor.return_value.__enter__.return_value
    cursor.description, cursor.fetchall.return_value = [('codeid',)], [('PAT-1',)]
    windows = pd.DataFrame({'active_legs': [['Left']], 'codeid_ids': [["7'; DROP TABLE x;--"]],
                            'start_time': [pd.Timestamp('2025-01-01')], 'end_time': [pd.Timestamp('2025-01-02')]})
    with pytest.raises(ValueError):                       # no es un entero: nunca llega al SQL
        manager.recover_activity_all(windows)
    windows['codeid_ids'] = [[7]]
    assert manager.recover_activity_all(windows)['CodeID'].tolist() == ['PAT-1']
    assert cursor.execute.call_args.args == ('SELECT codeid FROM codeids WHERE id = %s;', (7,))

    cursor.execute.side_effect = RuntimeError('fallo de lectura')
    with pytest.raises(RuntimeError):
        manager.fetch_data('SELECT 1;')
    assert manager.connection_mock.rollback.called


def test_store_data_validates_quotes_identifiers_and_propagates_errors(manager):
    cursor = manager.connection_mock.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = [11]
    frame = pd.DataFrame({'codeid_id': [1], 'start_time': [pd.Timestamp('2025-01-01 10:00:00')],
                          'end_time': [pd.Timestamp('2025-01-01 10:00:05')], 'duration': [5.0],
                          'x"; DROP TABLE y;--': [1]})
    before = frame.copy()
    assert manager.store_data('effective_gait', frame, verbose=0) == [11]
    pd.testing.assert_frame_equal(frame, before)          # el DataFrame del llamador no cambia
    statement, values = cursor.execute.call_args.args
    rendered = repr(statement)
    assert "Identifier('effective_gait')" in rendered and "Identifier('codeid_id')" in rendered
    assert 'DROP TABLE' not in rendered and len(values) == 4   # solo los campos del modelo

    assert manager.store_data('effective_gait', frame.iloc[0:0], verbose=0) == []
    with pytest.raises(ValueError):
        manager.store_data('tabla_desconocida', frame, verbose=0)
    with pytest.raises(Exception, match='codeid_id'):       # fila que no cumple el modelo
        manager.store_data('effective_gait', frame.drop(columns=['codeid_id']), verbose=0)
    cursor.execute.side_effect = RuntimeError('fallo de escritura')
    with pytest.raises(RuntimeError):
        manager.store_data('effective_gait', frame, verbose=0)
    assert manager.connection_mock.rollback.called


def test_store_codeid_returns_the_existing_id_at_any_verbosity(manager):
    cursor = manager.connection_mock.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = [None, [5]]
    assert manager.store_codeid('PAT-1', verbose=2) == (5, False)


def test_transform_activityleg_does_not_modify_its_input(manager):
    cursor = manager.connection_mock.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = [3]
    segments = pd.DataFrame({
        'time_from': [pd.Timestamp('2025-01-01 10:00:00')], 'time_until': [pd.Timestamp('2025-01-01 10:01:00')],
        'CodeID': ['PAT-1'], 'DeviceName': ['Sock'], 'Foot': ['Left'], 'total_value': [10.0], 'mac': ['AA'],
    })
    before = segments.copy()
    result = manager.transform_activityleg(segments)
    pd.testing.assert_frame_equal(segments, before)
    assert result.loc[0, ['codeid_id', 'foot', 'device_name', 'duration']].tolist() == [3, 'Left', 'Sock', 60.0]


def test_failed_reads_are_raised_not_returned_as_empty(manager):
    manager.influxdb_client.query_api.return_value.query.side_effect = RuntimeError('influx caído')
    with pytest.raises(RuntimeError):
        manager.get_codeids_in_range('2025-01-01 00:00:00', '2025-01-02 00:00:00')
    manager.connection_mock.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError('pg caído')
    with pytest.raises(RuntimeError):
        manager.get_activity_ids_by_start_date_range('2025-01-01 00:00:00', '2025-01-02 00:00:00')
    assert manager.connection_mock.rollback.called


# --- LR-45 / LR-46 / LR-47 / LR-50: MovementDetector ------------------------------------

def bare_detector(**params):
    from msGait.movement_detector import MovementDetector
    detector = object.__new__(MovementDetector)
    detector.verbose, detector.sampling_rate, detector.data_manager = 0, 50, MagicMock()
    detector._load_parameters({'freq_band_min': 0.4, 'freq_band_max': 1.6, 'min_continuous_seconds': 10, **params})
    return detector


@pytest.mark.parametrize('verbose', [0, 1])
def test_selected_ids_do_not_depend_on_verbosity(verbose, capsys):
    from msGait import movement_detector as module
    with patch.object(module, 'DataManager') as fake:
        dm = fake.return_value
        dm.get_activity_ids_by_start_date_range.return_value = [4, 9]
        dm.segments_retrieval.return_value = pd.DataFrame()
        dm.get_config.return_value = {'freq_band_min': 0.4, 'freq_band_max': 1.6, 'min_continuous_seconds': 10}
        detector = module.MovementDetector(None, 50, fstart='2025-01-01 00:00:00', fend='2025-01-02 00:00:00', verbose=verbose)
    assert detector.ids == [4, 9]
    assert dm.segments_retrieval.call_args.kwargs['ids'] == [4, 9]


def test_empty_signal_is_not_effective_and_errors_are_not_swallowed():
    detector = bare_detector()
    assert detector.is_effective_by_time(np.array([]), 0.2) is False
    detector.data_manager.store_data.side_effect = RuntimeError('fallo de escritura')
    with pytest.raises(RuntimeError):
        detector.save_to_postgresql('effective_gait', pd.DataFrame({'a': [1]}))
    detector.save_to_postgresql('effective_gait', pd.DataFrame())      # vacío: no escribe ni falla


def test_continuity_is_the_duration_of_the_active_streak_in_seconds():
    detector = bare_detector()
    isolated = np.zeros(1000)
    isolated[[100, 400, 700]] = 1.0                          # tres muestras aisladas: antes bastaban
    assert detector.is_effective_by_time(isolated, 0.2) is False
    sustained = np.where(np.arange(1000) < 600, 1.0, 0.0)    # 12 s seguidos a 50 Hz: antes no bastaba
    assert detector.is_effective_by_time(sustained, 0.2) is True
    assert detector.is_effective_by_time(np.where(np.arange(1000) < 400, 1.0, 0.0), 0.2) is False   # 8 s < 10 s
    # La duración se mide con las marcas de tiempo: las mismas 600 muestras a 100 Hz son 6 s.
    fast = pd.Timestamp('2025-01-01') + pd.to_timedelta(np.arange(1000) / 100, unit='s')
    assert detector.is_effective_by_time(sustained, 0.2, fast) is False
    # Un movimiento que oscila sigue siendo una racha: las pausas cortas (< 1 / freq_band_min) no la cortan...
    walking = (np.arange(1000) % 50 < 20).astype(float)      # activo 0,4 s de cada 1 s, durante 20 s
    assert detector.is_effective_by_time(walking, 0.2) is True
    # ...pero un hueco sin datos más largo sí.
    holed = pd.Timestamp('2025-01-01') + pd.to_timedelta(np.arange(1000) / 50 + (np.arange(1000) >= 300) * 5.0, unit='s')
    assert detector.active_streaks(sustained, 0.2, holed)[:600].sum() == 0
    assert bare_detector(min_continuous_seconds=3).is_effective_by_time(np.where(np.arange(1000) < 400, 1.0, 0.0), 0.2)
    for bad in ({'tail_policy': 'otra'}, {'tail_policy': 'minimum'}):
        with pytest.raises(ValueError):
            bare_detector(**bad)
    with pytest.raises(ValueError, match='min_continuous_seconds'):
        object.__new__(type(detector))._load_parameters({'freq_band_min': 0.4, 'freq_band_max': 1.6})


def test_band_power_is_integrated_and_uses_the_measured_rate():
    from scipy.signal import welch
    detector = bare_detector()
    t = np.arange(256) / 50
    wave = np.sin(2 * np.pi * 1.0 * t)
    freqs, power = welch(wave, fs=50)
    old_sum = power[(freqs >= 0.4) & (freqs <= 1.6)].sum()            # criterio anterior: suma de bins
    scale = 50 / 256
    assert detector.is_effective_by_welch(wave, old_sum * scale * 0.999) is True     # mismo resultado con el umbral reescalado
    assert detector.is_effective_by_welch(wave, old_sum * scale * 1.001) is False
    # La potencia integrada no depende de la resolución: con la mitad de muestras por segmento sigue en la misma escala.
    assert detector.is_effective_by_welch(np.tile(wave, 4)[:128], old_sum * scale * 0.5) is True
    # La misma señal muestreada a 100 Hz oscila a 2 Hz: fuera de la banda si se usa la frecuencia medida.
    assert detector.is_effective_by_welch(wave, old_sum * scale * 0.5, fs=100.0) is False
    stamps = pd.Timestamp('2025-01-01') + pd.to_timedelta(np.r_[np.arange(100) / 100, 60 + np.arange(100) / 100], unit='s')
    assert detector.estimate_rate(stamps) == pytest.approx(100, rel=0.01)           # el hueco de 59 s no cuenta
    assert detector.is_effective_by_welch(np.array([]), 0.0) is False


def movement_frame(seconds, fs=50.0, start=0.0, moving=True):
    """Serie IMU sintética: |g| oscila a 1 Hz entre 0 y 200 grados/s si hay movimiento."""
    t = start + np.arange(int(seconds * fs)) / fs
    gyro = 100 * (1 + np.sin(2 * np.pi * t)) if moving else np.zeros(len(t))
    return pd.DataFrame({'_time': pd.Timestamp('2025-01-01 10:00:00') + pd.to_timedelta(t, unit='s'),
                         'Ax': 0.0, 'Ay': 1.0, 'Az': 0.0, 'Gx': gyro, 'Gy': 0.0, 'Gz': 0.0})


def run_detector(frame, capsys=None, **params):
    detector = bare_detector(gyro_threshold=75, **params)
    detector.fetch_sensor_data = lambda *args: frame.copy()
    windows = pd.DataFrame({'CodeID': ['PAT-1'], 'codeid_id': [1], 'foot': ['Left'],
                            'start_time': [frame['_time'].iloc[0]], 'end_time': [frame['_time'].iloc[-1]]})
    return detector.detect_effective_movement(windows, vb=1 if capsys else 0)


def test_last_window_is_not_discarded():
    frame = movement_frame(22)                               # 1100 muestras: 4 ventanas de 256 y 76 de cola
    aligned = run_detector(frame)
    assert len(aligned) == 1 and aligned.loc[0, 'duration'] == pytest.approx(22 - 1 / 50, abs=0.01)
    assert run_detector(frame, tail_policy='minimum', tail_min_samples=50).loc[0, 'duration'] == pytest.approx(22 - 1 / 50, abs=0.01)
    short_tail = run_detector(frame, tail_policy='minimum', tail_min_samples=200)
    assert short_tail.loc[0, 'duration'] == pytest.approx(4 * 256 / 50 - 1 / 50, abs=0.01)


def test_segments_are_not_joined_across_missing_or_inactive_data(capsys):
    # Dos tramos de marcha separados por 8 s sin datos: antes se unían (gap <= 10 s).
    hole = pd.concat([movement_frame(15.36), movement_frame(15.36, start=23.36)], ignore_index=True)
    assert len(run_detector(hole)) == 2
    # Separados por 8 s de datos inactivos: tampoco se unen.
    rest = pd.concat([movement_frame(15.36), movement_frame(8, start=15.36, moving=False),
                      movement_frame(15.36, start=23.36)], ignore_index=True)
    found = run_detector(rest)
    assert len(found) == 2 and found['duration'].max() < 20      # resolución de ventana (5,12 s a 50 Hz)
    # Marcha continua: un solo tramo.
    assert len(run_detector(movement_frame(40))) == 1
    # Calcetín a 100 Hz: se avisa y se analiza con la frecuencia medida.
    assert len(run_detector(movement_frame(30, fs=100.0), capsys)) == 1
    assert 'MVNT-FS-DEV' in capsys.readouterr().out


@pytest.mark.parametrize('suffix', ['', '.250000', '+00:00', '.250000+02:00'])
def test_gait_overlap_accepts_any_iso_timestamp(suffix):
    rows = pd.DataFrame({
        'codeid_id': [1, 1], 'leg': ['Left', 'Right'], 'duration': [10.0, 10.0],
        'start_time': [f'2025-01-01T10:00:00{suffix}', f'2025-01-01T10:00:05{suffix}'],
        'end_time': [f'2025-01-01T10:00:10{suffix}', f'2025-01-01T10:00:15{suffix}'],
    })
    gait = bare_detector().detect_effective_gait(rows)
    assert gait['duration'].tolist() == [5.0]
    mixed = rows.assign(start_time=['2025-01-01T10:00:00', '2025-01-01T10:00:05+00:00'])
    with pytest.raises(ValueError, match='ISO 8601'):
        bare_detector().detect_effective_gait(mixed)


# --- LR-48 / LR-49: CodeIDProcessor -----------------------------------------------------

def segments(rows):
    frame = pd.DataFrame(rows, columns=['CodeID', 'time_from', 'time_until', 'codeid_id'])
    for column in ('time_from', 'time_until'):
        frame[column] = pd.Timestamp('2025-01-01') + pd.to_timedelta(frame[column], unit='s')
    return frame


def processor():
    from msCodeID.codeid_processor import CodeIDProcessor
    return object.__new__(CodeIDProcessor)


def test_intersections_stay_within_one_person_and_need_positive_duration():
    right = segments([('A', 0, 10, 1), ('B', 0, 10, 2)])
    left = segments([('A', 10, 20, 1), ('A', 5, 8, 1), ('B', 20, 30, 2)])
    result = processor().inter_segs(right, left)
    # A-derecha solo cruza con el intervalo 5-8 de A; el que empieza en 10 solo la toca. B no cruza con nadie.
    assert result[['R1_id', 'R2_id']].values.tolist() == [[0, 1]]
    assert (result['time_until'] - result['time_from']).dt.total_seconds().tolist() == [3.0]
    assert result[['codeid_id_1', 'codeid_id_2']].values.tolist() == [[1, 1]]
    assert processor().inter_segs(right, left.iloc[0:0]).empty


def test_interval_sweep_matches_the_exhaustive_crossing():
    rng = np.random.default_rng(1)

    def random_segments(count):
        start = np.sort(rng.integers(0, 500, count))
        return segments([('A' if i % 2 else 'B', int(s), int(s + rng.integers(1, 40)), 1) for i, s in enumerate(start)])

    right, left = random_segments(40), random_segments(40)
    expected = sorted(
        (i, j) for i, a in right.iterrows() for j, b in left.iterrows()
        if a['CodeID'] == b['CodeID'] and max(a['time_from'], b['time_from']) < min(a['time_until'], b['time_until'])
    )
    result = processor().inter_segs(right, left)
    assert sorted(map(tuple, result[['R1_id', 'R2_id']].values.tolist())) == expected and len(expected) > 5


def test_activity_groups_are_cut_by_codeid_and_input_is_not_modified():
    times = pd.Timestamp('2025-01-01 10:00:00') + pd.to_timedelta([0, 60, 60, 120], unit='s')
    raw = pd.DataFrame({'_time': times.astype(str), 'CodeID': ['A', 'A', 'B', 'B'], 'DeviceName': 'Sock',
                        'Foot': 'Left', '_value': 1, 'mac': 'AA', 'result': 0, 'table': 0, '_field': 'Ax', 'lng': 0, 'lat': 0})
    before = raw.copy()
    groups = processor().identify_activity_segments(raw, 80, 'Left')
    pd.testing.assert_frame_equal(raw, before)
    assert groups['CodeID'].tolist() == ['A', 'B'] and groups['total_value'].tolist() == [2, 2]


# --- LR-51: interfaces sin implementar ---------------------------------------------------

def test_placeholders_say_they_are_not_implemented():
    from msGait.gait_classifier import GaitClassifier
    from msGait.trajectory_analyzer import TrajectoryAnalyzer
    for call in (lambda: GaitClassifier(None).classify_gait({'a': 1}), lambda: GaitClassifier(None).classify_trajectory(1),
                 lambda: TrajectoryAnalyzer(None).analyze_trajectory({'a': 1}), lambda: TrajectoryAnalyzer(None).calculate_trajectory(1)):
        with pytest.raises(NotImplementedError):
            call()


# --- LR-43 / LR-44 / LR-52: API ---------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    pytest.importorskip('fastapi')
    pytest.importorskip('httpx')
    from fastapi.testclient import TestClient
    import api
    monkeypatch.delenv('MSGAIT_API_KEY', raising=False)
    return TestClient(api.app), api                       # sin 'with': no se ejecuta el arranque real


def test_api_has_no_default_key_and_fails_closed(client, monkeypatch):
    http, api = client
    body = {'ids': [1]}
    assert http.post('/gait/detect', json=body).status_code in (401, 403)                      # sin cabecera
    assert http.post('/gait/detect', json=body, headers={'X-API-Key': 'DEV_TOKEN_SEGURIDAD_UPM'}).status_code == 503
    monkeypatch.setenv('MSGAIT_API_KEY', 'clave-de-prueba')
    assert http.post('/gait/detect', json=body, headers={'X-API-Key': 'otra'}).status_code == 401
    assert http.get('/sensor-data', params={'codeid_id': 1, 'foot': 'Left', 'start': '2025-01-01 10:00:00',
                                            'end': '2025-01-01 10:00:10'}, headers={'X-API-Key': 'clave-de-prueba-x'}).status_code == 401
    assert api.SECURE_EXPORT_DIR.is_absolute()


def test_api_rejects_empty_lists_negative_sizes_and_bad_dates(client, monkeypatch):
    http, api = client
    monkeypatch.setenv('MSGAIT_API_KEY', 'clave-de-prueba')
    key = {'X-API-Key': 'clave-de-prueba'}
    for body in ({'ids': []}, {'ids': [1], 'head_rows': -1}, {'ids': [1], 'verbose': 9}, {}):
        assert http.post('/gait/detect', json=body, headers=key).status_code == 422
    assert http.get('/sensor-data', params={'codeid_id': 1, 'foot': 'Left', 'start': "2025-01-01' OR 1=1",
                                            'end': '2025-01-01 10:00:10'}, headers=key).status_code == 422
    assert http.get('/activity-windows', params={'start': "x'; DROP"}, headers=key).status_code == 422
    monkeypatch.setattr(api, '_dm', MagicMock())
    monkeypatch.setattr(api, '_detector', MagicMock())
    assert http.get('/activity-windows', headers=key).status_code == 400
    assert http.get('/sensor-data', params={'codeid_id': 1, 'foot': 'Left', 'start': '2025-01-02 00:00:00',
                                            'end': '2025-01-01 00:00:00'}, headers=key).status_code == 400


def test_api_metadata_reads_need_the_key_and_every_endpoint_is_bounded(client, monkeypatch):
    http, api = client
    day = {'start': '2025-01-01 00:00:00', 'end': '2025-01-02 00:00:00'}
    for path in ('/codeids', '/activity-windows'):
        assert http.get(path, params=day).status_code in (401, 403)                 # sin cabecera
    monkeypatch.setenv('MSGAIT_API_KEY', 'clave-de-prueba')
    key = {'X-API-Key': 'clave-de-prueba'}
    dm, detector = MagicMock(), MagicMock()
    monkeypatch.setattr(api, '_dm', dm)
    monkeypatch.setattr(api, '_detector', detector)

    # /gait/detect: como mucho 50 identificadores, enteros >= 1 y sin duplicados; vista previa de 0 a 50 filas.
    for body in ({'ids': list(range(1, 52))}, {'ids': [1, 1]}, {'ids': [0]}, {'ids': [1], 'head_rows': 51}):
        assert http.post('/gait/detect', json=body, headers=key).status_code == 422

    # /codeids y /activity-windows: rango de 31 días como máximo y paginación.
    long_range = {'start': '2025-01-01 00:00:00', 'end': '2025-02-02 00:00:00'}
    for path in ('/codeids', '/activity-windows'):
        assert http.get(path, params=long_range, headers=key).status_code == 400
        assert http.get(path, params={**day, 'limit': 501}, headers=key).status_code == 422
        assert http.get(path, params={**day, 'offset': -1}, headers=key).status_code == 422
    dm.get_codeids_in_range.return_value = [f'PAT-{i:03d}' for i in range(250)]
    assert len(http.get('/codeids', params=day, headers=key).json()) == 100            # límite por defecto
    assert http.get('/codeids', params={**day, 'limit': 500, 'offset': 200}, headers=key).json()[0] == 'PAT-200'
    dm.segments_retrieval.return_value = pd.DataFrame({'id': range(250)})
    page = http.get('/activity-windows', params={**day, 'limit': 20, 'offset': 240}, headers=key).json()
    assert [row['id'] for row in page] == list(range(240, 250))
    for ids in (list(range(1, 52)), [3, 3], [0]):
        assert http.get('/activity-windows', params={'ids': ids}, headers=key).status_code == 422
    assert http.get('/activity-windows', params={'ids': [1, 2]}, headers=key).status_code == 200

    # /sensor-data: 10 minutos y 100 000 filas como máximo.
    sensor = {'codeid_id': 1, 'foot': 'Left', 'start': '2025-01-01 10:00:00'}
    assert http.get('/sensor-data', params={**sensor, 'end': '2025-01-01 10:10:01'}, headers=key).status_code == 400
    detector.fetch_sensor_data.return_value = pd.DataFrame({'Ax': np.zeros(100_001)})
    too_many = http.get('/sensor-data', params={**sensor, 'end': '2025-01-01 10:10:00'}, headers=key)
    assert too_many.status_code == 413 and '100000' in too_many.json()['detail']
    detector.fetch_sensor_data.return_value = pd.DataFrame({'Ax': np.zeros(3)})
    assert len(http.get('/sensor-data', params={**sensor, 'end': '2025-01-01 10:10:00'}, headers=key).json()) == 3


def test_api_readiness_and_shutdown(client, monkeypatch):
    http, api = client
    assert http.get('/health').json() == {'status': 'ok'}
    assert http.get('/health/ready').status_code == 503                 # aún sin arrancar
    dm, detector = MagicMock(), MagicMock(spec=api.MovementDetector)
    monkeypatch.setattr(api, '_dm', dm)
    monkeypatch.setattr(api, '_detector', detector)
    assert http.get('/health/ready').json() == {'status': 'ready', 'postgresql': 'ok', 'influxdb': 'ok'}
    dm.fetch_data.side_effect = RuntimeError('caída')
    assert http.get('/health/ready').status_code == 503
    api._shutdown()
    assert detector.close.called and dm.close_all.called and api._dm is None
