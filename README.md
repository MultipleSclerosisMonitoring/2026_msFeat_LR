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
├── scripts/                 # Tools: translations, review of reference trials, migration of the old HDF5 file
├── dudas/ + DUDAS.pdf       # Open questions for the supervisor (source and PDF)
├── tests/                   # Offline tests
├── config/                  # YAML configuration and guide
├── data/                    # Data storage
├── InfluxDBms/              # Legacy connection YAML (references to .env variables, no credentials)
├── pyproject.toml           # Project metadata, dependency ranges, commands and licence
├── poetry.lock              # Exact versions the tests ran with
├── .github/workflows/       # Tests on Python 3.11, 3.12 and 3.13 from a clean install
└── README.md                # This file (Main documentation) 
```

# Alcance

**Dentro del TFG:** el paquete `src/gait_analysis/` (con `pipeline.py`, `metrics.py` y `plots.py`), sus lanzadores de la raíz, `benchmarks/`, `scripts/`, `tests/` y, de `pkgs/`, la parte compartida: `msConfig` (`load_config`, `ConfigError`), `msTools.i18n`, `msTools.timeutils` y `msTools.flux`.

**Código heredado retirado.** El resto del paquete heredado del proyecto anterior (DPC_2024), que el pipeline no usaba, se retiró del repositorio el 18/10/2026: la API (`pkgs/api.py`), `msGait`, `msCodeID`, `ms_monitoring`, `msTools.data_manager`, `msTools.models`, `create_tables.sql`, `msgait.service` y su documentación y dependencias propias. Declararlo fuera del alcance no resolvía sus fallos (cruce de piernas sin identidad explícita, conexiones compartidas en la API). Sigue disponible en el historial de Git y en el repositorio original. Antes se habían retirado `consulta_influxdb.py`, `InfluxDBms/cInfluxDB.py` y `pkgs/config.yaml`.

# Instalación y ejecución

Las dependencias se gestionan con [Poetry](https://python-poetry.org/). `pyproject.toml` declara los intervalos de versiones admitidos y `poetry.lock` fija las versiones exactas con las que se han pasado los tests.

```bash
pipx install poetry                 # una sola vez en el equipo
poetry install --all-extras         # crea .venv/ con el pipeline, las gráficas y los tests
poetry run pytest                   # tests
```

| Para... | Orden |
| --- | --- |
| Ejecutar el pipeline | `poetry run msfeat-pipeline` |
| Generar las gráficas | `poetry run msfeat-graficas` |
| Añadir una dependencia | `poetry add nombre` (actualiza `pyproject.toml` y `poetry.lock`) |
| Rehacer `poetry.lock` tras editar `pyproject.toml` a mano | `poetry lock` |
| Subir a las últimas versiones dentro de los intervalos | `poetry update` |
| Instalar solo el pipeline | `poetry install` (extras: `graficos`, `dev`) |

`poetry run python pipeline_test.py` y `poetry run python graficas_tfg.py` siguen funcionando: son lanzadores de las mismas órdenes. Sin Poetry también se puede instalar con `python -m pip install ".[dev]"`, pero entonces no se usan las versiones de `poetry.lock`.

El proyecto es un paquete de Python: `pipeline`, `metrics` y `plots` están dentro de `gait_analysis`, junto con `msConfig` y `msTools` (y sus traducciones). La carpeta de trabajo, donde se buscan `config/`, `.env`, `tests.csv` y `data/`, es la del repositorio; con el paquete instalado fuera de él es la carpeta actual, o la que indique la variable de entorno `MSFEAT_HOME`.

Versiones de Python: 3.11 a 3.13. `.github/workflows/tests.yml` instala el proyecto desde cero y pasa los tests con las tres en cada subida a GitHub. En local solo se ha probado con 3.13 en Windows.

Licencia: AGPL-3.0-or-later (GNU Affero GPL) para todo el repositorio (`LICENSE`).

La configuración usa YAML para parámetros/rutas y `.env` para credenciales.
En una instalación nueva:

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

Completar las variables de `.env`. En este espacio de trabajo ya existen `config/config.yaml` y `.env`; conservar los archivos locales existentes.

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

# Limitaciones declaradas

**Métricas del TFG, sin validar con una referencia.** No hay eventos anotados, distancias medidas ni otro dispositivo con el que comparar; la comprobación disponible es visual (Grafana) y de coherencia interna. En concreto:

- **Giros (LR-18).** Se detectan por ángulo acumulado en torno a la vertical estimada. Los umbrales (25 y 10 grados/s, 90 grados) no están calibrados con giros etiquetados.
- **Zancadas y cadencia (LR-19, LR-20).** La cadencia es de zancada, por pie, a partir del contacto de talón por presión. No se ha comprobado con anotaciones que cada contacto detectado sea un apoyo real.
- **Polaridad de la presión (LR-22).** La presión sube cuando se pisa y el valor guardado baja; la lectura es relativa, no una presión absoluta, y por eso cada sensor se compara con su propio rango. No hay calibración física por hardware.
- **Longitud de zancada (LR-25).** Integración entre pies planos, sin constante que calibrar. No hay distancia de referencia (tampoco anotada en los 6MWT) ni error de estimación. En el T25FW se avisa si la suma de longitudes de un intento no es coherente con los 7,62 m de la prueba (`WARN_T25FW_DISTANCE`, columnas `t25fw_suma_longitudes_m` y `t25fw_distancia_coherente`); es un aviso, no una validación ni una calibración.
- **Excursión del tobillo y MTC (LR-26).** La excursión no tiene señal de referencia; la MTC no se calcula.
- **Asimetría sin umbral (LR-38).** La figura de asimetría no dibuja ninguna línea de referencia: no hay un umbral publicado para la simetría de la cadencia de zancada, y la de los dos pies es casi igual por construcción. Se muestra un punto por paciente con la mediana.

El flujo de GitHub Actions (`.github/workflows/tests.yml`) instala el proyecto desde cero y pasa los tests en Linux y Windows con Python 3.11, 3.12 y 3.13.
