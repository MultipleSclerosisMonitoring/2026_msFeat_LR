"""Configuración de Sphinx para la documentación de 2026_msFeat_LR.

Genera la documentación a partir de los README del repositorio y de los
docstrings (formato Google) del paquete. Para compilarla en local:

    python -m pip install ".[graficos]" -r docs/requirements.txt
    sphinx-build -W --keep-going -b html docs docs/_build/html

El flujo ``.github/workflows/docs.yml`` hace lo mismo y publica el resultado en
GitHub Pages.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# El código del repositorio tiene prioridad sobre una versión instalada.
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "pkgs")]

project = "2026_msFeat_LR"
author = "Lucía Rosado Rivas"
copyright = "2026, Lucía Rosado Rivas"
release = "0.1.0"  # la de pyproject.toml
language = "es"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "myst_parser",
]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
exclude_patterns = ["_build"]

# Docstrings en formato Google.
napoleon_google_docstring = True
napoleon_numpy_docstring = False
autodoc_member_order = "bysource"
autodoc_typehints = "description"
autodoc_default_options = {"members": True, "show-inheritance": True}

# Los README enlazan archivos del repositorio que no forman parte de la documentación.
myst_heading_anchors = 3
suppress_warnings = ["myst.xref_missing", "myst.header"]

html_theme = "furo"
html_title = "2026_msFeat_LR"
html_show_sourcelink = False
