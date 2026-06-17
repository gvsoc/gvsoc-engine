Execution Modes
---------------

The same GVSOC engine and models are compiled into several variants, called
*execution modes*. Each mode enables a different set of features, trading
simulation speed for instrumentation:

==========  ==========================================================================  ===========
Mode        Features                                                                    Speed
==========  ==========================================================================  ===========
optimized   No traces, events, profiling or assertions. This is the default.            fastest
asserts     Same as *optimized* (no traces or profiling) but with model assertions       fast
            enabled.
profile     Event and statistics infrastructure, used for statistics and profiling.     slower
debug       Instruction and system traces, VCD / event waveforms, and memory checking.  slowest
==========  ==========================================================================  ===========

A given simulation can be run in any of these modes without rebuilding: GVSOC
selects the matching pre-compiled engine and models at launch time.

Automatic Selection
...................

By default GVSOC runs in the *optimized* mode. Requesting a feature on the
command line automatically switches to the mode that supports it, so you
normally never have to select a mode yourself:

=========================  ==================================================  =========
Option                     Feature                                             Mode
=========================  ==================================================  =========
*(none)*                   Plain run                                           optimized
``--trace``                System / instruction traces                         debug
``--vcd`` / ``--event``    VCD / event waveforms                               debug
``--memcheck``             Memory access checking                              debug
``--power``                Power modeling                                      debug
``--stats``                Statistics                                          profile
``--gui``                  Interactive GUI (uses the event infrastructure)     profile
=========================  ==================================================  =========

When several features are combined, the most complete mode is used: *debug*
takes precedence over *profile*, which takes precedence over *asserts*, which
takes precedence over *optimized*. For example, ``--stats --trace`` runs in
*debug* mode.

The *asserts* mode is never selected automatically; it is only entered by
forcing it explicitly (see below).

Forcing a Mode
..............

A mode can also be forced explicitly, regardless of the requested features,
using one of the following options:

====================  ==================================================================
Option                Effect
====================  ==================================================================
``--debug-mode``      Force the *debug* mode.
``--profile-mode``    Force the *profile* mode.
``--asserts-mode``    Force the *asserts* mode (the only way to enter it).
====================  ==================================================================

Forcing a mode is useful to:

* run the *asserts* mode, which is never selected automatically;
* keep running in a heavier mode (for example to leave traces available) without
  having to pass the corresponding feature options;
* guarantee a specific mode for reproducibility.

For example, to run with model assertions enabled but without the cost of traces
or profiling: ::

  gvrun --target rv64 --parameter binary=test.elf run --asserts-mode

Model Assertions
................

Models can check internal invariants with the ``vp::BlockTrace::assert`` method.
On failure the assertion is reported exactly like a trace line — with the
timestamp, the cycle stamp and the path of the component instance that failed —
and the simulation aborts.

Assertions are compiled in for the *profile*, *debug* and *asserts* modes. In
the *optimized* mode they are disabled and have no runtime cost, so a plain run
stays at full speed. The *asserts* mode is meant to keep assertions active while
remaining as fast as the optimized build, which makes it well suited to
regression runs.
