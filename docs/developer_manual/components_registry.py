# Registry of GVSoC components to include in the generated documentation.
#
# Each entry generates one page under components/_generated/<module>.<class>.rst.
# The generator resolves `module` via the paths listed in GVSOC_MODULES (the
# same env var the build system uses), so any import path that works at
# simulation time works here.
#
# Fields:
#   module     — Python import path of the generator file
#   class      — generator class name inside that module
#   title      — section title to render at the top of the page
#   tests_dirs — list of test-directory descriptors. Each entry is either
#                a plain string (directory path, relative to the repo root)
#                or a dict with:
#                    dir       — same directory path
#                    component — dotted component name (matching a key in
#                                per_component.json). When set, the generator
#                                renders a Coverage: N/M lines (P%) line for
#                                this variant, sourced from the JSON produced
#                                by scripts/coverage_per_component.py.
#
# Optional fields:
#   coverage_aggregate — list of dotted component names to sum into the
#                page's top-of-page "Component coverage" line. When
#                omitted, the aggregate is derived from every tests_dirs
#                entry that has a `component` field. Any source that the
#                attribution script routed to the `shared` bucket
#                because it was claimed by multiple of the aggregate
#                variants is folded in (common code counts once).

COMPONENTS = [
    {
        'module':     'interco.router_v2',
        'class':      'Router',
        'title':      'Router (v2)',
        'tests_dirs': [
            {
                'dir': 'gvsoc/core/tests/interco/router_untimed',
                'component': 'interco.router_v2.untimed',
            },
            {
                'dir': 'gvsoc/core/tests/interco/router_bandwidth',
                'component': 'interco.router_v2.bandwidth',
            },
            {
                'dir': 'gvsoc/core/tests/interco/router_backpressure',
                'component': 'interco.router_v2.backpressure',
            },
            {
                'dir': 'gvsoc/core/tests/interco/router_beat',
                'component': 'interco.router_v2.beat',
            },
        ],
    },
]
