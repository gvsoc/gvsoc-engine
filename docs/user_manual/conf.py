# Configuration file for the Sphinx documentation builder.
#
# This file only contains a selection of the most common options. For a full
# list see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Path setup --------------------------------------------------------------

# If extensions (or modules to document with autodoc) are in another directory,
# add these directories to sys.path here. If the directory is relative to the
# documentation root, use os.path.abspath to make it absolute, like shown here.
#
import os
import sys
from pathlib import Path
# sys.path.insert(0, os.path.abspath('.'))


# -- Project information -----------------------------------------------------

project = 'GVSOC'
copyright = '2019, Germain Haugou'
author = 'Germain Haugou'

# Add gvsoc_control python module
sys.path.insert(0, os.path.abspath('../engine/python/'))

# -- Target documentation embedding ------------------------------------------
#
# Walks GVSOC_MODULES and embeds the docs/ tree shipped by each module
# under targets/_generated/ (gitignored) — the documentation counterpart
# of CMake pulling in each module's CMakeLists.txt. If GVSOC_MODULES is not
# set, a stub is written so the toctree in targets/index.rst still resolves.
# The Makefile's `doc` target exports GVSOC_MODULES.

_DOC_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_DOC_ROOT.parent / '_ext'))
import target_docs  # noqa: E402

_embedded_extensions = []
if os.environ.get('GVSOC_MODULES'):
    _embedded_extensions = target_docs.generate(_DOC_ROOT, 'user_manual')
else:
    print('GVSOC_MODULES not set — skipping target documentation embedding.')
    _stub_dir = _DOC_ROOT / 'targets' / '_generated'
    _stub_dir.mkdir(parents=True, exist_ok=True)
    (_stub_dir / 'index.rst').write_text(
        'Target documentation\n'
        '====================\n\n'
        '``GVSOC_MODULES`` was not set when the docs were built, so no '
        'target-specific documentation was embedded. Rebuild via '
        '``make doc`` (which exports ``GVSOC_MODULES``) to populate this '
        'section.\n'
    )

# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'sphinx.ext.autodoc'
]

# Extensions required by the embedded module docs (e.g. sphinx.ext.graphviz),
# picked up from each module's standalone conf.py.
for _ext in _embedded_extensions:
    if _ext not in extensions:
        extensions.append(_ext)

# Add any paths that contain templates here, relative to this directory.
templates_path = ['_templates']

# List of patterns, relative to source directory, that match files and
# directories to ignore when looking for source files.
# This pattern also affects html_static_path and html_extra_path.
exclude_patterns = ['_build', 'Thumbs.db', '.DS_Store']


# -- Options for HTML output -------------------------------------------------

# The theme to use for HTML and HTML Help pages.  See the documentation for
# a list of builtin themes.
#
html_theme = 'sphinx_rtd_theme'

# Add any paths that contain custom static files (such as style sheets) here,
# relative to this directory. They are copied after the builtin static files,
# so a file named "default.css" will overwrite the builtin "default.css".
# html_static_path = ['_static']

html_use_smartypants = False
smartquotes = False