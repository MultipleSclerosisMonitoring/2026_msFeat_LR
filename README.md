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
2026_msFeat_TEA/
├── src/gait_analysis/       # Core package: extraction & processing logic
│   ├── __init__.py          # Package initializer
│   ├── extractor.py         # InfluxDB connection and HDF5 management
│   └── README.md            # Technical documentation for the module
├── scripts/                 # Executable scripts (Entry points)
│   ├── batch_extractor.py   # CLI tool for massive data extraction
│   └── README.md            # Usage guide for scripts
├── data/                    # Data storage
│   └── raw/                 # Local HDF5 databases (.h5)
├── InfluxDBms/              # Database connection credentials (YAML)
├── pyproject.toml           # Project metadata and local package config
├── requirements.txt         # External dependencies list
└── README.md                # This file (Main documentation) 
```

# Installation & Setup
## Create the virtual environment
python -m venv .venv

## Activate the environment (Windows PowerShell)
.\.venv\Scripts\activate

## Dependency Installation
pip install -r requirements.txt

## Local Package Installation
pip install -e .