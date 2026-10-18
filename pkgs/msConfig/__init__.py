"""Shared YAML/environment loader, with no database or scientific imports."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import dotenv_values



def _project_root() -> Path:
    """Locate the working folder that holds config/, data/ and the outputs.

    Returns:
        Path: ``MSFEAT_HOME`` when set; otherwise the checkout this file lives
        in (editable install); otherwise the current directory (installed wheel).
    """
    home = os.environ.get('MSFEAT_HOME')
    if home:
        return Path(home).expanduser().resolve()
    checkout = Path(__file__).resolve().parents[2]
    return checkout if (checkout / 'pyproject.toml').is_file() else Path.cwd()


PROJECT_ROOT = _project_root()
LOCALES_DIR = Path(__file__).resolve().parents[1] / 'msTools' / 'locales'
CONNECTION_SECTIONS = ('influxdb', 'postgresql')
logger = logging.getLogger('msGait.Config')
DEFAULT_CONFIG = PROJECT_ROOT / 'config' / 'config.yaml'
_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


class ConfigError(ValueError):
    """Invalid configuration; messages never include configuration values."""


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    if config_path is None:
        path = DEFAULT_CONFIG
    else:
        path = Path(config_path).expanduser()
        if not path.is_absolute() and not path.exists():
            candidate = PROJECT_ROOT / path
            if candidate.exists():
                path = candidate
            elif len(path.parts) == 1:
                path = PROJECT_ROOT / 'InfluxDBms' / path
    path = path.resolve()
    if not path.is_file():
        raise ConfigError('No existe el YAML solicitado. Cree config/config.yaml desde la plantilla.')
    return path


def _expand(value: Any, env: dict[str, str | None], field: str = '') -> Any:
    if isinstance(value, dict):
        return {k: _expand(v, env, f'{field}.{k}'.strip('.')) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, env, field) for v in value]
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            name = match.group(1)
            if env.get(name) in (None, ''):
                raise ConfigError(f'Falta la variable {name} requerida por {field}.')
            return str(env[name])
        return _PATTERN.sub(replace, value)
    return value


def _positive_int(section: dict, key: str, label: str, default: int) -> None:
    value = section.get(key, default)
    try:
        if isinstance(value, bool) or not re.fullmatch(r'[0-9]+', str(value)):
            raise ValueError
        value = int(value)
        if value <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ConfigError(f'{label}.{key} debe ser un entero positivo.') from None
    section[key] = value


def _boolean(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ('true', 'false'):
        return value.lower() == 'true'
    raise ConfigError(f'{label} debe ser true o false.')


def _number(section: dict, key: str, label: str, *, minimum: float = 0.0, strict: bool = True) -> float | None:
    """Validate an optional numeric analysis parameter and return it as float."""
    if key not in section:
        return None
    value = section[key]
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if number != number or number in (float('inf'), float('-inf')) or number < minimum or (strict and number == minimum):
            raise ValueError
    except (TypeError, ValueError):
        raise ConfigError(f'{label}.{key} debe ser un número finito {"mayor que" if strict else "mayor o igual que"} {minimum:g}.') from None
    return number


def influx_client_options(config: dict) -> dict:
    """Build the keyword arguments shared by every ``InfluxDBClient`` in the project.

    TLS verification follows the configuration and is on by default. When it is
    disabled a project warning is logged; urllib3 warnings are never silenced.

    Args:
        config: Configuration returned by :func:`load_config` with ``influxdb`` loaded.

    Returns:
        dict: ``url``, ``token``, ``org``, ``timeout`` (connection and read
        timeouts in milliseconds), ``verify_ssl`` and, if configured, ``ssl_ca_cert``.
    """
    inf = config['influxdb']
    # Pair (connection, read): a short connection timeout keeps a dead server from
    # consuming the whole read timeout on every retry.
    options = {'url': inf['url'], 'token': inf['token'], 'org': inf['org'],
               'timeout': (inf['connect_timeout'], inf['timeout']), 'verify_ssl': inf['verify_ssl']}
    if inf.get('ssl_ca_cert'):
        options['ssl_ca_cert'] = inf['ssl_ca_cert']
    if not inf['verify_ssl']:
        logger.warning('WARN_TLS_VERIFY_OFF: La verificación del certificado TLS de InfluxDB está desactivada '
                       '(influxdb.verify_ssl: false). La conexión no comprueba la identidad del servidor.')
    return options


def load_config(config_path: str | Path | None = None, *, dotenv_path: str | Path | None = None,
                sections: tuple[str, ...] | list[str] | None = None) -> dict:
    """Load YAML, expand environment references, validate, and resolve paths.

    Process environment wins over .env. No environment variables are mutated.
    Repo configs use the repo .env; external configs use an adjacent .env.
    All entries in ``paths`` are relative to the YAML directory.

    Connection settings and analysis parameters are validated separately.
    ``sections`` names the connection sections the task needs (``influxdb``,
    ``postgresql``): only those are expanded, validated and returned, so an
    offline task does not require database variables. ``None`` keeps the
    historical behaviour: ``influxdb`` is required and ``postgresql`` is
    validated when declared.

    Raises:
        ConfigError: If the YAML, a requested connection or an analysis
            parameter is invalid. Messages never include configuration values.
    """
    path = resolve_config_path(config_path)
    wanted = None if sections is None else set(sections)
    if wanted is not None and wanted - set(CONNECTION_SECTIONS):
        raise ConfigError('sections solo admite influxdb y postgresql.')
    if dotenv_path is not None:
        env_file = Path(dotenv_path).expanduser().resolve()
        if not env_file.is_file():
            raise ConfigError('No existe el archivo .env indicado.')
    else:
        env_file = (PROJECT_ROOT if path.is_relative_to(PROJECT_ROOT) else path.parent) / '.env'
    env = dict(dotenv_values(env_file, interpolate=False)) if env_file.is_file() else {}
    env.update(os.environ)
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (yaml.YAMLError, UnicodeError):
        raise ConfigError('YAML inválido; revise su estructura y codificación UTF-8.') from None
    if not isinstance(raw, dict) or any(not isinstance(k, str) for k in raw):
        raise ConfigError('El YAML debe contener un mapa de secciones.')
    if wanted is not None:
        # Connection sections the task does not use are neither expanded nor returned.
        raw = {key: value for key, value in raw.items() if key not in CONNECTION_SECTIONS or key in wanted}
        missing = sorted(wanted - set(raw))
        if missing:
            raise ConfigError(f'Falta la sección {missing[0]}.')
    config = _expand(raw, env)
    for label in ('influxdb', 'postgresql', 'paths', 'processing', 'batch', 'project', 'movement', 'registry'):
        if label in config and not isinstance(config[label], dict):
            raise ConfigError(f'{label} debe ser un mapa.')
    if wanted is None or 'influxdb' in wanted:
        _validate_influxdb(config, path)
    _validate_postgresql(config)
    _validate_analysis(config, path)
    return config


def _validate_influxdb(config: dict, path: Path) -> None:
    """Validate and normalise the ``influxdb`` connection section."""
    inf = config.get('influxdb')
    if not isinstance(inf, dict):
        raise ConfigError('Falta la sección influxdb.')
    for key in ('url', 'token', 'org', 'bucket'):
        if not isinstance(inf.get(key), str) or not inf[key].strip():
            raise ConfigError(f'influxdb.{key} debe ser una cadena no vacía.')
    try:
        parsed = urlparse(inf['url'])
        valid_url = parsed.scheme in ('http', 'https') and bool(parsed.hostname) and not parsed.username and not parsed.password
        parsed.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ConfigError('influxdb.url debe ser una URL HTTP(S) sin credenciales incrustadas.')
    inf['verify_ssl'] = _boolean(inf.get('verify_ssl', inf.get('verify', True)), 'influxdb.verify_ssl')
    _positive_int(inf, 'timeout', 'influxdb', 30000)
    _positive_int(inf, 'connect_timeout', 'influxdb', 10000)
    inf.setdefault('measurement', 'Gait')
    waits = inf.setdefault('retry_waits_s', [5, 15, 45])
    if not isinstance(waits, list) or any(isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0 for w in waits):
        raise ConfigError('influxdb.retry_waits_s debe ser una lista de esperas en segundos (vacía = sin reintentos).')
    if not isinstance(inf['measurement'], str) or not inf['measurement'].strip():
        raise ConfigError('influxdb.measurement debe ser una cadena no vacía.')
    if inf.get('ssl_ca_cert') is not None:
        if not isinstance(inf['ssl_ca_cert'], str) or not inf['ssl_ca_cert'].strip():
            raise ConfigError('influxdb.ssl_ca_cert debe ser una ruta no vacía.')
        bundle = Path(inf['ssl_ca_cert']).expanduser()
        bundle = (bundle if bundle.is_absolute() else path.parent / bundle).resolve()
        if not bundle.is_file():
            raise ConfigError('influxdb.ssl_ca_cert no existe.')
        inf['ssl_ca_cert'] = str(bundle)


DEFAULT_INFLUX_TIME = ({'criterio': 'civil'}, {'desde': '2025-11-01', 'criterio': 'utc'})


def _validate_influx_time(setting: Any) -> None:
    """Validate ``registry.influx_time``: ``civil``, ``utc`` or a list of periods."""
    if isinstance(setting, str):
        if setting not in ('civil', 'utc'):
            raise ConfigError('registry.influx_time debe ser civil, utc o una lista de periodos {desde, criterio}.')
        return
    if not isinstance(setting, list) or not setting:
        raise ConfigError('registry.influx_time debe ser civil, utc o una lista de periodos {desde, criterio}.')
    previous = None
    for position, period in enumerate(setting):
        if not isinstance(period, dict) or period.get('criterio') not in ('civil', 'utc') or set(period) - {'desde', 'criterio'}:
            raise ConfigError('Cada periodo de registry.influx_time necesita criterio (civil o utc) y, salvo el primero, desde.')
        if 'desde' not in period:
            if position:
                raise ConfigError('Solo el primer periodo de registry.influx_time puede no tener desde.')
            continue
        try:
            start = datetime.fromisoformat(str(period['desde']))
        except (TypeError, ValueError):
            raise ConfigError(f'registry.influx_time: fecha no válida en desde ({period["desde"]!r}).') from None
        if previous is not None and start <= previous:
            raise ConfigError('Los periodos de registry.influx_time deben ir en orden de fecha.')
        previous = start


def _validate_postgresql(config: dict) -> None:
    """Validate the ``postgresql`` connection section when it is loaded."""
    pg = config.get('postgresql')
    if pg is not None:
        for key in ('host', 'user', 'password', 'database'):
            if not isinstance(pg.get(key), str) or not pg[key].strip():
                raise ConfigError(f'postgresql.{key} debe ser una cadena no vacía.')
        _positive_int(pg, 'port', 'postgresql', 5432)
        if pg['port'] > 65535:
            raise ConfigError('postgresql.port debe ser menor o igual a 65535.')
        if pg.get('connect_timeout') is not None:
            _positive_int(pg, 'connect_timeout', 'postgresql', 10)


def _validate_analysis(config: dict, path: Path) -> None:
    """Validate paths and analysis parameters, independent of any connection."""
    paths = config.setdefault('paths', {})
    for key, value in paths.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f'paths.{key} debe ser una ruta no vacía.')
        item = Path(value).expanduser()
        paths[key] = str((item if item.is_absolute() else path.parent / item).resolve())
    # Single source of path defaults, relative to the repo root (never to the CWD).
    paths.setdefault('registry_csv', str(PROJECT_ROOT / 'tests.csv'))
    paths.setdefault('output_folder', str(PROJECT_ROOT / 'data' / 'raw'))
    paths.setdefault('h5_path', str(Path(paths['output_folder']) / 'gait_study_data.h5'))
    paths.setdefault('output_dataset', str(PROJECT_ROOT / 'resultados_excel' / 'DatasetMaestro.xlsx'))
    # Detected events live apart from the raw signals: <store folder>/../results.
    paths.setdefault('results_folder', str(Path(paths['h5_path']).parent.parent / 'results'))
    # An output must never overwrite the input registry or another output.
    targets = {key: os.path.normcase(paths[key]) for key in ('registry_csv', 'h5_path', 'output_dataset', 'output_report') if key in paths}
    names = list(targets)
    for index, first in enumerate(names):
        for second in names[index + 1:]:
            if targets[first] == targets[second]:
                raise ConfigError(f'paths.{first} y paths.{second} no pueden ser el mismo archivo.')
    processing = config.setdefault('processing', {})
    if 'fs' in processing:
        try:
            fs = float(processing['fs'])
            if not 10 < fs < float('inf'):
                raise ValueError
        except (TypeError, ValueError):
            raise ConfigError('processing.fs debe ser finito y mayor que 10 Hz para los filtros actuales.') from None
        processing['fs'] = fs
    excluded = config.setdefault('registry', {}).setdefault('exclude_codeids', [])
    if not isinstance(excluded, list) or any(not isinstance(code, str) or not code.strip() for code in excluded):
        raise ConfigError('registry.exclude_codeids debe ser una lista de identificadores de paciente.')
    # Cómo se buscan en InfluxDB las horas del registro (D14): un criterio único o uno por periodo.
    # Por defecto, UTC desde noviembre de 2025 (comprobado con datos reales) y hora civil antes (sin comprobar).
    setting = config['registry'].setdefault('influx_time', [dict(period) for period in DEFAULT_INFLUX_TIME])
    _validate_influx_time(setting)
    batch = config.setdefault('batch', {})
    batch.setdefault('test_type', '6MWT')
    if not isinstance(batch['test_type'], str) or not batch['test_type'].strip():
        raise ConfigError('batch.test_type debe ser un código de test no vacío (o "all").')
    project = config.setdefault('project', {})
    project.setdefault('language', 'es')
    available = sorted(item.name for item in LOCALES_DIR.iterdir() if item.is_dir()) if LOCALES_DIR.is_dir() else []
    if not isinstance(project['language'], str) or (available and project['language'] not in available):
        raise ConfigError(f'project.language debe ser uno de: {", ".join(available) or "es"}.')
    movement = config.get('movement')
    if movement is not None:
        low = _number(movement, 'freq_band_min', 'movement')
        high = _number(movement, 'freq_band_max', 'movement')
        if low is not None and high is not None and low >= high:
            raise ConfigError('movement.freq_band_min debe ser menor que movement.freq_band_max.')
        for key in ('accel_threshold', 'gyro_threshold', 'accel_power_threshold', 'gyro_power_threshold',
                    'min_continuous_seconds', 'max_pause_s', 'merge_gap_s', 'fs_tolerance'):
            _number(movement, key, 'movement')
        if movement.get('tail_policy', 'align') not in ('align', 'minimum'):
            raise ConfigError('movement.tail_policy debe ser align o minimum.')
        if 'tail_min_samples' in movement:
            _positive_int(movement, 'tail_min_samples', 'movement', 1)
        elif movement.get('tail_policy') == 'minimum':
            raise ConfigError('movement.tail_policy: minimum exige movement.tail_min_samples.')
        if 'min_continuous_hits' in movement:
            _positive_int(movement, 'min_continuous_hits', 'movement', 1)
