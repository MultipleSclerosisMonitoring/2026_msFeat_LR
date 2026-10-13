# Source Code (Core Logic)

This directory contains the internal source code of the project. It is structured as a modular Python package to ensure clean separation of concerns and high maintainability.

## Directory Layout
* **`gait_analysis/`**: The primary package with the whole pipeline: registry validation, database extraction, HDF5 storage, gait metrics, Excel report and figures.

## Modular Architecture
The package is a library. It is used by the commands `msfeat-pipeline` and `msfeat-graficas` (and their launchers `pipeline_test.py` and `graficas_tfg.py`), by `benchmarks/benchmark_pipeline.py` and by the one-off tools in `scripts/`. It is run from the repository folder (editable install); see the root README. 

**Key Design Principles:**
- **OOP Compliance**: Logic is encapsulated within classes to avoid global state issues.
- **Independence**: The core logic does not depend on specific execution environments, only on the defined configuration schemas.