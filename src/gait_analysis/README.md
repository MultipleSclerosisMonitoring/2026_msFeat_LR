# Gait Analysis Core Package

This package contains the core logic for the gait signal extraction and processing pipeline. It is designed as a standalone library to ensure modularity and professional Object-Oriented Programming (OOP) standards.

## Main Components

### 1. Acquisition path
* **`registry.py`**: validates the whole CSV registry and returns one result per row.
* **`query.py`**: the single Flux query (filtered by `_measurement` and `CodeID`, metadata kept) and the per-foot normalisation (devices of one foot joined by time, ordering, removal of identical rows).
* **`storage.py`**: one HDF5 file per event published atomically (temporary file, verification, replace), single-writer lock, JSON Lines manifest and atomic Excel publication.
* **`extractor.py`**: `acquire_trial`, the shared path query → normalise → store used by `pipeline.py`.
* **`logging_utils.py`**: verbose levels 0/1/2 for the `msGait.*` loggers.

### 2. Analysis and report
* **`metrics.py`**: `GaitMetricsCalculator` and `ProcessingConfig`. Signal preparation (measured rate, gaps, IMU quality), turns, heel contacts, strides, cadence, phases, 6MWT decay, stride length and ankle excursion.
* **`pipeline.py`**: `GaitPipeline` and the `msfeat-pipeline` command. Orchestrates acquisition and analysis per event, aggregates per patient and publishes the Excel report.
* **`plots.py`**: `GaitVisualizer` and the `msfeat-graficas` command. Exploratory figures from the Excel report.

### 3. Validation
* **Registry**: every CSV row is validated before any connection (`registry.py`); invalid rows are reported with status `INVALID_ROW`.
* **Configuration**: validated by `msConfig.load_config` (connections) and by `ProcessingConfig` in `metrics.py` (processing parameters).
* **Time**: registry timestamps are used as Madrid wall-clock time without converting to UTC, because the historical InfluxDB data stores wall-clock time labelled as UTC (`msTools.timeutils`).

## Internal Structure
```text
gait_analysis/
├── __init__.py      # Package initialization and pipeline version
├── pipeline.py      # Orchestration, aggregation and Excel report (msfeat-pipeline)
├── metrics.py       # Gait metrics engine and processing parameters
├── plots.py         # Figures (msfeat-graficas)
├── registry.py      # Registry validation
├── query.py         # Flux query and normalisation
├── storage.py       # Per-event HDF5 store, manifest, Excel
├── extractor.py     # Shared acquisition path
├── logging_utils.py # Verbose levels
└── README.md        # Technical documentation (This file)
```