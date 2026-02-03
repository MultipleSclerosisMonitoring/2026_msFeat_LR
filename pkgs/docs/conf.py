# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

import os
import sys
sys.path.insert(0, os.path.abspath('..'))

# -- Project information -----------------------------------------------------

project = 'MS Monitoring'
copyright = '2025, Diego Parrilla Calderón & Joaquín Ordieres-Meré'
author = 'Diego Parrilla Calderón & Joaquín Ordieres-Meré'
release = '0.1.0'   # must match the version in pyproject.toml

# -- General configuration ---------------------------------------------------

extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.viewcode',
    'sphinx.ext.napoleon',
    'sphinx.ext.intersphinx',
    'sphinx.ext.githubpages',
    'sphinx.ext.graphviz',
    'sphinx.ext.autosummary',
]

myst_enable_extensions = [
    "amsmath",
    "attrs_inline",
    "colon_fence",
    "deflist",
    "dollarmath",
    "fieldlist",
    "html_admonition",
    "html_image",
    "linkify",
    "replacements",
    "smartquotes",
    "strikethrough",
    "substitution",
    "tasklist",
]

templates_path = ['_templates']
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']

autosummary_generate = True
autodoc_default_options = {
    'members': True,
    'undoc-members': True,
    'private-members': True,
}

language = 'en'

# -- Options for HTML output -------------------------------------------------

html_theme = 'sphinx_rtd_theme'
html_static_path = ['_static']

rst_epilog = """
.. |a| unicode:: U+007C a U+007C
.. |g| unicode:: U+007C g U+007C
"""

