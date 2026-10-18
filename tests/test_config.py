"""Offline configuration contracts. Never load the repository's real secrets."""
import os
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pkgs'), str(ROOT / 'src'), str(ROOT)]
from msConfig import ConfigError, load_config


def fixture_config(tmp_path, extra=None):
    config = {'influxdb': {'url':'https://example.test:8086','org':'synthetic','bucket':'test','token':'${AUDIT_TOKEN}','verify_ssl':'${AUDIT_VERIFY}','timeout':'${AUDIT_TIMEOUT}'}, 'paths':{'registry_csv':'input/events.csv'}}
    if extra:
        config.update(extra)
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config))
    (tmp_path / '.env').write_text("AUDIT_TOKEN='synthetic-$literal#token'\nAUDIT_VERIFY=false\nAUDIT_TIMEOUT=45000\n")
    return path


def test_env_precedence_types_and_no_process_mutation(tmp_path, monkeypatch):
    path = fixture_config(tmp_path)
    monkeypatch.setenv('AUDIT_TOKEN', 'process-token')
    monkeypatch.delenv('AUDIT_VERIFY', raising=False)
    monkeypatch.delenv('AUDIT_TIMEOUT', raising=False)
    result = load_config(path)
    assert result['influxdb']['token'] == 'process-token'
    assert result['influxdb']['verify_ssl'] is False
    assert result['influxdb']['timeout'] == 45000
    assert 'AUDIT_VERIFY' not in os.environ


def test_literal_secret_and_paths_independent_of_cwd(tmp_path, monkeypatch):
    path = fixture_config(tmp_path)
    for name in ('AUDIT_TOKEN','AUDIT_VERIFY','AUDIT_TIMEOUT'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir('/')
    result = load_config(path)
    assert result['influxdb']['token'] == 'synthetic-$literal#token'
    assert result['paths']['registry_csv'] == str(tmp_path/'input/events.csv')


def test_missing_variable_is_actionable_and_no_secret_in_error(tmp_path, monkeypatch):
    path = fixture_config(tmp_path)
    monkeypatch.delenv('AUDIT_TOKEN',raising=False)
    monkeypatch.setenv('AUDIT_VERIFY', 'false')
    monkeypatch.setenv('AUDIT_TIMEOUT', '45000')
    (tmp_path/'.env').unlink()
    with pytest.raises(ConfigError, match='AUDIT_TOKEN') as error:
        load_config(path)
    assert 'synthetic' not in str(error.value)


@pytest.mark.parametrize('change', [{'timeout':0},{'timeout':'nope'},{'verify_ssl':'nope'},{'url':'invalid'},{'token':''}])
def test_invalid_connection_settings_fail_before_client(tmp_path, change):
    path = fixture_config(tmp_path)
    data = yaml.safe_load(path.read_text())
    data['influxdb'].update(change)
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        load_config(path)


def test_bad_yaml_does_not_echo_values(tmp_path):
    path = tmp_path/'bad.yaml'
    path.write_text('token: [DO_NOT_ECHO_SECRET')
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert 'DO_NOT_ECHO_SECRET' not in str(error.value)


def test_postgres_port_and_legacy_verify(tmp_path):
    path = fixture_config(tmp_path, {'postgresql':{'host':'localhost','user':'test','password':'dummy','database':'db','port':'5544'}})
    data=yaml.safe_load(path.read_text())
    del data['influxdb']['verify_ssl']
    data['influxdb']['verify'] = False
    path.write_text(yaml.safe_dump(data))
    result=load_config(path)
    assert result['postgresql']['port']==5544
    assert result['influxdb']['verify_ssl'] is False


def test_explicit_dotenv_and_nested_expansion(tmp_path):
    path=fixture_config(tmp_path, {'other':{'list':['${AUDIT_TOKEN}', '${AUDIT_TIMEOUT}/data']}})
    env=tmp_path/'selected.env'
    env.write_text('AUDIT_TOKEN=explicit\nAUDIT_VERIFY=true\nAUDIT_TIMEOUT=50000\n')
    result=load_config(path,dotenv_path=env)
    assert result['other']['list']==['explicit','50000/data']
    assert result['influxdb']['verify_ssl'] is True


def test_extractor_uses_shared_settings_and_h5_path(tmp_path):
    from gait_analysis.extractor import GaitDataExtractor
    path=fixture_config(tmp_path,{'paths':{'h5_path':'out/custom.h5'}})
    with patch('gait_analysis.extractor.InfluxDBClient') as client:
        extractor=GaitDataExtractor(str(path))
        assert extractor._h5_database == str(tmp_path/'out/custom.h5')
        assert client.call_args.kwargs['timeout']==(10000, 45000)
        assert client.call_args.kwargs['verify_ssl'] is False
        extractor.close()
        client.return_value.close.assert_called_once()


def test_pipeline_client_uses_shared_settings(tmp_path):
    from gait_analysis.pipeline import GaitPipeline
    path=fixture_config(tmp_path)
    with patch('gait_analysis.pipeline.InfluxDBClient') as direct:
        pipeline=GaitPipeline(str(path))
        assert direct.call_args.kwargs['timeout']==(10000, 45000)
        assert direct.call_args.kwargs['verify_ssl'] is False
        pipeline.client.close()


@pytest.mark.parametrize('fs', [10, 0, 'nan','inf','bad'])
def test_invalid_processing_frequency(tmp_path,fs):
    path=fixture_config(tmp_path,{'processing':{'fs':fs}})
    with pytest.raises(ConfigError, match='processing.fs'):
        load_config(path)
