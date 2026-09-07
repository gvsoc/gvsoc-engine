iDMA (v3)
=========

The v3 iDMA is a second :doc:`io_v2 <../../../interfaces/io_v2>` model of
the PULP iDMA family, living under ``gvsoc/pulp/models/ips/pulp/idma_v3/``
and exposed at Python module path ``ips.pulp.idma_v3``. It exists next to
:doc:`idma_v2` and is built so that its cycle behaviour follows from the
block structure of the RTL (``idma_backend``, ``idma_nd_midend``,
``idma_reg32_3d``) rather than from tuned constants: every RTL FIFO, buffer
and handshake rule has a counterpart, and the protocol managers plug into a
generic back-end.

Its first user is the el1 cluster DMA (:class:`ClusterDmaV3`, two
half-duplex OBI/AXI streams); :class:`RegDmaV3` is the single-stream
AXI-to-AXI shape (Cheshire, voscap, the Spatz cluster DMA once it is driven
by the xdma front-end :class:`SnitchDmaV3`).

Architecture
------------

.. code-block:: text

    front-end (register ports / xdma offload)
        │  IdmaNdReq                       ids next=2 / done=1, event per ND completion
        ▼
    request FIFO ── registered, req_fifo_depth, one per stream
        ▼
    ND mid-end  (IdmaMeNd)                one 1D per cycle, super_last on the last one
        │  Idma1dReq  (pull: the legalizer asks when idle)
        ▼
    back-end (IdmaBackend, one per stream)
        legalizer          ≤1 read split + ≤1 write split per cycle,
                           per-side page rule asked from the managers,
                           coupled (lock-step) or decoupled sides
        r_fifo / w_fifo    r_dp_req / w_dp_req, depth num_ax_in_flight
        wlast              w_last, depth meta_fifo_depth, popped by the response
        byte-lane buffer   width lanes x buffer_depth bytes, next-cycle visibility
        ▼                                          ▲
    read manager ──► buffer ──► write manager      │ response (B / rvalid)
    (AXI / OBI)                 (AXI / OBI)  ──────┘  completes the 1D on its last burst

The per-cycle evaluation of a back-end runs in one clock event, in the
order legalizer, write managers (buffer pops), read managers (held response
beats, buffer pushes), and only re-arms itself while something progressed;
every stall is released by the event that ends it (a FIFO pop, a response
beat, a bus retry, a launch), so an idle DMA costs nothing.

Rules the model reproduces
--------------------------

The following facts come from the RTL and are what the cycle references of
the testsets are derived from:

- **Launch**: a transfer becomes visible to the mid-end one cycle after the
  register read that launches it (plus ``launch_bubble`` from idle, the
  datapath clock gate); the legalizer takes a 1D request and emits its first
  split the cycle after; consecutive 1D requests leave no bubble (the next
  one is taken in the cycle of the last split).
- **Legalizer**: each side may not cross the boundary its manager reports,
  2^(log2(width) + burst_len) bytes for AXI (capped at the 4 KiB page), one
  bus word for OBI. Both sides advance together taking the shorter burst
  when both protocols burst and the transfer is not decoupled (Spatz);
  otherwise each advances on its own (el1, always). A side only advances when
  its FIFO can take the split and its manager's address channel is ready.
- **Ordering FIFOs**: registered (an entry pushed in cycle N is visible from
  N+1, a slot popped in N is reusable from N+1). ``num_ax_in_flight`` bounds
  the outstanding read and write bursts, ``meta_fifo_depth`` the write
  bursts awaiting their response.
- **Buffer**: ``width`` independent byte lanes of ``buffer_depth`` entries;
  a response beat enters only when every lane it strobes has room, its
  bytes are visible to the write side the next cycle, a full lane accepts a
  push in the cycle it is popped. Realignment is the lane indexing:
  ``read_shift = src % width`` on the way in, ``write_shift = -(dst %
  width)`` on the way out, so a write beat needs bytes of two read beats
  when the two ends are misaligned (one extra cycle).
- **AXI write**: data beats are independent of the address phase, one per
  cycle when the buffer holds the strobed bytes; the burst response (one
  data-less ack in io_v2) retires the burst. With the RAW coupler
  (``raw_coupling``) the address phase waits for the first response beat
  of a read burst.
- **OBI**: address and data go out together, one word per burst, one
  transaction outstanding, the data valid one cycle after the grant; a
  registered two-entry address FIFO sits in front of the write manager, so
  the legalizer cannot run ahead on the write side and the next line of a
  multi-line transfer into the TCDM waits for the previous line's last word.
- **Completion**: the response of the last write burst of a 1D completes it;
  the 1D flagged ``super_last`` completes the ND transfer; DONE_ID and the
  completion event follow one cycle later. A zero-length request is
  rejected at once, ahead of the transfers in flight, and flagged last.

Timing anchors (memory latency 1, no contention): launch to first request
2 cycles (3 with the launch bubble), request to first response beat 2
cycles, read beat to write beat 1 cycle (2 misaligned), one beat per cycle
per direction, write response 2 cycles, DONE_ID +1.

Model classes and their RTL counterparts
----------------------------------------

==========================  ==========================================  ================================
Class                       RTL                                         Parameters
==========================  ==========================================  ================================
``IdmaFeReg``               ``idma_reg32_3d`` + ``idma_transfer_id_gen``  ``nb_reg_ports``, streams, ``nb_events``, ``launch_bubble``
``IdmaFeXdma``              ``axi_dma_tc_snitch_fe`` (xdma insns)       --
``IdmaMeNd``                request ``stream_fifo`` + ``idma_nd_midend``  ``req_fifo_depth``, ``nb_dims``
``IdmaLegalizer``           ``idma_legalizer_*``                        page rule from the managers
``IdmaBackend``             ``idma_backend`` FIFOs + ``idma_dataflow_element``  ``num_ax_in_flight``, ``meta_fifo_depth``, ``buffer_depth``, ``width``
``IdmaAxiRead``             ``idma_axi_read`` + AR fall-through         ``burst_len``
``IdmaAxiWrite``            ``idma_axi_write`` (+ ``idma_channel_coupler``)  ``burst_len``, ``raw_coupling``
``IdmaObiPortGroup``        ``mem_to_banks`` + ``obi_mux`` + rready converter  ports, port width, address mask
``IdmaObiRead`` / ``Write`` ``idma_obi_read`` / ``idma_obi_write``      --
==========================  ==========================================  ================================

io_v2 mapping
-------------

- ``axi_read`` (``IoV2Beat(width)``): one data-less request per read burst
  at the word-aligned address, freed by the manager on the last response
  beat; each response beat is a distinct allocator object copied into the
  buffer and freed at once. A beat the buffer cannot take is answered
  ``IO_RESP_DENIED`` and re-offered through ``resp_retry()`` from the
  back-end tick once the cycle's pops are applied; the producer must
  support response back-pressure (beat router, beat adapters), which the
  manager asserts at start.
- ``axi_write`` (``IoV2Beat(width)``): one size-0-pool beat per cycle whose
  data aliases a per-burst staging buffer, covering the contiguous strobed
  run, with ``is_first`` / ``is_last`` / ``burst_id`` and one initiator per
  burst; the target owns granted beats, a denied beat is held and re-sent
  in ``retry()``; the burst ack (inline on the last beat or one data-less
  ``resp()``) is the B response.
- ``tcdm_read_<i>`` / ``tcdm_write_<i>`` (``IoV2SingleReq``): the port
  group issues the narrow accesses of one word together, re-issues denied
  ports synchronously in ``retry()`` as the HCI crossbar expects, and
  completes the word at the slowest port's ``get_full_latency()``.
- ``input_<n>`` (``IoV2SingleReq``): register accesses answer inline; the
  NEXT_ID read is denied while the stream's request FIFO is full and retried
  once a slot is free, round-robin between the ports.

Generators
----------

.. autoclass:: ips.pulp.idma_v3.cluster_dma.ClusterDmaV3
   :members: i_INPUT, i_ENABLE, o_TCDM_WRITE, o_TCDM_READ, o_AXI_READ, o_AXI_WRITE, o_EVENT, o_FC_EVENT, o_BUSY
   :show-inheritance:

.. autoclass:: ips.pulp.idma_v3.cluster_dma_config.ClusterDmaV3Config
   :show-inheritance:

.. autoclass:: ips.pulp.idma_v3.reg_dma.RegDmaV3
   :members: i_INPUT, o_AXI_READ, o_AXI_WRITE, o_IRQ, o_BUSY
   :show-inheritance:

.. autoclass:: ips.pulp.idma_v3.reg_dma_config.RegDmaV3Config
   :show-inheritance:

.. autoclass:: ips.pulp.idma_v3.snitch_dma.SnitchDmaV3
   :members: i_OFFLOAD, o_OFFLOAD_GRANT, o_AXI_READ, o_AXI_WRITE, o_BUSY
   :show-inheritance:

.. autoclass:: ips.pulp.idma_v3.snitch_dma_config.SnitchDmaV3Config
   :show-inheritance:

Tests
-----

- ``gvsoc/pulp/tests/idma_v3_axi``: :class:`RegDmaV3` configured like the
  Spatz cluster DMA (512-bit, 3 bursts in flight, 4 KiB pages, coupled
  legalizer, RAW coupler) between two memories behind a beat router:
  aligned and page-crossing bursts, decoupled sides, misalignment, 2D, 32
  KiB with three bursts in flight, request FIFO back-pressure, zero length,
  memory latency, the 8-byte width. Cycle references follow the rules above
  with tolerances of 2-4 cycles. The :class:`SnitchDmaV3` cases drive the
  same transfers through the offload wire.
- ``gvsoc/pulp/tests/idma_v3_cluster``: :class:`ClusterDmaV3` between a TCDM
  behind an untimed crossbar stand-in (four narrow ports) and an L2 behind a
  beat router: both streams, L1 to L1, unaligned ends, 2D and 3D, zero
  length, queue back-pressure, the two read managers sharing the read
  ports, the second register port, the clock gate, memory latency.
- ``tests/calibration/targets/el/dma_l1_l2`` and ``dma_ext_ram`` hold the
  el1 bandwidth goldens measured with this model.

Differences from v2
-------------------

- No central back-end legalizing one burst size for both ends: each side
  is split on its own protocol's rule, as in the RTL, and the managers keep
  no private data queues; the byte-lane buffer is the only data storage.
- No protocol-pair table: a back-end holds one read manager and one write
  manager per protocol, chosen by the source and destination protocol of
  the request; a missing manager is fatal.
- Registered FIFOs and next-cycle buffer visibility give the launch,
  read-to-write and response timings without per-block constants; the only
  tuning knob left is ``launch_bubble`` (the datapath clock gate).
- The zero-length rejection, the RAW coupler and the OBI write address
  FIFO are modelled; the 2D and 3D references of the cluster testset moved
  accordingly.
- Parameters come from a ``config_tree`` dataclass compiled into the
  component (no JSON properties).
