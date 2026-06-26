// SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
//
// SPDX-License-Identifier: Apache-2.0
//
// Authors: Germain Haugou (germain.haugou@gmail.com)

#pragma once

#include "vp/queue.hpp"
#include "vp/vp.hpp"

// IO v2 protocol — burst conventions
// ----------------------------------
//
// For a full walkthrough of the v2 protocol (object, ports, the three
// request statuses, sync / async / DENIED-retry flows, burst protocol
// with the three response forms, latency annotation, multiplexed
// ports and v1->v2 migration), see the developer-manual page at
// gvsoc/engine/docs/developer_manual/interfaces/io_v2.rst.
//
//
// Any single IoReq submitted via IoMaster::req() may be answered by the slave in
// one of three forms. All three are valid and must be tolerated by masters:
//
//   1. Sync big-packet:   slave returns IO_REQ_DONE inline with req->data filled
//                         and req->status set. No later resp() fires.
//   2. Async big-packet:  slave returns IO_REQ_GRANTED, later calls resp() once
//                         with is_first = is_last = true and the full size.
//   3. Beat stream:       slave returns IO_REQ_GRANTED, later calls resp() N
//                         times reusing the same IoReq object, mutating data,
//                         size, is_first and is_last between calls. Cumulative
//                         response sizes equal request size. burst_id is
//                         preserved across the beats. Final beat carries
//                         is_last = true and the burst's final status.
//
// DENIED / retry: when a slave returns IO_REQ_DENIED the master holds the
// request and re-sends it when the slave later calls retry(). The re-send MUST
// happen synchronously, inside the retry() callback (same cycle) — never
// deferred to a later cycle. See IoSlave::retry() below and the developer
// manual for why (some slaves only keep their accept window open during the
// synchronous call; deferring deadlocks). retry() carries an IoRetryChannel
// (READ / WRITE / ANY, default ANY) so read and write back-pressure can be
// independent on masters that split the two; see IoRetryChannel.
//
//                         The slave MAY also mutate addr to carry the per-beat
//                         start address (= burst_addr + cumulative emitted
//                         bytes). This deviates from AXI — where one AxADDR
//                         covers the whole burst and per-beat addresses are
//                         implicit — but is a cheap convenience for tracing
//                         and debugging on the master side, and the slave
//                         already tracks the cumulative offset internally.
//                         Masters MUST tolerate either convention: addr left
//                         at burst_addr, or addr updated per beat.
//
// Reads vs writes (the AXI mental model):
//
//   - A read burst is always submitted as exactly one req() with
//     size = total_burst_bytes, is_first = is_last = true. The response shape is
//     the slave's choice (any of the three forms above).
//   - A write burst may be submitted as either one req() carrying the full
//     payload (big-packet write) or N req() calls each carrying one beat of
//     data with is_first/is_last/burst_id set per beat (beat-form write). The
//     slave responds per-req in either case.
//
// Beat-fidelity components (cycle-accurate routers, cycle-accurate iDMAs) can
// route their outgoing IoMaster through a BeatResponseAdapter (see
// gvsoc/core/models/utils/io_v2_beat_adapter.hpp) which normalises whatever
// response form the slave produces into a uniform per-beat callback stream.
// That lets the same beat-fidelity component interoperate with a fast
// functional memory (sync DONE) as well as with a beat-aware DRAM controller.
//
// Synchronous sub-protocol (IoV2Sync):
//
//   The above lists the general v2 contract. A slave port may *additionally*
//   advertise the synchronous sub-protocol on the Python side via
//   :class:`gvsoc.signature.IoV2Sync`. That contract tightens the surface to
//   "form 1 only" — the slave always answers inline:
//
//     - Always returns IO_REQ_DONE from req_meth (never GRANTED / DENIED).
//     - Never calls resp() / retry().
//     - May still annotate req->latency; the master reads it inline on
//       return and stalls by that amount (the latency is *not* deferred).
//
//   A master that also declares IoV2Sync on its output may rely on this and
//   skip its async resp()/retry() code paths (it still honours the inline
//   latency). The C++ API surface is unchanged: the contract is enforced by
//   Python signature checks plus model-author discipline. memory_v3 is the
//   canonical IoV2Sync slave.

namespace vp {

class IoSlave;
class IoReq;

typedef enum
{
  READ=0,
  WRITE=1,
  LR=2,
  SC=3,
  SWAP=4,
  ADD=5,
  XOR=6,
  AND=7,
  OR=8,
  MIN=9,
  MAX=10,
  MINU=11,
  MAXU=12,
} IoReqOpcode;

typedef enum { IO_REQ_GRANTED, IO_REQ_DENIED, IO_REQ_DONE } IoReqStatus;

typedef enum { IO_RESP_OK, IO_RESP_INVALID } IoRespStatus;

// Flow-control result of IoSlave::resp() — the response-path analogue of
// IoReqStatus on the request path. A response is terminal, so it has only two
// outcomes: the consumer accepted the beat (IO_RESP_ACCEPTED, ownership returns
// to the producer's caller) or back-pressured it (IO_RESP_DENIED, the producer
// holds the beat unchanged and re-sends it on resp_retry()). This is kept
// separate from IoRespStatus, which reports the response *payload* status
// (OK / INVALID error) — an orthogonal axis a beat carries regardless of whether
// it was accepted.
typedef enum { IO_RESP_ACCEPTED, IO_RESP_DENIED } IoRespAck;

// Channel a retry() pertains to. A master that splits traffic into independent
// read / write channels (AXI AR/AW vs R) can be back-pressured on one channel
// while the other keeps flowing. A slave names the channel it became ready on;
// IO_RETRY_ANY (the default) means "all channels". The READ/WRITE values match
// the request direction (is_write) so a master can index its per-channel state
// directly. A channel-agnostic slave always uses IO_RETRY_ANY, and a
// single-channel master ignores the value.
typedef enum { IO_RETRY_READ = 0, IO_RETRY_WRITE = 1, IO_RETRY_ANY = 2 } IoRetryChannel;

typedef IoReqStatus(IoSlaveReqMeth)(vp::Block *, vp::IoReq *);
typedef IoReqStatus(IoSlaveReqMethMuxed)(vp::Block *, IoReq *, int id);

// The response callback returns IoRespAck: the consumer answers IO_RESP_ACCEPTED
// to take the beat, or IO_RESP_DENIED to back-pressure it (the producer then
// holds the beat and re-sends on resp_retry()). A consumer that always accepts
// simply returns IO_RESP_ACCEPTED.
typedef IoRespAck(IoMasterRespMeth)(vp::Block *, vp::IoReq *);
typedef IoRespAck(IoMasterRespMethMuxed)(vp::Block *, vp::IoReq *, int id);
typedef void(IoMasterRetryMeth)(vp::Block *, IoRetryChannel channel);
typedef void(IoMasterRetryMethMuxed)(vp::Block *, int id, IoRetryChannel channel);

// Response-path back-pressure (the AXI R/RREADY analogue), symmetric to the
// request-path retry above. A producer replies with IoSlave::resp(); the
// consumer's resp callback returns IO_RESP_DENIED to refuse the beat this cycle
// (IoSlave::resp() propagates that return). The producer then holds the beat and
// re-sends it when the consumer later calls IoMaster::resp_retry(), which
// dispatches to the producer's resp_retry_meth. A consumer that always accepts
// returns IO_RESP_ACCEPTED and no producer ever needs a resp_retry_meth — the
// whole mechanism is free when unused.
typedef void(IoSlaveRespRetryMeth)(vp::Block *, IoRetryChannel channel);
typedef void(IoSlaveRespRetryMethMuxed)(vp::Block *, int id, IoRetryChannel channel);

class IoReq : public vp::QueueElem {
    friend class IoMaster;
    friend class IoSlave;

  public:
    IoReq() {}

    IoReq(uint64_t addr, uint8_t *data, uint64_t size, bool is_write)
        : addr(addr), data(data), size(size), opcode((IoReqOpcode)is_write) {}

    void set_next(IoReq *req) { next = req; }
    IoReq *get_next() { return next; }

    uint64_t get_addr() { return addr; }
    void set_addr(uint64_t value) { addr = value; }

    bool get_is_write() { return (bool)this->opcode; }
    void set_is_write(bool is_write) { this->opcode = (IoReqOpcode)is_write; }

    IoReqOpcode get_opcode() { return this->opcode; }
    void set_opcode(IoReqOpcode opcode) { this->opcode = opcode; }

    void set_size(uint64_t size) { this->size = size; }
    uint64_t get_size() { return size; }

    uint8_t *get_data() { return data; }
    void set_data(uint8_t *data) { this->data = data; }

    uint8_t *get_second_data() { return this->second_data; }
    void set_second_data(uint8_t *data) { this->second_data = data; }

    IoRespStatus get_resp_status() { return this->status; }
    void set_resp_status(IoRespStatus status) { this->status = status; }

    // Accumulated latency annotation (in cycles) imposed along the forwarding path.
    //
    // Components (routers, targets) that model timing without scheduling ClockEvents
    // — i.e. that return IO_REQ_DONE inline after a logical delay — use this to tell
    // the master how many cycles the transaction "should have" taken. The master can
    // then stall its own cycle counter by this amount.
    //
    // For models that already use wall-clock scheduling (ClockEvent-based deferred
    // resp), leaving the field at 0 is fine — the delay is reflected in the real
    // cycle at which resp() fires.
    //
    // For beat-stream responses the slave can set this field per beat to give the
    // master a per-beat earliest-ready offset (in addition to the natural pacing of
    // when resp() actually fires).
    //
    // Masters reusing a request should call prepare() (or reset this field
    // explicitly) before each send.
    int64_t get_latency() const { return this->latency; }
    void set_latency(int64_t v) { this->latency = std::max(this->latency, v); }
    void inc_latency(int64_t v) { this->latency += v; }

    // Bandwidth-occupancy annotation (in cycles), kept *separate* from the head
    // latency above. Unlike latency, duration combines with **max**, not sum:
    // series resources that overlap (e.g. two bandwidth routers streaming the
    // same packet) each occupy the link for `duration` cycles but those
    // occupations pipeline, so the end-to-end transfer time is the bottleneck
    // (the max), not the total. A component that models throughput as
    // ceil(size / bandwidth) reports it here via set_duration().
    //
    // The full transaction time a master should stall by is get_full_latency()
    // = head latency (additive) + occupancy (max). Defaults to 0, so a request
    // that never traverses a bandwidth-limited resource has get_full_latency()
    // == get_latency().
    int64_t get_duration() const { return this->duration; }
    void set_duration(int64_t v) { this->duration = std::max(this->duration, v); }
    int64_t get_full_latency() const { return this->latency + this->duration; }

    // Reset per-send fields. Call before resubmitting a request object.
    void prepare() { this->latency = 0; this->duration = 0; this->status = IO_RESP_OK; }

    uint64_t addr;
    uint8_t *data;
    // Non-initialized flags for additional atomics data
    uint8_t *second_data;
    uint64_t size;
    IoReqOpcode opcode;
    // Response status. Defaults to IO_RESP_OK; slaves only need to set it
    // explicitly when reporting an error (IO_RESP_INVALID). prepare() also
    // resets it to IO_RESP_OK so masters reusing a request object don't
    // observe a stale error status from a previous send.
    IoRespStatus status = IO_RESP_OK;
    bool is_first = true;
    bool is_last = true;
    int64_t burst_id = -1;
    int64_t latency = 0;
    int64_t duration = 0;

    IoReq *next;
    IoReq *parent;
    void *initiator;
    // Temporary field
    uint64_t remaining_size;
};

/*
 * Class for IO master ports
 */
class IoMaster : public vp::MasterPort {
    friend class IoSlave;

  public:
    // Constructor
    inline IoMaster(IoMasterRetryMeth *retry_meth, IoMasterRespMeth *resp_meth);

    inline IoMaster(int id, IoMasterRetryMethMuxed *retry_meth, IoMasterRespMethMuxed *resp_meth);

    /*
     * Master binding methods
     */

    // Return if this master port is bound.
    bool is_bound();

    // True if the bound slave registered a resp_retry handler — i.e. it can be
    // told to re-send a back-pressured response. A consumer must not deny a
    // resp() (nor call resp_retry()) on a slave that lacks one.
    inline bool is_resp_retry_bound() { return this->slave_resp_retry_meth != NULL; }

    // Can be called by master component to send an IO request.
    inline IoReqStatus req(IoReq *req);

    // Signals the bound slave (the producer of responses) that this master is
    // ready to accept responses again after a previous resp() it denied (by
    // returning IO_RESP_DENIED). Carries no request, symmetric to IoSlave::retry()
    // on the request path: the master does not track which beats it denied, it
    // just announces readiness and the producer re-sends its held beat.
    //
    // Like request-path retry, this MUST be serviced synchronously: the producer
    // re-submits its held beat inside its resp_retry callback (same cycle). The
    // `channel` names which response channel became ready (READ/WRITE/ANY).
    inline void resp_retry(IoRetryChannel channel = IO_RETRY_ANY);

    /*
     * Reserved for framework
     */

    // Called by the framework to bind the master port to a slave port.
    virtual inline void bind_to(vp::Port *port, js::Config *config);

    // Called by the framework to finalize the binding, for example in order
    // to take into account cross-domains bindings
    void finalize();

  private:
    /*
     * Master callbacks
     */

    // Grant callback set by the user.
    // This gets called anytime the slave is granting an IO request.
    // This is set to an empty callback by default.
    void *retry_meth;

    // Response callback set by the user.
    // This gets called anytime the slave is sending back a response.
    // This is set to an empty callback by default.
    void *resp_meth;

    /*
     * Slave callbacks
     */

    // Callback set by the user on slave port and retrieved during binding
    void *slave_req_meth;

    // Slave resp_retry callback, retrieved from the slave port during binding.
    // Lets this master signal the producer that responses can flow again. NULL
    // when the producer did not register one (it never expects to be denied).
    void *slave_resp_retry_meth;

    /*
     * Stubs
     */

    // This is a stub setup when the slave is multiplexing the port so that we
    // can capture the master call and transform it into the slave call with the
    // right mux ID.
    static inline IoReqStatus req_muxed_stub(IoMaster *_this, IoReq *req);

    // Same idea for resp_retry: inject the slave mux ID on the master->slave
    // response-retry call when the slave port is multiplexed.
    static inline void resp_retry_muxed_stub(IoMaster *_this, IoRetryChannel channel);

    /*
     * Internal data
     */

    // Slave context when slave port is multiplexed.
    // We keep here a copy of the slave context when the slave port is multiplexed
    // as the normal variable for this context is used to store ourself so that
    // the stub is working well.
    vp::Block *slave_context_for_mux;

    void *slave_req_meth_for_mux;

    void *slave_resp_retry_meth_for_mux;

    // This data is the multiplex ID that we need to send to the slave when the slave port
    // is multiplexed.
    int slave_mux_id;

    // Multiplexed ID set by the slave when port is multiplxed
    int mux_id = -1;
};

/*
 * Class for IO slave ports
 */
class IoSlave : public vp::SlavePort {

    friend class IoMaster;

  public:

    // Constructor. resp_retry_meth is optional: a producer only needs it if its
    // bound consumer may deny a resp() (response-path back-pressure). Leave it
    // NULL for the common case where responses are always accepted.
    inline IoSlave(IoSlaveReqMeth *meth, IoSlaveRespRetryMeth *resp_retry_meth = nullptr);
    inline IoSlave(int id, IoSlaveReqMethMuxed *meth,
                   IoSlaveRespRetryMethMuxed *resp_retry_meth = nullptr);

    /*
     * Slave binding methods
     */

    // Signals the bound master that this slave is ready to accept again after a
    // previous IO_REQ_DENIED. Carries no request: the slave does not track which
    // requests it denied; it just announces readiness and the master re-sends.
    //
    // `channel` names which channel became ready (see IoRetryChannel). A
    // channel-agnostic slave leaves it at the default IO_RETRY_ANY ("all
    // channels"); a master that splits read/write back-pressure re-sends only
    // the held request(s) on that channel. A single-channel master ignores it.
    //
    // Synchronous-retry constraint: the master MUST re-submit its held
    // request(s) inside its retry callback — same cycle, before the call
    // returns — and must NOT defer the re-send to a later cycle. Some slaves
    // (e.g. the zero-buffer arbiter log_ico_v2) only keep their accept window
    // open for the duration of this call; a master that re-sends a cycle later
    // misses it and the two sides live-lock. See the developer manual
    // (interfaces/io_v2.rst, "Retry must be serviced synchronously").
    inline void retry(IoRetryChannel channel = IO_RETRY_ANY)
    {
        IoMasterRetryMeth *meth = (IoMasterRetryMeth *)this->master_retry_meth;
        meth((vp::Block *)this->get_remote_context(), channel);
    }

    // Can be called to reply to an IO request.
    // Replying means that the slave has finished handing the request and it is now
    // owned back by the master which can then proceed with the request.
    //
    // Returns the consumer's IoRespAck: IO_RESP_ACCEPTED when it took the beat
    // (ownership returns to the master) or IO_RESP_DENIED when it refused it this
    // cycle (response-path back-pressure) — in which case the producer keeps
    // ownership, must hold the beat unchanged, and re-send it when the consumer
    // later calls IoMaster::resp_retry(). A consumer that always accepts simply
    // returns IO_RESP_ACCEPTED.
    inline IoRespAck resp(IoReq *req)
    {
        IoMasterRespMeth *meth = (IoMasterRespMeth *)this->master_resp_meth;
        return meth((vp::Block *)this->get_remote_context(), req);
    }

    /*
     * Reserved for framework
     */

    // Called by the framework to bind the slave port to a master port.
    inline void bind_to(vp::Port *_port, js::Config *config);

    // Called by the framework to finalize the binding, for example in order
    // to take into account cross-domains bindings
    void finalize();

  private:
    /*
     * Slave callbacks
     */

    // Request callback set by the user.
    // This gets called anytime the master is sending a request.
    // This is set to an empty callback by default.
    void *req_meth;

    // Response-retry callback set by the user (optional). Invoked when the bound
    // master calls IoMaster::resp_retry() to announce it can accept responses
    // again. NULL when this producer never expects its responses to be denied.
    void *resp_retry_meth;

    /*
     * Master callbacks
     */

    // Response callback set by the user on master port and retrived during binding
    void *master_resp_meth;

    // Grant callback set by the user on master port and retrived during binding
    void *master_retry_meth;

    /*
     * Stubs
     */

    // This is a stub setup when the slave is multiplexing the port so that we
    // can capture the master call and transform it into the slave call with the
    // right mux ID.
    static inline void retry_muxed_stub(IoSlave *_this, IoRetryChannel channel);

    // This is a stub setup when the slave is multiplexing the port so that we
    // can capture the master call and transform it into the slave call with the
    // right mux ID.
    static inline IoRespAck resp_muxed_stub(IoSlave *_this, IoReq *req);

    /*
     * Internal data
     */

    // Multiplexed ID set by the slave when port is multiplxed
    int mux_id = -1;

    // This data is the multiplex ID that we need to send to the slave when the slave port
    // is multiplexed.
    int master_mux_id = -1;

    // Slave context when slave port is multiplexed.
    // We keep here a copy of the slave context when the slave port is multiplexed
    // as the normal variable for this context is used to store ourself so that
    // the stub is working well.
    vp::Block *master_context_for_mux = NULL;

    void *master_retry_meth_for_mux;

    void *master_resp_meth_for_mux;

};

inline IoMaster::IoMaster(IoMasterRetryMeth *retry_meth, IoMasterRespMeth *resp_meth)
    : resp_meth((void *)resp_meth), retry_meth((void *)retry_meth)
{
}

inline IoMaster::IoMaster(int id, IoMasterRetryMethMuxed *retry_meth, IoMasterRespMethMuxed *resp_meth)
    : mux_id(id), resp_meth((void *)resp_meth), retry_meth((void *)retry_meth)
{
}

inline IoReqStatus IoMaster::req(IoReq *req)
{
    IoSlaveReqMeth *meth = (IoSlaveReqMeth *)this->slave_req_meth;
    return meth((vp::Block *)this->get_remote_context(), req);
}

inline void IoMaster::resp_retry(IoRetryChannel channel)
{
    // Only valid against a producer that registered a resp_retry_meth — i.e. one
    // that can be denied. A master that denies a resp() must be bound to such a
    // producer, otherwise the held beat could never be re-sent.
    vp_assert(this->slave_resp_retry_meth != NULL, this->get_owner()->get_trace(),
        "resp_retry() on a slave that did not register a resp_retry_meth\n");
    IoSlaveRespRetryMeth *meth = (IoSlaveRespRetryMeth *)this->slave_resp_retry_meth;
    meth((vp::Block *)this->get_remote_context(), channel);
}

inline void IoMaster::bind_to(vp::Port *_port, js::Config *config) {
    IoSlave *port = (IoSlave *)_port;
    this->remote_port = port;

    vp_assert(port != NULL, this->get_owner()->get_trace(), "Binding to NULL slave port\n");

    if (port->mux_id == -1)
    {
        // Normal binding, just register the method and context into the master
        // port for fast access
        this->slave_req_meth = port->req_meth;
        this->slave_resp_retry_meth = port->resp_retry_meth;
        this->set_remote_context(port->get_context());
    }
    else
    {
        // Multiplexed binding, tweak the normal callback so that we enter
        // the stub to insert the multiplex ID.
        this->slave_req_meth_for_mux = port->req_meth;
        this->slave_req_meth = (void *)&IoMaster::req_muxed_stub;

        // Same for the response-retry signal — only route through the stub when
        // the producer registered a muxed resp_retry handler; otherwise leave it
        // NULL so the resp_retry() assert fires on misuse instead of calling a
        // stub with a NULL underlying handler.
        if (port->resp_retry_meth != NULL)
        {
            this->slave_resp_retry_meth_for_mux = port->resp_retry_meth;
            this->slave_resp_retry_meth = (void *)&IoMaster::resp_retry_muxed_stub;
        }
        else
        {
            this->slave_resp_retry_meth = NULL;
        }

        this->set_remote_context(this);
        this->slave_context_for_mux = (vp::Block *)port->get_context();
        this->slave_mux_id = port->mux_id;
    }
}

inline bool IoMaster::is_bound() { return this->remote_port != NULL; }

inline IoReqStatus IoMaster::req_muxed_stub(IoMaster *_this, IoReq *req)
{
    // The normal callback was tweaked in order to get there when the master is sending a
    // request. Now generate the normal call with the mux ID using the saved handler
    IoSlaveReqMethMuxed *meth = (IoSlaveReqMethMuxed *)_this->slave_req_meth_for_mux;
    return meth((vp::Block *)_this->slave_context_for_mux, req, _this->slave_mux_id);
}

inline void IoMaster::resp_retry_muxed_stub(IoMaster *_this, IoRetryChannel channel)
{
    // Same pattern as req_muxed_stub but for the response-retry signal: inject
    // the slave mux ID on the way to the muxed resp_retry handler.
    IoSlaveRespRetryMethMuxed *meth =
        (IoSlaveRespRetryMethMuxed *)_this->slave_resp_retry_meth_for_mux;
    meth((vp::Block *)_this->slave_context_for_mux, _this->slave_mux_id, channel);
}

inline void IoMaster::finalize()
{
}

inline IoSlave::IoSlave(IoSlaveReqMeth *meth, IoSlaveRespRetryMeth *resp_retry_meth)
: req_meth((void *)meth), resp_retry_meth((void *)resp_retry_meth)
{
}

inline IoSlave::IoSlave(int id, IoSlaveReqMethMuxed *meth,
                        IoSlaveRespRetryMethMuxed *resp_retry_meth)
: req_meth((void *)meth), resp_retry_meth((void *)resp_retry_meth), mux_id(id)
{
}

inline void IoSlave::retry_muxed_stub(IoSlave *_this, IoRetryChannel channel) {
    // The normal callback was tweaked in order to get there when the master is sending a
    // request. Now generate the normal call with the mux ID using the saved handler
    IoMasterRetryMethMuxed *meth = (IoMasterRetryMethMuxed *)_this->master_retry_meth_for_mux;
    meth((vp::Block *)_this->master_context_for_mux, _this->master_mux_id, channel);
}

inline IoRespAck IoSlave::resp_muxed_stub(IoSlave *_this, IoReq *req) {
    // The normal callback was tweaked in order to get there when the master is sending a
    // request. Now generate the normal call with the mux ID using the saved handler
    IoMasterRespMethMuxed *meth = (IoMasterRespMethMuxed *)_this->master_resp_meth_for_mux;
    return meth((vp::Block *)_this->master_context_for_mux, req, _this->master_mux_id);
}

inline void IoSlave::bind_to(vp::Port *_port, js::Config *config) {
    // Instantiate a new slave port which is just used as a reference to reply
    // to the correct master port
    SlavePort::bind_to(_port, config);
    IoMaster *port = (IoMaster *)_port;

    if (port->mux_id == -1)
    {
        this->master_resp_meth = port->resp_meth;
        this->master_retry_meth = port->retry_meth;
        this->set_remote_context(port->get_context());
    }
    else
    {
        this->master_retry_meth_for_mux = port->retry_meth;
        this->master_retry_meth = (void *)&IoSlave::retry_muxed_stub;

        this->master_resp_meth_for_mux = port->resp_meth;
        this->master_resp_meth = (void *)&IoSlave::resp_muxed_stub;

        this->set_remote_context(this);
        this->master_context_for_mux = (vp::Block *)port->get_context();
        this->master_mux_id = port->mux_id;
    }
}

inline void IoSlave::finalize()
{
}

};
