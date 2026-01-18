# Gait Analysis Core Package

This package contains the core logic for the gait signal extraction and processing pipeline. It is designed as a standalone library to ensure modularity and professional Object-Oriented Programming (OOP) standards.

## Main Components

### 1. `extractor.py` (Data Acquisition Engine)
The primary module responsible for the communication between **InfluxDB** and local **HDF5** storage.
* **Database Logic**: Implements Flux queries to retrieve high-frequency sensor data (IMU/Socks).
* **Timezone Management**: Automatically synchronizes clinical local time with UTC.
* **HDF5 Serialization**: Manages the hierarchical persistence of `pandas.DataFrames`.

### 2. Data Validation (Pydantic Models)
To ensure system robustness, the package uses **Pydantic v2** for strict schema validation:
* **`InfluxConfig`**: Validates YAML configuration files (URL, Token, Org, Bucket) before connection.
* **Type Hinting**: All methods are fully typed to prevent runtime errors and improve IDE support.

## Internal Structure
```text
gait_analysis/
├── __init__.py      # Package initialization and class exporting
├── extractor.py     # InfluxDB client and HDF5 logic
└── README.md        # Technical documentation (This file)
```