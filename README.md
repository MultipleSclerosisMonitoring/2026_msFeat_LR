# TFG: Mass Gait Signal Extraction and Feature Analysis

## Project Overview
This project focuses on the automated acquisition and processing of high-frequency gait signals from clinical tests. The system is designed to be independent, scalable, and follows professional Object-Oriented Programming (OOP) standards.

## Tech Stack
* **Language:** Python 3.12
* **Database:** InfluxDB (Flux Query Language)
* **Data Storage:** HDF5 (Hierarchical Data Format)
* **Key Libraries:** Pandas, Pydantic (v2), Influxdb-client, PyYAML, Tables.

## Project Structure
```text
2026_msFeat_LR/
├── src/gait_analysis/       # Core package: extraction & processing logic
│   ├── __init__.py          # Package initializer
│   ├── extractor.py         # InfluxDB connection and HDF5 management
│   └── README.md            # Technical documentation for the module
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
python -m pip install -e .
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
python main.py --check-config
python main.py --config config/config.yaml
python pipeline_test.py --config config/config.yaml
python generar_informe_clinico.py --config config/config.yaml
```

[Guía completa de configuración, precedencia, rutas y pruebas](config/README.md).
[Revisión de implementación y limitaciones](docs/revision_2026-10-03/INFORME.md).
