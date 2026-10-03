"""Shared YAML/environment loader, with no database or scientific imports."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / 'config' / 'config.yaml'
_PATTERN = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


class ConfigError(ValueError):
    """Invalid configuration; messages never include configuration values."""


def resolve_config_path(config_path: str | Path | None = None) -> Path:
    if config_path is None:
        path = DEFAULT_CONFIG
        if not path.exists():
            path = PROJECT_ROOT / 'pkgs' / '.config.yaml'
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


def load_config(config_path: str | Path | None = None, *, dotenv_path: str | Path | None = None) -> dict:
    """Load YAML, expand environment references, validate, and resolve paths.

    Process environment wins over .env. No environment variables are mutated.
    Repo configs use the repo .env; external configs use an adjacent .env.
    All entries in ``paths`` are relative to the YAML directory.
    """
    path = resolve_config_path(config_path)
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
    config = _expand(raw, env)
    for label in ('influxdb', 'postgresql', 'paths', 'processing', 'batch', 'project'):
        if label in config and not isinstance(config[label], dict):
            raise ConfigError(f'{label} debe ser un mapa.')
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
    inf.setdefault('measurement', 'Gait')
    if not isinstance(inf['measurement'], str) or not inf['measurement'].strip():
        raise ConfigError('influxdb.measurement debe ser una cadena no vacía.')
    pg = config.get('postgresql')
    if pg is not None:
        for key in ('host', 'user', 'password', 'database'):
            if not isinstance(pg.get(key), str) or not pg[key].strip():
                raise ConfigError(f'postgresql.{key} debe ser una cadena no vacía.')
        _positive_int(pg, 'port', 'postgresql', 5432)
        if pg['port'] > 65535:
            raise ConfigError('postgresql.port debe ser menor o igual a 65535.')
    paths = config.setdefault('paths', {})
    for key, value in paths.items():
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f'paths.{key} debe ser una ruta no vacía.')
        item = Path(value).expanduser()
        paths[key] = str((item if item.is_absolute() else path.parent / item).resolve())
    processing = config.setdefault('processing', {})
    if 'fs' in processing:
        try:
            fs = float(processing['fs'])
            if not 10 < fs < float('inf'):
                raise ValueError
        except (TypeError, ValueError):
            raise ConfigError('processing.fs debe ser finito y mayor que 10 Hz para los filtros actuales.') from None
        processing['fs'] = fs
    config.setdefault('batch', {}).setdefault('test_type', '6MWT')
    config.setdefault('project', {}).setdefault('language', 'es')
    return config
