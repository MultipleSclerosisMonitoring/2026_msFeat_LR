"""Contratos offline del acceso a datos: dependencias de pkgs/ y consultas Flux. Sin InfluxDB ni red."""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'pkgs'), str(ROOT / 'src'), str(ROOT)]
from msTools.flux import flux_range, flux_string


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


def test_legacy_code_is_no_longer_in_the_repository():
    # R73: el código heredado que el pipeline no usa se retiró del repositorio (feedback del 18/10/2026).
    for retired in ('pkgs/api.py', 'pkgs/msGait', 'pkgs/msCodeID', 'pkgs/ms_monitoring', 'pkgs/msTools/data_manager.py',
                    'pkgs/msTools/models.py', 'pkgs/msTools/create_tables.sql', 'pkgs/msgait.service', 'pkgs/docs',
                    'pkgs/pyproject.toml', 'pkgs/leeme.txt'):
        assert not (ROOT / retired).exists(), retired


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
