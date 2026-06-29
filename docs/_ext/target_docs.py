"""Embed target-specific documentation shipped by GVSoC modules.

Invoked from each manual's ``conf.py`` at doc build time. This is the
documentation counterpart of what CMake already does with
``GVSOC_MODULES``: just as the build walks every module root and pulls in
its ``CMakeLists.txt``, this walks every module root and pulls in its
documentation tree.

A module advertises documentation by shipping, next to (or under) one of
its ``GVSOC_MODULES`` roots, a ``docs/<manual>/`` directory containing an
``index.rst``, where ``<manual>`` is ``user_manual`` or
``developer_manual``. So the user manual embeds every module's
``docs/user_manual`` and the developer manual embeds every module's
``docs/developer_manual``.

For backward compatibility, the user manual also accepts a *flat*
``docs/index.rst`` (the older single-tree layout) when no
``docs/user_manual`` is present.

For each discovered tree we:

  - copy it into ``targets/_generated/<slug>/`` (gitignored), stripping
    the standalone-build scaffolding (``conf.py``, ``Makefile``,
    ``_build/`` ...) so the files slot cleanly into the engine's own
    Sphinx project;
  - add ``<slug>/index`` to the generated ``targets/_generated/index.rst``
    toctree, which the static ``targets/index.rst`` page includes.

The ``docs`` directory is looked up both directly under each module root
(e.g. ``gvsoc/acu`` is itself a module root) and one level up (e.g.
``gvsoc/voscap/targets`` is a module root while the docs live in the
sibling ``gvsoc/voscap/docs``). Trees are de-duplicated by their resolved
path, so a module contributing several roots is embedded only once. The
slug is the name of the module directory that owns the ``docs`` tree.

A discovered tree is skipped when its ``index.rst`` redefines a Sphinx
reference label that the host manual already declares. This drops a
module that merely re-ships a copy of the engine's own manual (e.g.
``gvsoc/core/docs`` carries the same ``_gvsoc`` / ``_gvsoc_dev`` labels as
the engine docs being built) and prevents duplicate-label build errors.

If no module ships docs for this manual, a stub index is written so the
toctree in ``targets/index.rst`` still resolves.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
from pathlib import Path


# Files / directories from a module's standalone Sphinx project that must
# not be copied into the engine doc tree: build scaffolding and the
# per-project config that would otherwise be picked up as a source file.
_COPY_EXCLUDE = {
    'conf.py',
    'Makefile',
    'make.bat',
    '_build',
    '__pycache__',
    '.gitignore',
}

_LABEL_RE = re.compile(r'^\.\.\s+_([\w.-]+):\s*$')


def _module_paths() -> list[Path]:
    """GVSOC_MODULES, split on ';', as absolute Paths that exist."""
    raw = os.environ.get('GVSOC_MODULES', '')
    out: list[Path] = []
    for p in raw.split(';'):
        p = p.strip()
        if not p:
            continue
        path = Path(p)
        if path.is_dir():
            out.append(path)
    return out


def _index_labels(index: Path) -> set[str]:
    """Reference labels (``.. _name:``) declared in an rst file."""
    labels: set[str] = set()
    try:
        for line in index.read_text().splitlines():
            m = _LABEL_RE.match(line)
            if m:
                labels.add(m.group(1))
    except OSError:
        pass
    return labels


def _module_slug(docs_dir: Path, manual: str) -> str:
    """Name of the module directory owning ``docs_dir``.

    ``.../acu/docs/user_manual`` -> ``acu`` (split layout);
    ``.../acu/docs``             -> ``acu`` (flat layout).
    """
    if docs_dir.name == manual and docs_dir.parent.name == 'docs':
        return docs_dir.parent.parent.name or 'module'
    if docs_dir.name == 'docs':
        return docs_dir.parent.name or 'module'
    return docs_dir.parent.name or 'module'


def _owning_module(docs_dir: Path, manual: str) -> Path:
    """The module root directory that owns ``docs_dir`` (the dir above
    ``docs``), so a split layout suppresses the same module's flat
    fallback."""
    if docs_dir.name == manual and docs_dir.parent.name == 'docs':
        return docs_dir.parent.parent
    return docs_dir.parent


def _candidate_docs(root: Path, manual: str) -> list[Path]:
    """Ordered candidate ``docs`` trees for one module root.

    The split ``docs/<manual>`` layout wins over the flat ``docs`` layout,
    and ``<root>`` wins over ``<root>/..``. The flat layout is only a
    candidate for the user manual (a bare ``docs/`` has historically held
    user-facing documentation).
    """
    bases = [root, root.parent]
    candidates: list[Path] = []
    for base in bases:
        candidates.append(base / 'docs' / manual)
    if manual == 'user_manual':
        for base in bases:
            candidates.append(base / 'docs')
    return candidates


def _discover_docs(roots: list[Path], manual: str,
                   host_labels: set[str]) -> list[tuple[str, Path]]:
    """Return ``[(slug, docs_dir)]`` for every module shipping docs.

    Results are de-duplicated by resolved docs path and by owning module
    (the split layout suppresses the flat fallback for the same module).
    A tree redefining a host reference label is skipped. On a slug
    collision the later tree gets a numeric suffix.
    """
    found: dict[Path, str] = {}
    seen_modules: set[Path] = set()
    used_slugs: set[str] = set()
    for root in roots:
        for candidate in _candidate_docs(root, manual):
            index = candidate / 'index.rst'
            if not index.is_file():
                continue
            docs_abs = candidate.resolve()
            if docs_abs in found:
                continue
            module_dir = _owning_module(docs_abs, manual)
            if module_dir in seen_modules:
                continue
            if _index_labels(index) & host_labels:
                # Re-ships a copy of the host manual — skip to avoid
                # duplicate labels (e.g. gvsoc/core/docs).
                continue
            slug = _module_slug(candidate, manual)
            if slug in used_slugs:
                n = 2
                while f'{slug}_{n}' in used_slugs:
                    n += 1
                slug = f'{slug}_{n}'
            used_slugs.add(slug)
            seen_modules.add(module_dir)
            found[docs_abs] = slug
    return sorted(((slug, path) for path, slug in found.items()),
                  key=lambda e: e[0])


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy ``src`` into ``dst`` minus the standalone-build scaffolding."""
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(*_COPY_EXCLUDE),
    )


def _module_extensions(docs_dir: Path) -> list[str]:
    """Sphinx extensions declared in the module's standalone ``conf.py``.

    A module's docs may rely on Sphinx extensions (e.g.
    ``sphinx.ext.graphviz``) enabled by its own ``conf.py``. When the tree
    is embedded into the host manual, the host must enable those too or the
    directives render as errors. We AST-parse the module's ``conf.py``
    ``extensions = [...]`` literal and return it so the host can merge it —
    the doc counterpart of CMake inheriting a module's build settings.

    The ``conf.py`` is looked up next to the docs ``index.rst`` and in the
    parent ``docs/`` directory (split layouts share one ``conf.py``). AST
    is used so a non-literal or broken ``conf.py`` can't break the build.
    """
    for conf in (docs_dir / 'conf.py', docs_dir.parent / 'conf.py'):
        if not conf.is_file():
            continue
        try:
            tree = ast.parse(conf.read_text())
        except (OSError, SyntaxError):
            return []
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == 'extensions'
                       for t in node.targets):
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return []
            if isinstance(value, (list, tuple)):
                return [e for e in value if isinstance(e, str)]
        return []
    return []


def generate(doc_root: Path, manual: str) -> list[str]:
    """Stage every module ``<manual>`` doc tree under ``targets/_generated``.

    ``manual`` is ``user_manual`` or ``developer_manual``. Returns the
    sorted list of extra Sphinx extensions the embedded modules declare,
    so the caller can merge them into the host manual's ``extensions``.
    """
    out_dir = doc_root / 'targets' / '_generated'
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    host_labels = _index_labels(doc_root / 'index.rst')
    discovered = _discover_docs(_module_paths(), manual, host_labels)

    extra_extensions: set[str] = set()
    for slug, docs_dir in discovered:
        _copy_tree(docs_dir, out_dir / slug)
        extra_extensions.update(_module_extensions(docs_dir))

    # Generated toctree index. Ordering follows discovery (stable).
    lines = [
        'Target documentation',
        '=====================',
        '',
    ]
    if discovered:
        lines += [
            '.. toctree::',
            '   :maxdepth: 2',
            '',
        ]
        for slug, _ in discovered:
            lines.append(f'   {slug}/index')
        lines.append('')
    else:
        lines += [
            'No module under ``GVSOC_MODULES`` ships a ``docs/'
            f'{manual}`` directory, so there is no target-specific '
            'documentation to embed.',
            '',
        ]
    (out_dir / 'index.rst').write_text('\n'.join(lines))

    return sorted(extra_extensions)
