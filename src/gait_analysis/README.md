# Gait Analysis Core Package

This package contains the core logic for the gait signal extraction and processing pipeline. It is designed as a standalone library to ensure modularity and professional Object-Oriented Programming (OOP) standards.

## Main Components

### 1. Acquisition path
* **`registry.py`**: validates the whole CSV registry and returns one result per row.
* **`query.py`**: the single Flux query (filtered by `_measurement` and `CodeID`, metadata kept) and the per-foot normalisation (device selection, ordering, de-duplication).
* **`storage.py`**: one HDF5 file per event published atomically (temporary file, verification, replace), single-writer lock, JSON Lines manifest and atomic Excel publication.
* **`extractor.py`**: `acquire_trial`, the shared path query → normalise → store used by `pipeline_test.py`.
* **`logging_utils.py`**: verbose levels 0/1/2 for the `msGait.*` loggers.

### 2. Validation
* **Registry**: every CSV row is validated before any connection (`registry.py`); invalid rows are reported with status `INVALID_ROW`.
* **Configuration**: validated by `msConfig.load_config` (connections) and by `ProcessingConfig` in `GaitMetricsCalculator.py` (processing parameters).
* **Time**: registry timestamps are used as Madrid wall-clock time without converting to UTC, because the historical InfluxDB data stores wall-clock time labelled as UTC (`msTools.timeutils`).

## Internal Structure
```text
gait_analysis/
├── __init__.py      # Package initialization
├── registry.py      # Registry validation
├── query.py         # Flux query and normalisation
├── storage.py       # Per-event HDF5 store, manifest, Excel
├── extractor.py     # Shared acquisition path
├── logging_utils.py # Verbose levels
└── README.md        # Technical documentation (This file)
```