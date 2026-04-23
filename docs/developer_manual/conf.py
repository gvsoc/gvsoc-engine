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
import subprocess
from pathlib import Path
# sys.path.insert(0, os.path.abspath('.'))


# -- Project information -----------------------------------------------------

project = 'GVSOC'
copyright = '2019, ETH Zurich, University of Bologna and GreenWaves Technologies, SAS'
author = 'Germain Haugou'

# Add gvsoc_control python module
sys.path.insert(0, os.path.abspath('../engine/python/'))

subprocess.call('doxygen doxyfile', shell=True)

# -- Component doc generation ------------------------------------------------
#
# Walks GVSOC_MODULES and emits one rst page per component listed in
# components_registry.COMPONENTS. The output goes into
# components/_generated/ (gitignored). If GVSOC_MODULES is not set, component
# pages are skipped entirely — the Makefile's `doc` target exports it.

_DOC_ROOT = Path(__file__).resolve().parent
_REPO_ROOT = _DOC_ROOT.parents[3]  # .../gvsoc/engine/docs/developer_manual -> repo root

if os.environ.get('GVSOC_MODULES'):
    # autodoc needs the installed Python packages (gvsoc.systree pulls gapylib,
    # which sits under $GVSOC_WORKDIR/install/bin). Add both install roots.
    _install_root = Path(os.environ.get('GVSOC_WORKDIR', str(_REPO_ROOT))) / 'install'
    for extra in (_install_root / 'python', _install_root / 'bin'):
        if extra.is_dir() and str(extra) not in sys.path:
            sys.path.insert(0, str(extra))

    sys.path.insert(0, str(_DOC_ROOT / '_ext'))
    sys.path.insert(0, str(_DOC_ROOT))
    import component_pages  # noqa: E402
    from components_registry import COMPONENTS  # noqa: E402

    component_pages.generate(_DOC_ROOT, _REPO_ROOT, COMPONENTS)
else:
    print('GVSOC_MODULES not set — skipping component page generation.')
    # Leave a stub so components/index.rst's toctree still resolves.
    _stub_dir = _DOC_ROOT / 'components' / '_generated'
    _stub_dir.mkdir(parents=True, exist_ok=True)
    (_stub_dir / 'index.rst').write_text(
        'Generated component pages\n'
        '=========================\n\n'
        '``GVSOC_MODULES`` was not set when the docs were built, so no '
        'component pages were generated. Rebuild via ``make doc`` (which '
        'exports ``GVSOC_MODULES``) to populate this section.\n'
    )

# -- General configuration ---------------------------------------------------

# Add any Sphinx extension module names here, as strings. They can be
# extensions coming with Sphinx (named 'sphinx.ext.*') or your custom
# ones.
extensions = [
    'sphinx.ext.autodoc',
    'sphinx.ext.napoleon',
    'breathe',
]

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

breathe_projects = {
    "gvsoc": "doxygen/xml"
}
breathe_default_project = "gvsoc"


# -- Coverage report copy ----------------------------------------------------
#
# Copy the lcov / genhtml report tree into the Sphinx build output so
# the "view" links emitted by the component pages (pointing at
# ``../../coverage-report/<abs-path>.gcov.html``) resolve under the
# published docs. The source location is the directory passed via
# ``GVSOC_DOC_COVERAGE_HTML`` (the Makefile sets it to
# ``$(abspath $(COV_REPORT_DIR))``). No-op when the env var is unset
# or points at a non-existent directory.

def _copy_coverage_report(app, exception):
    import shutil
    if exception is not None:
        return
    src = os.environ.get('GVSOC_DOC_COVERAGE_HTML')
    if not src or not os.path.isdir(src):
        return
    dst = os.path.join(app.outdir, 'coverage-report')
    shutil.copytree(src, dst, dirs_exist_ok=True)


def setup(app):
    app.connect('build-finished', _copy_coverage_report)
