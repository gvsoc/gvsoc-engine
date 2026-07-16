Power modeling
==============

Overview
........

GVSOC includes a power framework which lets models report their power consumption while the
simulation is running. It is built around a few objects:

- **Power sources** (:cpp:class:`vp::PowerSource`) are instantiated by models to represent
  anything which burns power: the leakage of an IP, the background dynamic power of a clocked
  block, or the energy of a discrete event like a memory access or an instruction.
- **Power tables** give each source its power numbers, as values measured at various
  temperatures, voltages and frequencies. The framework interpolates them to get the value at
  the current operating point.
- **Power traces** (:cpp:class:`vp::PowerTrace`) collect the consumption of one or more
  sources. Each component automatically gets one, and traces are organized hierarchically so
  that the consumption of a component includes the one of all its children. They are the basis
  for both the power report and the power VCD traces.
- The **power engine** (:cpp:class:`vp::PowerEngine`) is the central object which manages
  power modeling and dumps the power report.
- The **thermal model** (see :ref:`power_thermal`) optionally closes the loop: it periodically
  samples the power consumed by parts of the system, runs a thermal simulator, and applies the
  resulting temperatures back onto the power sources so that temperature-dependent tables
  (typically leakage) are re-evaluated.

Power modeling is enabled with the ``--power`` option. When it is disabled,
``power.is_enabled()`` statically returns false so that all the power estimation code costs
nothing. Models with non-trivial power code should use it as a guard:

.. code-block:: cpp

    if (this->power.is_enabled())
    {
        this->access_power.account_energy_quantum();
    }

Simple accounting calls do not need the guard, they are already inactive when power modeling
is disabled.


Power sources
.............

A power source models one contributor to the power consumption. A model can declare any
number of them, to keep concurrent activities separate in the reports. Each source can
account up to three kinds of consumption:

- **Leakage**: static power (in W), consumed as soon as the component is powered on. It is
  entirely managed by the framework once started: models usually call
  ``leakage_power_start()`` once and never touch it again — the supply state then decides
  whether it is accounted.
- **Dynamic background power**: power (in W) consumed continuously while some activity is
  going on, for example a clocked block in idle. Models start and stop it with
  ``dynamic_power_start()`` / ``dynamic_power_stop()``.
- **Energy quantum**: a fixed amount of energy (in pJ) consumed by one discrete event, like
  one memory access or one instruction. Models account one quantum per event with
  ``account_energy_quantum()``.

One source can carry both a dynamic table (quantum or background, depending on its unit) and
a leakage table. The current quantum, background power and leakage values are automatically
re-evaluated from the tables whenever the temperature, voltage or frequency changes.

A typical model declares its sources in the constructor, straight from the power tables
carried by its compiled config (see :ref:`power_tables`), and starts the leakage in
``start()``:

.. code-block:: cpp

    #include <vp/power/power_table_convert.hpp>

    // Declarations
    vp::PowerSource background_power;
    vp::PowerSource access_power;

    // Constructor
    vp::new_power_source_from_config(this->power, "background", &background_power,
        this->cfg.power.background);
    vp::new_power_source_from_config(this->power, "access", &access_power,
        this->cfg.power.access);

    // start()
    this->background_power.leakage_power_start();

    // On each access
    this->access_power.account_energy_quantum();

A source whose tables were left empty (because the power model file has no entry for it,
or no file was given at all) is *inert*: all its accounting calls are no-ops. Models can
therefore declare their sources unconditionally and let the platform decide, per instance,
which ones get power numbers.


.. _power_tables:

Power tables
............

Power numbers are given per source as tables of values measured at various temperatures,
voltages and frequencies. At runtime the framework linearly interpolates between the given
points to estimate the value at the current operating point (see
:cpp:class:`vp::PowerLinearTable`). A value can be declared frequency-independent with the
special frequency ``any``.

Each source can have two tables:

- ``dynamic``: either a per-event energy quantum (unit ``pJ``) or a background power
  (unit ``W``).
- ``leakage``: always a power in W.

Until they are changed, the operating point defaults to 25 degrees, 1.2 V and 50 MHz. The
voltage carried by the built-in voltage port and the voltage keys of the tables are plain
numbers: they just have to use the same unit (the tutorials use mV, the engine default of
``1.2`` matches tables expressed in V).

The tables are written in YAML power model files and read directly into the config tree.
The schema has one top-level entry per source name, then ``values`` indexed by temperature,
voltage and frequency:

.. code-block:: yaml

    background:
      leakage:
        type: linear
        unit: W
        values:
          25.0:
            1.2:
              any: 0.001
          125.0:
            1.2:
              any: 0.201
    write_32:
      dynamic:
        type: linear
        unit: pJ
        values:
          25.0:
            1.2:
              any: 5000.0

Models supporting power modeling declare one ``PowerSourceConfig`` field per source,
grouped in a plain-data nested config, plus a ``power_model`` field carrying the path of a
power model file (the shared config classes live in ``vp.power_config``):

.. code-block:: python

    from vp.power_config import PowerSourceConfig, consume_power_model

    class MemoryV3PowerConfig(Config):
        _defer_parent_init: ClassVar[bool] = True

        background: PowerSourceConfig = cfg_field(default_factory=PowerSourceConfig)
        read_32:    PowerSourceConfig = cfg_field(default_factory=PowerSourceConfig)
        # ... one field per source

    class MemoryV3Config(Config):
        size: int = ...
        power_model: str = cfg_field(default='')
        power: MemoryV3PowerConfig = cfg_field(default_factory=MemoryV3PowerConfig,
            init=False)

The component loads the file itself, in its ``configure()`` hook:

.. code-block:: python

    def configure(self):
        consume_power_model(self.get_config())

so that the platform instantiating it only needs to name the file:

.. code-block:: python

    mem = Memory(self, 'l1', config=MemoryV3Config(size=0x1000,
        power_model='chips/gap/gap9/power_models/l1.yaml'))

Every source named in the file is applied onto the field with the same name — a name with
no matching field is a generation-time error — and fields the file does not mention keep
their empty tables, so their sources stay inert. Since ``configure()`` runs after the tree
is built but before code generation, the path can also be set programmatically after
instantiation, or exposed as a target parameter and given on the command line.

The ``_defer_parent_init = True`` marker on the nested class is what makes the code
generator inline the group as a by-value struct inside the model's compiled config, so the
C++ model reads its tables directly from ``this->cfg.power.<name>``, as shown in the
previous section (see ``models/memory/memory_v3.cpp`` for a complete example).

For special cases, ``vp.power_config`` also provides ``apply_power_yaml`` — apply a file
onto a power config explicitly, optionally restricted to a list of source names — and
``load_power_yaml_list`` for positional, variable-count tables which must stay a
``list[PowerSourceConfig]`` (e.g. the iss_v2 per-instruction-group energies, indexed by
ISA power group).


Power traces
............

Power traces collect the energy accounted by power sources. Every component automatically
gets a default trace named *power_trace*, which is the default parent of all the sources
declared in the component. Traces are chained hierarchically: whatever a trace accounts is
also accounted to the trace of the parent component, so the top-level trace always shows the
consumption of the whole system, and the power report can show its distribution level by
level.

A model can declare additional traces to give finer granularity to the reports, and associate
each source to one of them:

.. code-block:: cpp

    vp::PowerTrace core_trace;
    this->power.new_power_trace("power_trace_core", &core_trace);
    this->power.new_power_source("insn", &insn_power, config, &core_trace);

Each power trace exposes three real-valued VCD traces under the component path:
``power_trace`` (total instant power), ``dyn_power_trace`` (dynamic part) and
``static_power_trace`` (leakage part). They are dumped like any other VCD event trace, for
example with ``--vcd --event=.*``, and display well with an analog step format. The instant
power is the sum of the background and leakage powers plus the energy quanta of the current
cycle converted to power, so a power spike is visible on each accounted event.

Each trace maintains two sets of energy counters:

- The **report window** counters, reset each time a capture starts, used by the power report.
- The **total** counters, which accumulate since the beginning of the simulation and are
  never reset. Consumers which need to observe power over time (the thermal model, the proxy,
  the GUI) take deltas of the totals through
  :cpp:func:`vp::BlockPower::get_total_energy`, so any number of them can watch the same
  component without interfering with each other or with the report.


Power supply and voltage control
................................

Every component has two built-in slave ports:

- ``power_supply`` (``vp::WireSlave<int>``), which applies a :cpp:enum:`vp::PowerSupplyState`
  to the component and its whole sub-hierarchy;
- ``voltage`` (``vp::WireSlave<int>``), which does the same with a new voltage.

On the Python side they are returned by ``i_POWER()`` and ``i_VOLTAGE()``, so a power
controller model (e.g. a PMU) just needs ``vp::WireMaster<int>`` ports bound to them:

.. code-block:: python

    pmu.o_POWER_CTRL(cluster.i_POWER())
    pmu.o_VOLTAGE_CTRL(cluster.i_VOLTAGE())

When a supply state is applied, the framework walks the sub-hierarchy and:

- turns every power source on or off (``OFF`` stops accounting both leakage and dynamic
  power, ``ON`` and ``ON_CLOCK_GATED`` resume it),
- calls the ``power_supply_set`` hook of each component.

The distinction between ``ON`` and ``ON_CLOCK_GATED`` is left to the models: a typical
implementation starts its background dynamic power when fully on and stops it when clock
gated or off:

.. code-block:: cpp

    void MyComp::power_supply_set(vp::PowerSupplyState state)
    {
        if (state == vp::PowerSupplyState::ON)
        {
            this->background_power.dynamic_power_start();
        }
        else
        {
            this->background_power.dynamic_power_stop();
        }
    }

Voltage changes simply re-evaluate all the tables of the sub-hierarchy at the new voltage.
The clock frequency does not need any manual handling: each clock domain propagates frequency
changes to the power sources of its components, so frequency-dependent tables are re-evaluated
on DFS.


Power reports
.............

With ``--power``, the engine writes a report to *power_report.csv* in the run directory. The
report gives, for each power trace of the hierarchy, the average dynamic power, leakage
power, total power and contribution over the measured window:

.. code-block:: text

    Trace path; Dynamic power (W); Leakage power (W); Total (W); Percentage

The window is controlled with capture triggers:

- The memory models (``power_trigger: true``) start a capture when the value ``0xabbaabba``
  is written to their offset 0, and stop it — dumping one report — on ``0xdeadcaca``. This
  lets the simulated application delimit precisely the regions of interest, and several
  windows can be captured in one run. On each capture stop, the average power of the window
  is also reported in the terminal.
- Programmatically, models can call :cpp:func:`vp::PowerEngine::start_capture` and
  :cpp:func:`vp::PowerEngine::stop_capture` on the engine returned by
  ``this->power.get_engine()``.
- If the application never delimited any window itself, the engine dumps one report covering
  the whole run at the end of the simulation, so ``--power`` always produces a report.

External tools (launcher, proxy, GUI) query power through the ``gv::Power`` interface of the
GVSOC external API: ``get_instant_power`` and ``get_average_power`` return the consumption of
the whole system, and ``report_get`` returns a hierarchical ``gv::PowerReport`` tree with one
node per block. This is what feeds the live power display of the GUI.


.. _power_thermal:

Thermal modeling
................

The power framework evaluates all the tables at the current temperature of each source, and
:cpp:func:`vp::BlockPower::temperature_set_all` applies a new temperature to a whole
sub-hierarchy. On top of this, the ``ThermalModel`` component
(``models/thermal/thermal_model.py``) implements a closed thermal loop between the power
framework and a thermal simulator.

This component is provided as an example: it shows how to sample power, drive a thermal
simulator and apply temperatures back onto the power framework, but its built-in simulator
is a deliberately simple stand-in. Use it as the starting point for integrating a real
thermal simulator — either by implementing the :cpp:class:`ThermalSimulator` interface it
drives (see below), or by taking the component itself as a template for a tighter
integration.

Closed loop
-----------

The thermal model is a platform-level component configured with a list of **sync points**,
each identified by the path of a component from the platform top (e.g. ``chip/cluster``).
Every ``period`` picoseconds it:

1. Samples the energy consumed since the previous update by each sync point — through the
   never-reset total energy counters, so it does not interfere with ``--power`` report
   captures — and derives the average power over the interval.
2. Hands the per-sync-point powers to the thermal simulator, which returns the new
   per-sync-point temperatures.
3. Applies each temperature to all the power sources below the sync point component, so that
   temperature-dependent tables (typically leakage) are re-evaluated.

This creates the expected feedback: activity heats a component, the higher temperature
increases its leakage, which heats it further, until it converges. For the loop to have an
effect, the power tables must of course contain values at more than one temperature:

.. code-block:: yaml

    background:
      leakage:
        type: linear
        unit: W
        values:
          25.0:
            1.2:
              any: 0.001    # 0.001 W at 25 degrees...
          125.0:
            1.2:
              any: 0.201    # ...up to 0.201 W at 125 degrees

Each sync point exposes a real-valued VCD trace ``temp_<name>`` with its temperature, and a
child trace ``temp_<name>/power`` with the power sampled at each update. They show up in the
GUI timeline whenever power modeling is enabled, like the other power signals, and the GUI
can additionally render the sync points as a floorplan heat map (see the geometry keys
below).

The built-in simulator is a first-order RC network: each sync point independently converges
towards ambient plus its own dissipation, following ``T += dt/tau * (P*Rth + T_amb - T)``,
with no thermal coupling between points. It is good enough to demonstrate the loop and to
test temperature-dependent tables, not to predict real temperatures. It sits behind the
small :cpp:class:`ThermalSimulator` C++ interface (``models/thermal/thermal_simulator.hpp``),
whose ``update`` method receives the interval duration and the per-sync-point powers and
returns the new temperatures — implementing this interface (e.g. as a bridge to an external
thermal simulator, with the sync points mapped to its floorplan areas) is the intended way
to plug a real simulator into the loop.

Sync points file
----------------

The sync points are typically described in a YAML file, exported from the thermal simulator
side where they correspond to its areas:

.. code-block:: yaml

    period: 10000000000     # sampling period in ps, optional (default 10 ms)
    temp_ambient: 25.0      # optional, built-in RC simulator only
    temp_init: 25.0         # optional
    verbose: false          # optional, print one line per sync point per update
    temp_min: 25.0          # optional, GUI heat-map color scale (both or neither)
    temp_max: 60.0
    sync_points:
      cluster:
        path: chip/cluster  # component path from the platform top
        rth: 100.0          # thermal resistance to ambient in K/W (RC simulator)
        tau: 2.0e-6         # thermal time constant in seconds (RC simulator)
        geometry:           # optional, GUI heat map only: floorplan rectangle
          x: 0.0            # in abstract units, y grows downward
          y: 0.0
          w: 2.0
          h: 1.0

The ``geometry`` rectangles and the ``temp_min``/``temp_max`` bounds are only used by the GUI
heat-map view; the thermal model itself ignores them. Without them the heat map auto-ranges
its color scale and lays the sync points out automatically.

A platform can load the file at generation time:

.. code-block:: python

    from thermal.thermal_model import (ThermalModel, ThermalModelConfig,
                                       load_thermal_yaml)

    config = ThermalModelConfig(period=1_000_000, temp_ambient=25.0)
    config.sync_points = load_thermal_yaml('thermal.yaml')
    thermal = ThermalModel(self, 'thermal', config=config)
    clock.o_CLOCK(thermal.i_CLOCK())

Enabling it per run
-------------------

All the thermal config fields are runtime-carried: their values travel through the per-run
runtime config file instead of the compiled platform tree, so changing them does not require
rebuilding the target. The component also declares a ``file`` target parameter carrying the
path of a sync points YAML file, and stays fully inert (no periodic event) while it has no
sync points. A target can therefore embed a dormant thermal model and let the user activate
and configure it entirely from the command line:

.. code-block:: shell

    gvrun --target=<target> --power --parameter <path>/file=thermal.yaml run

A ``verbose`` parameter forces the per-update trace lines
(``thermal <name> power_w=<power> temp_c=<temp>``) on for one run:

.. code-block:: shell

    gvrun --target=<target> --power --parameter <path>/verbose=true run

The thermal model requires power modeling: without ``--power`` it warns once and stays
inactive. A complete closed-loop testbench, including a ``--power`` capture running
concurrently with the thermal loop, is available in ``core/tests/power/thermal``.


Tutorial
........

Tutorial *14 - How to add power traces to a component* (see :doc:`tutorials`) walks through
extending a component with power sources, controlling its supply state and voltage from a
second component, and measuring the resulting consumption.


API reference
.............

Block power API
---------------

Models access the power framework through the ``power`` member of their component, an
instance of ``vp::BlockPower``:

.. doxygenclass:: vp::BlockPower
   :project: gvsoc
   :members:

Power source
------------

.. doxygenclass:: vp::PowerSource
   :project: gvsoc
   :members:

Power supply states
-------------------

.. doxygenenum:: vp::PowerSupplyState
   :project: gvsoc

Power trace
-----------

.. doxygenclass:: vp::PowerTrace
   :project: gvsoc
   :members:

Power engine
------------

.. doxygenclass:: vp::PowerEngine
   :project: gvsoc
   :members:

Power tables
------------

.. doxygenstruct:: vp::PowerTableEntry
   :project: gvsoc
   :members:

.. doxygenstruct:: vp::PowerSourceTable
   :project: gvsoc
   :members:

.. doxygenclass:: vp::PowerLinearTable
   :project: gvsoc
   :members:

Power table config helpers
--------------------------

.. doxygenfunction:: vp::new_power_source_from_config
   :project: gvsoc

External power API
------------------

.. doxygenclass:: gv::Power
   :project: gvsoc
   :members:

.. doxygenclass:: gv::PowerReport
   :project: gvsoc
   :members:

Thermal simulator interface
---------------------------

.. doxygenclass:: ThermalSimulator
   :project: gvsoc
   :members:

.. doxygenclass:: RcThermalSimulator
   :project: gvsoc
   :members:
