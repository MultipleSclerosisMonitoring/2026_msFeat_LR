msCodeID package
================

The **msCodeID** package provides tools to process wearable device CodeIDs:

- **Fetch** raw sensor tick counts from InfluxDB.  
- **Identify** contiguous activity segments for each foot (“Left” and “Right”).  
- **Transform** and validate segments for storage in PostgreSQL.  

Architecture Overview
---------------------

.. graphviz::
  :caption: CodeIDProcessor Class Overview
  :align: center

  digraph class_msCodeID {
    graph [fontname="Helvetica"];
    node  [shape=record, fontname="Helvetica"];
    edge  [fontname="Helvetica"];
    CodeIDProcessor [label="{CodeIDProcessor|+ fetch_codeid_data()\l+ identify_activity_segments()\l+ inter_segs()\l+ merge_activity_legs_to_all()\l+ save_to_postgresql()\l}"];
    DataManager [label="{DataManager|…}"];
    CodeIDProcessor -> DataManager;
  }

Core Components
---------------

- **CodeIDProcessor** (in `codeid_processor.py`)  
  - `__init__(data_manager: DataManager)`  
  - `fetch_codeid_data(codeid, start_datetime, end_datetime)`  
  - `identify_activity_segments(df, threshold_seconds, foot)`  
  - `inter_segs(sg1, sg2)`  
  - `merge_activity_legs_to_all(act_segR, act_segL, inter)`  
  - `save_to_postgresql(table_name, df)`  

- **ActivitySegment** (Pydantic model in `msGait.models`)  
  Used to validate segment dictionaries before insertion into `fullref_sensor_codeid`.

Submodules
----------

msCodeID.codeid_processor module
--------------------------------

.. automodule:: msCodeID.codeid_processor
  :members:
  :undoc-members:
  :show-inheritance:

Package Contents
----------------

.. automodule:: msCodeID
  :members:
  :undoc-members:
  :show-inheritance:
