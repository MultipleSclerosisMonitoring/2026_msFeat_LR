msTools package
===============

The **msTools** package provides shared utilities for MS Monitoring, including configuration management, database interactions, data models, internationalization, and time utilities.

Architecture Overview
---------------------

.. graphviz::
  :caption: DataManager Class Overview
  :align: center

  digraph class_msTools {
    rankdir=TB;
    graph [fontname="Helvetica"];
    node  [shape=record, fontname="Helvetica"];
    edge  [fontname="Helvetica"];

    DataManager [
      label="{DataManager|
        + __init__(config_path: str)\l
        + load_config()\l
        + get_config(section: str)\l
        + get_codeids_in_range(start, end)\l
        + segments_retrieval(...)\l
        + recover_activity_all(...)\l
        + transform_activityleg(...)\l
        + store_data(table, df)\l
        + get_real_codeid(codeid_id)\l
      }"
    ];

    Models [
      label="{Pydantic Models|
        CodeID\l
        ActivityLeg\l
        ActivityAll\l
      }"
    ];

    TimeUtils [
      label="{timeutils|
        + ensure_utc(ts)\l
      }"
    ];

    I18n [
      label="{i18n|
        + init_translation(locale)\l
        + _()\l
      }"
    ];

    DataManager -> Models;
    DataManager -> TimeUtils;
    DataManager -> I18n;
  }

Core Components
---------------

- **DataManager** (`data_manager.py`)  
  - `__init__(config_path: str)`  
  - `load_config(config_path: str) -> Dict`  
  - `get_config(sect: str) -> Dict`  
  - `get_codeids_in_range(start_datetime: str, end_datetime: str) -> List[str]`  
  - `segments_retrieval(fstart: Optional[str], fend: Optional[str], ids: Optional[List[int]], verbose: int) -> pd.DataFrame`  
  - `recover_activity_all(act: pd.DataFrame, vb: int) -> pd.DataFrame`  
  - `store_codeid(codeid: str, verbose: int) -> Tuple[int, bool]`  
  - `transform_activityleg(data: pd.DataFrame) -> pd.DataFrame`  
  - `store_data(table_name: str, data: pd.DataFrame, verbose: int) -> List[int]`  
  - `get_real_codeid(codeid_id: int) -> str`  
  - Manages InfluxDBClient and psycopg2 connections, and closing logic.

- **Pydantic Models** (`models.py`)  
  - `CodeID`  
  - `ActivityLeg`  
  - `ActivityAll`  

- **Time Utilities** (`timeutils.py`)  
  - `ensure_utc(ts) -> pd.Timestamp`

- **Internationalization** (`i18n.py`)  
  - `init_translation(idioma: str)`  
  - `_()` helper for translations  

Submodules
----------

msTools.data_manager module
---------------------------

.. automodule:: msTools.data_manager
  :members:
  :undoc-members:
  :show-inheritance:

msTools.models module
---------------------

.. automodule:: msTools.models
  :members:
  :undoc-members:
  :show-inheritance:

msTools.timeutils module
------------------------

.. automodule:: msTools.timeutils
  :members:
  :undoc-members:
  :show-inheritance:

msTools.i18n module
-------------------

.. automodule:: msTools.i18n
  :members:
  :undoc-members:
  :show-inheritance:

Package Contents
----------------

.. automodule:: msTools
  :members:
  :undoc-members:
  :show-inheritance:
