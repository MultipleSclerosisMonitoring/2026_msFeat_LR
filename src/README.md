# Source Code (Core Logic)

This directory contains the internal source code of the project. It is structured as a modular Python package to ensure clean separation of concerns and high maintainability.

## Directory Layout
* **`gait_analysis/`**: The primary package containing the classes and methods for database extraction, data validation with Pydantic, and HDF5 storage management.

## Modular Architecture
The package is a library. It is used by the root command `pipeline_test.py` (the only pipeline entry point), by `benchmarks/benchmark_pipeline.py` and by the one-off tools in `scripts/`. It is run from the repository folder (editable install); see the root README. 

**Key Design Principles:**
- **OOP Compliance**: Logic is encapsulated within classes to avoid global state issues.
- **Independence**: The core logic does not depend on specific execution environments, only on the defined configuration schemas.