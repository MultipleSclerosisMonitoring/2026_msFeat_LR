"""Contratos de configuración, instalación, TLS y traducción (LR-55 a LR-62). Sin red."""
import logging
import os
import re
import subprocess
import sys
import tomllib
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pkgs'), str(ROOT / 'src'), str(ROOT)]
from msConfig import ConfigError, influx_client_options, load_config


def write_config(tmp_path, extra=None, influx=None):
    config = {'influxdb': {'url': 'https://example.test:8086', 'org': 'o', 'bucket': 'b', 'token': 't', **(influx or {})}}
    config.update(extra or {})
    path = tmp_path / 'config.yaml'
    path.write_text(yaml.safe_dump(config))
    return path


# --- LR-55 / LR-56 / LR-61: dependencias, paquete y versión -----------------------------

def test_dependencies_are_ranged_and_locked():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    requirements = project['project']['dependencies']
    names = [re.split(r'[<>=!~\[]', requirement)[0] for requirement in requirements]
    assert len(set(names)) == len(names)
    for requirement, name in zip(requirements, names):     # distribución válida, con mínimo y máximo, sin módulos estándar
        assert re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*>=[0-9][0-9.]*,<[0-9][0-9.]*', requirement), requirement
        assert name not in sys.stdlib_module_names
    extras = project['project']['optional-dependencies']
    assert {'graficos', 'pg', 'api', 'dev'} <= set(extras) and any(item.startswith('seaborn') for item in extras['graficos'])
    assert any(item.startswith('psycopg2') for item in extras['pg'])
    # Poetry: backend de construcción, versiones de Python admitidas, licencia y versiones probadas (poetry.lock).
    assert project['build-system']['build-backend'] == 'poetry.core.masonry.api'
    assert project['project']['requires-python'] == '>=3.11,<3.14' and project['project']['license'] == 'AGPL-3.0-or-later'
    assert not (ROOT / 'requirements.txt').exists()
    locked = {package['name'].lower().replace('_', '-') for package in tomllib.loads((ROOT / 'poetry.lock').read_text(encoding='utf-8'))['package']}
    assert {name.lower() for name in names} <= locked
    workflow = yaml.safe_load((ROOT / '.github/workflows/tests.yml').read_text(encoding='utf-8'))
    assert workflow['jobs']['tests']['strategy']['matrix']['python-version'] == ['3.11', '3.12', '3.13']


def test_every_imported_distribution_is_declared():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    declared = ' '.join(project['dependencies'] + sum(project['optional-dependencies'].values(), [])).lower()
    module_to_distribution = {'yaml': 'pyyaml', 'dotenv': 'python-dotenv', 'influxdb_client': 'influxdb-client'}
    local = {'gait_analysis', 'msConfig', 'msTools', 'msGait', 'msCodeID'}
    files = [*(ROOT / 'src').rglob('*.py'), *(ROOT / 'pkgs' / 'msConfig').rglob('*.py'), ROOT / 'pkgs/msTools/flux.py',
             ROOT / 'pkgs/msTools/timeutils.py', ROOT / 'pkgs/msTools/i18n.py']
    for file in files:
        for module in re.findall(r'^\s*(?:from|import)\s+([A-Za-z_][A-Za-z0-9_]*)', file.read_text(encoding='utf-8'), re.M):
            if module in sys.stdlib_module_names or module in local:
                continue
            assert module_to_distribution.get(module, module).lower() in declared, f'{module} ({file.name}) no está declarado'


def test_packaged_library_is_self_consistent_and_versioned():
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    packages = {(item['include'], item['from']) for item in project['tool']['poetry']['packages']}
    assert {('gait_analysis', 'src'), ('msConfig', 'pkgs'), ('msTools', 'pkgs'), ('msGait', 'pkgs'), ('msCodeID', 'pkgs')} <= packages   # DataManager necesita msGait.models
    import gait_analysis
    assert gait_analysis.__version__ == project['project']['version']
    assert 'version_pipeline' in (ROOT / 'src/gait_analysis/pipeline.py').read_text(encoding='utf-8')
    # Las órdenes instaladas apuntan a funciones que existen; los nombres históricos siguen funcionando.
    import importlib
    for target in project['project']['scripts'].values():
        module, function = target.split(':')
        assert callable(getattr(importlib.import_module(module), function))
    for shim, module in (('pipeline_test.py', 'gait_analysis.pipeline'), ('graficas_tfg.py', 'gait_analysis.plots'),
                         ('GaitMetricsCalculator.py', 'gait_analysis.metrics')):
        text = (ROOT / shim).read_text(encoding='utf-8')
        assert f'from {module} import' in text and 'sys.path' not in text
    # Ningún módulo del paquete retoca la ruta de importación, y los catálogos viajan dentro de él.
    for file in [*(ROOT / 'src').rglob('*.py'), *(ROOT / 'pkgs/msConfig').rglob('*.py'), ROOT / 'pkgs/msTools/i18n.py']:
        assert 'sys.path' not in file.read_text(encoding='utf-8'), file
    from msTools import i18n
    assert Path(i18n.LOCALES_DIR).parent == Path(i18n.__file__).resolve().parent and 'en' in i18n.available_languages()


def test_working_folder_can_be_set_outside_the_checkout(tmp_path):
    code = "import msConfig; print(msConfig.PROJECT_ROOT); print(msConfig.DEFAULT_CONFIG)"
    env = {**os.environ, 'MSFEAT_HOME': str(tmp_path), 'PYTHONPATH': os.pathsep.join([str(ROOT / 'pkgs'), str(ROOT / 'src')])}
    done = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == [str(tmp_path.resolve()), str(tmp_path.resolve() / 'config' / 'config.yaml')]


# --- LR-58: validación por tarea y de parámetros de análisis -----------------------------

def test_offline_tasks_do_not_need_database_variables(tmp_path, monkeypatch):
    path = write_config(tmp_path, {'postgresql': {'host': '${NO_DEFINIDA_PG}', 'user': 'u', 'password': 'p', 'database': 'd'}},
                        influx={'token': '${NO_DEFINIDA_INFLUX}'})
    monkeypatch.delenv('NO_DEFINIDA_PG', raising=False)
    monkeypatch.delenv('NO_DEFINIDA_INFLUX', raising=False)
    offline = load_config(path, sections=())
    assert 'influxdb' not in offline and 'postgresql' not in offline and offline['paths']['h5_path']
    with pytest.raises(ConfigError, match='NO_DEFINIDA_INFLUX'):
        load_config(path, sections=('influxdb',))
    with pytest.raises(ConfigError, match='NO_DEFINIDA'):
        load_config(path)                                  # comportamiento histórico: todo lo declarado
    with pytest.raises(ConfigError):
        load_config(path, sections=('otra',))
    with pytest.raises(ConfigError, match='postgresql'):
        load_config(write_config(tmp_path), sections=('postgresql',))

    from gait_analysis.pipeline import GaitPipeline
    with patch('gait_analysis.pipeline.InfluxDBClient') as client:
        pipeline = GaitPipeline(str(path), offline=True)
    assert pipeline.client is None and client.call_count == 0


@pytest.mark.parametrize('extra, text', [
    ({'batch': {'test_type': []}}, 'batch.test_type'),
    ({'batch': {'test_type': ' '}}, 'batch.test_type'),
    ({'project': {'language': 'xx'}}, 'project.language'),
    ({'movement': {'freq_band_min': 1.6, 'freq_band_max': 0.4}}, 'freq_band_min'),
    ({'movement': {'accel_threshold': -1}}, 'accel_threshold'),
    ({'movement': {'min_continuous_hits': 0}}, 'min_continuous_hits'),
    ({'movement': [1]}, 'movement'),
    ({'paths': {'registry_csv': 'datos.csv', 'output_dataset': 'datos.csv'}}, 'mismo archivo'),
    ({'paths': {'h5_path': 'x.h5', 'output_dataset': 'x.h5'}}, 'mismo archivo'),
])
def test_invalid_analysis_parameters_are_rejected(tmp_path, extra, text):
    with pytest.raises(ConfigError, match=text):
        load_config(write_config(tmp_path, extra), sections=())


def test_valid_analysis_parameters_are_accepted(tmp_path):
    config = load_config(write_config(tmp_path, {
        'batch': {'test_type': 'all'}, 'project': {'language': 'en'},
        'movement': {'freq_band_min': 0.4, 'freq_band_max': 1.6, 'min_continuous_hits': 3},
    }), sections=())
    assert config['project']['language'] == 'en' and config['movement']['min_continuous_hits'] == 3


# --- LR-59: TLS -------------------------------------------------------------------------

def test_tls_is_verified_by_default_and_warnings_are_not_silenced(tmp_path, caplog):
    assert yaml.safe_load((ROOT / 'config/config.example.yaml').read_text(encoding='utf-8'))['influxdb']['verify_ssl'] is True
    options = influx_client_options(load_config(write_config(tmp_path)))
    assert options['verify_ssl'] is True and 'ssl_ca_cert' not in options and options['url'] == 'https://example.test:8086'

    bundle = tmp_path / 'ca.pem'
    bundle.write_text('x')
    options = influx_client_options(load_config(write_config(tmp_path, influx={'ssl_ca_cert': 'ca.pem'})))
    assert options['ssl_ca_cert'] == str(bundle.resolve())
    with pytest.raises(ConfigError, match='ssl_ca_cert'):
        load_config(write_config(tmp_path, influx={'ssl_ca_cert': 'no_existe.pem'}))

    with caplog.at_level(logging.WARNING, logger='msGait'):
        assert influx_client_options(load_config(write_config(tmp_path, influx={'verify_ssl': False})))['verify_ssl'] is False
    assert 'WARN_TLS_VERIFY_OFF' in caplog.text


def test_no_module_silences_tls_warnings_or_rewrites_the_host():
    code = (
        "import sys, warnings; sys.path[:0]=['.', 'pkgs', 'src']\n"
        "import urllib3, pipeline_test, gait_analysis.extractor, msTools.data_manager\n"
        "with warnings.catch_warnings(record=True) as caught:\n"
        "    warnings.simplefilter('default')\n"
        "    warnings.warn('x', urllib3.exceptions.InsecureRequestWarning)\n"
        "print(len(caught))"
    )
    done = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == '1'
    for file in ('src/gait_analysis/pipeline.py', 'src/gait_analysis/extractor.py', 'pkgs/msTools/data_manager.py'):
        text = (ROOT / file).read_text(encoding='utf-8')
        assert 'disable_warnings' not in text and '138.100' not in text and 'normalize_influx_url' not in text, file


# --- LR-62: traducción -------------------------------------------------------------------

def test_language_changes_messages_even_for_aliases_imported_earlier(tmp_path):
    from msTools import i18n
    from msTools.i18n import _ as early_alias          # importado antes de elegir idioma
    from gait_analysis.registry import load_registry
    try:
        i18n.init_translation('en')
        assert early_alias('Pie') == 'Foot' and early_alias('NO_DATA_CODEID').startswith('No data found')
        with pytest.raises(FileNotFoundError, match='Registry .* not found'):
            load_registry(str(tmp_path / 'no_existe.csv'))
        i18n.init_translation('es')
        assert early_alias('Pie') == 'Pie' and early_alias('NO_DATA_CODEID').startswith('No hay datos')
        with pytest.raises(FileNotFoundError, match='No se encuentra el registro'):
            load_registry(str(tmp_path / 'no_existe.csv'))
        assert set(i18n.available_languages()) == {'en', 'es'}
    finally:
        i18n.init_translation('es')


def test_catalogues_are_complete_and_compiled():
    done = subprocess.run([sys.executable, 'scripts/traducciones.py', '--check'], cwd=ROOT, capture_output=True, text=True,
                          encoding='utf-8', errors='replace')
    assert done.returncode == 0, done.stdout + done.stderr
    assert not list((ROOT / 'pkgs' / 'msTools' / 'locales').rglob('*.po~'))


def test_english_translations_keep_every_placeholder():
    sys.path.insert(0, str(ROOT / 'scripts'))
    import traducciones
    english = traducciones.read_po(ROOT / 'pkgs/msTools/locales/en/LC_MESSAGES/msGait.po')
    for message in traducciones.source_messages():
        fields = lambda text: sorted(re.findall(r'\{[^{}]*\}', text))
        assert fields(message) == fields(english[message]), message


# --- LR-60: documentación ----------------------------------------------------------------

def test_readmes_only_reference_files_that_exist():
    for readme in ('README.md', 'config/README.md', 'src/README.md', 'src/gait_analysis/README.md'):
        text = (ROOT / readme).read_text(encoding='utf-8')
        for script in re.findall(r'python ([A-Za-z_/]+\.py)', text):
            assert (ROOT / script).is_file(), f'{readme} menciona {script}'
        for link in re.findall(r'\]\((?!http)([^)#]+)\)', text):
            assert ((ROOT / readme).parent / link).exists(), f'{readme} enlaza {link}'
        assert 'InfluxConfig' not in text and 'generar_informe_clinico' not in text and 'main.py' not in text
        assert 'Pydantic' not in text and 'Balanceos' not in text
    core = (ROOT / 'src/gait_analysis/README.md').read_text(encoding='utf-8')
    for module in sorted(p.name for p in (ROOT / 'src/gait_analysis').glob('*.py')):
        assert module in core, f'src/gait_analysis/README.md no menciona {module}'


def test_retired_files_are_gone_and_legacy_config_verifies_tls():
    for retired in ('consulta_influxdb.py', 'InfluxDBms/cInfluxDB.py', 'pkgs/config.yaml'):
        assert not (ROOT / retired).exists(), retired
    legacy = yaml.safe_load((ROOT / 'pkgs/.config.yaml').read_text(encoding='utf-8'))
    assert legacy['influxdb'].get('verify_ssl') is True and 'verify' not in legacy['influxdb']
    assert legacy['movement']['min_continuous_seconds'] == 10 and 'min_continuous_hits' not in legacy['movement']


def test_one_licence_for_every_component():
    assert (ROOT / 'LICENSE').read_bytes() == (ROOT / 'pkgs/LICENSE').read_bytes()
    assert 'AFFERO' in (ROOT / 'LICENSE').read_text(encoding='utf-8')[:200]
    for file in ('pyproject.toml', 'pkgs/pyproject.toml'):
        assert 'AGPL-3.0-or-later' in (ROOT / file).read_text(encoding='utf-8'), file
    for readme in ('pkgs/README.md', 'pkgs/msGait/README.md', 'pkgs/msTools/README.md', 'pkgs/msCodeID/README.md',
                   'pkgs/ms_monitoring/README.md'):
        assert 'MIT' not in (ROOT / readme).read_text(encoding='utf-8'), readme


# --- Contrato de la hora: el registro guarda instantes con zona; InfluxDB, hora de reloj ---

@pytest.mark.parametrize('registry, wallclock', [
    ('2025-12-12 19:24:00+01', '2025-12-12T19:24:00Z'),            # invierno
    ('2026-07-01 19:24:00+02', '2026-07-01T19:24:00Z'),            # verano
    ('2026-03-29 01:59:59+01', '2026-03-29T01:59:59Z'),            # justo antes del cambio de hora
    ('2026-03-29 03:00:00+02', '2026-03-29T03:00:00Z'),            # justo después
    ('2025-11-17 19:24:36.317605+01', '2025-11-17T19:24:36.317605Z'),
    ('2025-11-17 19:24:36', '2025-11-17T19:24:36Z'),               # sin zona: se toma tal cual
])
def test_registry_time_is_used_as_wall_clock_without_converting_to_utc(registry, wallclock):
    from msTools.timeutils import experiment_wallclock, influx_wallclock_z
    assert influx_wallclock_z(registry) == wallclock
    assert experiment_wallclock(registry).tzinfo is None


def test_template_shows_the_default_processing_values():
    from gait_analysis.metrics import ProcessingConfig
    template = yaml.safe_load((ROOT / 'config/config.example.yaml').read_text(encoding='utf-8'))
    assert template['processing'] == ProcessingConfig().to_record()
    assert template['registry'] == {'exclude_codeids': []} and template['influxdb']['connect_timeout'] == 10000
