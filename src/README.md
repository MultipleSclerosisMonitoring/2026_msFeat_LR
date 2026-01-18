# Source Code (Core Logic)

This directory contains the internal source code of the project. It is structured as a modular Python package to ensure clean separation of concerns and high maintainability.

## Directory Layout
* **`gait_analysis/`**: The primary package containing the classes and methods for database extraction, data validation with Pydantic, and HDF5 storage management.

## Modular Architecture
The code is designed to be imported as a library by the scripts located in the root `scripts/` folder. 

**Key Design Principles:**
- **OOP Compliance**: Logic is encapsulated within classes to avoid global state issues.
- **Independence**: The core logic does not depend on specific execution environments, only on the defined configuration schemas.