iDMA (v2)
=========

The v2 iDMA is the :doc:`io_v2 <../../../interfaces/io_v2>` port of
the PULP iDMA family, living under
``gvsoc/pulp/models/pulp/ips/pulp/idma_v2/`` and exposed at Python
module path ``ips.pulp.idma_v2``. Every IO port speaks the v2 IO
protocol (``vp/itf/io_v2.hpp``) and every burst flows through the
reusable :class:`BeatResponseAdapter` so the back-ends see a uniform
per-beat callback stream regardless of how the downstream slave
chooses to respond.

Architecture
------------

A v2 iDMA instance is a single composite Python generator that wires
three layered C++ sub-blocks into one shared library:

.. code-block:: text

    CPU / accelerator
        │  (programming interface — register or offload)
        ▼
    ┌──────────────┐
    │  Front-end   │  decode descriptors, queue them
    └──────┬───────┘
           │  IdmaTransfer*
           ▼
    ┌──────────────┐
    │ Middle-end   │  IDmaMe2D — fan a 2D transfer out as
    │   (2D)       │  a stream of 1D bursts
    └──────┬───────┘
           │  IdmaBurst*  (source-side burst then destination-side burst)
           ▼
    ┌──────────────┐
    │   Back-end   │  Single AXI read/write back-end pair —
    └──────┬───────┘  every burst goes out on the AXI master.
           │
           │  io_v2 master, routed through BeatResponseAdapter
           ▼
        bus / memory

The front-end is the only stage that differs between the three
generators :class:`ips.pulp.idma_v2.reg_dma.RegDmaV2`,
:class:`ips.pulp.idma_v2.snitch_dma.SnitchDmaV2` and
:class:`ips.pulp.idma_v2.cheshire_dma.CheshireDmaV2`. Everything from
``IDmaMe2D`` downward is shared and compiled into all three.

Beat-streaming response normalisation
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The AXI back-end (``be/idma_be_axi.cpp``) routes its bus-facing
master through a :class:`utils.io_v2_beat_adapter.BeatResponseAdapter`.
A logical read burst is issued as one io_v2 ``req()`` with ``size =
total_burst_bytes`` and ``is_first = is_last = true``. Whatever form
the downstream slave chooses to answer — sync ``IO_REQ_DONE``, async
big-packet ``resp()``, or native beat ``resp()`` — the adapter spreads
``ceil(size / axi_width)`` beats so the last beat lands at
``now + req->latency`` with a per-beat step of
``max(1, latency / N)``. This avoids double-counting when an upstream
router (e.g. ``router_v2_bandwidth``) already encodes the bandwidth
cost in ``req->latency``. The back-end's ``on_beat`` handler forwards
each chunk straight to the destination back-end when the BE can
accept, saving an extra fsm-hop per beat.

Writes still stream beat-by-beat: one io_v2 ``req()`` per
``axi_width`` bytes with ``is_first`` / ``is_last`` / ``burst_id``
set per beat. Each beat's response feeds a single ``on_beat``
callback that walks the source-side ack FIFO so write completion
follows the slowest-of-source-and-destination semantics.

The adapter is a generic utility under
``gvsoc/core/models/utils/io_v2_beat_adapter.{hpp,cpp}`` and is also
used by ``router_v2_beat`` (one adapter per output port). See the
top-of-file comment in ``io_v2_beat_adapter.hpp`` for the
three-point rule governing **when** an io_v2 component should use
it — only beat-fidelity midboxes and initiators that feed a
per-beat consumer at cycle-accurate spacing qualify; functional
passthroughs, request-side latency annotators and terminal targets
do not.

Front-ends
----------

The three front-ends accept different programming interfaces but
produce the same ``IdmaTransfer`` descriptors downstream.

Register front-end — :class:`RegDmaV2`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: ips.pulp.idma_v2.reg_dma.RegDmaV2
   :members: i_INPUT, o_AXI, o_AXI_READ, o_AXI_WRITE, o_IRQ
   :show-inheritance:

Snitch-offload front-end — :class:`SnitchDmaV2`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: ips.pulp.idma_v2.snitch_dma.SnitchDmaV2
   :members: i_OFFLOAD, o_OFFLOAD_GRANT, o_AXI
   :show-inheritance:

Cheshire-offload front-end — :class:`CheshireDmaV2`
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. autoclass:: ips.pulp.idma_v2.cheshire_dma.CheshireDmaV2
   :members: i_INPUT, o_OFFLOAD_GRANT, o_AXI
   :show-inheritance:

Configuration
-------------

:class:`RegDmaV2` takes a full :class:`RegDmaConfig` dataclass;
:class:`SnitchDmaV2` and :class:`CheshireDmaV2` accept the same
fields as constructor keyword arguments. The fields are:

.. autoclass:: ips.pulp.idma_v2.reg_dma_config.RegDmaConfig
   :show-inheritance:

``axi_width`` drives the beat-streaming math: it is the beat size on
the AXI master, and the number of beats spread by the
:class:`BeatResponseAdapter` is ``ceil(burst_size / axi_width)``.

Middle-end (2D iterator)
------------------------

Class: ``IDmaMe2D``
(``gvsoc/pulp/models/pulp/ips/pulp/idma_v2/me/idma_me_2d.cpp``).

The middle-end owns one ``transfer_queue`` sized by
``transfer_queue_size`` and one ``current_transfer`` cursor. The
front-end calls ``enqueue_transfer(t)`` (with ``can_accept_transfer()``
gating the queue depth) and the middle-end's FSM peels off 1D bursts
from the current transfer, one burst per FSM tick, until the
transfer is exhausted:

- A **1D transfer** (``config bit 1 == 0``) is treated as a 2D
  transfer with ``reps = 1`` so the same loop handles both shapes.
- For each iteration the middle-end emits one ``IdmaTransfer``
  carrying ``(src, dst, size = current_transfer->size)``, increments
  the parent's ``nb_bursts``, calls ``be->enqueue_transfer(burst)``
  on the central back-end, then advances
  ``current_src += src_stride`` / ``current_dst += dst_stride`` and
  decrements ``current_reps``.
- When ``current_reps`` hits zero the parent transfer is marked
  ``bursts_sent`` and the next queued transfer is pulled.
- ``ack_transfer(burst)`` decrements ``nb_bursts``; when both
  ``bursts_sent`` and ``nb_bursts == 0`` are reached the front-end's
  ``ack_transfer(parent)`` fires and the front-end can retire the
  descriptor (and possibly raise its completion IRQ).

Timing
~~~~~~

The FSM emits **at most one burst per cycle**, gated by
``be->can_accept_transfer()`` — i.e. the central BE must not have a
burst still in flight. So an N-rep 2D transfer with central-BE-bound
throughput costs N cycles of FSM ticks for the loop itself, plus
whatever the back-ends take to actually move the data. The middle-end
adds no extra latency on the data path; it is purely a
descriptor-fan-out stage.

Central back-end (transfer router)
----------------------------------

Class: ``IDmaBe``
(``gvsoc/pulp/models/pulp/ips/pulp/idma_v2/be/idma_be.cpp``).

Between the middle-end and the protocol back-ends sits a small
transfer-router that:

1. **Routes every burst through the single AXI read/write back-end
   pair.** Source and destination addresses are not interpreted — the
   AXI back-end is the iDMA's only egress, matching RTL. Any TCDM
   access has to loop back through the surrounding interconnect (e.g.
   an AXI-to-mem bridge in front of the TCDM banks).
2. **Legalises burst sizes** by taking
   ``min(src_be->get_burst_size(src, size),
   dst_be->get_burst_size(dst, size))``. The AXI back-end caps at
   ``AXI_PAGE_SIZE = 4 KiB`` (and at ``config.burst_size`` if set);
   the legalised size is what actually gets issued.
3. **Issues paired bursts** — for one legalised burst it calls
   ``src_be->read_burst(transfer, src, sz)`` and
   ``dst_be->write_burst(transfer, dst, sz)`` in the same FSM
   tick. The destination ``write_burst`` allocates a slot but does
   not issue anything yet — it just stages a buffer that the
   source side will fill via ``be->write_data``.
4. **Pipes data between back-ends** — when the source back-end
   wants to forward a chunk it calls ``be->write_data(transfer,
   data, size)``, which the central BE routes to the matching
   destination back-end. Acks return via ``be->ack_data`` which
   forwards to ``src_be->write_data_ack`` so the source can free
   its staging buffer.
5. **Closes the burst** on ``ack_data`` when the cumulative
   ``ack_size`` reaches zero, then ``me->ack_transfer(t)``.

Timing constraints worth knowing:

- **Source-back-end serialisation.** The FSM refuses to start a
  new burst as long as the previously-used source back-end is
  still draining and the new burst would route to a *different*
  source back-end (``prev_transfer_src_be``'s ``is_empty()`` is
  checked). This keeps the upstream pull-stream coherent — only one
  source protocol drives ``write_data`` calls at a time. If the new
  burst stays on the same source, it can pipeline directly.
- **Burst-at-a-time on the central BE.** Only one 1D burst is
  legalised per FSM tick; the FSM re-arms itself for the next
  cycle as long as the current transfer still has bytes left.

AXI protocol back-end
---------------------

Class: ``IDmaBeAxi``
(``gvsoc/pulp/models/pulp/ips/pulp/idma_v2/be/idma_be_axi.cpp``).

The AXI back-end is the most complex of the two protocol back-ends
because it actually models an AXI-like beat-streaming bus through a
:class:`BeatResponseAdapter`. The single source file is instantiated
**twice** by every iDMA generator (``be_axi_read`` and
``be_axi_write``) so reads and writes hold separate slot pools and
appear as two independent io_v2 master ports
(``axi_read`` / ``axi_write``). Both instances share the same
``axi_width`` and ``burst_queue_size``.

Slot pool
~~~~~~~~~

``burst_queue_size`` slot descriptors (``BurstInfo``) are
pre-allocated at construction. Each slot owns:

- a 4 KiB staging buffer (one AXI page),
- a pre-sized pool of ``vp::IoReq`` objects (one per axi_width-sized
  beat plus one extra for unaligned head), so the issue path never
  allocates,
- the per-burst cursors ``bytes_buffered`` (writes only),
  ``bytes_issued``, ``bytes_responded`` and ``bytes_acked``,
- a ``write_pending_acks`` FIFO of source-chunk ack records.

``can_accept_burst()`` simply tests the free-slot list, so the
central BE only enqueues a new burst when a slot is available;
otherwise the FSM stalls and waits for ``be->update()`` from a
recycled slot.

Read pipeline
~~~~~~~~~~~~~

  1. ``read_burst()`` allocates a slot, sets ``total_size``,
     pushes the slot on ``pending_bursts``.
  2. The FSM's ``issue_beat()`` emits **exactly one io_v2 req per
     read burst** with ``size = total_size``,
     ``is_first = is_last = true``, ``burst_id = slot``. It then
     leaves ``pending_bursts``.
  3. The :class:`BeatResponseAdapter` normalises whatever response
     form the slave produced (sync DONE, async big-packet, beat
     stream) into ``ceil(total_size / axi_width)`` ``on_beat``
     callbacks. The adapter spreads the beats so the last one
     lands at ``now + req->latency``, with per-beat step
     ``max(1, latency / N)``. Throughput therefore matches the
     slave's announced cost (i.e. a router_v2_bandwidth's
     ``burst_duration = size / bandwidth`` translates directly to
     "one beat per ``bandwidth / axi_width`` cycles").
  4. Each ``on_beat`` carries ``(data, size, offset)``. If the
     destination back-end can take a chunk now
     (``be->is_ready_to_accept_data``) the AXI back-end calls
     ``be->write_data`` **synchronously inside on_beat** and
     queues an ack record on ``read_ack_queue`` — saving the
     1-cycle FSM hop that a queue-and-drain design would cost. If
     the destination is back-pressured, the chunk parks on
     ``read_push_queue`` and the back-end's own FSM drains it
     when the destination becomes ready (``be->update()``).
  5. ``write_data_ack`` (called from the destination via
     ``ack_data``) pops one entry off ``read_ack_queue`` and
     advances ``bytes_acked``. When ``bytes_acked == total_size``
     the slot returns to the free pool and the central BE is
     nudged via ``be->update()``.

Steady-state read throughput is therefore **one beat per cycle**
(matching the AXI back-end's beat width), bounded by whichever is
slower:

- the slave's announced bandwidth (via ``req->latency``);
- the destination back-end's accept rate
  (``can_accept_data`` / ``write_data_ack`` chain);
- the chain of routers between iDMA and slave (each
  ``router_v2_beat`` adds 0 cycles in steady state, since its
  adapter paces the response on the response channel — but the
  *first* beat is delayed by router_latency).

Write pipeline
~~~~~~~~~~~~~~

  1. ``write_burst()`` allocates a slot, records ``total_size``,
     pushes onto ``pending_bursts`` *and* onto
     ``write_fill_queue`` so the source side knows where to land
     its first chunk.
  2. The source back-end calls ``write_data(transfer, data, size)``
     which copies the chunk into the head slot's staging buffer
     and appends ``(data, size)`` to that slot's
     ``write_pending_acks``. Multiple source chunks can stack into
     one slot until ``bytes_buffered == total_size``, at which
     point the slot pops off ``write_fill_queue``.
  3. ``issue_beat()`` emits one io_v2 req per ``axi_width`` bytes
     already buffered (``min(axi_width, total_size - bytes_issued,
     bytes_buffered - bytes_issued)`` — beats can only leave once
     their bytes have arrived from the source). Each beat carries
     its slot id as ``burst_id`` plus per-beat ``is_first`` /
     ``is_last`` flags. Pacing is one beat per cycle.
  4. ``on_beat`` on a write beat advances ``bytes_responded``
     by ``event.size`` and then walks ``write_pending_acks``,
     popping every entry whose end-byte falls at or before
     ``bytes_responded`` and calling
     ``be->ack_data(transfer, src_chunk_ptr, size)`` — i.e. the
     source only sees a chunk acknowledged after the
     corresponding write beats have really been responded to. When
     ``bytes_responded == total_size`` the slot returns to the
     free pool.

Steady-state write throughput is **one beat per cycle**, bounded by
the same three factors as reads (slave bandwidth, source feed rate,
upstream routers).

Deny / retry
~~~~~~~~~~~~

The adapter surfaces ``IO_REQ_DENIED`` directly: ``issue_beat``
rolls back the slot's beat-pool cursor (writes) or simply doesn't
mark the read as issued, sets ``denied_blocked = true`` and stops
issuing. The downstream slave's ``retry()`` fires the adapter's
``on_retry`` which clears ``denied_blocked`` and nudges the FSM.

Tests
-----

End-to-end tests for the v2 iDMA live under
``gvsoc/pulp/tests/idma_v2/``. They wire a small
``IDmaTesterV2`` (stimulus + checker) against a
:class:`RegDmaV2` programmed through its register front-end and
through a configurable ``router_v2_*`` between iDMA and memory.

The testset's source-of-truth is ``testset.cfg``; each case sets
``expected_cycles`` and a tolerance, computed against the iDMA's
beat-streaming bandwidth model. See
``gvsoc/pulp/tests/idma_v2/test.py`` for the per-case stimulus
specs.
