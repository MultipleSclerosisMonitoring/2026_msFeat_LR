# TFG: Mass Gait Signal Extraction and Feature Analysis

## Project Overview
This project focuses on the automated acquisition and processing of high-frequency gait signals from clinical tests. The system is designed to be independent and follows professional Object-Oriented Programming (OOP) standards. Processing is sequential; per-event time and memory are measured with `benchmarks/benchmark_pipeline.py`.

## Tech Stack
* **Language:** Python 3.11 to 3.13 (dependencies managed with Poetry)
* **Database:** InfluxDB (Flux Query Language)
* **Data Storage:** HDF5 (Hierarchical Data Format)
* **Key Libraries:** Pandas, NumPy, SciPy, Influxdb-client, PyYAML, Tables, openpyxl (figures: Matplotlib, Seaborn).

## Project Structure
```text
2026_msFeat_LR/
├── src/gait_analysis/       # Core package
│   ├── pipeline.py          # Single entry point (msfeat-pipeline): InfluxDB → HDF5 → metrics → Excel
│   ├── metrics.py           # Gait metrics engine (GaitMetricsCalculator, ProcessingConfig)
│   ├── plots.py             # Figures from the Excel dataset (msfeat-graficas)
│   ├── registry.py          # Registry (CSV) validation, one result per row
│   ├── query.py             # Flux query contract and per-foot normalisation
│   ├── storage.py           # Per-event HDF5 store, manifest, atomic Excel
│   ├── extractor.py         # Shared acquisition path (query → normalise → store)
│   ├── logging_utils.py     # Verbose levels (0/1/2)
│   └── README.md            # Technical documentation for the module
├── pipeline_test.py, graficas_tfg.py, GaitMetricsCalculator.py   # Launchers kept under their historical names
├── benchmarks/              # Time/memory benchmark per event
├── scripts/                 # One-off tools (migration of the old HDF5 file)
├── dudas/ + DUDAS.pdf       # Open questions for the supervisor (source and PDF)
├── tests/                   # Offline tests
├── config/                  # YAML configuration and guide
├── data/                    # Data storage
├── InfluxDBms/              # Database connection credentials (YAML)
├── pyproject.toml           # Project metadata, dependency ranges, commands and licence
├── poetry.lock              # Exact versions the tests ran with
├── .github/workflows/       # Tests on Python 3.11, 3.12 and 3.13 from a clean install
└── README.md                # This file (Main documentation) 
```

# Alcance

**Dentro del TFG:** el paquete `src/gait_analysis/` (con `pipeline.py`, `metrics.py` y `plots.py`), sus lanzadores de la raíz, `benchmarks/`, `scripts/`, `tests/` y, de `pkgs/`, la parte compartida: `msConfig` (`load_config`, `ConfigError`), `msTools.i18n`, `msTools.timeutils` y `msTools.flux`.

**Fuera del TFG (código heredado, se conserva sin mantener):** `pkgs/api.py`, `pkgs/msGait`, `pkgs/msCodeID`, `pkgs/ms_monitoring`, `pkgs/msTools/data_manager.py`, `create_tables.sql`, `pkgs/msgait.service`, `consulta_influxdb.py` e `InfluxDBms/`. No hay base PostgreSQL de pruebas en el repositorio, y el servicio `msgait.service` no usa este repositorio. Lo corregido en ese código solo está probado con simulaciones. Su tratamiento de la hora puede desfasarse 1-2 h entre lo que guarda y lo que busca; se deduce del código y no se ha comprobado con datos.

# Instalación y ejecución

Las dependencias se gestionan con [Poetry](https://python-poetry.org/). `pyproject.toml` declara los intervalos de versiones admitidos y `poetry.lock` fija las versiones exactas con las que se han pasado los tests.

```bash
pipx install poetry                 # una sola vez en el equipo
poetry install --all-extras         # crea .venv/ con el pipeline, las gráficas, el código heredado y los tests
poetry run pytest                   # tests
```

| Para... | Orden |
| --- | --- |
| Ejecutar el pipeline | `poetry run msfeat-pipeline` |
| Generar las gráficas | `poetry run msfeat-graficas` |
| Añadir una dependencia | `poetry add nombre` (actualiza `pyproject.toml` y `poetry.lock`) |
| Rehacer `poetry.lock` tras editar `pyproject.toml` a mano | `poetry lock` |
| Subir a las últimas versiones dentro de los intervalos | `poetry update` |
| Instalar solo el pipeline | `poetry install` (extras: `graficos`, `pg`, `api`, `dev`) |

`poetry run python pipeline_test.py` y `poetry run python graficas_tfg.py` siguen funcionando: son lanzadores de las mismas órdenes. Sin Poetry también se puede instalar con `python -m pip install ".[dev]"`, pero entonces no se usan las versiones de `poetry.lock`.

El proyecto es un paquete de Python: `pipeline`, `metrics` y `plots` están dentro de `gait_analysis`, junto con `msConfig` y `msTools` (y sus traducciones). La carpeta de trabajo, donde se buscan `config/`, `.env`, `tests.csv` y `data/`, es la del repositorio; con el paquete instalado fuera de él es la carpeta actual, o la que indique la variable de entorno `MSFEAT_HOME`.

Versiones de Python: 3.11 a 3.13. `.github/workflows/tests.yml` instala el proyecto desde cero y pasa los tests con las tres en cada subida a GitHub. En local solo se ha probado con 3.13 en Windows.

Licencia: AGPL-3.0-or-later (`LICENSE`).

La configuración usa YAML para parámetros/rutas y `.env` para credenciales.
En una instalación nueva:

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

Completar las variables de `.env`. En este espacio de trabajo ya se migró el acceso de `pkgs/.config.yaml`; conservar los archivos locales existentes.

```bash
poetry run msfeat-pipeline --check-config
poetry run msfeat-pipeline --config config/config.yaml
poetry run msfeat-pipeline --ids 58,60 --verbose 2
poetry run msfeat-pipeline --missing-only
poetry run msfeat-pipeline --stored-only
poetry run msfeat-pipeline --audit
poetry run msfeat-graficas
```

[Guía completa de configuración, precedencia, rutas y pruebas](config/README.md).
