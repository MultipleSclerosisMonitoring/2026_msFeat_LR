.. MS Monitoring documentation master file

MS Monitoring Documentation
===========================

Welcome to the **MS Monitoring** documentation. This guide covers installation, usage, and the architecture of each module in the `ms_monitoring` project.

.. automodule:: msTools.i18n
   :members:
   :undoc-members:
   :show-inheritance:

.. graphviz::
   :caption: High-Level Workflow Overview
   :align: center

   digraph overview {
      rankdir=LR;
      graph [fontname="Helvetica"];
      node  [shape=box, fontname="Helvetica"];
      edge  [fontname="Helvetica"];

      FindMSCodeIDs   [label="find_mscodeids CLI"];
      IdentifySegs    [label="CodeIDProcessor\n.identify_activity_segments()"];
      DetectMovement  [label="find_gait CLI\n(MovementDetector.detect_effective_movement())"];
      DetectGait      [label="find_gait CLI\n(MovementDetector.detect_effective_gait())"];

      FindMSCodeIDs -> IdentifySegs -> DetectMovement -> DetectGait;
   }

Contents
--------

.. toctree::
   :maxdepth: 2
   :caption: Sections:

   usage
   modules

Indices and Tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
