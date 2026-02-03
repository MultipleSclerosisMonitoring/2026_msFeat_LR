.. _modules:

ms_monitoring package
=====================

This section describes the **ms_monitoring** project and its principal modules.

Architecture Overview
---------------------

.. graphviz::
   :caption: Class Diagram for Core Components
   :align: center

   digraph class_core {
      rankdir=TB;
      graph [fontname="Helvetica"];
      node  [shape=record, fontname="Helvetica"];
      edge  [fontname="Helvetica"];


      CodeIDProcessor [label="{CodeIDProcessor|+ fetch_codeid_data()\l+ identify_activity_segments()\l+ merge_activity_legs_to_all()\l}"];
      MovementDetector [label="{MovementDetector|+ detect_effective_movement()\l+ detect_effective_gait()\l}"];
      DataManager [label="{DataManager|+ get_codeids_in_range()\l+ store_data()\l+ segments_retrieval()\l+ recover_activity_all()\l}"];

      CodeIDProcessor -> DataManager;
      MovementDetector -> DataManager;
   }


Submodules
----------

The documentation for each component lives in its own `.rst`:

.. toctree::
   :maxdepth: 1

   find_gait
   find_mscodeids
   msCodeID
   msGait
   msTools
