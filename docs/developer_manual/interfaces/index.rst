Interfaces
==========

GVSOC components talk to each other through typed master / slave port
pairs. The most common one is the memory-mapped IO request, used for
every CPU-bus / interconnect / memory / peripheral access. Two
protocol versions coexist:

- :doc:`io_v2` — **recommended for new models**. Adds an explicit
  burst protocol (sync big-packet, async big-packet, beat-stream),
  an AXI-like ``retry()`` handshake for back-pressure, and a clean
  one-master-per-slave binding rule.
- :doc:`io_v1` — the original protocol, still used by a number of
  models. Documented for reference; new ports should not be wired
  on v1.

The two protocols use different ``vp::IoSlave`` / ``vp::IoMaster``
classes (declared in ``vp/itf/io.hpp`` and ``vp/itf/io_v2.hpp``
respectively) and different signatures at bind time, so a single
GVSOC simulation can mix v1 and v2 islands as long as v1 ports only
bind to v1 ports and vice-versa.

.. toctree::
   :maxdepth: 2

   io_v2
   io_v1
