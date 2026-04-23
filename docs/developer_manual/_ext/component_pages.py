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
    from two sources, in order:

    1. A ``.add_description("...")`` call on the test object when the
       creation expression is assigned (``t = testset.new_make_test(...)``).
       This matches the explicit-description API introduced alongside
       ``set_components()``.
    2. A contiguous ``# ...`` comment block immediately above the creation
       call. This is the older convention still used in many testsets.
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

        # First pass: for each Assign whose RHS is a new_*_test(...) call,
        # remember the variable name so we can find matching
        # .add_description("...") later.
        var_to_test_name: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Call):
                continue
            method = getattr(node.value.func, 'attr', None)
            if not method or not method.startswith('new_') or not method.endswith('_test'):
                continue
            test_name = _call_str_arg(node.value, 'name', 0)
            if test_name is None:
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    var_to_test_name[tgt.id] = test_name

        # Second pass: collect .add_description("...") calls keyed by the
        # last-seen binding of the receiver var (standard usage pattern
        # is "t = testset.new_*_test(...); t.add_description('...')" in
        # sequence). This is lexically simple: for every add_description
        # call on a Name receiver, attribute the string to the test that
        # the receiver *currently* holds.
        name_to_desc: dict[str, str] = {}
        # Walk statements in order so we see the binding before the call.
        current_binding: dict[str, str] = {}
        for top in ast.walk(tree):
            if not isinstance(top, ast.FunctionDef):
                continue
            for stmt in top.body:
                if (isinstance(stmt, ast.Assign)
                        and isinstance(stmt.value, ast.Call)):
                    method = getattr(stmt.value.func, 'attr', None)
                    if (method and method.startswith('new_')
                            and method.endswith('_test')):
                        tn = _call_str_arg(stmt.value, 'name', 0)
                        if tn is not None:
                            for tgt in stmt.targets:
                                if isinstance(tgt, ast.Name):
                                    current_binding[tgt.id] = tn
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                    call = stmt.value
                    if (isinstance(call.func, ast.Attribute)
                            and call.func.attr == 'add_description'
                            and isinstance(call.func.value, ast.Name)):
                        var = call.func.value.id
                        if var in current_binding and call.args:
                            a0 = call.args[0]
                            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                                name_to_desc[current_binding[var]] = a0.value

        # Third pass: emit one entry per new_*_test(...), preferring an
        # explicit description over the preceding-comment fallback.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
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
            description = name_to_desc.get(name, '')
            if not description:
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


def _aggregate(coverage: dict | None,
               names: list[str],
               include_shared: bool = True) -> tuple[int, int] | None:
    """Sum ``lines_hit`` / ``lines_found`` across ``names`` in ``coverage``.

    ``coverage`` is the parsed ``per_component.json`` document produced
    by ``scripts/coverage_per_component.py``. Returns ``(lines_hit,
    lines_found)`` or ``None`` when coverage is unavailable.

    When ``include_shared`` is set, any source entry in the ``shared``
    bucket whose ``claimed_by`` list is a subset of ``names`` is folded
    into the total — that's how common code (e.g. ``router_common.cpp``
    used by every router variant) gets counted once at the component
    level without being credited to any individual variant.
    """
    if coverage is None:
        return None
    lh, lf = 0, 0
    want = set(names)
    for name in names:
        bucket = coverage.get(name)
        if not bucket:
            continue
        lh += bucket.get('lines_hit', 0)
        lf += bucket.get('lines_found', 0)
    if include_shared:
        shared = coverage.get('shared') or {}
        for src in shared.get('sources') or []:
            claimed_by = src.get('claimed_by') or []
            if claimed_by and set(claimed_by).issubset(want):
                lh += src.get('lines_hit', 0)
                lf += src.get('lines_found', 0)
    return lh, lf


def _collect_sources(coverage: dict | None,
                     names: list[str]) -> list[dict]:
    """Return a sorted, de-duplicated per-file coverage list for ``names``.

    Pulls sources from each named bucket directly and from the
    ``shared`` bucket when ``claimed_by`` is fully inside ``names``.
    Each returned entry is a dict with ``path``, ``lines_hit``,
    ``lines_found`` and ``pct``.
    """
    if coverage is None:
        return []
    want = set(names)
    by_path: dict[str, dict] = {}

    def _add(src: dict) -> None:
        path = src.get('path')
        if not path:
            return
        lh = int(src.get('lines_hit') or 0)
        lf = int(src.get('lines_found') or 0)
        prev = by_path.get(path)
        if prev is None:
            by_path[path] = {'path': path, 'lines_hit': lh, 'lines_found': lf}
        else:
            # Should not happen within one bucket, but merge defensively.
            prev['lines_hit'] = max(prev['lines_hit'], lh)
            prev['lines_found'] = max(prev['lines_found'], lf)

    for name in names:
        bucket = coverage.get(name) or {}
        for src in bucket.get('sources') or []:
            _add(src)
    shared = coverage.get('shared') or {}
    for src in shared.get('sources') or []:
        claimed_by = src.get('claimed_by') or []
        if claimed_by and set(claimed_by).issubset(want):
            _add(src)

    out = list(by_path.values())
    for e in out:
        lf = e['lines_found']
        e['pct'] = (100.0 * e['lines_hit'] / lf) if lf else 0.0
    out.sort(key=lambda e: e['path'])
    return out


def _genhtml_relurl(abs_src: str) -> str:
    """Return the relative URL from ``components/_generated/<page>.html``
    to the genhtml-produced annotated source for ``abs_src``.

    The coverage report is copied into the Sphinx build output at
    ``<outdir>/coverage-report/`` by the ``build-finished`` hook in
    ``conf.py``. Genhtml mirrors the absolute source path inside its
    output directory and appends ``.gcov.html`` to the file name. We
    strip the leading ``/`` from the source path to build the relative
    URL — generated component pages always sit at depth 2
    (``components/_generated/<stem>.html``), so ``../..`` gets us back
    to ``<outdir>/``.
    """
    stripped = abs_src.lstrip('/')
    return f'../../coverage-report/{stripped}.gcov.html'


def _render_page(entry: dict, roots: list[Path], repo_root: Path,
                 coverage: dict | None) -> tuple[str, str]:
    """Return ``(stem, rst_body)`` for one component.

    ``coverage`` is the parsed ``per_component.json`` document (or
    ``None`` if unavailable). When present, each tests_dirs entry with
    an associated ``component`` key pulls its coverage line from it.
    """
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

    # Normalise tests_dirs: each entry is either a str (dir only) or a
    # dict with keys ``dir`` and optional ``component``. Done before the
    # aggregate-coverage block below so the default aggregate list can
    # be derived from the variants declared here.
    tests_dirs_raw = entry.get('tests_dirs') or []
    if not tests_dirs_raw and entry.get('tests_dir'):
        tests_dirs_raw = [entry['tests_dir']]
    tests_dirs: list[dict] = []
    for td in tests_dirs_raw:
        if isinstance(td, str):
            tests_dirs.append({'dir': td, 'component': None})
        elif isinstance(td, dict):
            tests_dirs.append({
                'dir': td['dir'],
                'component': td.get('component'),
            })

    # Aggregate coverage across all variants registered for this
    # component. Explicit ``coverage_aggregate`` on the entry wins;
    # otherwise use every tests_dirs entry that carries a component
    # name. See _aggregate() for the shared-bucket handling.
    agg_names = entry.get('coverage_aggregate')
    if agg_names is None:
        agg_names = [td['component'] for td in tests_dirs if td.get('component')]
    if agg_names and coverage is not None:
        total = _aggregate(coverage, agg_names)
        sources = _collect_sources(coverage, agg_names)
        if total is not None:
            lh, lf = total
            pct = (100.0 * lh / lf) if lf else 0.0
            joined = ', '.join(f'``{n}``' for n in agg_names)
            lines += ['Coverage', '--------', '']
            lines += [
                f'Component coverage: **{lh}/{lf} lines** '
                f'({pct:.1f}%), aggregated across {joined}.',
                '',
            ]
            if sources:
                lines += ['Per-file coverage', '~~~~~~~~~~~~~~~~~', '']
                lines += [
                    '.. list-table::',
                    '    :header-rows: 1',
                    '    :widths: 55 15 10 15',
                    '',
                    '    * - File',
                    '      - Lines',
                    '      - Coverage',
                    '      - Annotated source',
                ]
                for src in sources:
                    abs_path = src['path']
                    try:
                        display = str(Path(abs_path).relative_to(repo_root))
                    except ValueError:
                        display = abs_path
                    url = _genhtml_relurl(abs_path)
                    lines += [
                        f'    * - ``{display}``',
                        f'      - {src["lines_hit"]} / {src["lines_found"]}',
                        f'      - {src["pct"]:.1f}%',
                        f'      - `view <{url}>`_',
                    ]
                lines.append('')
            else:
                lines += [
                    '.. note::',
                    '',
                    '   No per-file coverage data is available yet — '
                    'run the relevant tests with ``COVERAGE=1`` and '
                    'rebuild the docs.',
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

    if tests_dirs:
        lines += ['Tests', '-----', '']
        for td in tests_dirs:
            rel = td['dir']
            component = td['component']
            tests_dir = (repo_root / rel).resolve()
            tests = _extract_tests(tests_dir)
            # One sub-section per directory (sub-heading = leaf dir name).
            heading = Path(rel).name
            lines += [heading, '~' * max(len(heading), 4), '']
            lines += [f'Source: ``{rel}``.', '']
            if component:
                lines += [f'Component: ``{component}``.', '']
            # Coverage line for this variant, when available.
            if component and coverage is not None:
                cov = coverage.get(component)
                if cov is not None:
                    lf = cov.get('lines_found', 0)
                    lh = cov.get('lines_hit', 0)
                    pct = cov.get('pct', 0.0)
                    lines += [
                        f'Coverage: **{lh}/{lf} lines** '
                        f'({pct:.1f}%).',
                        '',
                    ]
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

def _load_coverage() -> dict | None:
    """Load per-component coverage JSON from ``GVSOC_DOC_COVERAGE`` if set.

    Returns the parsed document or ``None`` if the env var is unset, the
    file is missing, or the content is unparseable. Errors are logged
    to stderr but never raised — the doc build must survive.
    """
    import json
    path = os.environ.get('GVSOC_DOC_COVERAGE')
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        print(f'warning: failed to load coverage from {path}: {exc}',
              file=sys.stderr)
        return None


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

    coverage = _load_coverage()

    rendered: list[tuple[str, str]] = []
    for entry in registry:
        stem, body = _render_page(entry, roots, repo_root, coverage)
        (out_dir / f'{stem}.rst').write_text(body)
        rendered.append((stem, entry.get('title', stem)))

    (out_dir / 'index.rst').write_text(_render_index(rendered))
