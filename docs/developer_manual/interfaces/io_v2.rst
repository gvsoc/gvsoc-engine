Memory-mapped IO request (v2)
=============================

The v2 IO protocol — declared in
``gvsoc/engine/engine/include/vp/itf/io_v2.hpp`` — is the recommended
interface for new memory-mapped models. It carries reads, writes,
atomics, and bursted accesses between an initiator (a master) and a
target (a slave). Each ``vp::IoSlave`` port is bound to **exactly
one** ``vp::IoMaster``; for fan-in, use one slave port per master
plus a muxed slave declaration (see `Multiplexed ports`_).

The protocol replaces v1 (:doc:`io_v1`) and changes four things:

- **No request-argument stack.** v1's per-request ``args[16]`` /
  ``payload[64]`` slots are gone; models keep their state on the
  side (member fields, per-initiator tables, ``IoReq::parent`` /
  ``IoReq::initiator``).
- **Single master per slave.** The per-master stub machinery
  (``req->resp_port``) is gone — slaves reply on their own slave
  port via ``in.resp(req)``.
- **DENIED uses a ``retry()`` handshake**, not per-request
  ``grant()``. The slave signals "I can accept again" once; the
  master is responsible for re-sending denied requests.
- **Bursts are explicit.** ``is_first`` / ``is_last`` / ``burst_id``
  fields on every ``IoReq`` make multi-beat semantics first-class
  on the wire, with three valid response forms (sync big-packet,
  async big-packet, beat stream).

At a glance, the wiring between an initiator and a target looks like
this:

.. code-block:: text

      Master component                            Slave component
      ┌────────────────────┐                  ┌────────────────────┐
      │                    │                  │                    │
      │   vp::IoMaster ────┼──── binding ────►┼──── vp::IoSlave    │
      │   out              │                  │   in               │
      │                    │                  │                    │
      └─┬────────────────▲─┘                  └─▲────────────────┬─┘
        │                │                      │                │
        │ out.req(req)   │                      │ req() handler  │
        │   forward      │                      │ (slave logic)  │
        │                │                      │                │
        │                │ in.resp(req)         │                │
        │                │   async response     │                │
        │                ├──────────────────────┤                │
        │                │                      │                │
        │                │ in.retry()           │                │
        │                │   "I can accept now" │                │
        │                ├──────────────────────┤                │
        │                │                      │                │
        ▼                                                        ▼

The master submits work with ``out.req(req)``; the slave processes
the request and either replies inline (``IO_REQ_DONE``), accepts
ownership and replies asynchronously later (``IO_REQ_GRANTED`` +
``in.resp(req)``), or refuses for now (``IO_REQ_DENIED`` +
``in.retry()`` when ready).

The IoReq object
----------------

A ``vp::IoReq`` is the unit of work that travels both directions on
the bus. Fields a master or slave usually touches:

.. list-table::
   :header-rows: 1
   :widths: 20 30 50

   * - Field
     - Type
     - Role
   * - ``addr``
     - ``uint64_t``
     - Request address. The master sets it; routers may rewrite it
       when forwarding (e.g. subtracting a base offset before
       handing it to a downstream slave).
   * - ``data``
     - ``uint8_t *``
     - Pointer to the payload buffer. The master allocates and
       owns the buffer; the slave writes into it (read) or reads
       from it (write).
   * - ``size``
     - ``uint64_t``
     - Number of bytes. For a read burst this is the total burst
       size; for a beat write it is the beat size.
   * - ``opcode``
     - ``vp::IoReqOpcode``
     - ``READ`` / ``WRITE`` plus the atomic variants
       (``LR`` / ``SC`` / ``SWAP`` / ``ADD`` / ``XOR`` /
       ``AND`` / ``OR`` / ``MIN`` / ``MAX`` / ``MINU`` /
       ``MAXU``).
   * - ``status``
     - ``vp::IoRespStatus``
     - Response status the master reads when ``IO_REQ_DONE`` or
       ``resp()`` fires. Defaults to ``IO_RESP_OK`` both at
       construction and after ``prepare()``; the slave only needs
       to call ``req->set_resp_status(IO_RESP_INVALID)`` to report
       an error. Slaves are not required to set ``IO_RESP_OK``
       explicitly on the success path.
   * - ``is_first``, ``is_last``
     - ``bool``
     - Burst markers. ``is_first = is_last = true`` for a
       non-burst single-beat request; multi-beat bursts set them
       per beat. See `Burst protocol`_.
   * - ``burst_id``
     - ``int64_t``
     - Optional correlation tag shared by every beat / response of
       a burst. ``-1`` means "no burst".
   * - ``latency``
     - ``int64_t``
     - Cycle delay annotated by routers / targets that model timing
       inline. See `Latency annotation`_.
   * - ``second_data``
     - ``uint8_t *``
     - Second operand pointer for atomics (e.g. ``SC`` /
       ``SWAP``). Not used for plain reads / writes.
   * - ``initiator``
     - ``void *``
     - Free-form handle the master can stash on the request. The
       protocol never reads it; useful for the master's own
       routing on the response path.
   * - ``parent`` / ``next``
     - ``vp::IoReq *``
     - Free-form linked-list / parent-link slots. Useful when a
       midbox splits one parent request into several sub-requests
       and needs to walk back on completion.
   * - ``remaining_size``
     - ``uint64_t``
     - Scratch field; not reserved for a specific consumer.

Ownership / mutation rules:

- The **master owns** the request until ``req()`` returns
  ``IO_REQ_GRANTED``.
- While ``GRANTED``, the **slave owns** the request and must
  eventually call ``in.resp(req)``.
- After ``resp()``, ownership returns to the master.
- On ``IO_REQ_DONE``, ownership never leaves the master — the
  slave only mutates the object in-place during the call.
- On ``IO_REQ_DENIED``, ownership stays with the master; the slave
  has not touched the request.

A master reusing a request between sends should call
``req->prepare()`` to reset per-send fields (currently
``latency = 0`` and ``status = IO_RESP_OK``).

Ports
-----

Master ports
~~~~~~~~~~~~

A master port (``vp::IoMaster``) is bound to one slave port. The
constructor takes the master's response and retry callbacks (set at
construction time — there are no ``set_*_meth`` setters):

.. code-block:: cpp

    static void retry(vp::Block *__this);
    static void resp (vp::Block *__this, vp::IoReq *req);

    vp::IoMaster out{ &MyComp::retry, &MyComp::resp };
    new_master_port("output", &out, this);   // bind via vp::Component

The muxed form lets one master serve several slaves via per-call
dispatch:

.. code-block:: cpp

    static void retry_muxed(vp::Block *__this, int id);
    static void resp_muxed (vp::Block *__this, vp::IoReq *req, int id);

    vp::IoMaster out_i{ i, &MyComp::retry_muxed, &MyComp::resp_muxed };

Slave ports
~~~~~~~~~~~

A slave port (``vp::IoSlave``) takes its request handler at
construction:

.. code-block:: cpp

    static vp::IoReqStatus req(vp::Block *__this, vp::IoReq *req);

    vp::IoSlave in{ &MyComp::req };
    new_slave_port("input", &in, this);

Muxed form for fan-in:

.. code-block:: cpp

    static vp::IoReqStatus req_muxed(vp::Block *__this,
                                     vp::IoReq *req, int id);
    vp::IoSlave in_i{ i, &MyComp::req_muxed };

Reply / signalling
~~~~~~~~~~~~~~~~~~

The slave replies (or signals readiness) **via its own slave port**:

.. code-block:: cpp

    in.resp(req);     // async response — the master's resp() fires
    in.retry();       // "I can accept again" — the master's retry() fires

There is no per-request ``grant`` / ``resp_port`` look-up: the
binding records the master's callbacks on the slave side at
configuration time, so ``in.resp(req)`` knows where to deliver.

Request statuses
----------------

Every ``IoMaster::req(req)`` returns one of:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Status
     - Semantics
   * - ``IO_REQ_DONE``
     - Fully handled inline. The reply (data + ``req->status``) is
       already in ``req``. No further callback fires.
   * - ``IO_REQ_GRANTED``
     - Slave took ownership; the response will arrive later via
       ``resp()``. The master must not reuse the request until then.
   * - ``IO_REQ_DENIED``
     - Slave could not accept. The master keeps the request and
       resubmits after ``retry()`` fires.

The response status (``req->status``) is independent:

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Status
     - Semantics
   * - ``IO_RESP_OK``
     - Access succeeded.
   * - ``IO_RESP_INVALID``
     - Access failed (bad address, decoder error, ...). Inline
       ``IO_REQ_DONE`` may still carry ``IO_RESP_INVALID``; the two
       axes are orthogonal.

Synchronous flow
----------------

The simplest slave: fill the data in place and return
``IO_REQ_DONE``. The entire round trip happens inside the master's
``out.req(req)`` call.

.. code-block:: text

      master                                          slave
        │                                               │
        │── out.req(req) ──────────────────────────────►│
        │                                               │ ◄ fills req->data
        │                                               │ ◄ sets req->status
        │◄──────────────── IO_REQ_DONE ─────────────────│
        │                                               │
        ▼                                               ▼
      continues with response data
      already in req->data

.. code-block:: cpp

    vp::IoReqStatus MyComp::req(vp::Block *__this, vp::IoReq *req)
    {
        auto *_this = (MyComp *)__this;

        if (req->get_addr() != 0 || req->get_size() != 4)
        {
            req->set_resp_status(vp::IO_RESP_INVALID);
            return vp::IO_REQ_DONE;
        }
        if (req->get_is_write())
        {
            _this->reg_value = *(uint32_t *)req->get_data();
        }
        else
        {
            *(uint32_t *)req->get_data() = _this->reg_value;
        }
        req->set_resp_status(vp::IO_RESP_OK);
        return vp::IO_REQ_DONE;
    }

The master observes the data in ``req->data`` and the status in
``req->status`` immediately on return.

Asynchronous flow
-----------------

If a slave can take the request but needs cycles to produce the
response (e.g. memory latency), it returns ``IO_REQ_GRANTED`` and
calls ``in.resp(req)`` later — usually from a ``ClockEvent``
handler scheduled the right number of cycles ahead.

.. code-block:: text

      master                                          slave
        │                                               │
        │── out.req(req) ──────────────────────────────►│
        │                                               │ ◄ stashes req
        │                                               │ ◄ schedules event
        │◄──────────────── IO_REQ_GRANTED ──────────────│
        │                                               │
        │                                          ─────┤  (ClockEvent fires)
        │                                               │ ◄ fills req->data
        │                                               │ ◄ sets req->status
        │◄────────────────  in.resp(req) ───────────────│
        │ master's resp(req) callback fires             │
        │                                               │
        ▼                                               ▼

.. code-block:: cpp

    static void event_handler(vp::Block *__this, vp::ClockEvent *event)
    {
        auto *_this = (MyComp *)__this;
        _this->pending->set_resp_status(vp::IO_RESP_OK);
        _this->in.resp(_this->pending);
    }

    vp::IoReqStatus MyComp::req(vp::Block *__this, vp::IoReq *req)
    {
        auto *_this = (MyComp *)__this;
        _this->pending = req;
        _this->done_event.enqueue(_this->latency_cycles);
        return vp::IO_REQ_GRANTED;
    }

The master receives the response when ``in.resp(req)`` fires and is
dispatched to its ``resp`` callback.

DENIED + ``retry()`` handshake
------------------------------

When the slave cannot accept the request right now (input FIFO
full, internal arbiter busy, ...) it returns ``IO_REQ_DENIED``. The
slave is **not** expected to remember which request was denied;
the master holds onto the request and resubmits when ``retry()``
fires.

When the blocking condition clears, the slave calls ``in.retry()``
once — without a request argument. Every master bound to the
slave that has a denied request pending will see its ``retry``
callback fire and can re-submit.

.. code-block:: text

      master                                          slave (FIFO full)
        │                                               │
        │── out.req(req) ──────────────────────────────►│
        │◄──────────────── IO_REQ_DENIED ───────────────│
        │ (master keeps req                             │
        │  and remembers it owes a re-send)             │
        │                                               │
        │                                          ─────┤  (FIFO drains)
        │◄──────────────── in.retry() ──────────────────│  "ready again"
        │ master's retry() callback fires               │
        │                                               │
        │── out.req(req) ──────────────────────────────►│  (re-send)
        │◄────────── IO_REQ_GRANTED (or DONE) ──────────│
        │                                               │
        ▼                                               ▼

Note that ``in.retry()`` carries no request — the slave doesn't
track *which* requests were denied; it just announces that its
input is once again ready. A master that has nothing pending
ignores the callback. A master that had multiple denied requests
re-tries them in whatever order it chose to remember them. A
slave that fills back up between the ``retry()`` and the master's
next ``req()`` simply returns ``IO_REQ_DENIED`` again, and the
loop repeats until acceptance.

.. _io_v2_synchronous_retry:

**Retry must be serviced synchronously.** A master that receives
``retry()`` **must** re-submit its held request(s) *inside the
``retry()`` callback* — i.e. in the same cycle, before the call
returns. It must not merely flag the request and defer the
re-send to a later cycle (for example by enqueuing a clock event).

This is a hard protocol constraint, not just a latency
optimisation. Some slaves only keep their "ready to accept"
window open for the duration of the synchronous ``retry()`` call.
The canonical example is the zero-buffer arbiter ``log_ico_v2``:
it denies every request, runs its round-robin arbitration, and
then calls ``retry()`` on the winner while an internal
``in_election`` flag is raised. Only while that flag is set does
the next ``req()`` get forwarded inline to the bank; the flag is
cleared as soon as ``retry()`` returns. A master that re-sends one
cycle later misses the window, is denied again, and the two sides
live-lock forever. Servicing the retry synchronously closes the
loop within the same cycle.

This matches AXI level-``READY`` semantics: ``retry()`` is the
slave raising ``READY``, and the master's still-asserted ``VALID``
beat is expected to transfer on that same edge.

.. code-block:: cpp

    // Slave side
    vp::IoReqStatus MyComp::req(vp::Block *__this, vp::IoReq *req)
    {
        auto *_this = (MyComp *)__this;
        if (_this->fifo.size_left() < req->get_size())
        {
            // Don't accept; the master will resubmit on retry().
            _this->retry_owed = true;
            return vp::IO_REQ_DENIED;
        }
        _this->fifo.push(req);
        return vp::IO_REQ_GRANTED;
    }

    void MyComp::on_fifo_drain()
    {
        if (this->retry_owed)
        {
            this->retry_owed = false;
            this->in.retry();   // wakes the denied master(s)
        }
    }

.. code-block:: cpp

    // Master side — the retry callback set on out's IoMaster ctor.
    // The re-send happens *here*, synchronously, inside the callback —
    // never deferred to a later cycle (see "Retry must be serviced
    // synchronously" above).
    static void retry(vp::Block *__this)
    {
        auto *_this = (MyComp *)__this;
        if (_this->denied_req)
        {
            vp::IoReqStatus st = _this->out.req(_this->denied_req);
            // st can be DONE / GRANTED (accepted), or DENIED again
            // — leave denied_req set and wait for the next retry().
            if (st != vp::IO_REQ_DENIED) _this->denied_req = nullptr;
        }
    }

Unlike v1, a slave cannot enqueue multiple denied requests and
``grant()`` them one by one. A slave that wants to model a queue of
N pending requests should accept them (``GRANTED``) and respond
later, not deny them.

Burst protocol
--------------

Three fields make multi-beat transfers explicit:

- ``is_first`` / ``is_last`` — mark the position of a beat inside
  a burst. For a non-burst single-beat request both flags are
  ``true``.
- ``burst_id`` — correlator shared by every beat of one burst.
  ``-1`` means "not part of a burst".

Read burst shape (AXI-asymmetric)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A read burst is always submitted as **exactly one** ``req()`` with
``size = total_burst_bytes``, ``is_first = is_last = true``, and
``burst_id = <ID or -1>``. The slave decides how to respond — see
the three forms below.

The three response forms
~~~~~~~~~~~~~~~~~~~~~~~~

For any single ``req()`` the slave may pick:

1. **Sync big-packet.** Fill ``req->data`` in place, set
   ``req->status``, return ``IO_REQ_DONE``. No later ``resp()``.

2. **Async big-packet.** Return ``IO_REQ_GRANTED``, then call
   ``in.resp(req)`` **once**, later, with the whole payload
   (``is_first = is_last = true``, ``size`` unchanged).

3. **Beat stream.** Return ``IO_REQ_GRANTED``, then call
   ``in.resp(req)`` ``N`` times on the **same** ``IoReq`` object,
   mutating ``data`` / ``size`` / ``is_first`` / ``is_last``
   between calls. Cumulative response sizes must equal the
   request's ``size``; the final beat carries ``is_last = true``
   and the burst's final ``IO_RESP_OK`` / ``IO_RESP_INVALID``.

All three are valid and masters must tolerate any of them.
Diagrammatically, the three shapes look like this:

.. code-block:: text

   Form 1 — sync big-packet:

      master                                          slave
        │                                               │
        │── req(size=N, is_first=is_last=true) ────────►│
        │                                               │ ◄ fills full N bytes
        │◄────────────── IO_REQ_DONE ───────────────────│
        │   (whole response delivered)                  │
        ▼                                               ▼


   Form 2 — async big-packet:

      master                                          slave
        │                                               │
        │── req(size=N, is_first=is_last=true) ────────►│
        │◄────────────── IO_REQ_GRANTED ────────────────│
        │              (...delay...)                    │
        │◄── resp(size=N, is_first=is_last=true) ───────│
        │   (whole response delivered                   │
        │    in one async resp())                       │
        ▼                                               ▼


   Form 3 — beat stream:

      master                                          slave
        │                                               │
        │── req(size=N, is_first=is_last=true) ────────►│
        │◄────────────── IO_REQ_GRANTED ────────────────│
        │              (...delay...)                    │
        │◄── resp(size=w, is_first=t, is_last=f) ───────│  beat 0
        │◄── resp(size=w, is_first=f, is_last=f) ───────│  beat 1
        │               . . .                           │
        │◄── resp(size=w, is_first=f, is_last=t) ───────│  beat N/w−1
        │   (Σ sizes = N; final beat carries is_last)   │
        ▼                                               ▼

A master that needs uniform per-beat handling regardless of which
form the slave picked just declares its outbound binding with
``signature=IoV2Beat(beat_width=W)``. The framework auto-inserts an
``IoV2BeatAdapter`` between this master and any ``IoV2BigPacket`` /
legacy-string ``'io_v2'`` slave; the master sees one normal
``resp()`` per beat with ``size``/``data``/``is_first``/``is_last``/
``burst_id``/``status`` mutated per beat. See
`Beat-fidelity consumers`_.

Write burst shapes
~~~~~~~~~~~~~~~~~~

Writes are master-driven (the master picks the wire shape) and
come in two forms:

- **Beat-form** — N ``req()`` calls, each with one beat-sized
  payload plus per-beat ``is_first`` / ``is_last`` / ``burst_id``.
  The slave responds per ``req()``. Useful when the master models
  per-beat write timing (typical AXI W channel).
- **Big-packet** — one ``req()`` carrying the full burst data,
  ``is_first = is_last = true``. The slave consumes it
  atomically and responds once. Useful when the master doesn't
  need per-beat write semantics.

A slave that accepts both forms (most do — a flat write of
``size`` bytes works the same in either case) is the simplest
shape. Components that model per-beat write timing (e.g. the iDMA
AXI back-end) emit the beat-form; functional memories accept
either.

.. code-block:: text

   Beat-form write:

      master                                          slave
        │                                               │
        │── req(size=w, is_first=t, is_last=f) ────────►│  beat 0
        │◄── IO_REQ_DONE / GRANTED → resp() ────────────│
        │── req(size=w, is_first=f, is_last=f) ────────►│  beat 1
        │◄── IO_REQ_DONE / GRANTED → resp() ────────────│
        │               . . .                           │
        │── req(size=w, is_first=f, is_last=t) ────────►│  beat N/w−1
        │◄── IO_REQ_DONE / GRANTED → resp() ────────────│
        ▼                                               ▼


   Big-packet write:

      master                                          slave
        │                                               │
        │── req(size=N, is_first=is_last=true) ────────►│
        │   (whole payload in one shot)                 │
        │◄── IO_REQ_DONE / GRANTED → resp() ────────────│
        ▼                                               ▼

The cumulative-size invariant
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Across every response that a single submitted ``req()`` produces
(whether one or N), the **sum** of ``response.size`` must equal the
request's ``size``, in cumulative byte order, with no gaps or
overlaps. The final response carries ``is_last = true`` and the
final status. Masters use this invariant to retire the request
exactly once (free buffer + free ``IoReq`` only on
``is_last == true``).

Slave skeletons for the three response forms
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

**Sync big-packet** (matches the simple synchronous flow above —
just fill the whole buffer and return ``IO_REQ_DONE``).

**Async big-packet** — same as the async flow above, just with
``req->size`` covering the full burst:

.. code-block:: cpp

    static void resp_event(vp::Block *__this, vp::ClockEvent *)
    {
        auto *_this = (MemoryLikeSlave *)__this;
        // pending->data already filled from backing storage
        _this->pending->set_resp_status(vp::IO_RESP_OK);
        _this->in.resp(_this->pending);     // is_first/is_last untouched
    }

**Beat stream** — N calls on the same IoReq, mutating per beat:

.. code-block:: cpp

    void BeatSlave::deliver_one_beat()
    {
        IoReq *req = this->pending;
        uint64_t off = this->bytes_responded;
        uint64_t beat = std::min<uint64_t>(BEAT_WIDTH, req->get_size() - off);

        req->set_data(this->backing + req->get_addr() + off);
        req->set_size(beat);
        req->is_first = (off == 0);
        req->is_last  = (off + beat == this->total_size);
        req->set_resp_status(vp::IO_RESP_OK);
        this->in.resp(req);

        this->bytes_responded += beat;
        if (!req->is_last) this->beat_event.enqueue(1);  // pace at 1/cycle
    }

Master-side consumer
~~~~~~~~~~~~~~~~~~~~

A master must tolerate all three forms. The typical pattern: on
each response, accumulate the bytes against an expected total and
free the request only when ``is_last`` fires (which the slave is
required to set on the cumulative-final response):

.. code-block:: cpp

    static void resp(vp::Block *__this, vp::IoReq *req)
    {
        auto *_this = (MyMaster *)__this;
        auto *info  = (BurstInfo *)req->initiator;

        info->bytes_received += req->get_size();
        // ... do whatever the master does with each chunk ...

        if (req->is_last)
        {
            // Burst complete — safe to retire the IoReq.
            delete[] info->data;
            delete info;
            delete req;
        }
    }

If the master *needs* a per-beat callback stream (e.g. a beat-rate
DMA back-end that paces a downstream pipeline at one chunk per
cycle), it should not implement this consumer logic by hand —
declare the outbound binding with
``signature=IoV2Beat(beat_width=W)`` and the framework auto-inserts
an ``IoV2BeatAdapter`` that normalises whatever response form the
slave produced into a uniform per-beat ``resp()`` at cycle-accurate
spacing. See `Beat-fidelity consumers`_.

Latency annotation
------------------

``req->latency`` is a cycle count carrying the timing cost of the
transaction along the path from slave to master. Components that
model timing inline — i.e. that return ``IO_REQ_DONE`` after a
logical delay — add cycles to it before returning. The master
reads it and stalls its own pipeline accordingly (the
``IoV2BeatAdapter``, for instance, spreads beats so the last
one lands at ``now + latency``).

For models that already use wall-clock scheduling (a
``ClockEvent``-based deferred ``resp()``) the field can stay at 0
— the delay is already baked into the cycle at which ``resp()``
fires.

For beat-stream responses the slave can set the per-beat
``latency`` to give the master an earliest-ready offset for each
beat (in addition to the natural pacing of when ``resp()`` is
called).

A master reusing the same request object across submissions should
call ``req->prepare()`` (or explicitly reset ``latency``) between
sends.

Beat-fidelity consumers
-----------------------

When a master wants to consume responses as a per-beat stream
**regardless** of which of the three forms the slave chose, declare
the outbound binding with a class-based :class:`gvsoc.signature.IoV2Beat`
signature. The framework's binding pass auto-inserts an
``IoV2BeatAdapter`` between the master and the slave whenever the
slave's signature is the legacy ``'io_v2'`` string or an explicit
:class:`gvsoc.signature.IoV2BigPacket` — the master then sees a
normal ``IoMaster``-shaped flow with one ``resp()`` per beat:

.. code-block:: python

    from gvsoc.signature import IoV2Beat

    class MyDma(st.Component):
        def o_AXI(self, slave_itf):
            # Framework auto-inserts utils.io_v2_beat_adapter when the
            # bound slave is IoV2BigPacket (or legacy 'io_v2').
            self.itf_bind('axi', slave_itf,
                          signature=IoV2Beat(beat_width=64))

On the C++ side the master is a plain ``vp::IoMaster``; the
``resp_meth`` is invoked once per beat with the per-beat fields
mutated on the request:

- ``req->size``, ``req->data`` — slice for this beat,
- ``req->is_first`` / ``req->is_last`` — position in this
  response stream,
- ``req->burst_id`` — captured snapshot, stable across cascaded
  beat-aware routers,
- ``req->status`` — final status carried by every beat.

Signature matrix and ``IoV2BeatAdapter`` insertion rule:

==========================  ==================  ===============================
master \\ slave              ``IoV2BigPacket``   ``IoV2Beat(W)``
==========================  ==================  ===============================
``IoV2BigPacket``            no adapter          no adapter (master tolerates beat-form responses)
``IoV2Beat(W)``              **adapter inserted**  no adapter when widths match; error otherwise
==========================  ==================  ===============================

The adapter component is ``utils.io_v2_beat_adapter`` (source at
``gvsoc/core/models/utils/io_v2_beat_adapter.{hpp,cpp}``). It appears
in the tree under the name ``{master_comp}_{master_port}_bridge``,
parented to the component owning the binding; the framework also
clones the master's clock binding onto the bridge. Existing users
that consume their bus via this mechanism are ``router_v2_beat``
and the iDMA's ``idma_be_axi`` — see
:doc:`../components/ips/pulp/idma_v2` for a worked example.

Generic mechanism
~~~~~~~~~~~~~~~~~

``Signature.bridge_to(other_sig, parent, name)`` is the single hook
the binding pass calls. Any future signature axis — clock-domain
crossing, voltage-domain crossing, address-width adapter,
endianness — adds a new ``Signature`` subclass with its own
``is_compatible`` / ``bridge_to`` logic and the framework picks it
up unchanged. The pass also lives in legacy ``gvsoc.systree`` (the
gapy build-time path) so the generated ``tree.cpp`` and the
runtime systree always agree.

Multiplexed ports
-----------------

A muxed port lets one ``IoSlave`` distinguish requests by ID, or
one ``IoMaster`` send to one of several slaves by ID. Use the
muxed constructor + the corresponding muxed method signature
(``id`` argument added to the callback). The framework records the
ID at bind time and adds a thin stub that injects it into the
non-muxed dispatch path.

This is how routers expose one input port per upstream master
under a single Python ``i_INPUT(id)`` accessor: each ``IoSlave`` is
constructed with a unique ``id``, and the request handler reads
``id`` to find which input the request came in on.

.. code-block:: cpp

    static vp::IoReqStatus req_muxed(vp::Block *__this,
                                     vp::IoReq *req, int id);

    // declare one IoSlave per input, each with its mux id
    for (int i = 0; i < N; i++)
    {
        in[i] = new vp::IoSlave(i, &MyComp::req_muxed);
        new_slave_port("input_" + std::to_string(i), in[i], this);
    }

v1 → v2 migration cheat-sheet
-----------------------------

For models still on the v1 ``vp/itf/io.hpp``, the recipe is:

.. list-table::
   :header-rows: 1
   :widths: 50 50

   * - v1 (``io.hpp``)
     - v2 (``io_v2.hpp``)
   * - ``IO_REQ_OK``
     - ``IO_REQ_DONE`` (+ ``req->set_resp_status(IO_RESP_OK)``)
   * - ``IO_REQ_INVALID``
     - ``IO_REQ_DONE`` + ``req->set_resp_status(IO_RESP_INVALID)``
   * - ``IO_REQ_PENDING``
     - ``IO_REQ_GRANTED``
   * - ``vp::IoSlave in;`` + ``in.set_req_meth(&f);``
     - ``vp::IoSlave in{&f};`` (constructor takes the method)
   * - ``vp::IoMaster out;`` + ``out.set_resp_meth(&r); out.set_grant_meth(&g);``
     - ``vp::IoMaster out{&g, &r};`` (constructor takes retry + resp)
   * - ``req->get_resp_port()->resp(req)``
     - ``in.resp(req)`` (reply on your own slave port)
   * - ``req->get_resp_port()->grant(req)`` — one per request
     - ``in.retry()`` — one call, no request, when ready
   * - ``req->arg_alloc / arg_get / arg_push``
     - keep state on the model (member fields, per-initiator
       table, or ``req->parent`` / ``req->initiator``)
   * - ``req->inc_latency(n)`` + return ``IO_REQ_OK``
     - either ``req->inc_latency(n)`` + ``IO_REQ_DONE``, or
       schedule ``in.resp(req)`` ``n`` cycles ahead and return
       ``IO_REQ_GRANTED``
   * - ``out.req_forward(req)``
     - ``out.req(req)`` (no equivalent; if you need transparent
       passthrough, remember the request and forward the response
       back in your own ``resp`` callback)

The two protocols are wire-incompatible — a v1 master can't bind
to a v2 slave or vice-versa — but a single simulation can include
both as long as ports only bind same-version-to-same-version.

See also
--------

- The header itself: ``gvsoc/engine/engine/include/vp/itf/io_v2.hpp``
  (the burst-conventions section at the top of the file is the
  authoritative source for the on-the-wire contract).
- The framework-inserted beat-response adapter:
  ``gvsoc/core/models/utils/io_v2_beat_adapter.{hpp,cpp}``
  (standalone ``vp::Component`` named ``utils.io_v2_beat_adapter``;
  auto-inserted by the binding pass on any io_v2 path whose master
  declares ``signature=IoV2Beat(...)`` and whose slave is
  ``IoV2BigPacket`` or the legacy ``'io_v2'`` string).
- A worked end-to-end example using the v2 protocol with all
  three response forms: :doc:`../components/ips/pulp/idma_v2`.
