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
              'movement': {'freq_band_min': 0.4, 'freq_band_max': 1.6, 'min_continuous_hits': 3}}
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


# --- LR-45 / LR-46 / LR-47: MovementDetector --------------------------------------------

def bare_detector():
    from msGait.movement_detector import MovementDetector
    detector = object.__new__(MovementDetector)
    detector.verbose, detector.min_continuous_hits, detector.data_manager = 0, 3, MagicMock()
    return detector


@pytest.mark.parametrize('verbose', [0, 1])
def test_selected_ids_do_not_depend_on_verbosity(verbose, capsys):
    from msGait import movement_detector as module
    with patch.object(module, 'DataManager') as fake:
        dm = fake.return_value
        dm.get_activity_ids_by_start_date_range.return_value = [4, 9]
        dm.segments_retrieval.return_value = pd.DataFrame()
        dm.get_config.return_value = {'freq_band_min': 0.4, 'freq_band_max': 1.6, 'min_continuous_hits': 3}
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
    assert http.get('/activity-windows', params={'start': "x'; DROP"}).status_code == 422
    monkeypatch.setattr(api, '_dm', MagicMock())
    monkeypatch.setattr(api, '_detector', MagicMock())
    assert http.get('/activity-windows').status_code == 400
    assert http.get('/sensor-data', params={'codeid_id': 1, 'foot': 'Left', 'start': '2025-01-02 00:00:00',
                                            'end': '2025-01-01 00:00:00'}, headers=key).status_code == 400


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
