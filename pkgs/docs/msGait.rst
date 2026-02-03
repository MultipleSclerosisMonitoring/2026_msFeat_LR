msGait package
===============

The **msGait** package focuses on gait signal processing, classification, and analysis:

- **MovementDetector**: Detects effective movement windows and gait episodes using raw sensor streams.  
- **GaitClassifier**: (Future) Classifies gait patterns based on detected segments.  
- **TrajectoryAnalyzer**: (Future) Analyzes movement trajectories for additional metrics.  

Architecture Overview
---------------------

.. graphviz::
  :caption: MovementDetector Class Overview
  :align: center

  digraph class_msGait {
    rankdir=TB;
    graph [fontname="Helvetica"];
    node  [shape=record, fontname="Helvetica"];
    edge  [fontname="Helvetica"];

    MovementDetector [label="{MovementDetector|
      + __init__(config_file,…)\l
      + fetch_sensor_data()\l
      + calculate_magnitude()\l
      + detect_effective_movement()\l
      + detect_effective_gait()\l
      + save_to_postgresql()\l
    }"];

    DataManager [label="{DataManager|
      + segments_retrieval()\l
      + fetch_sensor_data()\l
      + store_data()\l
    }"];

    MovementDetector -> DataManager;
  }


Core Components
---------------

- **MovementDetector** (`movement_detector.py`)  
  - `__init__(config_file, sampling_rate, sect, fstart, fend, ids, verbose)`  
  - `fetch_sensor_data(start_time, end_time, codeid_id, foot)`  
  - `calculate_magnitude(df)`  
  - `is_effective_by_welch(signal, power_threshold)`  
  - `is_effective_by_time(signal, threshold)`  
  - `detect_effective_movement(activity_windows, nomf, vb)`  
  - `detect_effective_gait(df_effective, vb)`  
  - `save_to_postgresql(table_name, df, verbose)`  

- **GaitClassifier** (`gait_classifier.py`)  
  (Document future classification methods here)

- **TrajectoryAnalyzer** (`trajectory_analyzer.py`)  
  (Document future trajectory analysis methods here)

- **Models** (`models.py`)  
  - `EffectiveMovement` (Pydantic)  
  - `ActivitySegment` (Pydantic)

Submodules
----------

msGait.gait_classifier module
------------------------------

.. automodule:: msGait.gait_classifier
  :members:
  :undoc-members:
  :show-inheritance:

msGait.movement_detector module
--------------------------------

.. automodule:: msGait.movement_detector
  :members:
  :undoc-members:
  :show-inheritance:

msGait.trajectory_analyzer module
---------------------------------

.. automodule:: msGait.trajectory_analyzer
  :members:
  :undoc-members:
  :show-inheritance:

msGait.models module
--------------------

.. automodule:: msGait.models
  :members:
  :undoc-members:
  :show-inheritance:
