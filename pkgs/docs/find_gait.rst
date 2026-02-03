.. _find_gait:

find_gait
=========

Utility to detect and store effective gait periods based on segments
in the ``activity_all`` table of PostgreSQL.

**Location:** ``ms_monitoring/find_gait.py``

.. graphviz::
  :caption: Simplified Flow for `find_gait`
  :align: center

  digraph find_gait_flow {
    rankdir=LR;
    graph [fontname="Helvetica"];
    node  [shape=box, fontname="Helvetica"];
    edge  [fontname="Helvetica"];

    // Actors and services
    User         [label="User"];
    FindGait_CLI [label="find_gait CLI"];
    Movement     [label="MovementDetector"];
    DataMgr      [label="DataManager"];
    InfluxDB     [label="InfluxDB"];
    PostgreSQL   [label="PostgreSQL"];

    // Main sequence
    User -> FindGait_CLI [label="run `python -m ms_monitoring.find_gait`"];
    FindGait_CLI -> Movement [label="__init__(ids, config)"];
    Movement -> DataMgr    [label="load stored segments"];
    DataMgr -> PostgreSQL  [label="SELECT * FROM activity_all"];
    Movement -> InfluxDB   [label="fetch raw sensor data"];
    InfluxDB -> Movement   [label="raw DataFrame"];
    Movement -> Movement   [label="process segments\n(fetch → compute → detect)"];
    Movement -> DataMgr    [label="save results"];
    DataMgr -> PostgreSQL  [label="INSERT effective_movement + gait"];
    Movement -> FindGait_CLI[label="return results"];
    FindGait_CLI -> User    [label="print summaries"];
  }

Usage
-----

Run as a module from your virtual environment:

.. code-block:: bash

    python -m ms_monitoring.find_gait \
      -i "[12,34,56]" \
      -c config.yaml \
      -l en \
      --output raw_data.xlsx \
      --save \
      -v 2

Arguments
---------

- ``-i, --ids``  
  JSON list of ``activity_all`` record IDs.  
  e.g. ``"[12,34,56]"`` (required)

- ``-c, --config``  
  Path to the YAML configuration file. (required)

- ``-l, --lang``  
  Interface language (``en``, ``es``). Default: ``en``.

- ``-o, --output``  
  Optional path to an Excel file where raw sensor data will be exported.

- ``--save``  
  If provided, saves results to PostgreSQL tables  
  (``effective_movement`` and ``effective_gait``).

- ``-v, --verbose``  
  Verbosity level (0: none, 1: info, 2: debug).

- ``--head-rows``  
  Number of rows to display when verbosity ≥ 2. Default: 8.

Examples
--------

Detailed output, export raw data, and save to database::

  $ python -m ms_monitoring.find_gait \
      -i "[12,34,56]" \
      -c config.yaml \
      -l en \
      --output raw_data.xlsx \
      --save \
      -v 2 \
      --head-rows 3

Minimal run with default language and no saving::

  $ python -m ms_monitoring.find_gait \
      -i "[78,90]" \
      -c config.yaml \
      -v 1
