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
        'Ax': 0.0, 'Ay': 1.0 + 0.3 * walking, 'Az': 0.0, 'Gx': 0.0, 'Gy': 0.0, 'Gz': 0.0,
        'S0': 500 + 400 * walking, 'S1': 500 + 400 * walking, 'S2': 500 + 400 * walking,
    })


def write_csv(tmp_path, rows, columns=('id', 'codeid', 'd_from', 'd_until', 't_code')):
    path = tmp_path / 'events.csv'
    pd.DataFrame(rows, columns=list(columns)).to_csv(path, index=False)
    return path


def write_config(tmp_path, csv_path):
    config = {
        'influxdb': {'url': 'https://example.test:8086', 'org': 'o', 'bucket': 'b', 'token': 't', 'verify_ssl': False, 'retry_waits_s': []},
        'paths': {'registry_csv': str(csv_path), 'h5_path': 'store/custom.h5', 'output_dataset': 'out/dataset.xlsx'},
        'batch': {'test_type': 'GAIT'},
        'processing': {'min_strides': {'GAIT': 2, '6MWT': 30, '2MWT': 10, 'T25FW': 4}, 'imu_min_active_s': 5},
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
    ('10', 'PAT-1', '2025-01-01 10:00:00+01', '2025-01-01 10:00:20+01', 'GAIT'),
    ('11', 'PAT-1', '2025-01-02 10:00:00+01', '2025-01-02 10:00:20+01', 'GAIT'),
]
TRIAL = Trial('10', 'PAT-1', 'GAIT', 'a', 'b', '2025-01-01T10:00:00Z', '2025-01-01T10:00:20Z', 20.0)
KEY = '/p_PAT-1/GAIT/trial_10/Left'


def left_frame(seconds=20):
    return split_series(normalize_query_result(signal('Left', seconds=seconds)), '10')['Left'].frame


# --- LR-08: validación del registro -------------------------------------------------

def test_registry_missing_columns_fails_as_a_set(tmp_path):
    path = write_csv(tmp_path, [('1', 'P', 'GAIT')], columns=('id', 'codeid', 't_code'))
    with pytest.raises(RegistryError, match='d_from'):
        load_registry(str(path))


def test_registry_returns_result_per_invalid_row(tmp_path):
    path = write_csv(tmp_path, ROWS + [
        ('12', 'PAT-1', '2025-01-02 10:00:20', '2025-01-02 10:00:00', 'GAIT'),
        ('13', 'PAT-1', 'no-es-fecha', '2025-01-02 10:00:00', 'GAIT'),
        ('14', 'PAT"x', '2025-01-02 10:00:00', '2025-01-02 10:00:20', 'GAIT'),
        ('15', 'PAT-1', '2025-01-02 10:00:00', '2025-01-02 10:00:20', 'GAIT'),
        ('15', 'PAT-2', '2025-01-02 10:00:00', '2025-01-02 10:00:20', 'GAIT'),
    ])
    trials, invalid = load_registry(str(path))
    assert [t.trial_id for t in trials] == ['10', '11']
    assert trials[0].start_iso == '2025-01-01T10:00:00Z' and trials[0].duration_s == 20.0
    assert [r.trial_id for r in invalid] == ['12', '13', '14', '15', '15']
    assert all(r.status == 'INVALID_ROW' and r.detail for r in invalid)


def test_registry_validates_rows_outside_the_selection(tmp_path, caplog):
    path = write_csv(tmp_path, ROWS + [('20', 'PAT-2', 'mal', 'mal', '2MWT')])
    with caplog.at_level(logging.INFO, logger='msGait'):
        trials, invalid = load_registry(str(path), 'GAIT')
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
    # Las filas idénticas se eliminan; las que repiten hora con otros valores son muestras reales y se conservan.
    assert len(foot.frame) == 20 * FS + 1 and foot.exact_duplicates == 3 and foot.time_conflicts == 1
    assert not foot.was_sorted and foot.frame['_time'].is_monotonic_increasing
    assert foot.frame['Ax'].eq(9.0).sum() == 1 and foot.report()['time_conflicts'] == 1
    assert {'Foot', 'DeviceName', 'mac', 'lat', 'lng', 'CodeID', '_measurement', 'app', 'type'} <= set(foot.frame.columns)


def test_split_joins_the_devices_of_a_foot_by_time(caplog):
    # Cambio de calcetín por batería: los tramos de los dos dispositivos se unen.
    swapped = normalize_query_result([signal('Left', seconds=10), signal('Left', seconds=5, mac='BB:other', start='2025-01-01 10:00:10')])
    with caplog.at_level(logging.INFO, logger='msGait'):
        foot = split_series(swapped, '10')['Left']
    assert foot.device == 'AA:Left, BB:other' and len(foot.frame) == 15 * FS and foot.overlap_dropped == 0
    assert foot.devices_found == {'AA:Left': 1000, 'BB:other': 500} and foot.frame['_time'].is_monotonic_increasing
    assert 'INFO_MULTIPLE_DEVICES' in caplog.text and 'WARN_DEVICES_OVERLAP' not in caplog.text
    assert set(foot.frame['mac']) == {'AA:Left', 'BB:other'}

    # Si se solapan, en el tramo común se conserva el que tiene más muestras en él, con aviso.
    caplog.clear()
    dense = signal('Left', seconds=5, mac='BB:other', start='2025-01-01 10:00:08')
    sparse = signal('Left', seconds=10).iloc[::2]
    with caplog.at_level(logging.INFO, logger='msGait'):
        foot = split_series(normalize_query_result([sparse, dense]), '10')['Left']
    assert 'WARN_DEVICES_OVERLAP' in caplog.text and foot.overlap_dropped == 100
    start = pd.Timestamp('2025-01-01 10:00:08')
    assert set(foot.frame.loc[foot.frame['_time'] >= start, 'mac']) == {'BB:other'}
    assert set(foot.frame.loc[foot.frame['_time'] < start, 'mac']) == {'AA:Left'} and len(foot.frame) == 400 + 500


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
    assert (store_dir_for(h5) / 'p_PAT-1/GAIT/trial_10.h5').is_file() and not h5.exists()
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
    pd.DataFrame({'a': [1]}).to_hdf(h5, key='p_X/GAIT/trial_3', format='table')
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
    assert sorted(list_keys(h5)) == ['/p_PAT-1/GAIT/trial_10/Left', '/p_PAT-1/GAIT/trial_10/Right']

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
    csv = write_csv(tmp_path, ROWS + [('12', 'PAT-1', 'mal', 'mal', 'GAIT')])
    config = write_config(tmp_path, csv)
    responses = [[signal('Left'), signal('Right')], RuntimeError('timeout')]
    with patch('gait_analysis.extractor.InfluxDBClient', fake_client(responses)):
        extractor = GaitDataExtractor(str(config))
        results = extractor.run_batch_extraction()
    h5 = tmp_path / 'store/custom.h5'
    assert {r.trial_id: r.status for r in results} == {'12': 'INVALID_ROW', '10': 'OK', '11': 'QUERY_ERROR'}
    assert sorted(list_keys(h5)) == ['/p_PAT-1/GAIT/trial_10/Left', '/p_PAT-1/GAIT/trial_10/Right']
    assert read_meta(h5, '/p_PAT-1/GAIT/trial_10/Left')['d_from'] == '2025-01-01 10:00:00+01'

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
    from gait_analysis.pipeline import GaitPipeline
    csv = write_csv(tmp_path, ROWS)
    config = write_config(tmp_path, csv)
    # Evento 10: ambos pies. Evento 11: solo el izquierdo, más lento.
    responses = [[signal('Left'), signal('Right')], [signal('Left', start='2025-01-02 10:00:00', cadence_hz=0.5)]]
    client = fake_client(responses)
    with patch('gait_analysis.pipeline.InfluxDBClient', client):
        pipeline = GaitPipeline(str(config))
        results = pipeline.run_from_csv()
    assert [r.status for r in results] == ['OK', 'OK']
    query = client.return_value.query_api.return_value.query_data_frame.call_args_list[0].args[0]
    assert 'r["_measurement"] == "Gait"' in query

    # LR-05: el almacén es el configurado, no una ruta relativa al directorio de ejecución.
    assert len(list_keys(tmp_path / 'store/custom.h5')) == 3

    book = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name=None)
    assert set(book) == {
        'Leeme', 'Datos_Ensayos', 'Datos_Pacientes', 'Calidad_Series', 'Agregacion_Pacientes', 'Ensayos', 'Asimetria', 'Zancadas', 'Estado_Eventos',
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
    assert trials_sheet['cobertura_cadencia'].eq(1.0).all() and set(trials_sheet['filtro_altura'].dropna()) == {'sin_filtro'}
    assert {'zancadas', 'cadencia_zancada', 'contactos_talon', 'tiempo_zancada_seg', 'cobertura_imu', 'cobertura_presion',
            'inclinacion_grados', 'vertical_estimada', 'angulo_giros_grados', 'modalidad_zancadas'} <= set(trials_sheet.columns)
    assert set(trials_sheet['modalidad_zancadas']) == {'S2'} and 'pasos' not in trials_sheet.columns
    # Validación sin etiquetas: los dos pies del evento 10 cuentan las mismas zancadas.
    assert asymmetry.loc[10, ['zancadas_Left', 'zancadas_Right', 'diferencia_zancadas']].tolist() == [18, 18, 0]
    assert bool(asymmetry.loc[10, 'zancadas_coherentes']) and pd.isna(asymmetry.loc[11, 'diferencia_zancadas'])

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
    assert by_trial.loc[['n_total', 'n_validos', 'n_sin_valor'], 'GAIT_Right_cadencia_zancada'].tolist() == [2, 1, 1]
    assert by_trial.loc[['n_total', 'n_validos'], 'GAIT_symmetry_index'].tolist() == [2, 1]
    assert by_patient.loc['n_validos', 'GAIT_Left_cadencia_zancada'] == 1

    # LR-01: agregación posterior por paciente con regla explícita (media; asimetría solo bilateral).
    patients, trace = book['Datos_Pacientes'], book['Agregacion_Pacientes']
    assert patients['CodeID'].tolist() == ['PAT-1'] and 'TrialID' not in patients.columns
    expected = round((asymmetry.loc[10, 'cadencia_Left'] + asymmetry.loc[11, 'cadencia_Left']) / 2, 2)
    assert patients.loc[0, 'GAIT_Left_cadencia_zancada'] == pytest.approx(expected)
    assert patients.loc[0, 'GAIT_Right_cadencia_zancada'] == pytest.approx(asymmetry.loc[10, 'cadencia_Right'])
    assert patients.loc[0, 'GAIT_symmetry_index'] == 0.0
    assert trace[['n_ensayos', 'n_bilaterales', 'eventos']].values.tolist() == [[2, 1, '10, 11']]
    assert 'asimetría solo con ensayos bilaterales' in trace.loc[0, 'regla']

    # Reanudación: con todo completado se recalcula desde el HDF5 sin consultar.
    client = fake_client([])
    with patch('gait_analysis.pipeline.InfluxDBClient', client):
        resumed = GaitPipeline(str(config)).run_from_csv(missing_only=True)
    assert [r.source for r in resumed] == ['hdf5', 'hdf5']
    again = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name='Ensayos')
    assert again['cadencia_zancada'].tolist() == book['Ensayos']['cadencia_zancada'].tolist()
    # El dispositivo se recupera de los atributos guardados, no queda vacío.
    assert again['dispositivo'].tolist() == ['AA:Left', 'AA:Right', 'AA:Left']

    # Auditoría desde el mismo comando, sin consultar.
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client([])):
        assert GaitPipeline(str(config)).audit() == {'missing': [], 'orphan': [], 'legacy': [], 'unconfirmed': []}


def test_path_defaults_are_shared_and_independent_of_cwd(tmp_path, monkeypatch):
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump({'influxdb': {'url': 'https://example.test:8086', 'org': 'o', 'bucket': 'b', 'token': 't'}}))
    monkeypatch.chdir(tmp_path)
    paths = load_config(path)['paths']
    assert paths['registry_csv'] == str(PROJECT_ROOT / 'tests.csv')
    assert paths['h5_path'] == str(PROJECT_ROOT / 'data' / 'raw' / 'gait_study_data.h5')
    assert paths['output_dataset'] == str(PROJECT_ROOT / 'resultados_excel' / 'DatasetMaestro.xlsx')
    from gait_analysis.pipeline import GaitPipeline
    with patch('gait_analysis.pipeline.InfluxDBClient'), patch('gait_analysis.extractor.InfluxDBClient'):
        assert GaitPipeline(str(path)).h5_path == GaitDataExtractor(str(path))._h5_database == paths['h5_path']


# --- Política de información por niveles ----------------------------------------------

@pytest.mark.parametrize('verbose,level', [(0, logging.WARNING), (1, logging.INFO), (2, logging.DEBUG), (9, logging.DEBUG)])
def test_verbosity_levels(verbose, level):
    assert configure_logging(verbose) == level
    assert logging.getLogger('msGait.Pipeline').getEffectiveLevel() == level
    assert logging.getLogger('urllib3').getEffectiveLevel() == logging.WARNING


# --- Mínimos por prueba, intentos juntos, reintentos y consultas en paralelo ------------

def step_signal(side, seconds, start, cadence_hz=1.0):
    """Señal con talón cargado el 60 % del ciclo, para pruebas cortas."""
    frame = signal(side, seconds=seconds, start=start, cadence_hz=cadence_hz)
    t = np.arange(len(frame)) / FS
    pressure = np.where((t * cadence_hz) % 1.0 < 0.6, 100.0, 300.0) + np.arange(len(frame)) % 2
    for column in ('S0', 'S1', 'S2'):
        frame[column] = pressure
    return frame


def test_tug_reports_no_gait_metrics_and_t25fw_pools_attempts_of_the_same_day(tmp_path):
    from gait_analysis.pipeline import GaitPipeline
    rows = [
        ('20', 'PAT-1', '2025-01-01 10:00:00+01', '2025-01-01 10:00:12+01', 'TUG'),
        ('21', 'PAT-1', '2025-01-01 10:05:00+01', '2025-01-01 10:05:05+01', 'T25FW'),
        ('22', 'PAT-1', '2025-01-01 10:06:00+01', '2025-01-01 10:06:05+01', 'T25FW'),
        ('23', 'PAT-1', '2025-02-01 10:00:00+01', '2025-02-01 10:00:05+01', 'T25FW'),
    ]
    config = write_config(tmp_path, write_csv(tmp_path, rows))
    responses = [
        [step_signal('Left', 12, '2025-01-01 10:00:00'), step_signal('Right', 12, '2025-01-01 10:00:00')],
        [step_signal('Left', 5, '2025-01-01 10:05:00')], [step_signal('Left', 5, '2025-01-01 10:06:00')],
        [step_signal('Left', 5, '2025-02-01 10:00:00')],
    ]
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client(responses)):
        results = GaitPipeline(str(config)).run_from_csv(test_type='all')
    assert [r.status for r in results] == ['OK'] * 4
    book = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name=None)
    trials = book['Ensayos'].set_index(['TrialID', 'Foot'])
    patients = book['Datos_Pacientes'].replace('N/A', np.nan)

    # TUG: ni cadencia ni fases ni longitud ni altura; queda la duración.
    tug = trials.loc[(20, 'Left')]
    assert pd.isna(tug['cadencia_zancada']) and pd.isna(tug['apoyo_prc']) and pd.isna(tug['stride_length_cm'])
    assert tug['estado_cadencia'] == 'NO_APLICA' and 'no se informa en TUG' in tug['motivo_cadencia']
    assert patients.loc[0, 'TUG_duracion_seg'] == 12.0 and pd.isna(patients.loc[0, 'TUG_Left_cadencia_zancada'])
    assert not bool(book['Asimetria'].set_index('TrialID').loc[20, 'bilateral_available'])

    # T25FW: un intento de 5 s deja 2 zancadas tras descartar la primera (mínimo 4): sin cadencia propia...
    assert trials.loc[(21, 'Left'), 'contactos_talon'] == 4 and trials.loc[(21, 'Left'), 'min_zancadas'] == 4
    assert trials.loc[(21, 'Left'), 'zancadas'] == 2                    # 3 zancadas menos la primera (arranque)
    assert pd.isna(trials.loc[(21, 'Left'), 'cadencia_zancada']) and trials.loc[(21, 'Left'), 'motivo_cadencia'] == 'ERR_TOO_FEW_STRIDES'
    # ...pero los dos intentos del mismo día suman 4 y dan la cadencia del paciente; el del otro día, solo, no.
    assert patients.loc[0, 'T25FW_Left_cadencia_zancada'] == pytest.approx(60, abs=1)
    assert patients.loc[0, 'T25FW_Left_apoyo_prc'] == pytest.approx(60, abs=3)
    trace = book['Agregacion_Pacientes'].set_index('test_type')
    assert trace.loc['T25FW', 'zancadas_juntadas'] == 4 and 'orientativo' in trace.loc['T25FW', 'regla']
    assert 'orientativo' not in trace.loc['TUG', 'regla']


def test_connection_failures_are_retried_and_recorded(tmp_path, caplog):
    from gait_analysis.pipeline import GaitPipeline
    path = write_config(tmp_path, write_csv(tmp_path, ROWS))
    config = yaml.safe_load(path.read_text())
    config['influxdb']['retry_waits_s'] = [0, 0]
    path.write_text(yaml.safe_dump(config))
    responses = [ConnectionError('timeout'), [signal('Left'), signal('Right')],
                 ConnectionError('timeout'), ConnectionError('timeout'), ConnectionError('timeout')]
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client(responses)), caplog.at_level(logging.WARNING, logger='msGait'):
        results = GaitPipeline(str(path)).run_from_csv()
    assert [(r.status, r.attempts) for r in results] == [('OK', 2), ('QUERY_ERROR', 3)]
    assert caplog.text.count('WARN_QUERY_RETRY') == 3
    records = [r for r in Manifest(manifest_path_for(tmp_path / 'store/custom.h5')).records() if r['record'] == 'event']
    assert [r['attempts'] for r in records] == [2, 3]
    with pytest.raises(Exception):
        from msConfig import load_config as load
        config['influxdb']['retry_waits_s'] = [-1]
        path.write_text(yaml.safe_dump(config))
        load(path)


def test_parallel_queries_keep_order_and_a_single_writer(tmp_path):
    from gait_analysis.pipeline import GaitPipeline
    rows = [(str(30 + i), 'PAT-1', f'2025-01-0{i + 1} 10:00:00+01', f'2025-01-0{i + 1} 10:00:20+01', 'GAIT') for i in range(5)]
    config = write_config(tmp_path, write_csv(tmp_path, rows))
    client = Mock()

    def answer(query):
        day = query.split('start: 2025-01-0')[1][0]
        return [signal('Left', start=f'2025-01-0{day} 10:00:00'), signal('Right', start=f'2025-01-0{day} 10:00:00')]

    client.return_value.query_api.return_value.query_data_frame.side_effect = answer
    with patch('gait_analysis.pipeline.InfluxDBClient', client):
        results = GaitPipeline(str(config)).run_from_csv(workers=3)
    assert [r.trial_id for r in results] == ['30', '31', '32', '33', '34'] and {r.status for r in results} == {'OK'}
    assert client.return_value.query_api.return_value.query_data_frame.call_count == 5
    assert len(list_keys(tmp_path / 'store/custom.h5')) == 10 and not Path(f"{tmp_path / 'store/custom.h5'}.lock").exists()
    assert pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name='Ensayos')['TrialID'].tolist() == [30, 30, 31, 31, 32, 32, 33, 33, 34, 34]


def test_trial_table_quality_sheet_derived_events_and_excluded_patients(tmp_path, caplog):
    from gait_analysis.storage import events_path_for
    from gait_analysis.pipeline import GaitPipeline
    rows = ROWS + [('40', 'AMIR-48', '2025-01-03 10:00:00+01', '2025-01-03 10:00:20+01', 'GAIT'),
                   ('41', 'PAT-1', '2025-01-04 10:00:00+01', '2025-01-04 10:00:20+01', 'OTRA')]
    path = write_config(tmp_path, write_csv(tmp_path, rows))
    config = yaml.safe_load(path.read_text())
    config['registry'] = {'exclude_codeids': ['AMIR-48']}
    path.write_text(yaml.safe_dump(config))
    responses = [[signal('Left'), signal('Right')], [signal('Left', start='2025-01-02 10:00:00')],
                 [signal('Left', start='2025-01-04 10:00:00')]]
    client = fake_client(responses)
    with patch('gait_analysis.pipeline.InfluxDBClient', client), caplog.at_level(logging.INFO, logger='msGait'):
        results = GaitPipeline(str(path)).run_from_csv(test_type='all')
    # El paciente excluido no se consulta ni se borra del registro: queda con su estado y motivo.
    assert {r.trial_id: r.status for r in results} == {'40': 'EXCLUDED', '10': 'OK', '11': 'OK', '41': 'OK'}
    assert client.return_value.query_api.return_value.query_data_frame.call_count == 3
    book = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name=None)
    assert 'registry.exclude_codeids' in book['Estado_Eventos'].set_index('trial_id').loc[40, 'detail']

    # Una fila por ensayo con su fecha, sin condensar: lista para seguir al paciente en el tiempo.
    per_trial = book['Datos_Ensayos']
    assert per_trial['TrialID'].tolist() == [10, 11, 41] and per_trial['d_from'].iloc[0] == '2025-01-01 10:00:00+01'
    assert per_trial.loc[0, 'GAIT_Left_cadencia_zancada'] == pytest.approx(60, abs=1)
    assert len(book['Datos_Pacientes']) == 1

    # Una prueba sin mínimo de zancadas definido no informa métricas de marcha.
    other = book['Ensayos'].set_index('TrialID').loc[41]
    assert other['estado_cadencia'] == 'NO_APLICA' and 'processing.min_strides' in other['motivo_cadencia']

    quality = book['Calidad_Series']
    assert {'criterio_presion', 'rango_talon', 'periodicidad_talon', 'imu_estado', 'min_zancadas', 'motivo_cadencia'} <= set(quality.columns)
    assert len(quality) == 4 and set(quality['criterio_presion'].dropna()) == {'periodicidad'} and 'INFO_QUALITY_SUMMARY' in caplog.text
    assert quality.set_index('TrialID').loc[41, ['criterio_presion']].isna().all()       # sin análisis de marcha no hay criterio

    # Lo detectado en cada evento queda guardado para reconstruir de dónde sale cada cifra.
    # Carpeta de resultados separada de los datos en bruto; una tabla de eventos por ensayo y pie.
    derived = tmp_path / 'results/eventos/p_PAT-1/GAIT/trial_10.h5'
    assert derived == events_path_for(tmp_path / 'results', Trial('10', 'PAT-1', 'GAIT', '', '', '', '', 20.0))
    assert not (tmp_path / 'store').joinpath('custom_derivados').exists()
    with pd.HDFStore(str(derived), mode='r') as store:
        assert {'/Left/eventos', '/Right/eventos'} <= set(store.keys())
        events = store.get('Left/eventos')
        meta = store.get_storer('Left/eventos').attrs.trial_meta
    assert {'tipo', 'inicio_s', 'fin_s', 'motivo', 'instante'} <= set(events.columns)
    kinds = events['tipo'].value_counts().to_dict()
    assert kinds['contacto_talon'] == 20 and kinds['despegue'] >= 18 and kinds['tramo_excluido'] == 1
    assert events.loc[events['tipo'] == 'tramo_excluido', 'motivo'].tolist() == ['arranque']
    assert events['instante'].min() >= pd.Timestamp('2025-01-01 10:00:00') and events['instante'].max() < pd.Timestamp('2025-01-01 10:00:21')
    assert meta['version_pipeline'] and len(meta['huella_config_processing']) == 16
    assert pd.Timestamp(meta['fecha_ejecucion']) <= pd.Timestamp.now()

    # El estado de confianza acompaña a cada valor; los WARNING entran en las medias y se cuentan.
    assert per_trial.loc[0, 'GAIT_Left_estado_cadencia'] == 'OK' and 'GAIT_Left_estado_fases' in per_trial.columns
    patients = book['Datos_Pacientes']
    assert {'GAIT_ensayos_warning', 'GAIT_Left_estado_cadencia_n_warning'} <= set(patients.columns) and not any(column.endswith('estado_cadencia') for column in patients.columns)
    assert {'imu_estado', 'presion_activa_seg', 'giros_excluidos', 'zancadas_margen_giro'} <= set(quality.columns)

def test_only_network_errors_are_retried(tmp_path):
    from gait_analysis.extractor import is_network_error
    from influxdb_client.rest import ApiException
    assert is_network_error(ConnectionError('rechazada')) and is_network_error(TimeoutError('tiempo agotado'))
    assert is_network_error(ApiException(status=503, reason='no disponible'))
    assert not is_network_error(ApiException(status=401, reason='no autorizado'))
    assert not is_network_error(ApiException(status=400, reason='consulta mal formada'))
    assert not is_network_error(ValueError('contenido')) and not is_network_error(FileNotFoundError('x'))

    from gait_analysis.pipeline import GaitPipeline
    path = write_config(tmp_path, write_csv(tmp_path, ROWS))
    config = yaml.safe_load(path.read_text())
    config['influxdb']['retry_waits_s'] = [0, 0]
    path.write_text(yaml.safe_dump(config))
    client = fake_client([ApiException(status=401, reason='no autorizado'), [signal('Left', start='2025-01-02 10:00:00')]])
    with patch('gait_analysis.pipeline.InfluxDBClient', client) as constructor:
        results = GaitPipeline(str(path)).run_from_csv()
    # Un 401 no se reintenta: un solo intento y se sigue con el resto del lote.
    assert [(r.status, r.attempts) for r in results] == [('QUERY_ERROR', 1), ('OK', 1)]
    # Tiempo de conexión corto, separado del de lectura.
    assert constructor.call_args.kwargs['timeout'][0] == 10000


def test_t25fw_stride_lengths_are_checked_against_the_test_distance_as_a_warning(caplog):
    from gait_analysis.metrics import ProcessingConfig
    from gait_analysis.pipeline import GaitPipeline
    from gait_analysis.registry import Trial
    pipeline = object.__new__(GaitPipeline)
    pipeline.processing = ProcessingConfig()
    assert pipeline.processing.t25fw_distance_m == 7.62

    def strides(lengths_cm, excluded=0):
        return [{'longitud_cm': value, 'motivo_exclusion': ''} for value in lengths_cm] + \
               [{'longitud_cm': float('nan'), 'motivo_exclusion': 'en_giro'}] * excluded

    def check(test, events):
        trial = Trial('21', 'PAT-1', test, 'a', 'b', 'a', 'b', 5.0)
        with caplog.at_level(logging.WARNING, logger='msGait'):
            caplog.clear()
            return pipeline._t25fw_distance_check(trial, 'Left', events), 'WARN_T25FW_DISTANCE' in caplog.text

    # 5 zancadas de 1,2 m = 6,0 m: faltan 1,62 m, menos de dos zancadas (parciales del principio y del final).
    assert check('T25FW', strides([120] * 5)) == ({'t25fw_suma_longitudes_m': 6.0, 't25fw_distancia_coherente': True}, False)
    # Más distancia que la de la prueba, o mucha menos: aviso, sin tocar la longitud ni su estado.
    assert check('T25FW', strides([120] * 8)) == ({'t25fw_suma_longitudes_m': 9.6, 't25fw_distancia_coherente': False}, True)
    assert check('T25FW', strides([120] * 2)) == ({'t25fw_suma_longitudes_m': 2.4, 't25fw_distancia_coherente': False}, True)
    # Cada zancada que no pudo integrarse amplía el margen en una zancada media.
    assert check('T25FW', strides([120] * 2, excluded=3))[0]['t25fw_distancia_coherente'] is True
    result, warned = check('T25FW', strides([], excluded=2))
    assert np.isnan(result['t25fw_suma_longitudes_m']) and not warned
    assert check('6MWT', strides([120] * 50)) == ({}, False)       # solo en T25FW: no hay distancia anotada en el 6MWT


# --- Feedback del tutor del 18/10/2026: integridad (fase 1) ---------------------------

def test_resume_never_relabels_stored_signals_of_another_registry_row(tmp_path, caplog):
    """Mismo identificador, otro paciente: lo guardado no se reutiliza con la etiqueta nueva."""
    from gait_analysis.pipeline import GaitPipeline
    config = write_config(tmp_path, write_csv(tmp_path, ROWS))
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client([[signal('Left'), signal('Right')],
                                                                      [signal('Left', start='2025-01-02 10:00:00')]])):
        GaitPipeline(str(config)).run_from_csv()
    # El registro cambia después: el evento 10 pasa a ser de PAT-2 conservando el identificador.
    write_csv(tmp_path, [('10', 'PAT-2', *ROWS[0][2:]), ROWS[1]])

    with caplog.at_level(logging.ERROR, logger='msGait'):
        results = GaitPipeline(str(config), offline=True).run_from_csv(stored_only=True)
    mismatch = {r.trial_id: r for r in results}['10']
    assert mismatch.status == 'IDENTITY_MISMATCH' and 'codeid: PAT-1 != PAT-2' in mismatch.detail
    assert {r.trial_id: r.status for r in results}['11'] == 'OK' and 'ERR_STORED_IDENTITY' in caplog.text
    book = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name=None)
    assert book['Datos_Ensayos']['TrialID'].tolist() == [11] and 'PAT-2' not in set(book['Ensayos']['CodeID'])

    # Con conexión (--missing-only) se vuelve a consultar y se guarda con la identidad nueva.
    client = fake_client([[signal('Left'), signal('Right')]])
    with patch('gait_analysis.pipeline.InfluxDBClient', client):
        resumed = GaitPipeline(str(config)).run_from_csv(missing_only=True)
    assert {r.trial_id: (r.status, r.source) for r in resumed} == {'10': ('OK', 'influx'), '11': ('OK', 'hdf5')}
    assert client.return_value.query_api.return_value.query_data_frame.call_count == 1
    assert read_meta(tmp_path / 'store/custom.h5', '/p_PAT-2/GAIT/trial_10/Left')['codeid'] == 'PAT-2'


def test_reanalysis_without_events_replaces_the_previous_derived_file(tmp_path):
    from gait_analysis.storage import NO_EVENTS, events_path_for, write_derived
    previous = {'Left/eventos': pd.DataFrame({'tipo': ['contacto_talon'], 'inicio_s': [1.0]})}
    target = write_derived(tmp_path, TRIAL, previous, {'huella_config_processing': 'antigua'})
    assert target == events_path_for(tmp_path, TRIAL)

    assert write_derived(tmp_path, TRIAL, {'Left/eventos': pd.DataFrame()}, {'huella_config_processing': 'nueva'}) == target
    with pd.HDFStore(str(target), mode='r') as store:
        assert store.keys() == ['/resumen'] and store.get('resumen')['motivo'].tolist() == [NO_EVENTS]
        assert store.get_storer('resumen').attrs.trial_meta == {'huella_config_processing': 'nueva'}


def test_run_without_data_fails_sets_the_previous_excel_aside_and_closes_after_publishing(tmp_path):
    from gait_analysis.pipeline import GaitPipeline, NoDataError
    config = write_config(tmp_path, write_csv(tmp_path, ROWS))
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client([[signal('Left'), signal('Right')],
                                                                      [signal('Left', start='2025-01-02 10:00:00')]])):
        GaitPipeline(str(config)).run_from_csv()
    manifest = Manifest(manifest_path_for(tmp_path / 'store/custom.h5'))
    output = tmp_path / 'out/dataset.xlsx'
    excel = manifest.records()[-1]['excel']
    assert excel['estado'] == 'publicado' and Path(excel['ruta']) == output and output.exists()

    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client([RuntimeError('timeout')] * 2)):
        with pytest.raises(NoDataError, match='ERR_NO_DATA_PROCESSED'):
            GaitPipeline(str(config)).run_from_csv()
    assert not output.exists()
    aside = list(output.parent.glob('dataset.anterior-*.xlsx'))
    assert len(aside) == 1 and pd.read_excel(aside[0], sheet_name='Datos_Ensayos')['TrialID'].tolist() == [10, 11]
    last = manifest.records()[-1]
    assert last['record'] == 'run_end' and last['excel']['estado'] == 'no_publicado' and aside[0].name in last['excel']['detalle']


def test_excel_publication_failure_is_recorded_when_closing_the_run(tmp_path):
    from gait_analysis.pipeline import GaitPipeline
    config = write_config(tmp_path, write_csv(tmp_path, ROWS))
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client([[signal('Left'), signal('Right')],
                                                                      [signal('Left', start='2025-01-02 10:00:00')]])), \
            patch('gait_analysis.pipeline.publish_excel', side_effect=StorageError('archivo abierto')):
        with pytest.raises(StorageError):
            GaitPipeline(str(config)).run_from_csv()
    last = Manifest(manifest_path_for(tmp_path / 'store/custom.h5')).records()[-1]
    assert last['record'] == 'run_end' and last['excel'] == {'estado': 'error', 'detalle': 'archivo abierto'}


def test_check_config_validates_the_processing_parameters(tmp_path, capsys):
    from gait_analysis.pipeline import main
    config = write_config(tmp_path, write_csv(tmp_path, ROWS))
    with pytest.raises(SystemExit) as valid:
        main(['--config', str(config), '--check-config'])
    assert valid.value.code == 0 and 'Configuración válida' in capsys.readouterr().out

    data = yaml.safe_load(config.read_text())
    data['processing']['max_interpolation_s'] = 0.5          # mayor que max_gap_s: el YAML se carga, el análisis no
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(SystemExit) as invalid:
        main(['--config', str(config), '--check-config'])
    assert 'max_interpolation_s' in str(invalid.value.code)


@pytest.mark.parametrize('axis, accepted', [('vertical', True), ('Gx', True), ('Gz', True), ('Ax', False), ('Az', False)])
def test_turn_axis_only_accepts_angular_velocity(axis, accepted):
    from gait_analysis.metrics import ProcessingConfig
    from msConfig import ConfigError
    if accepted:
        assert ProcessingConfig.from_mapping({'turn_axis': axis}).turn_axis == axis
    else:
        with pytest.raises(ConfigError, match='turn_axis'):
            ProcessingConfig.from_mapping({'turn_axis': axis})


# --- Feedback del tutor del 18/10/2026: cálculo (fase 2) ------------------------------

def test_t25fw_asymmetry_never_compares_feet_from_different_days(tmp_path):
    """Pie izquierdo válido un día y derecho otro día: no hay ningún día bilateral, no hay asimetría."""
    from gait_analysis.pipeline import GaitPipeline
    rows = [
        ('31', 'PAT-1', '2025-01-01 10:05:00+01', '2025-01-01 10:05:05+01', 'T25FW'),
        ('32', 'PAT-1', '2025-01-01 10:06:00+01', '2025-01-01 10:06:05+01', 'T25FW'),
        ('33', 'PAT-1', '2025-02-01 10:05:00+01', '2025-02-01 10:05:08+01', 'T25FW'),
        ('34', 'PAT-1', '2025-02-01 10:06:00+01', '2025-02-01 10:06:08+01', 'T25FW'),
    ]
    config = write_config(tmp_path, write_csv(tmp_path, rows))
    responses = [
        [step_signal('Left', 5, '2025-01-01 10:05:00')], [step_signal('Left', 5, '2025-01-01 10:06:00')],
        [step_signal('Right', 8, '2025-02-01 10:05:00', cadence_hz=0.8)], [step_signal('Right', 8, '2025-02-01 10:06:00', cadence_hz=0.8)],
    ]
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client(responses)):
        GaitPipeline(str(config)).run_from_csv(test_type='all')
    book = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name=None)
    patients = book['Datos_Pacientes'].replace('N/A', np.nan)
    # Cada pie tiene su cadencia del paciente (de sesiones distintas)...
    assert patients.loc[0, 'T25FW_Left_cadencia_zancada'] == pytest.approx(60, abs=1)
    assert patients.loc[0, 'T25FW_Right_cadencia_zancada'] == pytest.approx(48, abs=1)
    # ...pero compararlas daría un 22 % sin ningún ensayo bilateral: la asimetría queda vacía.
    assert pd.isna(patients.loc[0, 'T25FW_symmetry_index'])
    trace = book['Agregacion_Pacientes'].set_index('test_type')
    assert trace.loc['T25FW', 'sesiones_bilaterales'] == 0 and 'sesiones distintas' in trace.loc['T25FW', 'regla']


def test_t25fw_asymmetry_is_the_mean_of_the_bilateral_days(tmp_path):
    from gait_analysis.pipeline import GaitPipeline
    rows = [
        ('41', 'PAT-1', '2025-01-01 10:05:00+01', '2025-01-01 10:05:05+01', 'T25FW'),
        ('42', 'PAT-1', '2025-01-01 10:06:00+01', '2025-01-01 10:06:05+01', 'T25FW'),
        ('43', 'PAT-1', '2025-02-01 10:05:00+01', '2025-02-01 10:05:08+01', 'T25FW'),
        ('44', 'PAT-1', '2025-02-01 10:06:00+01', '2025-02-01 10:06:08+01', 'T25FW'),
    ]
    config = write_config(tmp_path, write_csv(tmp_path, rows))
    both = lambda start, right_hz, seconds=5: [step_signal('Left', seconds, start), step_signal('Right', seconds, start, cadence_hz=right_hz)]
    responses = [both('2025-01-01 10:05:00', 1.0), both('2025-01-01 10:06:00', 1.0),      # día 1: 60 y 60
                 both('2025-02-01 10:05:00', 0.8, 8), both('2025-02-01 10:06:00', 0.8, 8)]      # día 2: 60 y 48
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client(responses)):
        GaitPipeline(str(config)).run_from_csv(test_type='all')
    book = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name=None)
    patients = book['Datos_Pacientes'].replace('N/A', np.nan)
    asymmetry = patients.loc[0, 'T25FW_symmetry_index']
    expected = (0.0 + abs(60 - 48) / 54 * 100) / 2                        # media de los índices diarios
    assert asymmetry == pytest.approx(expected, abs=1.0)
    assert book['Agregacion_Pacientes'].set_index('test_type').loc['T25FW', 'sesiones_bilaterales'] == 2


def test_t25fw_morning_and_afternoon_sessions_of_the_same_day_are_not_pooled(tmp_path):
    """Mismo día, dos sesiones separadas más de pooling_session_gap_s: no se juntan ni se comparan entre sí."""
    from gait_analysis.pipeline import GaitPipeline
    rows = [
        ('51', 'PAT-1', '2025-01-01 10:05:00+01', '2025-01-01 10:05:05+01', 'T25FW'),     # mañana: solo el izquierdo
        ('52', 'PAT-1', '2025-01-01 10:06:00+01', '2025-01-01 10:06:05+01', 'T25FW'),
        ('53', 'PAT-1', '2025-01-01 17:05:00+01', '2025-01-01 17:05:08+01', 'T25FW'),     # tarde: solo el derecho
        ('54', 'PAT-1', '2025-01-01 17:06:00+01', '2025-01-01 17:06:08+01', 'T25FW'),
    ]
    config = write_config(tmp_path, write_csv(tmp_path, rows))
    responses = [
        [step_signal('Left', 5, '2025-01-01 10:05:00')], [step_signal('Left', 5, '2025-01-01 10:06:00')],
        [step_signal('Right', 8, '2025-01-01 17:05:00', cadence_hz=0.8)], [step_signal('Right', 8, '2025-01-01 17:06:00', cadence_hz=0.8)],
    ]
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client(responses)):
        GaitPipeline(str(config)).run_from_csv(test_type='all')
    book = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name=None)
    patients = book['Datos_Pacientes'].replace('N/A', np.nan)
    assert patients.loc[0, 'T25FW_Left_cadencia_zancada'] == pytest.approx(60, abs=1)
    assert patients.loc[0, 'T25FW_Right_cadencia_zancada'] == pytest.approx(48, abs=1)
    # Por día habría salido un 22 % comparando la mañana con la tarde; por sesión no hay ninguna bilateral.
    assert pd.isna(patients.loc[0, 'T25FW_symmetry_index'])
    assert book['Agregacion_Pacientes'].set_index('test_type').loc['T25FW', 'sesiones_bilaterales'] == 0


def test_sessions_are_cut_by_the_gap_between_attempts():
    from gait_analysis.pipeline import GaitPipeline
    at = lambda text: pd.Timestamp(f'2025-01-01 {text}')
    rows = [{'TrialID': tid, 'CodeID': code, 'inicio': at(start), 'fin': at(stop)} for tid, code, start, stop in (
        ('1', 'A', '10:00:00', '10:00:10'), ('2', 'A', '10:01:00', '10:01:10'),
        ('3', 'A', '10:31:10', '10:31:20'),                                   # 30 min justos tras el fin del 2: misma sesión
        ('4', 'A', '11:01:21', '11:01:30'),                                   # 30 min y 1 s: sesión nueva
        ('5', 'B', '10:00:30', '10:00:40'))]                                  # otro paciente, otra sesión
    sessions = GaitPipeline._sessions(rows, pd.Timedelta(minutes=30))
    assert sessions['1'] == sessions['2'] == sessions['3'] != sessions['4']
    assert sessions['5'] != sessions['1']


# --- D14: criterio horario configurable (registry.influx_time) --------------------------

@pytest.mark.parametrize('registry, civil, utc', [
    ('2025-12-12 19:24:00+01', '2025-12-12T19:24:00Z', '2025-12-12T18:24:00Z'),     # invierno: una hora
    ('2026-07-01 19:24:00+02', '2026-07-01T19:24:00Z', '2026-07-01T17:24:00Z'),     # verano: dos horas
])
def test_registry_times_follow_the_configured_criterion(tmp_path, registry, civil, utc):
    from msTools.timeutils import influx_time_z
    assert influx_time_z(registry) == influx_time_z(registry, 'civil') == civil
    assert influx_time_z(registry, 'utc') == utc
    path = write_csv(tmp_path, [('1', 'PAT-1', registry, registry.replace(':24:00', ':30:00'), '6MWT')])
    by_default, _ = load_registry(str(path))
    in_utc, _ = load_registry(str(path), time_criterion='utc')
    assert (by_default[0].start_iso, by_default[0].time_criterion) == (civil, 'civil')
    assert (in_utc[0].start_iso, in_utc[0].time_criterion) == (utc, 'utc')
    assert in_utc[0].duration_s == by_default[0].duration_s == 360.0


def test_influx_time_option_is_validated(tmp_path):
    from msConfig import ConfigError
    config = write_config(tmp_path, write_csv(tmp_path, ROWS))
    assert load_config(config)['registry']['influx_time'] == 'civil'
    data = yaml.safe_load(config.read_text())
    data['registry'] = {'influx_time': 'madrid'}
    config.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match='influx_time'):
        load_config(config)


def test_signals_downloaded_with_another_time_criterion_are_not_reused(tmp_path, caplog):
    """Cambiar de civil a utc obliga a volver a descargar: la ventana buscada era otra."""
    from gait_analysis.pipeline import GaitPipeline
    config = write_config(tmp_path, write_csv(tmp_path, ROWS))
    with patch('gait_analysis.pipeline.InfluxDBClient', fake_client([[signal('Left'), signal('Right')],
                                                                      [signal('Left', start='2025-01-02 10:00:00')]])):
        GaitPipeline(str(config)).run_from_csv()
    assert read_meta(tmp_path / 'store/custom.h5', KEY)['hora_influx'] == 'civil'
    data = yaml.safe_load(config.read_text())
    data['registry'] = {'influx_time': 'utc'}
    config.write_text(yaml.safe_dump(data))
    from gait_analysis.pipeline import NoDataError
    with caplog.at_level(logging.ERROR, logger='msGait'), pytest.raises(NoDataError):
        GaitPipeline(str(config), offline=True).run_from_csv(stored_only=True)      # nada reutilizable: sin datos
    assert caplog.text.count('ERR_STORED_IDENTITY') == 2 and 'hora_influx: civil != utc' in caplog.text
    # Y con conexión se vuelven a consultar, ahora con la ventana en UTC (una hora antes en invierno).
    client = fake_client([[signal('Left', start='2025-01-01 09:00:00'), signal('Right', start='2025-01-01 09:00:00')],
                          [signal('Left', start='2025-01-02 09:00:00')]])
    with patch('gait_analysis.pipeline.InfluxDBClient', client):
        resumed = GaitPipeline(str(config)).run_from_csv(missing_only=True)
    assert {r.status for r in resumed} == {'OK'} and client.return_value.query_api.return_value.query_data_frame.call_count == 2
    query = client.return_value.query_api.return_value.query_data_frame.call_args_list[0].args[0]
    assert 'start: 2025-01-01T09:00:00' in query
    provenance = pd.read_excel(tmp_path / 'out/dataset.xlsx', sheet_name='Procedencia').set_index('clave')['valor']
    assert provenance['criterio_hora_influx'] == 'utc'
