# TFG: Mass Gait Signal Extraction and Feature Analysis

## Project Overview
This project focuses on the automated acquisition and processing of high-frequency gait signals from clinical tests. The system is designed to be independent and follows professional Object-Oriented Programming (OOP) standards. Processing is sequential; per-event time and memory are measured with `benchmarks/benchmark_pipeline.py`.

## Tech Stack
* **Language:** Python 3.12
* **Database:** InfluxDB (Flux Query Language)
* **Data Storage:** HDF5 (Hierarchical Data Format)
* **Key Libraries:** Pandas, NumPy, SciPy, Influxdb-client, PyYAML, Tables, openpyxl (figures: Matplotlib, Seaborn).

## Project Structure
```text
2026_msFeat_LR/
├── pipeline_test.py         # Single entry point: InfluxDB → HDF5 → metrics → Excel
├── GaitMetricsCalculator.py # Gait metrics engine
├── graficas_tfg.py          # Figures from the Excel dataset
├── src/gait_analysis/       # Core package
│   ├── registry.py          # Registry (CSV) validation, one result per row
│   ├── query.py             # Flux query contract and per-foot normalisation
│   ├── storage.py           # Per-event HDF5 store, manifest, atomic Excel
│   ├── extractor.py         # Shared acquisition path (query → normalise → store)
│   ├── logging_utils.py     # Verbose levels (0/1/2)
│   └── README.md            # Technical documentation for the module
├── benchmarks/              # Time/memory benchmark per event
├── scripts/                 # One-off tools (migration of the old HDF5 file)
├── dudas/ + DUDAS.pdf       # Open questions for the supervisor (source and PDF)
├── tests/                   # Offline tests
├── config/                  # YAML configuration and guide
├── data/                    # Data storage
├── InfluxDBms/              # Database connection credentials (YAML)
├── pyproject.toml           # Project metadata and local package config
├── requirements.txt         # External dependencies list
└── README.md                # This file (Main documentation) 
```

# Instalación y ejecución

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"      # pipeline, gráficas y tests
```

Extras: `graficos` (gráficas), `pg` y `api` (paquete heredado `pkgs/`), `dev` (todo lo anterior salvo `pg`/`api`, más tests). Solo el pipeline: `python -m pip install -e .`

El proyecto se ejecuta desde la carpeta del repositorio: `pipeline_test.py`, `GaitMetricsCalculator.py` y `graficas_tfg.py` no forman parte del paquete instalable, y la configuración se busca en `config/` de esta carpeta. Versiones de Python previstas: 3.11 a 3.13 (probado solo con 3.13).

```bash
```

En PowerShell: `.\.venv\Scripts\Activate.ps1`.

La configuración usa YAML para parámetros/rutas y `.env` para credenciales.
En una instalación nueva:

```bash
cp config/config.example.yaml config/config.yaml
cp .env.example .env
```

Completar las variables de `.env`. En este espacio de trabajo ya se migró el acceso de `pkgs/.config.yaml`; conservar los archivos locales existentes.

```bash
python pipeline_test.py --check-config
python pipeline_test.py --config config/config.yaml
python pipeline_test.py --ids 58,60 --verbose 2
python pipeline_test.py --missing-only
python pipeline_test.py --stored-only
python pipeline_test.py --audit
python graficas_tfg.py
```

[Guía completa de configuración, precedencia, rutas y pruebas](config/README.md).
