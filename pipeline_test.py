"""Lanzador con el nombre histórico: equivale a la orden ``msfeat-pipeline``.

El código vive en ``gait_analysis.pipeline``; el paquete debe estar instalado
(``poetry install``).
"""
from gait_analysis.pipeline import *  # noqa: F401,F403
from gait_analysis.pipeline import GaitPipeline, main  # noqa: F401

if __name__ == "__main__":
    main()
