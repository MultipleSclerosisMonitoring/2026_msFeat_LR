"""Lanzador con el nombre histórico: equivale a la orden ``msfeat-graficas``.

El código vive en ``gait_analysis.plots``; el paquete debe estar instalado
(``poetry install``).
"""
from gait_analysis.plots import *  # noqa: F401,F403
from gait_analysis.plots import GaitVisualizer, main  # noqa: F401

if __name__ == "__main__":
    main()
