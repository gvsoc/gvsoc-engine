Memory-mapped IO request (v2)
=============================

The v2 IO protocol — declared in
``gvsoc/engine/engine/include/vp/itf/io_v2.hpp`` — is the recommended
interface for new memory-mapped models. It carries reads, writes,
atomics, and bursted accesses between an initiator (a master) and a
target (a slave). Each ``vp::IoSlave`` port is bound to **exactly
one** ``vp::IoMaster``; for fan-in, use one slave port per master
plus a muxed slave declaration (see `Multiplexed ports`_).

The protocol replaces v1 (:doc:`io_v1`) and changes five things:

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
- **The response path has its own back-pressure**, symmetric to the
  request-path handshake (the AXI ``R``/``RREADY`` analogue). A
  consumer's ``resp()`` callback can return ``IO_RESP_DENIED`` to
  refuse a beat for the current cycle; the producer holds the beat and
  re-sends it when the consumer signals readiness with
  ``resp_retry()``. This lets a beat consumer throttle a response
  stream (e.g. a router arbitrating many inputs down to one beat per
  cycle) — see `Response-path back-pressure`_.
- **Bursts are explicit.** ``is_first`` / ``is_last`` / ``burst_id``
  fields on every ``IoReq`` make multi-beat semantics first-class
  on the wire, with three valid response forms (sync whole-response,
  async whole-response, beat stream).

At a glance, the wiring between an initiator and a target looks like
this:

.. code-block:: text

      Master component                          Slave component
      ┌────────────────────┐                ┌────────────────────┐
      │  vp::IoMaster out  │──── binding ──►│  vp::IoSlave in    │
      └────────────────────┘                └────────────────────┘

      request path
        out.req(req)      ─────────────────►  req() handler (slave logic)
        in.retry()        ◄─────────────────  "request input ready again"

      response path
        in.resp(req)      ◄─────────────────  async response / one beat
        out.resp_retry()  ─────────────────►  "response input ready again"

The master submits work with ``out.req(req)``; the slave processes
the request and either replies inline (``IO_REQ_DONE``), accepts
ownership and replies asynchronously later (``IO_REQ_GRANTED`` +
``in.resp(req)``), or refuses for now (``IO_REQ_DENIED`` +
``in.retry()`` when ready). The response path is symmetric: the slave
delivers each response with ``in.resp(req)``, the master's consumer may
refuse a beat this cycle (``IO_RESP_DENIED``), and it signals
``out.resp_retry()`` when it can accept again — the ``in.*`` calls run
slave→master, the ``out.*`` calls master→slave.

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
     - Pointer to the payload buffer. The master allocates the
       buffer; the slave writes into it (read) or reads from it
       (write). On the beat plane, write-beat buffer lifetime
       follows beat ownership — see `Write-beat ownership`_ below.
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
     - Head latency: cycle delay annotated by routers / targets that
       model timing inline. **Additive** across series hops
       (``inc_latency``). See `Latency annotation`_.
   * - ``duration``
     - ``int64_t``
     - Bandwidth occupancy (e.g. ``ceil(size / bandwidth)``), kept
       separate from ``latency`` because it combines with **max**, not
       sum (``set_duration``): series resources that stream the same
       packet overlap, so the end-to-end transfer time is the
       bottleneck, not the total. See `Latency annotation`_.
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

Ownership / mutation rules. These are the parts common to **every**
sub-protocol; what a ``resp()`` actually returns (the same object, or
distinct pooled beats plus a per-burst ack) is plane-specific — see
`Protocols and their rules`_ and `Write-beat ownership`_ below.

- The **master owns** the request until ``req()`` returns
  ``IO_REQ_GRANTED``.
- While ``GRANTED``, the **slave owns** the request; ownership returns to
  the master when the slave responds (``in.resp(req)``). On the beat
  plane a *write beat* is the exception: ``GRANTED`` is terminal — the
  target consumes and frees it, and it never comes back (see
  `Write-beat ownership`_).
- On ``IO_REQ_DONE``, ownership never leaves the master — the
  slave only mutates the object in-place during the call.
- On ``IO_REQ_DENIED``, ownership stays with the master; the slave
  has not touched the request.

A master reusing a request between sends should call
``req->prepare()`` to reset per-send fields (currently
``latency = 0``, ``duration = 0`` and ``status = IO_RESP_OK``).

Ports
-----

Master ports
~~~~~~~~~~~~

A master port (``vp::IoMaster``) is bound to one slave port. The
constructor takes the master's response and retry callbacks (set at
construction time — there are no ``set_*_meth`` setters):

.. code-block:: cpp

    static void        retry(vp::Block *__this);
    static vp::IoRespAck resp(vp::Block *__this, vp::IoReq *req);

    vp::IoMaster out{ &MyComp::retry, &MyComp::resp };
    new_master_port("output", &out, this);   // bind via vp::Component

The ``resp`` callback returns :cpp:enum:`vp::IoRespAck`
(``IO_RESP_ACCEPTED`` / ``IO_RESP_DENIED``) — see `Response-path
back-pressure`_. A consumer that always accepts just
``return vp::IO_RESP_ACCEPTED;``.

The muxed form lets one master serve several slaves via per-call
dispatch:

.. code-block:: cpp

    static void        retry_muxed(vp::Block *__this, int id);
    static vp::IoRespAck resp_muxed(vp::Block *__this, vp::IoReq *req, int id);

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

Per-channel retry
~~~~~~~~~~~~~~~~~~

``retry()`` takes an :cpp:enum:`vp::IoRetryChannel` argument —
``IO_RETRY_READ``, ``IO_RETRY_WRITE`` or ``IO_RETRY_ANY`` (the
default). It lets a master that splits traffic into independent
read and write channels (AXI ``AR``/``R`` vs ``AW``/``W``) be
back-pressured on one channel while the other keeps flowing: a
denied write stalls only the write channel, and the slave names
that channel when it becomes ready again. ``READ``/``WRITE`` map to
the request direction (``is_write``), so a master can index its
per-channel state directly.

A channel-agnostic slave leaves the argument at ``IO_RETRY_ANY``
(``in.retry()``), which means "every stalled channel is ready" —
the master re-sends all held requests. A single-channel master
ignores the value. Today every slave emits ``IO_RETRY_ANY``;
``READ``/``WRITE`` are available for slaves that model independent
read/write readiness. ``interco/router_v2`` (the ``beat`` flavour)
is the reference consumer: it tracks one stall per (output,
channel) and re-issues only the channels a retry covers.

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

Response-path back-pressure
---------------------------

The request path has flow control (``req()`` returns ``DENIED`` and
the slave later signals ``retry()``); the response path has the
symmetric mechanism, the AXI ``R``/``RREADY`` analogue. The producer
of a response (a slave, an adapter, a router forwarding upstream)
replies with ``in.resp(req)``. The **consumer's** ``resp`` callback
**returns** an :cpp:enum:`vp::IoRespAck`: ``IO_RESP_ACCEPTED`` to take
the beat, or ``IO_RESP_DENIED`` to refuse it for the current cycle.
``in.resp(req)`` propagates that value to the producer; on
``IO_RESP_DENIED`` the producer must hold the beat unchanged and
re-send it when the consumer later calls ``out.resp_retry()``.

``IoRespAck`` is the response-path analogue of
:cpp:enum:`vp::IoReqStatus` (a response is terminal, so it has only the
two outcomes, no ``GRANTED``). It is kept separate from
``IoRespStatus`` — the ``OK`` / ``INVALID`` payload status on
``req->status`` — which is an orthogonal axis a beat carries regardless
of whether it was accepted.

This is a real return value on the callback, symmetric with ``req()``
(whose callback returns ``IoReqStatus``): the ``resp`` callback
signature is ``vp::IoRespAck NAME(vp::Block *, vp::IoReq *)`` (muxed:
``..., int id``). A consumer that always accepts simply
``return vp::IO_RESP_ACCEPTED;`` and no producer ever needs a
``resp_retry`` handler — the whole mechanism is free when unused. A
forwarding mid-box propagates a downstream consumer's verdict by
returning the inner ``resp()`` result directly
(``return in.resp(req);``).

``resp_retry()`` is the response-direction mirror of ``retry()``:

- ``retry()`` — slave method, fires the **master's** retry callback,
  meaning "my request input is ready again".
- ``resp_retry()`` — master method, fires the **slave's**
  ``resp_retry`` callback, meaning "my response input is ready again".

It carries the same :cpp:enum:`vp::IoRetryChannel` and obeys the same
**synchronous re-send** rule: the producer re-submits its held beat
inside the ``resp_retry`` callback, same cycle, never deferred.

A producer that can be denied registers a ``resp_retry`` handler as
the optional second argument of the ``IoSlave`` constructor
(``vp::IoSlave in{ &MyComp::req, &MyComp::resp_retry }``); it defaults
to ``nullptr`` for the common always-accepted case.

.. code-block:: cpp

    // Consumer side — the resp callback returns its accept/deny verdict.
    vp::IoRespAck MyComp::resp(vp::Block *__this, vp::IoReq *req)
    {
        auto *_this = (MyComp *)__this;
        if (!_this->resp_channel_free_this_cycle())
        {
            _this->owe_resp_retry = true;
            return vp::IO_RESP_DENIED;        // hold off — producer keeps the beat
        }
        _this->accept(req);
        return vp::IO_RESP_ACCEPTED;
    }

    // ...later, when the consumer's response channel frees:
    if (this->owe_resp_retry) { this->owe_resp_retry = false; this->out.resp_retry(); }

    // Producer side — hold the denied beat and re-send synchronously.
    if (this->in.resp(beat) == vp::IO_RESP_DENIED)
    {
        this->held_beat = beat;       // keep it unchanged
    }
    static void resp_retry(vp::Block *__this, vp::IoRetryChannel)
    {
        auto *_this = (MyComp *)__this;
        if (_this->held_beat && _this->in.resp(_this->held_beat) != vp::IO_RESP_DENIED)
            _this->held_beat = nullptr;
    }

``interco/router_v2`` (the ``beat`` flavour) is the reference
consumer: it arbitrates each input's response channel to **one beat
per cycle**, back-pressuring the losing output (its downstream
``io_v2_beat_adapter`` holds the beat) instead of forwarding more than
one response beat to a master in the same cycle. The general and
sync beat adapters are the reference producers.

Protocols and their rules
-------------------------

A port never speaks "io_v2" in the abstract: it commits to one of three
**sub-protocols**, declared by its Python signature (see `Signatures and
the adapter framework`_ for how they are declared and auto-bridged). The
flows shown so far — ``req()`` / ``resp()`` and the deny / back-pressure
handshakes — are common to all three. What differs per protocol — who
owns a request after ``GRANTED``, what a ``resp()`` delivers, who
allocates and frees the objects, how timing is annotated — is collected in
the three sub-sections below. At a glance:

.. list-table:: Rules that depend on the plane
   :header-rows: 1
   :widths: 24 38 38

   * - Rule
     - Non-beat plane (Sync / SingleReq)
     - Beat plane (``IoV2Beat``)
   * - Ownership after ``GRANTED``
     - The slave owns the request until it calls ``in.resp(req)`` with
       **that same object**; ownership then returns to the master.
     - Reads / atomics: the master's request round-trips (its read *data*,
       though, arrives in separate beats). A **write beat**: ``GRANTED``
       is terminal — the target consumes and **frees** the beat, which
       never comes back. See `Write-beat ownership`_.
   * - What ``resp()`` hands back
     - The master's own request object, filled in place.
     - Reads: *N* distinct allocator-backed beat objects correlated by
       ``req->initiator`` (the master copies each out and frees it).
       Writes: one data-less burst ack — or an inline ``DONE`` on the
       last beat.
   * - Allocation / freeing
     - The master owns its request statically; no pools, no ``free()``.
     - Read beats, write beats and acks come from a shared
       ``IoReqAllocator`` and are released with ``req->free()``. See
       `Pooled request allocation (IoReqAllocator)`_.
   * - Write acknowledgement
     - One ``resp()`` (or inline ``DONE``) per ``req()``.
     - One ack per **burst**: non-last beats are consumed+freed silently;
       the last beat answers inline ``DONE`` or a single data-less ack.
       See `Write burst shapes`_.
   * - Response forms a master must tolerate
     - ``IoV2Sync``: inline ``DONE`` only. ``IoV2SingleReq``: one
       single-beat response (inline / async / deny).
     - One ``resp()`` per beat (the beat adapters normalise any slave form
       into this).

The three sub-protocols are detailed in turn below. ``IoV2Sync`` and
``IoV2SingleReq`` share the same *non-beat* lifecycle — the master's own
request round-trips and is statically owned — which is why the table above
groups them in one column against the beat plane. When two bound ports
declare different sub-protocols, the framework auto-inserts an adapter
between them (see `Signatures and the adapter framework`_).

IoV2Sync
~~~~~~~~

Always-inline. The slave answers every ``req()`` with ``IO_REQ_DONE``
*inside* the call — it never uses ``resp()`` / ``retry()`` — and the reply
rides back on the master's own request object. A master on this protocol
can therefore drop all async bookkeeping: nothing is pooled, and nobody
but the master ever owns or frees the request. The inline round-trip is
the `Synchronous flow`_ shown earlier; ``memory_v3`` is the canonical sync
slave. Because the reply is inline, timing is annotated on the request
itself rather than deferred to a later ``resp()``:

Latency annotation
^^^^^^^^^^^^^^^^^^

The timing cost of an inline transaction is carried in **two**
fields, combined differently along the path from slave to master:

- ``req->latency`` — **head latency** (pipeline / contention delay).
  Components add to it with ``inc_latency(n)``, so it is **additive**
  across series hops: each hop contributes its own.
- ``req->duration`` — **bandwidth occupancy** (the time a resource is
  busy streaming the packet, typically ``ceil(size / bandwidth)``).
  Components set it with ``set_duration(n)``, which keeps the **max**,
  not the sum. Two bandwidth routers in series each occupy their link
  for ``duration`` cycles, but those occupations *overlap* (the bytes
  pipeline through both), so the end-to-end transfer time is the
  bottleneck — the max — not the total.

A master reads the whole transaction cost via
``req->get_full_latency()`` (``= latency + duration``) and stalls its
pipeline accordingly (the beat adapters, for instance, spread
beats so the last one lands at ``now + get_full_latency()``).
``duration`` defaults to 0, so a request that never traverses a
bandwidth-limited resource has ``get_full_latency() == get_latency()``
— consumers that read only ``get_latency()`` keep working on those
paths, but **any consumer downstream of a bandwidth router must read
``get_full_latency()``** or it loses the transfer time. The reference
bandwidth model is ``router_v2_bandwidth`` (``inc_latency(head)`` +
``set_duration(burst)``); the beat adapters consume it
with ``get_full_latency()``.

For models that already use wall-clock scheduling (a
``ClockEvent``-based deferred ``resp()``) the field can stay at 0
— the delay is already baked into the cycle at which ``resp()``
fires.

This inline annotation is a **non-beat mechanism** — it belongs to
``IoV2Sync`` and ``IoV2SingleReq``. On the beat plane (`IoV2Beat`_)
timing is carried entirely by *when* each beat's ``resp()`` fires, not by
these fields: a beat component never calls ``inc_latency`` /
``set_duration`` on the beats it emits, so ``latency`` and ``duration``
stay 0 on a pure beat path. The beat adapters do *read*
``get_full_latency()``, but only to translate a value a non-beat
bandwidth slave annotated on the far side into the correct beat pacing.

A master reusing the same request object across submissions should
call ``req->prepare()`` (or explicitly reset ``latency``) between
sends.


IoV2SingleReq
~~~~~~~~~~~~~

Single-word, identity-routed. One access yields exactly one **single-beat**
response, in any of three shapes — inline ``IO_REQ_DONE``; async
``IO_REQ_GRANTED`` + one ``resp()``; or ``IO_REQ_DENIED`` + ``retry()`` —
always delivered on the **master's own request object** (it round-trips by
identity). Components that correlate a response to its request by object
identity — functional / bandwidth routers (``in_flight_map[req]``),
width / opcode splitters (``req->parent``) — use this, which is exactly why
they cannot consume a multi-beat stream. Like ``IoV2Sync`` the request is
statically owned with no pools, and the inline `Latency annotation`_ above
applies unchanged. An optional ``width`` bounds the access granule for a
bank-interleaved fabric (see `Signatures and the adapter framework`_).

IoV2Beat
~~~~~~~~

Beat-streamed. A transaction is streamed one beat per cycle; read responses
and write beats are **distinct pooled objects** that cross a
*consumer-frees* boundary, writes are acknowledged once per burst (AXI
B-channel style), and every such object comes from an ``IoReqAllocator``
pool and is released with ``req->free()``. The wire shapes, the write-beat
ownership contract and the pooled allocator are detailed below.

The beat plane does **not** use the inline ``latency`` / ``duration``
annotation of the non-beat protocols (see `Latency annotation`_): beat
timing *is* the pacing of ``resp()`` — at most one beat per cycle at the
beat width — so beat components never set those fields. ``latency`` and
``duration`` stay 0 on a pure beat path; a non-zero ``get_full_latency()``
on a beat only ever comes through an adapter from a non-beat bandwidth
slave, which the adapter converts back into pacing.

Burst protocol
^^^^^^^^^^^^^^

Three fields make multi-beat transfers explicit:

- ``is_first`` / ``is_last`` — mark the position of a beat inside
  a burst. For a non-burst single-beat request both flags are
  ``true``.
- ``burst_id`` — correlator shared by every beat of one burst.
  ``-1`` means "not part of a burst".

Read burst shape (AXI-asymmetric)
"""""""""""""""""""""""""""""""""

A read burst is always submitted as **exactly one** ``req()`` with
``size = total_burst_bytes``, ``is_first = is_last = true``,
``burst_id = <ID or -1>`` and — on the beat protocol — **no data**
(``data == NULL``): the read data comes back inside the response
beats' own payloads (see `Pooled request allocation
(IoReqAllocator)`_). The slave decides how to respond — see the
three forms below.

The three response forms
""""""""""""""""""""""""

A read's response shape is **not** a per-request choice — it is fixed by
the slave's signature. There are three possible shapes, and a beat
master's auto-inserted adapter normalises whichever one its slave produces
into a uniform per-beat stream:

1. **Sync, whole response.** Fill ``req->data`` in place, set
   ``req->status``, return ``IO_REQ_DONE``. No later ``resp()``.

2. **Async, whole response.** Return ``IO_REQ_GRANTED``, then call
   ``in.resp(req)`` **once**, later, with the whole payload
   (``is_first = is_last = true``, ``size`` unchanged).

3. **Beat stream.** Return ``IO_REQ_GRANTED``, then call ``in.resp(...)``
   ``N`` times, once per beat, with ``data`` / ``size`` / ``is_first`` /
   ``is_last`` set per beat. Cumulative response sizes must equal the
   request's ``size``; the final beat carries ``is_last = true`` and the
   burst's final ``IO_RESP_OK`` / ``IO_RESP_INVALID``.

   Under the **initiator-owned request convention** (which all in-tree
   io_v2 models and adapters follow) a read MUST deliver each beat as a
   **distinct** allocator-backed response object (see `Pooled request
   allocation (IoReqAllocator)`_) — never the request reused — whose
   co-allocated payload carries the beat's data; the read burst request
   itself is data-less. The initiator keeps owning its request (and frees
   it itself on the last response), copies each beat's payload out and
   frees the beat with ``req->free()``. Correlation back to per-request
   state is via ``req->initiator``, copied onto every beat, not via
   object identity.

A beat master tolerates all three because its adapter absorbs whichever
shape the slave's signature dictates — a sync slave answers with form 1, a
beat slave with form 3, and a single-req slave is sub-read into single-beat
responses. Diagrammatically, the three shapes look like this:

.. code-block:: text

   Form 1 — sync, whole response:

      master                                          slave
        │                                               │
        │── req(size=N, is_first=is_last=true) ────────►│
        │                                               │ ◄ fills full N bytes
        │◄────────────── IO_REQ_DONE ───────────────────│
        │   (whole response delivered)                  │
        ▼                                               ▼


   Form 2 — async, whole response:

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
shape its slave uses just declares its outbound binding with
``signature=IoV2Beat(beat_width=W)``. The framework auto-inserts the
matching beat adapter (see `Signatures and the adapter framework`_); the
master sees one normal ``resp()`` per beat with
``size``/``data``/``is_first``/``is_last``/``burst_id``/``status`` mutated
per beat.

Write burst shapes
""""""""""""""""""

Writes are master-driven (the master picks the wire shape) and
come in two forms:

- **Beat-form** — N ``req()`` calls, each with one beat-sized
  payload plus per-beat ``is_first`` / ``is_last`` / ``burst_id``.
  Useful when the master models per-beat write timing (typical AXI
  W channel).
- **Whole-burst** — one ``req()`` carrying the full burst data,
  ``is_first = is_last = true``. The slave consumes it atomically.
  On a beat binding this is simply a one-beat burst. Useful when
  the master doesn't need per-beat write semantics.

There is **no separate address / setup request** for a write. Unlike a
read — which sends one data-less ``req(size=N)`` up front and receives the
data as response beats — a beat-form write streams the data on the
*request* path: the first ``req()`` is already beat 0, carrying data with
``is_first = true``. The only thing coming back is the single burst ack.
This is the read/write asymmetry the `Read burst shape (AXI-asymmetric)`_
heading flags.

On the beat plane a write burst is acknowledged **once per burst**,
AXI B-channel style (on non-beat bindings the slave still responds
per ``req()``):

- a **non-last** beat is only flow-controlled: ``GRANTED`` = the
  target took ownership, consumes and **frees** the beat, and never
  calls ``resp()`` for it; ``DENIED`` = per-beat back-pressure
  (the WREADY analogue), master re-sends from ``retry()``.
- the **last** beat produces the burst ack: inline ``IO_REQ_DONE``
  with the final status and latency/duration on the beat (master
  keeps its object — the fast path), or ``GRANTED`` (the target
  consumes + frees this beat too) followed by exactly one
  ``resp()`` carrying a **distinct data-less allocator-backed ack**
  with the burst's final status. The initiator frees the ack
  (``ack->free()``).
- error escape hatch: a target may reject a malformed beat at any
  position inline with ``IO_REQ_DONE`` + ``IO_RESP_INVALID``; the
  master keeps that beat and treats the burst as aborted.

.. code-block:: text

   Beat-form write:

      master                                          slave
        │                                               │
        │── req(size=w, is_first=t, is_last=f) ────────►│  beat 0
        │◄── IO_REQ_GRANTED (consumed+freed, no resp) ──│
        │── req(size=w, is_first=f, is_last=f) ────────►│  beat 1
        │◄── IO_REQ_GRANTED (consumed+freed, no resp) ──│
        │               . . .                           │
        │── req(size=w, is_first=f, is_last=t) ────────►│  beat N/w−1
        │◄── IO_REQ_DONE (inline burst ack)             │
        │    or IO_REQ_GRANTED ... resp(ack) ───────────│
        │    (one data-less ack for the whole burst)    │
        ▼                                               ▼


   Whole-burst write (a one-beat burst on beat bindings):

      master                                          slave
        │                                               │
        │── req(size=N, is_first=is_last=true) ────────►│
        │   (whole payload in one shot)                 │
        │◄── IO_REQ_DONE / GRANTED → resp(ack) ─────────│
        ▼                                               ▼

The cumulative-size invariant
"""""""""""""""""""""""""""""

For **reads** (and atomics), across every response that a single
submitted ``req()`` produces (whether one or N), the **sum** of
``response.size`` must equal the request's ``size``, in cumulative
byte order, with no gaps or overlaps. The final response carries
``is_last = true`` and the final status. Masters use this invariant
to retire the request exactly once (free buffer + free ``IoReq``
only on ``is_last == true``).

For **writes on the beat plane** the invariant is per burst, on the
request path: the beats' sizes sum to the burst size, and the burst
produces exactly one terminal acknowledgement — inline ``DONE`` on
the last beat or one data-less ack ``resp()``. The ``size`` carried
by the ack is informational only; masters retire the burst on the
ack (or on the inline ``DONE`` of the last beat).

Slave skeletons for the three response forms
""""""""""""""""""""""""""""""""""""""""""""

**Sync, whole response** (matches the simple synchronous flow above —
just fill the whole buffer and return ``IO_REQ_DONE``).

**Async, whole response** — same as the async flow above, just with
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
""""""""""""""""""""

A master must tolerate all three forms. The typical pattern: on
each response, accumulate the bytes against an expected total and
free the request only when ``is_last`` fires (which the slave is
required to set on the cumulative-final response):

.. code-block:: cpp

    static vp::IoRespAck resp(vp::Block *__this, vp::IoReq *req)
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
        return vp::IO_RESP_ACCEPTED;   // this master never back-pressures
    }

If the master *needs* a per-beat callback stream (e.g. a beat-rate
DMA back-end that paces a downstream pipeline at one chunk per
cycle), it should not implement this consumer logic by hand —
declare the outbound binding with
``signature=IoV2Beat(beat_width=W)`` and the framework auto-inserts the
matching beat adapter (see `Signatures and the adapter framework`_), which
normalises whatever response form the slave produced into a uniform
per-beat ``resp()`` at cycle-accurate spacing.


.. _Write-beat ownership:

Write-beat ownership (beat plane)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

On bindings whose slave side speaks the beat protocol, write-beat
ownership travels with the beat — **buffer included**:

- ``GRANTED`` transfers ownership to the callee; the caller must not
  touch the beat afterwards (snapshot any field you still need —
  size, ``is_last``, addr — *before* calling ``req()``).
- The buffer behind ``data`` is valid exactly as long as the beat is
  alive (not freed). Once the initiator has passed a write beat it
  can assume nothing about the buffer anymore — it must not touch,
  modify or reuse it, and may only reclaim it after the burst's ack
  (the ack implies every beat of the burst has been consumed and
  freed; a target must not issue the ack before that).
- The current owner keeps the buffer usable simply by holding the
  unfreed beat, and is free to pass the beat on to a forwarder to
  keep the buffer alive without reallocation or copy. A consumer
  must **not** free a write beat while it (or anything it handed
  out) still references the payload — freeing the beat is what
  releases the buffer.
- A mid-chain forwarder (beat router, clock bridge) passes the SAME
  object downstream; the single component that terminates the beat
  frees it once fully done with the payload. The burst's ack flows
  back through the chain as a normal ``resp()``, correlated by
  ``burst_id``/``initiator`` like a read beat.
- **Atomics are exempt**: opcodes other than ``WRITE`` carry
  response data, so they keep the classic round-trip — the
  initiator's own request comes back via ``resp()`` and nobody else
  frees it. Targets must key the write-beat rules on
  ``opcode == vp::WRITE``, not ``get_is_write()`` (true for every
  non-READ opcode).


Pooled request allocation (IoReqAllocator)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

On the beat protocol, requests and response beats routinely cross a
boundary where the **consumer frees an object the producer allocated**
(the terminal master frees the read beats an adapter produced, the
target frees the write beats a master produced, the initiator frees
the write ack a target produced), so no component can pool privately —
and a bare ``new`` / ``delete`` per beat dominates the model cost on
beat-heavy paths. ``vp::IoReqAllocator`` (``io_v2.hpp``) replaces it:

- **One shared allocator per payload size.** ``IoReqAllocator::get(size)``
  returns the process-wide pool for that size, creating it on first use.
  A component fetches its allocator(s) once at construction — its beat
  width is static. Payload size is always a **beat width** (a beat carries
  at most one beat of data, never a whole burst); size ``0`` serves
  data-less requests (read burst requests, write beats, write acks).
- **Request + payload in one block.** ``alloc()`` pops the freelist or
  lazily creates a request whose payload is co-allocated behind it. The
  data-pointer discipline is per pool kind: for **payload pools**
  (size > 0) ``req->data`` is set once to the co-allocated payload and
  **must never be repointed**; for the **size-0 pool** ``req->data`` is
  **caller-managed** — set it on every allocation (``NULL`` for
  data-less requests, the source-buffer pointer for write beats);
  ``free()`` never touches it. ``alloc()`` reinitializes no other field —
  callers set ``addr`` / ``size`` / ``opcode`` / ``is_first`` / ``is_last``
  / ``burst_id`` themselves, exactly as after a bare ``new``.
- **Free through the back-pointer.** Every allocator-provided request
  carries its home pool; any component frees it with ``req->free()``,
  regardless of who allocated it.

The rules that come with it (also stated in the header):

- **Read burst requests carry no data** (``data == NULL``). The producer
  of the response allocates each beat from its allocator; the beat's
  co-allocated payload holds the read data (the producer points its own
  downstream, non-beat read request at that payload so the target fills
  it directly, without an extra copy). The **terminal master copies each
  beat's payload into its destination buffer, then frees the beat** — and
  frees its own (data-less) burst request on the last one
  (initiator-owned convention; the burst request is never round-tripped
  as a read beat).
- **Write beats come from the size-0 pool** with ``data`` pointing into
  the initiator's source buffer — no per-beat payload copy. The target
  that consumes a beat frees it; buffer lifetime follows beat ownership
  (see `Write-beat ownership`_). A component that repacks write data
  anyway (e.g. the beat width adapter) may instead draw its downstream
  beats from a payload pool and copy. The burst's **ack** is a size-0
  pool object too (a producer may recycle the burst's consumed size-0
  last beat as the ack); the initiator frees it.
- **Downstream requests to non-beat protocols** (sync / single-req) are
  never freed by the target; components keep reusing their own request
  objects there and may repoint their ``data`` (e.g. at a response beat's
  payload).
- **Never bare ``new`` / ``delete``** for anything that crosses a
  consumer-frees boundary — always ``IoReqAllocator`` + ``req->free()``.

Non-beat bindings are untouched: their requests stay statically allocated
on their master, carry data both directions, and round-trip. (A
whole-burst *write* submitted on a beat binding is a one-beat burst and
follows the write-beat pool rules above.)


Signatures and the adapter framework
------------------------------------

Every binding carries a **signature** on each side declaring which slice
of the protocol that port speaks. When the two sides differ, the binding
pass asks the *master* signature's ``bridge_to()`` to insert an adapter;
the slave side never triggers insertion. Three signatures exist
(``gvsoc/engine/python/gvsoc/signature.py``):

.. note::

   Every port must declare a **concrete** sub-protocol (``IoV2Sync`` /
   ``IoV2SingleReq`` / ``IoV2Beat``); there is deliberately **no**
   transparent / "any" io_v2 signature. The bridge pass compares
   signatures hop by hop (never end to end), so a transparent port would
   hide the real endpoints' protocols from its neighbours and let a wrong
   or missing adapter through silently — protocol-transparent forwarders
   (remapper, clock bridge) instead take a concrete signature *per
   instance*, committing each to the plane it sits on.

.. list-table::
   :header-rows: 1
   :widths: 22 50 28

   * - Signature
     - Contract / when to use
     - Bridge inserted
   * - ``IoV2Sync``
     - Strict inline sub-protocol: the slave **always** returns
       ``IO_REQ_DONE`` and never calls ``resp()`` / ``retry()``, letting a
       master drop all async bookkeeping. Binds **only** to another
       ``IoV2Sync`` — anything else is a build-time error (no adapter can
       synthesise an inline response from an async slave).
     - none (a mismatch is a design error, raised at build)
   * - ``IoV2SingleReq(width=0)``
     - Single-word / single-beat accesses only (the HW lint
       ``req`` / ``gnt`` / ``r_valid`` fabric). Used by components that
       route a response by **request identity** — functional and bandwidth
       routers (``in_flight_map[req]``) and width/opcode splitters
       (``req->parent``) — which therefore cannot handle a multi-beat
       stream. Allows async + deny, never a multi-beat response.
       ``width`` (power of two, optional) bounds the access granule: on a
       slave it declares the largest aligned granule one access may cover
       (a bank-interleaved fabric); on a master, the widest access it
       issues. ``0`` = don't care: a width-0 slave accepts anything
       directly.
     - vs a beat slave → ``IoV2SingleReqToBeatAdapter``; vs a
       width-declaring single-req slave, when the master does not
       provably fit its granule (wider, or width-0/unknown) →
       ``IoV2SingleReqWidthAdapter``
   * - ``IoV2Beat(beat_width)``
     - Cycle-accurate consumer that wants **one** ``resp()`` per beat
       regardless of the slave's form. A same-width beat peer binds
       directly; a beat peer with a *different* width gets the width
       adapter (the wider width must be an integer multiple of the
       narrower one).
     - vs an ``IoV2Sync`` slave → ``IoV2BeatToSyncAdapter``; vs an
       ``IoV2SingleReq`` slave → ``IoV2BeatToSingleReqAdapter``; vs an
       ``IoV2Beat`` slave of a different width →
       ``IoV2BeatWidthAdapter``

The adapters below are auto-inserted from these rules. Start with the
beat adapter, which the cycle-accurate masters (``router_v2_beat``, the
iDMA) rely on.

Which adapter is inserted (master × slave)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The binding pass looks only at the two signatures and inserts whatever the
**master** signature's ``bridge_to()`` returns; the slave side never
triggers insertion. For the three concrete sub-protocols the full matrix is
— rows are the master signature, columns the slave signature:

.. list-table::
   :header-rows: 1
   :stub-columns: 1
   :widths: 18 27 27 28

   * - master ↓ / slave →
     - ``IoV2Sync``
     - ``IoV2SingleReq``
     - ``IoV2Beat``
   * - ``IoV2Sync``
     - direct
     - build error (a)
     - build error (a)
   * - ``IoV2SingleReq``
     - direct
     - direct (b)
     - ``IoV2SingleReqToBeatAdapter``
   * - ``IoV2Beat``
     - ``IoV2BeatToSyncAdapter``
     - ``IoV2BeatToSingleReqAdapter``
     - direct (c)

- **(a)** A sync master requires an always-inline slave; nothing looser can
  be synthesised, so a mismatch is a ``RuntimeError`` at build time, not an
  adapter.
- **(b)** Direct when the master provably fits the slave's declared width
  granule; otherwise ``IoV2SingleReqWidthAdapter`` is inserted — it splits a
  granule-straddling access into aligned sub-accesses, preserving the
  identity contract.
- **(c)** Direct when both beat widths are equal; otherwise
  ``IoV2BeatWidthAdapter`` (the bus up/down-sizer; the wider width must be an
  integer multiple of the narrower).

Each adapter named in the matrix is detailed in its own subsection below.

The beat-sync adapter
~~~~~~~~~~~~~~~~~~~~~~~

``IoV2BeatToSyncAdapter`` is the dedicated Beat→Sync adapter, auto-inserted
when an ``IoV2Beat`` master binds an ``IoV2Sync`` slave. Because a sync slave serves *any* size inline and always replies
``IO_REQ_DONE`` (never ``GRANTED`` / ``DENIED``), the adapter needs no
async/deny machinery at all — every downstream call completes inline. Each
**write** beat is forwarded inline to the sync slave, as its own single-beat
request (so a beat-form write of N beats makes N inline calls), the moment
the master streams it in, and consumed on the spot: non-last beats are
freed, the last is recycled as the single data-less burst ack. A **read**
burst — data-less under the beat protocol — carries no data to read at
submit, so nothing is pre-read: the adapter queues one lightweight
descriptor per beat and an FSM reads **each beat from the sync slave at its
own delivery cycle**, one beat object live at a time (no queue of pre-filled
beats is ever held). The beats are **paced by each beat's own bandwidth
occupancy**: the head beat lands at the head latency (``get_latency()``) and
each following beat ``max(1, get_duration())`` cycles after the previous, so
the transfer spreads naturally across the burst window (a beat's duration is
its bus-slot occupancy). A sync slave with no ``duration`` (e.g.
``memory_v3``) degrades to one beat per cycle. The one submit-time read is
the burst's **head beat** when the stream is idle: it is issued at submit
(the address phase) purely to sample the head latency, and its data rides
along to be delivered after that latency. The externally visible contract is the standard beat-response one:
one ``resp()`` per beat, ``is_first`` / ``is_last`` / ``burst_id`` /
``status`` per beat, and the same ownership rules (all read beats are distinct allocator-backed
objects the master copies out of and frees; the master owns and frees its
own burst request; writes are acknowledged once per burst — the adapter
consumes and frees the master's write beats and delivers one data-less
ack the master frees). Source:
``gvsoc/core/models/utils/io_v2_beat_to_sync_adapter.{hpp,cpp}``; the
``utils/io_v2_beat_to_sync_adapter`` test is a standalone exerciser.

The beat-single-req adapter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``IoV2BeatToSingleReqAdapter`` is the adapter for an ``IoV2SingleReq``
slave, auto-inserted when an ``IoV2Beat`` master binds one. Unlike a sync slave, a single-req slave is **not**
inline-only: it answers each request with a single-beat response in any of
three forms — inline ``IO_REQ_DONE``, async ``IO_REQ_GRANTED`` + one
``resp()``, or ``IO_REQ_DENIED`` + ``retry()`` — so the adapter keeps the
read sub-read pipeline (beat-sized sub-reads paced one per cycle), the
async/deny machinery and request- and response-path back-pressure.

Writes follow the per-burst ack contract: the adapter consumes the
master's write beats, forwards **each beat** as its own single-req request
downstream, and acknowledges the burst upstream exactly once (the last beat
recycled as the data-less ack) — never one whole-burst call.

Two ways it follows the HW ``axi2mem`` bridge it models:

* **On-the-fly burst generation.** An incoming read burst is pushed whole
  onto a queue; each beat-sized sub-read is generated from the front burst's
  cursor when it is issued (one per cycle) — nothing is pre-expanded, exactly
  like the HW address generator (base address + counter).
* **Bounded read bursts in flight.** ``max_read_bursts`` (config-tree field,
  default 4 — the HW r_id-FIFO depth) caps the read bursts accepted but not
  yet fully delivered. Beyond it the read request channel is back-pressured
  (``IO_REQ_DENIED``); ``in.retry()`` is raised when a burst completes and
  frees a slot. The master then re-sends the held read.

It also drops the **out-of-order
reassembly**: a single-req slave answers in request order (a valid burst
maps to one output, and the response returns on the very object sent), so
the adapter just tracks issued sub-reads in an **in-order FIFO**, pops them
as responses arrive, and forwards the **same** sub-read object straight
upstream as the beat (no searchable reorder buffer, no second per-beat
allocation). The in-order assumption is **checked** with ``traces.assert``
(asserts/debug builds): a response that is not the oldest outstanding
sub-read aborts the run rather than scrambling the beat stream. The SingleReq
single-beat-response contract (one response per forwarded request, covering
its whole size) is checked the same way.

Parameters (``beat_width``, ``max_read_bursts``) are read from the generated
config tree (the ``IoV2BeatToSingleReqAdapterConfig`` dataclass in the Python
generator → ``Component(config, cfg)`` on the C++ side), not from the raw
JSON config.

This is the calibrated path for the iDMA's ``o_AXI_READ`` / ``o_AXI_WRITE``
bound to a ``KIND_BANDWIDTH`` router (``IoV2SingleReq``); its cycle-tolerance
checkers pin the timing, which is unchanged by the in-order simplification
(the pacing and latency modelling are untouched). Source:
``gvsoc/core/models/utils/io_v2_beat_to_single_req_adapter.{hpp,cpp}``; the
``utils/io_v2_beat_to_single_req_adapter`` test is a standalone exerciser
covering inline / async / request-deny / response-back-pressure, the
read-burst limit, and a negative out-of-order case that trips the in-order
assert.

The single-req-to-beat adapter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``IoV2SingleReqToBeatAdapter`` is the adapter for an ``IoV2SingleReq``
master bound to a beat slave — the boundary the ISS LSU crosses onto a
``KIND_BEAT`` plane. A single-req
master carries its own per-request state and correlates the response by
object identity, so the adapter's only substantive job is translating the
two **allocation conventions**: downstream read data arrives in distinct
allocator-backed beats (copied into the master's buffer and freed,
``req->free()``), while the upstream response hands the master back its
**own** request object. Atomics are forwarded as the master's own object
(they keep ``data`` / ``second_data`` without copying and legitimately
round-trip). A pure **write** is forwarded as a size-0 pool beat aliasing
the master's payload — the beat target consumes and frees it, and the
adapter translates the burst ack (inline ``DONE`` or the distinct
data-less ack, which it frees) back into the master's own-object reply.
Each read's downstream data-less request is embedded in a pooled
per-access context.

It is **not** single-outstanding: because each read has its own
context (correlated through the beats' ``initiator``), any number of
accesses can be in flight concurrently — a pipelined LSU
(``nb_outstanding > 1``) is no longer stalled at the boundary, matching
the HW ``req``/``gnt``/``r_valid`` fabric it models. Flow control is
completely stateless: a downstream ``DENIED`` propagates straight
upstream (the master holds its own request, per the single-req contract)
and ``retry()`` is a pure pass-through. Zero added latency, no clock
events; inline ``DONE``\ s (writes, zero-size or error reads) are relayed
inline with their timing annotations untouched.

The single-beat response expectation is **checked** with ``traces.assert``
(asserts builds) for accesses that fit in one downstream beat; an access
wider than the beat plane is reassembled through a fill cursor.
Parameters (``beat_width``) come from the
generated config tree (``IoV2SingleReqToBeatAdapterConfig``). Source:
``gvsoc/core/models/utils/io_v2_single_req_to_beat_adapter.cpp``; the
``utils/io_v2_single_req_to_beat_adapter`` test is a standalone exerciser
whose ``pipelined`` cases prove several accesses stay outstanding
concurrently with no back-pressure.

The single-req width adapter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``IoV2SingleReqWidthAdapter`` enforces a single-req slave's declared
width granule. The motivating case is a bank-interleaved fabric — the
GAP9 shared-L2 ``LogIco`` with a 4-byte interleave — which decodes the
bank from the address and forwards the **whole** request there: an
access crossing an aligned granule boundary would land in one bank and,
because bank-local addresses are compressed, silently **alias a
different global address**. Historically the guarantee was purely by
construction (hand-wired lane splitters, masters known to be narrow);
declaring ``IoV2SingleReq(width=granule)`` on the fabric input makes it
a build-time property instead: any single-req master that does not
provably fit (wider declared width, or the common width-0/unknown one)
gets this adapter auto-inserted.

The adapter is nearly free: an access that fits one aligned granule
(``(addr % width) + size <= width``) passes straight through — the
master's own object forwarded, all three outcomes mapped 1:1, any
number outstanding, no state touched. Only a straddling access engages
the split machinery: it is chopped into granule-aligned sub-accesses
(no copy — sub-requests point into the master's buffer) issued
sequentially through one embedded, reused sub-request, and the master
still gets its own request back — inline ``IO_REQ_DONE`` when every
chunk completed inline (latency folded as ``max(chunk_index +
chunk_latency)``, a back-to-back issue sequence), or one ``resp()``
when the last chunk lands. Atomics are never split (``traces.assert``);
one split is active at a time (a second is DENIED and retried on
completion; fitting accesses are never blocked). A DENY of the *first*
chunk aborts the split statelessly — the master re-sends on the
forwarded retry; a mid-split DENY is held and re-sent inside
``retry()``.

``log_ico_v2`` additionally checks the granule at run time
(``traces.assert``, asserts builds), so an unchecked legacy binding
that lets a straddling access through aborts loudly instead of
corrupting memory. Parameters (``width``) come from the generated
config tree (``IoV2SingleReqWidthAdapterConfig``). Source:
``gvsoc/core/models/utils/io_v2_single_req_width_adapter.cpp``; the
``utils/io_v2_single_req_width_adapter`` test is a standalone exerciser
whose checkers pin the exact chunk sequences the fabric sees.

The beat width adapter
~~~~~~~~~~~~~~~~~~~~~~~

``IoV2BeatWidthAdapter`` is the per-combination adapter for an
``IoV2Beat`` master bound to an ``IoV2Beat`` slave of a **different**
width — the HW bus up/down-sizer. Both sides speak the beat sub-protocol
unchanged; the adapter only converts the beat granularity, repacking the
per-beat streams in both directions (*N* narrow beats ↔ one wide beat, at
cumulative-offset boundaries, short terminal beats included). The wider
width must be an integer multiple of the narrower one (anything else is
rejected at build).

The point of the conversion is that **each side runs at its own
bandwidth**: at most one beat per cycle per channel *at its own width*.
The narrow side streams every cycle at full occupancy while the wide side
carries the same bytes in fewer, wider beats — one every *ratio* cycles —
with back-pressure keeping both sides honest:

* **Read, narrow slave**: downstream beats arrive one per cycle and are
  packed; a wide upstream beat completes every *ratio* cycles.
* **Read, wide slave**: each wide downstream beat unpacks into *ratio*
  upstream beats (one per cycle); further downstream beats are refused
  (``IO_RESP_DENIED`` + later ``resp_retry()``), throttling the producer
  to one wide beat per *ratio* cycles.
* **Write, narrow slave**: each upstream write request is chopped into
  one-per-cycle downstream beats; further upstream write requests are
  ``DENIED`` while the chop backlog is full and re-enabled with
  ``retry(WRITE)`` — a beat-form writer is throttled to one wide beat
  every *ratio* cycles.
* **Write, wide slave**: consecutive upstream write beats of one burst
  are packed (payload copied into the allocator-backed chunk, the
  upstream beat then freed) into one wide downstream beat issued every
  *ratio* cycles; the burst is acknowledged once, when the downstream
  burst ack (or the last chunk's inline ``DONE``) arrives.

Both back-pressure points sit behind **configurable FIFOs**, so the
adapter can absorb workload before it starts denying:

* ``read_fifo_depth`` (in ``input_width`` beats) bounds the repacked
  upstream read beats buffered before the downstream producer is refused.
  Auto (0) = 2× the width ratio — double-buffering, the minimum for
  continuous streaming. Deeper absorbs e.g. an upstream master that
  stalls its response channel, without ever stalling the downstream.
* ``write_fifo_depth`` (in ``output_width`` chunks) bounds the complete,
  un-issued downstream write chunks buffered before upstream write
  requests are refused. Auto (0) = 2 — just enough for seamless chunk
  streaming. Deep enough (burst bytes / ``output_width``) and a beat-form
  writer streams its whole burst at the upstream rate with no
  back-pressure at all, the FIFO then draining at the downstream rate.

The auto-inserted bridge uses the defaults; instantiate the adapter
manually (``utils.io_v2_beat_width_adapter.IoV2BeatWidthAdapter``) to set
non-default depths — the signature checks keep a user-instantiated
adapter on the path.

Ownership follows the initiator-owned convention on both faces: upstream
read beats are distinct allocator-backed objects the terminal master
frees; upstream write beats are consumed and freed by the adapter (their
payload is copied into the chunks at submit), and the burst is
acknowledged with a single data-less ack the upstream master frees; the
downstream read descriptor and write chunks are the adapter's own
allocator-backed objects, the write chunks being freed by the downstream
target.

Parameters (``input_width``, ``output_width``, ``read_fifo_depth``,
``write_fifo_depth``) come from the generated config tree
(``IoV2BeatWidthAdapterConfig``). Source:
``gvsoc/core/models/utils/io_v2_beat_width_adapter.{hpp,cpp}``; the
``utils/io_v2_beat_width_adapter`` test is a standalone exerciser whose
checkers assert the per-side beat counts *and spacings* (e.g. a 32 B read
at 8→4 must show eight 4 B beats every cycle downstream against four 8 B
beats every 2 cycles upstream).

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

Model requirements
------------------

The hard contracts a model must honour to be correct on a v2 path. Each is
backed by code referenced in *See also*.

1. **Tolerate every response form your signature admits.** Pick the
   signature (``IoV2Sync`` / ``IoV2SingleReq`` / ``IoV2Beat``) that
   legitimately restricts what you must handle, and then handle all of it
   — an ``IoV2Sync`` master only ever sees inline ``DONE``, but an
   ``IoV2SingleReq`` master must accept inline / async / deny — don't
   assume a narrower form than your signature admits.
2. **Allocate through ``IoReqAllocator`` on the beat protocol.** Any
   request that crosses a consumer-frees boundary (a read burst request,
   a read response beat, a write beat, a write ack) comes from a shared
   per-size pool (``IoReqAllocator::get(size)``, fetched once at
   construction) and is released with ``req->free()`` — never bare
   ``new`` / ``delete``. Read burst requests and write acks are
   data-less; read response beats carry the data in their co-allocated
   payload, which must never be repointed; write beats come from the
   size-0 pool with caller-managed ``data`` aliasing the source buffer.
3. **Re-send a ``DENIED`` request synchronously inside ``retry()``** — same
   cycle, before the callback returns. Deferring it to a later cycle
   live-locks slaves (e.g. ``log_ico_v2``) whose accept window is only open
   during the call.
4. **Tolerate per-beat address mutation.** A slave may leave ``addr`` at
   the burst base or advance it to ``burst_addr + emitted``; masters must
   accept either.
5. **Copy out and free the response beats you receive.** The terminal
   master copies each read beat's payload into its destination buffer and
   frees the beat (``req->free()``) — every read beat, single- or
   multi-beat, is a distinct pooled object. The write ack is a data-less
   allocator-backed object you free (``ack->free()``) — never assume it is
   one of your beats (the last beat may also answer inline ``DONE``, in
   which case you keep it).

   Beat-plane write masters: send write beats from the size-0 pool with
   ``data`` aliasing your source buffer and the same ``initiator`` on
   every beat of a burst; never touch a beat after ``GRANTED`` (snapshot
   fields before ``req()``); reclaim the source buffer only after the
   burst ack. Targets: consume-and-free non-last beats, never ``resp()``
   them; ack exactly once per burst.
6. **Choose the signature deliberately.** ``IoV2Sync`` for inline-only
   slaves; ``IoV2SingleReq`` for single-word, identity-routed routers and
   splitters; ``IoV2Beat`` for cycle-accurate per-beat consumers. Every
   port must commit to one of these concrete sub-protocols (see the note
   under `Signatures and the adapter framework`_).
7. **Beat routers: preserve the master's ``is_last`` and size the burst
   budget.** A downstream beat adapter overwrites ``req->is_last`` per beat,
   so snapshot the master's value at forward time. Pick a shared
   ``max_pending_bursts`` pool or a per-input
   ``max_pending_bursts_per_input`` budget to match the modelled hardware.

Pitfalls and lessons learned
----------------------------

Real bugs hit while building the beat path, and the rule each produced.
These are the cheapest mistakes to repeat, so they are called out
explicitly.

- **Pooled-object double-free.** A DMA issued reads on a request object
  embedded in a privately pooled vector; the consuming side ``delete``\d it,
  corrupting the heap (the crash surfaced only at teardown). Private pools
  can never work when the freer is not the allocator — which is the normal
  case on the beat protocol. *Rule: allocate through the shared
  ``IoReqAllocator`` and free with ``req->free()``; the back-pointer routes
  the free to the right pool no matter who calls it* (requirement 2).
- **Clock-bridge aliasing.** Reusing one request object across the *N*
  beats of a read response let a *deferring* downstream (a frequency-
  crossing clock bridge that queues the response) alias and lose beats.
  *Rule: a multi-beat read delivers a distinct object per non-terminal
  beat; only single-beat / terminal responses round-trip one object.*
- **Series-router 2× latency.** Issuing all of a burst's sub-reads in one
  cycle made two series bandwidth routers each charge their per-request
  bandwidth wait, so a path that should stream at 8 B/cyc streamed at 4.
  *Rule: pace sub-read issuance to one per cycle and size the outstanding
  window ≥ the downstream round-trip* (the adapter does this). The
  complementary fix on the **non-beat (whole-transaction)** path is the
  ``duration`` field:
  ``router_v2_bandwidth`` reports its bandwidth cost via ``set_duration``
  (max-combined), so two series routers report the bottleneck transfer
  time, not the sum. Consumers must read ``get_full_latency()``, not
  ``get_latency()`` — otherwise they drop the bandwidth cost entirely.
- **Sync-retry deadlock.** A master that re-sent its denied request on the
  cycle *after* ``retry()`` live-locked a zero-buffer arbiter. *Rule:
  re-send inside the ``retry()`` callback* (requirement 3).
- **Lost ``is_last`` in a beat router.** With a beat adapter downstream
  overwriting ``req->is_last`` per response beat, the router could not tell
  when a write burst ended. *Rule: snapshot ``is_last`` per forward
  (``pending_master_is_last``)* (requirement 7).
- **Shared burst pool starvation.** A single ``max_pending_bursts`` pool
  shared across a beat router's inputs let one busy input consume every
  slot and starve the others. *Rule: give each input its own
  ``max_pending_bursts_per_input`` budget when modelling per-master,
  ID-bounded AXI outstanding.*
- **Non-pooled write beat freed by the target.** Under the per-burst
  write-ack contract the target frees the write beats it consumes; a
  master still sending embedded or heap-allocated beats hands the target
  an object it cannot free — heap corruption or a pool poisoned with a
  foreign block. *Rule: write beats come from the size-0
  ``IoReqAllocator`` pool; ``IoReq::free()`` asserts the back-pointer in
  asserts builds so an unported master dies loudly.*
- **Bridge retry starvation on write-heavy streams.** A depth-gated
  forwarder that only re-opens its accept window from its *response*
  drain deadlocks once writes stop producing per-beat responses — the
  window never re-opens. *Rule: a forwarder owing a ``retry()`` must
  re-check its occupancy on every drain that frees a slot, request path
  included.*

See also
--------

- The header itself: ``gvsoc/engine/engine/include/vp/itf/io_v2.hpp``
  (the burst-conventions section at the top of the file is the
  authoritative source for the on-the-wire contract).
- The signature classes and the ``bridge_to`` insertion logic:
  ``gvsoc/engine/python/gvsoc/signature.py``.
- The auto-inserted adapters and their insertion rules:
  `Signatures and the adapter framework`_ (sources under
  ``gvsoc/core/models/utils/io_v2_*_adapter.{hpp,cpp}``).
- Real heap-alloc/free masters to copy: the cluster DMA
  ``gvsoc/gap9/models/ips/gap/cluster/mchan_beat.cpp`` and the iDMA
  ``gvsoc/pulp/models/pulp/ips/pulp/idma_v2/be/idma_be_axi.cpp``.
- A worked end-to-end example using the v2 protocol with all
  three response forms: :doc:`../components/ips/pulp/idma_v2`.