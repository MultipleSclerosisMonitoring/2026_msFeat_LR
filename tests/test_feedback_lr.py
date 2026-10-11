"""Contratos offline de los puntos LR-01 a LR-11. No se consulta ningún servidor."""
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pkgs'), str(ROOT / 'src'), str(ROOT)]
from gait_analysis.extractor import GaitDataExtractor
from gait_analysis.logging_utils import configure_logging
from gait_analysis.query import build_trial_query, normalize_query_result, split_series
from gait_analysis.registry import RegistryError, Trial, load_registry
from gait_analysis.storage import (
    Manifest, SignalStore, StorageError, list_keys, manifest_path_for, publish_excel, read_meta, store_dir_for,
)
from msConfig import PROJECT_ROOT, load_config

FS = 100


def signal(side, seconds=20, start='2025-01-01 10:00:00', mac=None, cadence_hz=1.0):
    """Señal sintética de un pie con un impacto periódico en Ay y presión alterna."""
    n = seconds * FS
    t = np.arange(n) / FS
    walking = np.sin(2 * np.pi * cadence_hz * t)
    return pd.DataFrame({
        'result': '_result', 'table': 0,
        '_start': pd.Timestamp(start, tz='UTC'), '_stop': pd.Timestamp(start, tz='UTC'),
        '_time': pd.Timestamp(start, tz='UTC') + pd.to_timedelta(t, unit='s'),
        '_measurement': 'Gait', 'CodeID': 'PAT-1', 'DeviceName': f'Sock{side}', 'Foot': side,
        'mac': mac or f'AA:{side}', 'app': 'app', 'type': 'SCKS', 'lat': 40.4, 'lng': -3.7,
        'Ax': 0.0, 'Ay': walking, 'Az': 1.0, 'Gx': 0.0, 'Gy': 0.0, 'Gz': 0.0,
        'S0': 500 + 400 * walking, 'S1': 500 + 400 * walking, 'S2': 500 + 400 * walking,
    })


def write_csv(tmp_path, rows, columns=('id', 'codeid', 'd_from', 'd_until', 't_code')):
    path = tmp_path / 'events.csv'
    pd.DataFrame(rows, columns=list(columns)).to_csv(path, index=False)
    return path


def write_config(tmp_path, csv_path):
    config = {
        'influxdb': {'url': 'https://example.test:8086', 'org': 'o', 'bucket': 'b', 'token': 't', 'verify_ssl': False},
        'paths': {'registry_csv': str(csv_path), 'h5_path': 'store/custom.h5', 'output_dataset': 'out/dataset.xlsx'},
        'batch': {'test_type': 'TUG'},
    }
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config))
    return path


def fake_client(responses):
    """Cliente simulado: devuelve una respuesta por consulta, en orden."""
    client = Mock()
    client.return_value.query_api.return_value.query_data_frame.side_effect = list(responses)
    return client


ROWS = [
    ('10', 'PAT-1', '2025-01-01 10:00:00+01', '2025-01-01 10:00:20+01', 'TUG'),
    ('11', 'PAT-1', '2025-01-02 10:00:00+01', '2025-01-02 10:00:20+01', 'TUG'),
]
TRIAL = Trial('10', 'PAT-1', 'TUG', 'a', 'b', '2025-01-01T10:00:00Z', '2025-01-01T10:00:20Z', 20.0)
KEY = '/p_PAT-1/TUG/trial_10/Left'


def left_frame(seconds=20):
    return split_series(normalize_query_result(signal('Left', seconds=seconds)), '10')['Left'].frame


# --- LR-08: validación del registro -------------------------------------------------

def test_registry_missing_columns_fails_as_a_set(tmp_path):
    path = write_csv(tmp_path, [('1', 'P', 'TUG')], columns=('id', 'codeid', 't_code'))
    with pytest.raises(RegistryError, match='d_from'):
        load_registry(str(path))


def test_registry_returns_result_per_invalid_row(tmp_path):
    path = write_csv(tmp_path, ROWS + [
        ('12', 'PAT-1', '2025-01-02 10:00:20', '2025-01-02 10:00:00', 'TUG'),
        ('13', 'PAT-1', 'no-es-fecha', '2025-01-02 10:00:00', 'TUG'),
        ('14', 'PAT"x', '2025-01-02 10:00:00', '2025-01-02 10:00:20', 'TUG'),
        ('15', 'PAT-1', '2025-01-02 10:00:00', '2025-01-02 10:00:20', 'TUG'),
        ('15', 'PAT-2', '2025-01-02 10:00:00', '2025-01-02 10:00:20', 'TUG'),
    ])
    trials, invalid = load_registry(str(path))
    assert [t.trial_id for t in trials] == ['10', '11']
    assert trials[0].start_iso == '2025-01-01T10:00:00Z' and trials[0].duration_s == 20.0
    assert [r.trial_id for r in invalid] == ['12', '13', '14', '15', '15']
    assert all(r.status == 'INVALID_ROW' and r.detail for r in invalid)


def test_registry_validates_rows_outside_the_selection(tmp_path, caplog):
    path = write_csv(tmp_path, ROWS + [('20', 'PAT-2', 'mal', 'mal', '2MWT')])
    with caplog.at_level(logging.INFO, logger='msGait'):
        trials, invalid = load_registry(str(path), 'TUG')
    assert [t.trial_id for t in trials] == ['10', '11'] and invalid == []
    assert 'INFO_INVALID_ROW_UNSELECTED' in caplog.text and "id='20'" in caplog.text
    assert '1 inválidas fuera de la selección' in caplog.text


def test_registry_selection_by_type_and_id(tmp_path):
    path = write_csv(tmp_path, ROWS + [('20', 'PAT-2', '2025-01-03 10:00:00', '2025-01-03 10:02:00', '2MWT')])
    assert [t.trial_id for t in load_registry(str(path), '2MWT')[0]] == ['20']
    assert [t.trial_id for t in load_registry(str(path), 'all', ['11', '20'])[0]] == ['11', '20']


# --- LR-03 / LR-06: normalización y contrato de consulta ------------------------------

def test_list_of_frames_is_data_not_absence():
    df = normalize_query_result([signal('Left'), pd.DataFrame(), signal('Right')])
    assert len(df) == 2 * 20 * FS
    assert df['_time'].dt.tz is None
    assert not {'result', 'table', '_start', '_stop'} & set(df.columns)
    assert normalize_query_result([]).empty and normalize_query_result(None).empty


def test_query_filters_measurement_and_keeps_metadata(tmp_path):
    trial = load_registry(str(write_csv(tmp_path, ROWS)))[0][0]
    query = build_trial_query('bucket', 'Gait', trial)
    assert 'r["_measurement"] == "Gait"' in query and 'r["CodeID"] == "PAT-1"' in query
    assert 'drop(' not in query and 'keep(' not in query


# --- LR-02 / LR-07 / LR-09: series por pie, dispositivo, orden y duplicados -----------

def test_split_keeps_metadata_orders_and_deduplicates():
    left = signal('Left')
    conflict = left.iloc[[5]].assign(Ax=9.0)
    raw = normalize_query_result([pd.concat([left.iloc[::-1], left.iloc[:3], conflict]), signal('Right')])
    series = split_series(raw, '10')
    assert set(series) == {'Left', 'Right'}
    foot = series['Left']
    assert len(foot.frame) == 20 * FS and foot.exact_duplicates == 3 and foot.time_conflicts == 1
    assert not foot.was_sorted and foot.frame['_time'].is_monotonic_increasing and foot.frame['_time'].is_unique
    assert foot.frame['Ax'].eq(0.0).all()
    assert {'Foot', 'DeviceName', 'mac', 'lat', 'lng', 'CodeID', '_measurement', 'app', 'type'} <= set(foot.frame.columns)


def test_split_selects_device_with_most_samples(caplog):
    raw = normalize_query_result([signal('Left', seconds=20), signal('Left', seconds=5, mac='BB:other')])
    with caplog.at_level(logging.WARNING, logger='msGait'):
        foot = split_series(raw, '10')['Left']
    assert foot.device == 'AA:Left' and len(foot.frame) == 20 * FS
    assert foot.devices_found == {'AA:Left': 2000, 'BB:other': 500}
    assert 'WARN_MULTIPLE_DEVICES' in caplog.text


def test_split_without_foot_tag_is_unknown():
    raw = normalize_query_result(signal('Left').drop(columns=['Foot']))
    assert list(split_series(raw, '10')) == ['Unknown']


# --- LR-04 / LR-10: almacén, atributos, bloqueo y Excel atómico -----------------------

def test_store_publishes_one_verified_file_per_event(tmp_path):
    h5 = tmp_path / 's.h5'
    frame = left_frame()
    meta = {'id': '10', 'codeid': 'PAT-1', 'd_from': 'a', 'd_until': 'b'}
    with SignalStore(h5) as store:
        assert store.write_event(TRIAL, {'Left': (frame, meta)}) == {'Left': KEY}
        assert store.has(KEY) and not store.has(KEY.replace('Left', 'Right'))
        back = store.read(KEY)
        assert store.meta(KEY) == meta
    assert (store_dir_for(h5) / 'p_PAT-1/TUG/trial_10.h5').is_file() and not h5.exists()
    assert read_meta(h5, KEY) == meta and list_keys(h5) == [KEY]
    assert len(back) == len(frame) and back['mac'].iloc[0] == 'AA:Left'
    assert not Path(f'{h5}.lock').exists()


def test_failed_publication_keeps_the_previous_event_file(tmp_path):
    h5 = tmp_path / 's.h5'
    with SignalStore(h5) as store:
        store.write_event(TRIAL, {'Left': (left_frame(20), {'id': '10'})})
        # Fallo en la verificación y fallo en el reemplazo: en ambos casos no cambia nada.
        with patch('gait_analysis.storage.getattr', return_value={'id': 'otro'}, create=True):
            with pytest.raises(StorageError, match='Verificación'):
                store.write_event(TRIAL, {'Left': (left_frame(5), {'id': '10'})})
        with patch('gait_analysis.storage.os.replace', side_effect=PermissionError('en uso')):
            with pytest.raises(StorageError, match='en uso'):
                store.write_event(TRIAL, {'Left': (left_frame(5), {'id': '10'})})
        assert len(store.read(KEY)) == 20 * FS
    assert list(store_dir_for(h5).rglob('*.tmp-*')) == []


def test_store_single_writer_stale_lock_and_legacy_file(tmp_path, caplog):
    h5 = tmp_path / 's.h5'
    with SignalStore(h5):
        with pytest.raises(StorageError, match='bloqueado'):
            SignalStore(h5).__enter__()
    # Bloqueo de un proceso que ya terminó: se retira solo.
    finished = subprocess.run([sys.executable, '-c', 'import os; print(os.getpid())'], capture_output=True, text=True)
    Path(f'{h5}.lock').write_text(finished.stdout.strip())
    pd.DataFrame({'a': [1]}).to_hdf(h5, key='p_X/TUG/trial_3', format='table')
    with caplog.at_level(logging.WARNING, logger='msGait'):
        with SignalStore(h5) as store:
            assert Path(f'{h5}.lock').read_text() == str(os.getpid())
            store.write_event(TRIAL, {'Left': (left_frame(), {'id': '10'})})
    assert 'WARN_STALE_LOCK' in caplog.text and 'WARN_LEGACY_STORE' in caplog.text
    assert list_keys(h5) == [KEY] and not Path(f'{h5}.lock').exists()


def test_killed_process_leaves_published_events_and_resumes(tmp_path):
    """Corte real de proceso: muere dentro de la escritura del segundo evento."""
    csv = write_csv(tmp_path, ROWS)
    config = write_config(tmp_path, csv)
    script = tmp_path / 'killed_run.py'
    script.write_text(textwrap.dedent(f'''
        import os, sys
        sys.path[:0] = {[str(ROOT / 'pkgs'), str(ROOT / 'src'), str(ROOT), str(ROOT / 'tests')]!r}
        from unittest.mock import patch
        import gait_analysis.storage as storage
        from gait_analysis.extractor import GaitDataExtractor
        from test_feedback_lr import fake_client, signal

        real_replace, calls = os.replace, []
        def replace_then_die(source, target):
            calls.append(target)
            if len(calls) == 2:
                os._exit(9)  # muerte brusca: sin finally, sin liberar el bloqueo
            real_replace(source, target)

        responses = [[signal('Left'), signal('Right')], [signal('Left', start='2025-01-02 10:00:00')]]
        with patch('gait_analysis.extractor.InfluxDBClient', fake_client(responses)), \\
                patch.object(storage.os, 'replace', replace_then_die):
            GaitDataExtractor({str(config)!r}).run_batch_extraction()
    '''), encoding='utf-8')
    killed = subprocess.run([sys.executable, str(script)], capture_output=True, text=True)
    assert killed.returncode == 9, killed.stderr

    h5 = tmp_path / 'store/custom.h5'
    manifest = Manifest(manifest_path_for(h5))
    assert [r['record'] for r in manifest.records()] == ['run_start', 'event']  # sin run_end: interrumpida
    assert Path(f'{h5}.lock').exists() and len(list(store_dir_for(h5).rglob('*.tmp-*'))) == 1
    assert sorted(list_keys(h5)) == ['/p_PAT-1/TUG/trial_10/Left', '/p_PAT-1/TUG/trial_10/Right']

    client = fake_client([[signal('Left', start='2025-01-02 10:00:00')]])
    with patch('gait_analysis.extractor.InfluxDBClient', client):
        resumed = GaitDataExtractor(str(config)).run_batch_extraction(missing_only=True)
    assert {r.trial_id: r.status for r in resumed} == {'10': 'SKIPPED', '11': 'OK'}
    assert client.return_value.query_api.return_value.query_data_frame.call_count == 1
    assert len(list_keys(h5)) == 3 and list(store_dir_for(h5).rglob('*.tmp-*')) == []
    assert not Path(f'{h5}.lock').exists()
    assert manifest.records()[-1]['record'] == 'run_end'


def test_excel_is_replaced_only_after_verification(tmp_path):
    output = tmp_path / 'out.xlsx'
    publish_excel({'A': pd.DataFrame({'x': [1, 2]})}, output)
    with patch('gait_analysis.storage.pd.read_excel', return_value={'A': pd.DataFrame({'x': [1]})}):
        with pytest.raises(StorageError, match='Verificación'):
            publish_excel({'A': pd.DataFrame({'x': [7, 8, 9]})}, output)
    assert pd.read_excel(output)['x'].tolist() == [1, 2]
    assert list(tmp_path.glob('*.tmp-*')) == []


def test_manifest_ignores_truncated_line(tmp_path):
    manifest = Manifest(tmp_path / 'm.jsonl')
    manifest.append({'record': 'event', 'trial_id': '1', 'status': 'OK', 'feet': {'Left': {'key': '/k'}}})
    manifest.append({'record': 'event', 'trial_id': '2', 'status': 'EMPTY', 'feet': {}})
    with open(manifest.path, 'a', encoding='utf-8') as handle:
        handle.write('{"record": "event", "trial_id": "3", "sta')
    assert manifest.completed_keys() == {'1': ['/k']}


# --- Extractor: LR-02, LR-03, LR-04, LR-08, LR-10, LR-11 ------------------------------

def test_extractor_writes_per_event_and_foot_with_stable_id(tmp_path):
    csv = write_csv(tmp_path, ROWS + [('12', 'PAT-1', 'mal', 'mal', 'TUG')])
    config = write_config(tmp_path, csv)
    responses = [[signal('Left'), signal('Right')], RuntimeError('timeout')]
    with patch('gait_analysis.extractor.InfluxDBClient', fake_client(responses)):
        extractor = GaitDataExtractor(str(config))
        results = extractor.run_batch_extraction()
    h5 = tmp_path / 'store/custom.h5'
    assert {r.trial_id: r.status for r in results} == {'12': 'INVALID_ROW', '10': 'OK', '11': 'QUERY_ERROR'}
    assert sorted(list_keys(h5)) == ['/p_PAT-1/TUG/trial_10/Left', '/p_PAT-1/TUG/trial_10/Right']
    assert read_meta(h5, '/p_PAT-1/TUG/trial_10/Left')['d_from'] == '2025-01-01 10:00:00+01'

    records = Manifest(manifest_path_for(h5)).records()
    assert [r['record'] for r in records] == ['run_start', 'event', 'event', 'run_end']
    assert records[-1]['counts'] == {'INVALID_ROW': 1, 'OK': 1, 'QUERY_ERROR': 1}
    assert records[1]['feet']['Left']['rows'] == 20 * FS

    # Reanudación: solo se consulta el evento que no quedó completado.
    client = fake_client([[signal('Left', start='2025-01-02 10:00:00')]])
    with patch('gait_analysis.extractor.InfluxDBClient', client):
        extractor = GaitDataExtractor(str(config))
        resumed = extractor.run_batch_extraction(missing_only=True)
        report = extractor.audit()
    assert {r.trial_id: r.status for r in resumed} == {'12': 'INVALID_ROW', '10': 'SKIPPED', '11': 'OK'}
    assert client.return_value.query_api.return_value.query_data_frame.call_count == 1
    assert report == {'missing': [], 'orphan': [], 'legacy': [], 'unconfirmed': []}


def test_reordering_the_registry_does_not_change_keys(tmp_path):
    keys = []
    for rows in (ROWS, ROWS[::-1]):
        folder = tmp_path / str(len(keys))
        folder.mkdir()
        config = write_config(folder, write_csv(folder, rows))
        with patch('gait_analysis.extractor.InfluxDBClient', fake_client([signal('Left'), signal('Right')])):
            GaitDataExtractor(str(config)).run_batch_extraction(ids=['11'])
        keys.append(list_keys(folder / 'store/custom.h5'))
    assert keys[0] == keys[1] and len(keys[0]) == 1 and '/trial_11/' in keys[0][0]


# --- Pipeline: LR-01, LR-05, LR-06 ----------------------------------------------------

def test_pipeline_keeps_one_line_per_event_and_never_mixes_trials(tmp_path):
    from pipeline_test import GaitPipeline
    csv = write_csv(tmp_path, ROWS)
    config = write_config(tmp_path, csv)
    # Evento 10: ambos pies. Evento 11: solo el izquierdo, más lento.
    responses = [[signal('Left'), signal('Right')], [signal('Left', start='2025-01-02 10:00:00', cadence_hz=0.5)]]
    client = fake_client(responses)
    with patch('pipeline_test.InfluxDBClient', client):
        pipeline = GaitPipeline(str(config))
        results = pipeline.run_from_csv()
    assert [r.status for r in results] == ['OK', 'OK']
    query = client.return_value.query_api.return_value.query_data_frame.call_args_list[0].args[0]
    assert 'r["_measurement"] == "Gait"' in query

    # LR-05: el almacén es el configurado, no una ruta relativa al directorio de ejecución.
    assert len(list_keys(tmp_path / 'store/custom.h5')) == 3

    book = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name=None)
    assert set(book) == {
        'Leeme', 'Datos_Pacientes', 'Agregacion_Pacientes', 'Ensayos', 'Asimetria', 'Balanceos', 'Estado_Eventos',
        'Parametros', 'Procedencia', 'Estadisticas_Ensayos', 'Estadisticas_Globales',
    }
    assert list(book)[0] == 'Leeme' and set(book['Leeme']['hoja']) >= set(book) - {'Leeme'}
    # Ningún ensayo se pierde: los dos eventos del paciente siguen en Ensayos.
    assert book['Ensayos'][['TrialID', 'Foot']].values.tolist() == [[10, 'Left'], [10, 'Right'], [11, 'Left']]
    assert book['Ensayos']['dispositivo'].tolist() == ['AA:Left', 'AA:Right', 'AA:Left']
    asymmetry = book['Asimetria'].set_index('TrialID')
    assert asymmetry.loc[10, 'symmetry_index'] == 0.0
    # El segundo evento no hereda el pie derecho del primero: sin bilateralidad.
    assert pd.isna(asymmetry.loc[11, 'symmetry_index']) and pd.isna(asymmetry.loc[11, 'cadencia_Right'])
    assert asymmetry.loc[11, 'cadencia_Left'] < asymmetry.loc[10, 'cadencia_Left']
    # LR-33: la falta de un pie se declara, no se confunde con simetría medida.
    assert asymmetry['bilateral_available'].tolist() == [True, False]

    # LR-32: estado, causa, cobertura y método por métrica.
    trials_sheet = book['Ensayos']
    for metric in ('cadencia', 'fases', 'zancada', 'altura'):
        assert {f'estado_{metric}', f'motivo_{metric}', f'cobertura_{metric}', f'filtro_{metric}'} <= set(trials_sheet.columns)
    assert trials_sheet['cobertura_cadencia'].eq(1.0).all() and set(trials_sheet['filtro_altura']) == {'sin_filtro'}

    # LR-34: ensayos realmente procesados.
    assert book['Estado_Eventos'].set_index('trial_id')['n_metricas_validas'].gt(0).all()
    provenance = dict(zip(book['Procedencia']['clave'], book['Procedencia']['valor']))
    assert provenance['eventos_con_metricas_validas'] == '2' and len(provenance['huella_config_processing']) == 16
    assert provenance['commit_git'] and provenance['huella_registro']

    # LR-36: métricas explícitas con N total y N con valor, por ensayo y por paciente.
    by_trial = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name='Estadisticas_Ensayos', index_col=0)
    by_patient = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name='Estadisticas_Globales', index_col=0)
    assert list(by_trial.index) == ['n_total', 'n_validos', 'n_sin_valor', 'media', 'mediana', 'desv_tipica']
    assert not [c for c in by_trial.columns if 'duracion' in c or c in ('TrialID', 'CodeID')]
    assert by_trial.loc[['n_total', 'n_validos', 'n_sin_valor'], 'TUG_Right_cadencia'].tolist() == [2, 1, 1]
    assert by_trial.loc[['n_total', 'n_validos'], 'TUG_symmetry_index'].tolist() == [2, 1]
    assert by_patient.loc['n_validos', 'TUG_Left_cadencia'] == 1

    # LR-01: agregación posterior por paciente con regla explícita (media; asimetría solo bilateral).
    patients, trace = book['Datos_Pacientes'], book['Agregacion_Pacientes']
    assert patients['CodeID'].tolist() == ['PAT-1'] and 'TrialID' not in patients.columns
    expected = round((asymmetry.loc[10, 'cadencia_Left'] + asymmetry.loc[11, 'cadencia_Left']) / 2, 2)
    assert patients.loc[0, 'TUG_Left_cadencia'] == pytest.approx(expected)
    assert patients.loc[0, 'TUG_Right_cadencia'] == pytest.approx(asymmetry.loc[10, 'cadencia_Right'])
    assert patients.loc[0, 'TUG_symmetry_index'] == 0.0
    assert trace[['n_ensayos', 'n_bilaterales', 'eventos']].values.tolist() == [[2, 1, '10, 11']]
    assert 'asimetría solo con ensayos bilaterales' in trace.loc[0, 'regla']

    # Reanudación: con todo completado se recalcula desde el HDF5 sin consultar.
    client = fake_client([])
    with patch('pipeline_test.InfluxDBClient', client):
        resumed = GaitPipeline(str(config)).run_from_csv(missing_only=True)
    assert [r.source for r in resumed] == ['hdf5', 'hdf5']
    again = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name='Ensayos')
    assert again['cadencia'].tolist() == book['Ensayos']['cadencia'].tolist()
    # El dispositivo se recupera de los atributos guardados, no queda vacío.
    assert again['dispositivo'].tolist() == ['AA:Left', 'AA:Right', 'AA:Left']

    # Auditoría desde el mismo comando, sin consultar.
    with patch('pipeline_test.InfluxDBClient', fake_client([])):
        assert GaitPipeline(str(config)).audit() == {'missing': [], 'orphan': [], 'legacy': [], 'unconfirmed': []}


def test_path_defaults_are_shared_and_independent_of_cwd(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'influxdb': {'url': 'https://example.test:8086', 'org': 'o', 'bucket': 'b', 'token': 't'}}))
    monkeypatch.chdir(tmp_path)
    paths = load_config(path)['paths']
    assert paths['registry_csv'] == str(PROJECT_ROOT / 'tests.csv')
    assert paths['h5_path'] == str(PROJECT_ROOT / 'data' / 'raw' / 'gait_study_data.h5')
    assert paths['output_dataset'] == str(PROJECT_ROOT / 'DatasetMaestro.xlsx')
    from pipeline_test import GaitPipeline
    with patch('pipeline_test.InfluxDBClient'), patch('gait_analysis.extractor.InfluxDBClient'):
        assert GaitPipeline(str(path)).h5_path == GaitDataExtractor(str(path))._h5_database == paths['h5_path']


# --- Política de información por niveles ----------------------------------------------

@pytest.mark.parametrize('verbose,level', [(0, logging.WARNING), (1, logging.INFO), (2, logging.DEBUG), (9, logging.DEBUG)])
def test_verbosity_levels(verbose, level):
    assert configure_logging(verbose) == level
    assert logging.getLogger('msGait.Pipeline').getEffectiveLevel() == level
    assert logging.getLogger('urllib3').getEffectiveLevel() == logging.WARNING
