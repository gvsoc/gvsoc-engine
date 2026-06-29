#
# Copyright (C) 2026 GreenWaves Technologies, SAS, ETH Zurich and
#                    University of Bologna
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
Class-based port signatures for the gvrun2 systree.

A signature class describes a port's protocol/interface. The framework
compares master-side and slave-side signatures when bindings are flattened
and, if they don't match, asks the master signature to produce a bridge
component that is auto-inserted between the two ports.

Today the framework distinguishes three io_v2 sub-protocols (big-packet,
beat-mode, strict-sync) along the protocol axis. The same mechanism is the
natural extension point for clock-domain crossings, voltage-domain
crossings, address-width adapters, etc. Subclasses add the checks; the
binding machinery in systree_gvrun2.py does not need to grow.
"""


class Signature:
    """Base class for class-based port signatures.

    Subclasses describe a particular protocol/interface. The framework asks
    the master-side signature whether it can be bound directly to the
    slave-side signature, and if not, asks it to produce a bridge component.
    """

    # Stable string tag used to interoperate with the legacy ``signature='...'``
    # string check. None means there is no legacy-string equivalent.
    tag = None

    def label(self):
        """Human-readable description of this interface, for tooling.

        Used e.g. by the GUI model view to show a port's protocol next to its
        name. Defaults to the legacy tag, falling back to the class name.
        """
        return self.tag or type(self).__name__

    def is_compatible(self, other):
        """Return True if a master with ``self`` can bind directly to a slave with ``other``.

        ``other`` is either another :class:`Signature` or a legacy signature
        string. A string peer matches when it equals this signature's ``tag``,
        so a class-based master binds directly to a slave still declared with
        the historic ``signature='...'`` string (no bridge inserted).
        """
        if isinstance(other, str):
            return other == self.tag
        return type(self) is type(other)

    def bridge_to(self, other, parent, name):
        """Return a bridge Component to place between master (``self``) and slave (``other``),
        or None if no bridge is needed.

        The default raises if signatures aren't compatible — subclasses that
        know how to bridge to a specific peer override and return a Component.
        ``parent`` is the component hosting the binding; the bridge should
        register itself as a child of ``parent`` (which the standard
        Component constructor already does).
        """
        if self.is_compatible(other):
            return None
        raise RuntimeError(
            f"Incompatible signatures and no bridge defined: "
            f"master={type(self).__name__} -> slave={type(other).__name__}")


class IoV2BigPacket(Signature):
    """io_v2 master/slave operating on whole-packet semantics.

    The slave may answer in any of the three v2 response forms; a big-packet
    master tolerates all of them per the v2 protocol contract.

    A :class:`IoV2Sync` slave is a strict subset of this contract (only the
    sync DONE response form), so a big-packet master can bind to it directly
    without any adapter.
    """

    tag = 'io_v2'

    def label(self):
        return 'io_v2 (big-packet)'

    def is_compatible(self, other):
        # Legacy 'io_v2' string slave is the historic big-packet default.
        if isinstance(other, str):
            return other == self.tag
        # IoV2Sync and IoV2SingleReq are tighter subsets of the same response
        # surface; a big-packet master already handles their (single-beat)
        # responses, so it binds directly.
        return isinstance(other, (IoV2BigPacket, IoV2Sync, IoV2SingleReq))

    def bridge_to(self, other, parent, name):
        # A big-packet master cannot consume the per-beat response stream of a
        # beat slave: insert the inverse of IoV2BeatAdapter, which forwards the
        # access as beats and collapses the N-beat response into one big-packet
        # reply. Everything else (big-packet / sync / single-req / legacy
        # 'io_v2' string / unsignatured slaves) is directly compatible.
        if isinstance(other, IoV2Beat):
            from utils.io_v2_beat_collapse_adapter import IoV2BeatCollapseAdapter
            return IoV2BeatCollapseAdapter(parent, name, beat_width=other.beat_width)
        return None


class IoV2SingleReq(Signature):
    """io_v2 restricted to single-request / single-beat accesses — the HW lint
    (req / gnt / r_valid) memory fabric.

    Each access is a single word and the response is always a **single beat**:
    inline ``IO_REQ_DONE``, or ``IO_REQ_GRANTED`` + exactly one ``resp()`` (and
    ``IO_REQ_DENIED`` + ``retry()`` for back-pressure) — but **never** a
    multi-beat response stream. So this sits strictly between:

      - :class:`IoV2BigPacket` (looser — may answer with a multi-beat stream),
        and
      - :class:`IoV2Sync` (tighter — must answer inline, no async / no deny).

    Components that route a response back by *object identity* — functional /
    bandwidth routers (``in_flight_map[req]``), the width/opcode splitters
    (``req->parent``) — implement exactly this contract: they rely on the
    single-beat response coming back on the very request object they sent, so
    they cannot handle the per-beat stream of a beat slave. Declaring
    IoV2SingleReq makes that a checked invariant rather than an implicit
    assumption: a beat master/slave on the other side forces a converter.
    """

    tag = 'io_v2'

    def label(self):
        return 'io_v2 (single-req)'

    def is_compatible(self, other):
        if isinstance(other, str):
            return other == self.tag
        # Direct to any single-beat peer (single-req / big-packet / sync). A
        # beat peer (multi-beat) is handled in bridge_to.
        return isinstance(other, (IoV2SingleReq, IoV2BigPacket, IoV2Sync))

    def bridge_to(self, other, parent, name):
        # A beat slave streams multi-beat responses this single-req master
        # cannot consume: insert the collapse converter (beat -> single-beat),
        # exactly as a big-packet master would.
        if isinstance(other, IoV2Beat):
            from utils.io_v2_beat_collapse_adapter import IoV2BeatCollapseAdapter
            return IoV2BeatCollapseAdapter(parent, name, beat_width=other.beat_width)
        return None


class IoV2Sync(Signature):
    """io_v2 port operating under the synchronous sub-protocol.

    This is the big-packet response surface restricted to the *inline*
    form: the slave answers within the same ``req()`` call. It does not
    forbid latency — the slave may annotate ``req->latency`` and the master
    honours it inline (it stalls its own cycle counter by that amount after
    ``req()`` returns). What it forbids is the *asynchronous* machinery.

    Slave contract:

      - Always returns ``IO_REQ_DONE`` from ``req_meth`` (never ``GRANTED``,
        never ``DENIED``).
      - Never calls ``resp()`` or ``retry()`` — there is no async path.
      - May annotate ``req->latency`` synchronously (read by the master on
        return) and may set ``req->status`` (``IO_RESP_OK`` /
        ``IO_RESP_INVALID``).

    Master simplifications enabled by this contract:

      - ``resp_meth`` / ``retry_meth`` are never invoked. Masters can pass
        no-op stubs to the ``IoMaster`` constructor.
      - No per-request bookkeeping for outstanding async responses.
      - No event-scheduled wake-up: the master reads ``req->latency`` inline
        and proceeds, rather than waiting for a deferred ``resp()``.

    Compatibility:

      - Sync ↔ Sync: direct bind.
      - BigPacket master ↔ Sync slave: direct bind (Sync is a subset).
      - Beat master ↔ Sync slave: existing ``utils.io_v2_beat_adapter``
        normalises the inline DONE (with its latency annotation) into a
        per-beat ``resp()`` stream.
      - Sync master ↔ anything else: **RuntimeError** at build time. The
        simpler master contract is unbreakable, and no adapter can
        synthesise an inline response out of an async (``GRANTED`` +
        deferred ``resp()`` / ``DENIED`` + ``retry()``) slave without
        stalling the engine.
    """

    tag = 'io_v2'

    def label(self):
        return 'io_v2 (sync)'

    def is_compatible(self, other):
        return isinstance(other, IoV2Sync)

    def bridge_to(self, other, parent, name):
        if self.is_compatible(other):
            return None
        # A sync master cannot be safely connected to anything more
        # general: an async slave's behaviour cannot be folded into an
        # inline response without stalling the engine, and silently
        # relaxing the contract would break the master's simplifying
        # assumptions. This is a design error.
        raise RuntimeError(
            f"IoV2Sync master cannot bind to "
            f"{type(other).__name__ if not isinstance(other, str) else repr(other)} "
            f"slave: no adapter can synthesise an inline synchronous "
            f"response from an async slave. Either make the slave "
            f"sync-capable, or declare the master as IoV2BigPacket."
        )


class IoV2Beat(Signature):
    """io_v2 master that wants its response normalised to one ``resp()`` per beat.

    The master receives one ``resp_meth`` call per beat with ``req->is_first``,
    ``is_last``, ``burst_id``, ``size``, ``data`` and ``status`` set per beat
    (the existing beat-form response contract of v2).
    """

    tag = 'io_v2'

    def __init__(self, beat_width):
        self.beat_width = beat_width

    def label(self):
        return f'io_v2 (beat, width={self.beat_width})'

    def is_compatible(self, other):
        return isinstance(other, IoV2Beat) and other.beat_width == self.beat_width

    def bridge_to(self, other, parent, name):
        # Same-mode peer (IoV2Beat <-> IoV2Beat, same beat_width): no adapter.
        if self.is_compatible(other):
            return None
        # IoV2Sync slave: a dedicated, simpler adapter. Because a sync slave
        # always replies IO_REQ_DONE inline (never resp()/retry()) and serves
        # any size in one call, the adapter forwards the whole burst as a single
        # request and just spreads the response into per-beat resp() calls — no
        # per-beat sub-read pipeline, no async/deny bookkeeping.
        if isinstance(other, IoV2Sync):
            from utils.io_v2_beat_to_sync_adapter import IoV2BeatToSyncAdapter
            return IoV2BeatToSyncAdapter(parent, name, beat_width=self.beat_width)
        # IoV2SingleReq slave: a dedicated, per-combination adapter. A single-req
        # slave answers each request with a single-beat response (inline DONE,
        # async GRANTED + one resp(), or DENIED + retry()) but never a multi-beat
        # stream. The adapter keeps the general adapter's read sub-read pipeline
        # and async/deny machinery, and additionally checks the single-beat
        # response contract in asserts builds.
        if isinstance(other, IoV2SingleReq):
            from utils.io_v2_beat_to_single_req_adapter import IoV2BeatToSingleReqAdapter
            return IoV2BeatToSingleReqAdapter(parent, name, beat_width=self.beat_width)
        # Mismatched mode (IoV2Beat master <-> IoV2BigPacket slave, or legacy
        # ``'io_v2'`` string slave that is by default big-packet): the general
        # adapter normalises any of the slave's response forms (sync DONE, async
        # big-packet, beat stream) into a uniform per-beat stream. The legacy
        # string is the historic v2 default and matches IoV2BigPacket semantics.
        if isinstance(other, IoV2BigPacket) or other == IoV2BigPacket.tag:
            from utils.io_v2_beat_adapter import IoV2BeatAdapter
            return IoV2BeatAdapter(parent, name, beat_width=self.beat_width)
        # IoV2Beat <-> IoV2Beat with differing widths is a SoC design error,
        # not a missing adapter.
        return super().bridge_to(other, parent, name)


class Wire(Signature):
    """Generic typed wire interface (``vp::WireMaster<T>`` / ``vp::WireSlave<T>``).

    A single parametric class covering every ``wire<T>`` flavour. ``ctype`` is
    the C++ element type as written in the model (e.g. ``'bool'``, ``'int'``,
    ``'uint64_t'``, ``'mram_req_t*'``). Two wires are compatible only when they
    carry the same element type.

    The per-instance ``tag`` is the exact legacy string equivalent
    (``wire<bool>``, ``wire<mram_req_t*>``, …), so a ``Wire`` master binds
    interchangeably with a slave still declared as ``signature='wire<T>'``.
    """

    def __init__(self, ctype):
        self.ctype = ctype
        self.tag = f'wire<{ctype}>'

    def is_compatible(self, other):
        if isinstance(other, str):
            return other == self.tag
        return isinstance(other, Wire) and other.ctype == self.ctype


class Io(Signature):
    """Legacy IO v1 interface (``io.hpp``). Tag-equivalent to ``signature='io'``."""

    tag = 'io'


class Clock(Signature):
    """Clock-distribution interface. Tag-equivalent to ``signature='clock'``."""

    tag = 'clock'


class ClockGen(Signature):
    """Clock-generator control interface. Tag-equivalent to ``signature='clock_gen'``."""

    tag = 'clock_gen'


class ClockCtrl(Signature):
    """Clock-control interface. Tag-equivalent to ``signature='clock_ctrl'``."""

    tag = 'clock_ctrl'


class Clk(Signature):
    """Legacy clock interface (``clk.hpp``). Tag-equivalent to ``signature='clk'``."""

    tag = 'clk'


class Audio(Signature):
    """Audio interface (``audio.hpp``). Tag-equivalent to ``signature='audio'``."""

    tag = 'audio'


class Uart(Signature):
    """UART interface (``uart.hpp``). Tag-equivalent to ``signature='uart'``."""

    tag = 'uart'


class Cpi(Signature):
    """Camera Parallel Interface (``cpi.hpp``). Tag-equivalent to ``signature='cpi'``."""

    tag = 'cpi'


class Hyper(Signature):
    """HyperBus interface (``hyper.hpp``). Tag-equivalent to ``signature='hyper'``."""

    tag = 'hyper'


class I2c(Signature):
    """I2C interface (``i2c.hpp``). Tag-equivalent to ``signature='i2c'``."""

    tag = 'i2c'


class I2s(Signature):
    """I2S/TDM audio interface (``i2s.hpp``). Tag-equivalent to ``signature='i2s'``."""

    tag = 'i2s'


class Jtag(Signature):
    """JTAG interface (``jtag.hpp``). Tag-equivalent to ``signature='jtag'``."""

    tag = 'jtag'


class Qspim(Signature):
    """(Q)SPI master interface (``qspim.hpp``). Tag-equivalent to ``signature='qspim'``."""

    tag = 'qspim'
