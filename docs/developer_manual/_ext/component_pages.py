"""Generate one Sphinx rst page per GVSoC component in ``components_registry``.

Invoked from ``conf.py`` at doc build time. The output goes into
``components/_generated/`` and is gitignored.

For each registered component we emit:

  - A Sphinx ``autoclass`` directive so the NumPy-style docstring and
    ``__init__`` signature render automatically.
  - A ``Ports`` section scraped from the generator's source via ``ast``
    (listing ``i_*`` / ``o_*`` methods plus their first-line docstring).
  - A ``Tests`` section built by ``ast``-walking every ``testset.cfg`` under
    the component's tests directory.

AST is used rather than ``import`` + introspection so a broken import in one
generator does not take down the whole doc build.
"""

from __future__ import annotations

import ast
import os
import shutil
import sys
from pathlib import Path


# --------------------------------------------------------------------------- #
# Module resolution                                                           #
# --------------------------------------------------------------------------- #

def _module_paths() -> list[Path]:
    """GVSOC_MODULES, split on ';', as absolute Paths that exist."""
    raw = os.environ.get('GVSOC_MODULES', '')
    out = []
    for p in raw.split(';'):
        p = p.strip()
        if not p:
            continue
        path = Path(p)
        if path.is_dir():
            out.append(path)
    return out


def _resolve_module_file(module: str, roots: list[Path]) -> Path | None:
    """Turn ``interco.router_v2`` into an absolute path under one of ``roots``."""
    rel = Path(*module.split('.')).with_suffix('.py')
    for root in roots:
        candidate = root / rel
        if candidate.is_file():
            return candidate
    return None


# --------------------------------------------------------------------------- #
# AST helpers                                                                 #
# --------------------------------------------------------------------------- #

def _find_class(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _port_methods(cls: ast.ClassDef) -> list[tuple[str, str, str]]:
    """Return [(name, signature, first-line docstring)] for i_* / o_* methods."""
    ports: list[tuple[str, str, str]] = []
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (node.name.startswith('i_') or node.name.startswith('o_')):
            continue
        sig = _format_signature(node)
        doc = ast.get_docstring(node) or ''
        first = doc.strip().splitlines()[0].strip() if doc.strip() else ''
        ports.append((node.name, sig, first))
    return ports


def _format_signature(node: ast.FunctionDef) -> str:
    args = node.args
    parts = []
    # positional + defaults
    defaults = list(args.defaults)
    positional = list(args.args)
    # pad defaults to the right
    pad = [None] * (len(positional) - len(defaults)) + defaults
    for arg, default in zip(positional, pad):
        if arg.arg == 'self':
            continue
        s = arg.arg
        if default is not None:
            try:
                s += f'={ast.unparse(default)}'
            except Exception:
                s += '=...'
        parts.append(s)
    if args.vararg:
        parts.append(f'*{args.vararg.arg}')
    if args.kwarg:
        parts.append(f'**{args.kwarg.arg}')
    return f'{node.name}({", ".join(parts)})'


# --------------------------------------------------------------------------- #
# Test extraction                                                             #
# --------------------------------------------------------------------------- #

def _extract_tests(tests_dir: Path) -> list[tuple[str, str, str, Path]]:
    """Walk ``testset.cfg`` files and collect test declarations.

    Returns ``[(name, flags, description, source_file)]``. Description comes
    from a ``# ...`` comment on the line immediately above the call.
    """
    tests: list[tuple[str, str, str, Path]] = []
    if not tests_dir.is_dir():
        return tests

    for cfg in sorted(tests_dir.rglob('testset.cfg')):
        try:
            src = cfg.read_text()
        except OSError:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        src_lines = src.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Match testset.new_make_test(...) / testset.new_gvrun_test(...) /
            # anything.new_*_test on a Name or Attribute receiver.
            func = node.func
            method = getattr(func, 'attr', None)
            if not method or not method.startswith('new_'):
                continue
            if not method.endswith('_test'):
                continue

            name = _call_str_arg(node, 'name', 0)
            flags = _call_str_arg(node, 'flags', None)
            if name is None:
                continue
            description = _preceding_comment(src_lines, node.lineno)
            tests.append((name, flags or '', description, cfg))
    return tests


def _call_str_arg(call: ast.Call, kw: str, pos: int | None) -> str | None:
    for k in call.keywords:
        if k.arg == kw and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str):
            return k.value.value
    if pos is not None and pos < len(call.args):
        a = call.args[pos]
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            return a.value
    return None


def _preceding_comment(src_lines: list[str], lineno: int) -> str:
    """Return the ``#`` comment block directly above ``lineno`` (1-indexed).

    Walks upwards from ``lineno - 1`` collecting contiguous ``#`` comment
    lines. The block is contiguous: a blank or non-comment line breaks it.
    Within the block, a *bare* ``#`` line (no text after the hash) is
    treated as a paragraph break, so authors can write multi-paragraph
    descriptions::

        # First paragraph spanning
        # several lines.
        #
        # Second paragraph here.
        testset.new_make_test('foo', flags='CASE=foo')

    Returns an RST string with paragraphs separated by ``\\n\\n``. The
    leading ``#`` and the single space typically following it are stripped.
    """
    idx = lineno - 2  # line above, 0-indexed
    raw: list[str] = []
    while idx >= 0:
        stripped = src_lines[idx].strip()
        if stripped.startswith('#') and not stripped.startswith('#!'):
            raw.insert(0, stripped.lstrip('#').lstrip())
            idx -= 1
        else:
            break
    if not raw:
        return ''
    # Re-flow into paragraphs split on bare comment lines.
    paragraphs: list[str] = []
    current: list[str] = []
    for line in raw:
        if not line:
            if current:
                paragraphs.append(' '.join(current))
                current = []
        else:
            current.append(line)
    if current:
        paragraphs.append(' '.join(current))
    return '\n\n'.join(paragraphs)


# --------------------------------------------------------------------------- #
# RST emission                                                                #
# --------------------------------------------------------------------------- #

def _rst_escape(s: str) -> str:
    return s.replace('|', '\\|').replace('`', r'\`')


def _render_page(entry: dict, roots: list[Path], repo_root: Path) -> tuple[str, str]:
    """Return ``(stem, rst_body)`` for one component."""
    module = entry['module']
    cls_name = entry['class']
    title = entry.get('title') or f'{module}.{cls_name}'
    stem = f'{module}.{cls_name}'

    src_file = _resolve_module_file(module, roots)
    ports: list[tuple[str, str, str]] = []
    if src_file is not None:
        try:
            tree = ast.parse(src_file.read_text())
            cls = _find_class(tree, cls_name)
            if cls is not None:
                ports = _port_methods(cls)
        except (OSError, SyntaxError):
            pass

    lines: list[str] = []
    bar = '=' * max(len(title), 4)
    lines += [title, bar, '']
    if src_file is not None:
        try:
            rel = src_file.relative_to(repo_root)
            lines += [f'*Generator:* ``{rel}``', '']
        except ValueError:
            pass

    lines += [
        f'.. autoclass:: {module}.{cls_name}',
        '   :members:',
        '   :show-inheritance:',
        '',
    ]

    lines += ['Ports', '-----', '']
    if ports:
        for name, sig, doc in ports:
            bullet = f'- ``{sig}``'
            if doc:
                bullet += f' — {doc}'
            lines.append(bullet)
        lines.append('')
    else:
        lines += ['No ``i_*`` / ``o_*`` port methods found.', '']

    tests_dirs_rel = entry.get('tests_dirs') or []
    # Backwards compat: accept a single 'tests_dir' string too.
    if not tests_dirs_rel and entry.get('tests_dir'):
        tests_dirs_rel = [entry['tests_dir']]

    if tests_dirs_rel:
        lines += ['Tests', '-----', '']
        for rel in tests_dirs_rel:
            tests_dir = (repo_root / rel).resolve()
            tests = _extract_tests(tests_dir)
            # One sub-section per directory (sub-heading = leaf dir name).
            heading = Path(rel).name
            lines += [heading, '~' * max(len(heading), 4), '']
            lines += [f'Source: ``{rel}``.', '']
            if tests:
                for name, flags, desc, _ in tests:
                    lines += [name, '^' * max(len(name), 4), '']
                    if flags:
                        lines += [f'*Flags:* ``{_rst_escape(flags)}``', '']
                    if desc:
                        lines += [desc, '']
                    else:
                        lines += ['*(no description)*', '']
            else:
                lines += ['No tests found.', '']

    return stem, '\n'.join(lines) + '\n'


def _render_index(entries: list[tuple[str, str]]) -> str:
    lines = [
        'Generated component pages',
        '=========================',
        '',
        '.. toctree::',
        '   :maxdepth: 1',
        '',
    ]
    for stem, _title in entries:
        lines.append(f'   {stem}')
    lines.append('')
    return '\n'.join(lines)


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def generate(doc_root: Path, repo_root: Path, registry: list[dict]) -> None:
    """Generate all component pages under ``doc_root/components/_generated``."""
    out_dir = doc_root / 'components' / '_generated'
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    roots = _module_paths()
    # Make generators importable so autoclass can see them.
    for r in roots:
        sr = str(r)
        if sr not in sys.path:
            sys.path.insert(0, sr)

    rendered: list[tuple[str, str]] = []
    for entry in registry:
        stem, body = _render_page(entry, roots, repo_root)
        (out_dir / f'{stem}.rst').write_text(body)
        rendered.append((stem, entry.get('title', stem)))

    (out_dir / 'index.rst').write_text(_render_index(rendered))
