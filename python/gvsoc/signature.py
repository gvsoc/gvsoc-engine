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

Strict-protocol policy
----------------------

A class-based :class:`IoV2BigPacket` — the retired generic io_v2 form — is a
hard error when the systree is flattened (see :func:`assert_io_v2_strict`,
called from the bridge pass in systree.py / systree_gvrun2.py), forcing new
ports onto the concrete sub-protocols (:class:`IoV2Sync`,
:class:`IoV2SingleReq`, :class:`IoV2Beat`, :class:`IoV2Any`). A port that
still genuinely needs big-packet opts in explicitly per instance with
``IoV2BigPacket(allow=True)``.

The bare ``signature='io_v2'`` string is left permitted: it is the historic
big-packet spelling, still used only by the stand-alone stub tests, and —
unlike a class instance — a string master is skipped by the auto-bridge pass
(so it keeps the raw, adapter-free binding those tests rely on).
"""


def is_loose_io_v2(sig) -> bool:
    """True if ``sig`` is a forbidden generic big-packet io_v2 instance.

    Only a class-based :class:`IoV2BigPacket` constructed without
    ``allow=True`` is loose. The bare ``'io_v2'`` string is permitted (see
    the module docstring), and every concrete signature (Sync / SingleReq /
    Beat / Any) and non-io interface is not loose.
    """
    if isinstance(sig, Signature):
        return not sig.is_concrete_protocol()
    return False


def assert_io_v2_strict(master_sig, slave_sig, binding_desc):
    """Raise unless both endpoints commit to a concrete io_v2 sub-protocol.

    ``binding_desc`` is a human-readable ``master:port -> slave:port`` string
    used in the error.
    """
    for role, sig in (('master', master_sig), ('slave', slave_sig)):
        if is_loose_io_v2(sig):
            shown = sig.label() if isinstance(sig, Signature) else repr(sig)
            raise RuntimeError(
                f"Forbidden generic big-packet io_v2 on the {role} port of "
                f"binding {binding_desc} (signature {shown}). Declare a "
                f"concrete protocol instead: IoV2Sync (inline DONE), "
                f"IoV2SingleReq (single-beat, identity-routed), "
                f"IoV2Beat(width) (per-beat stream) or IoV2Any "
                f"(explicit transparent forwarder). If big-packet is really "
                f"needed while it is being prototyped, opt in explicitly with "
                f"IoV2BigPacket(allow=True).")


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

    def is_concrete_protocol(self):
        """Whether this signature names a specific, committed protocol.

        Only the retired generic big-packet io_v2 (:class:`IoV2BigPacket`)
        returns False; every other signature — the concrete io_v2
        sub-protocols and all non-io interfaces — is concrete. Used by the
        strict-protocol policy (:func:`is_loose_io_v2`).
        """
        return True

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

    Big-packet is the retired generic form: the strict-protocol policy
    rejects it by default. A port that still genuinely needs it — e.g. a
    model whose response form is being prototyped — opts in explicitly with
    ``IoV2BigPacket(allow=True)``, which documents the intent at the call
    site instead of relying on a global switch.
    """

    tag = 'io_v2'

    def __init__(self, allow: bool = False):
        self.allow = allow

    def label(self):
        return 'io_v2 (big-packet)'

    def is_concrete_protocol(self):
        # Forbidden by the strict-protocol policy unless this instance was
        # explicitly allowed (IoV2BigPacket(allow=True)).
        return self.allow

    def is_compatible(self, other):
        # Legacy 'io_v2' string slave is the historic big-packet default.
        if isinstance(other, str):
            return other == self.tag
        # IoV2Sync and IoV2SingleReq are tighter subsets of the same response
        # surface; a big-packet master already handles their (single-beat)
        # responses, so it binds directly. IoV2Any is a transparent peer.
        return isinstance(other, (IoV2BigPacket, IoV2Sync, IoV2SingleReq, IoV2Any))

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

    ``width`` (bytes, power of two) optionally bounds the access granule:

      - On a **slave**, it declares the largest aligned granule one access may
        cover — the natural fit for a bank-interleaved fabric (e.g. the GAP9
        shared-L2 ``LogIco`` with 4-byte interleaving), where an access
        crossing a granule boundary would be mis-routed to a single bank and
        alias another address.
      - On a **master**, it declares the widest access it ever issues.

    The default ``0`` means *don't care*: a width-0 **slave** accepts any
    single-req access directly (today's behaviour, unchecked). Once a slave
    declares a width, only a master that **provably** fits (its own declared
    width is non-zero and no larger) binds directly; any other master — one
    declaring a wider width, or the common width-0 master whose access sizes
    are unknown — gets ``IoV2SingleReqWidthAdapter`` auto-inserted, which
    passes fitting accesses straight through and chops a granule-straddling
    one into granule-aligned sub-accesses, preserving the identity contract.
    """

    tag = 'io_v2'

    def __init__(self, width: int = 0):
        if width < 0 or (width & (width - 1)) != 0:
            raise RuntimeError(
                f'IoV2SingleReq width must be 0 or a power of two (got {width})')
        self.width = width

    def label(self):
        if self.width:
            return f'io_v2 (single-req, width={self.width})'
        return 'io_v2 (single-req)'

    def is_compatible(self, other):
        if isinstance(other, str):
            return other == self.tag
        # Direct to any single-beat peer (single-req / big-packet / sync). A
        # beat peer (multi-beat) is handled in bridge_to.
        if isinstance(other, IoV2Any):
            # Transparent forwarder — relays the single-beat response 1:1.
            return True
        if isinstance(other, IoV2SingleReq):
            # Width rule: a width-0 slave accepts anything (don't care). A
            # width-declaring slave binds directly only to a master that
            # provably fits its granule (declared, non-zero, no wider);
            # everything else — wider or unknown (0) masters — goes through
            # the width adapter (see bridge_to).
            if other.width == 0:
                return True
            return self.width != 0 and self.width <= other.width
        return isinstance(other, (IoV2BigPacket, IoV2Sync))

    def bridge_to(self, other, parent, name):
        # A beat slave streams distinct, allocator-backed response beats this
        # identity-routing master cannot consume: insert the dedicated
        # single-req -> beat adapter. It translates the allocation conventions
        # (frees the read beats, hands the master back its own request) and,
        # because a single-req master carries its own per-request state, it
        # supports any number of outstanding accesses — unlike the
        # single-outstanding collapse adapter a big-packet master gets.
        if isinstance(other, IoV2Beat):
            from utils.io_v2_single_req_to_beat_adapter import IoV2SingleReqToBeatAdapter
            return IoV2SingleReqToBeatAdapter(parent, name,
                                              beat_width=other.beat_width)
        # Single-req slave with a tighter width granule than this master
        # guarantees (see the class docstring): insert the width adapter,
        # which splits each too-wide or granule-straddling access into
        # granule-aligned sub-accesses while preserving the identity contract.
        if isinstance(other, IoV2SingleReq) and not self.is_compatible(other):
            from utils.io_v2_single_req_width_adapter import IoV2SingleReqWidthAdapter
            return IoV2SingleReqWidthAdapter(parent, name, width=other.width)
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
        #
        # An IoV2Any slave gets the same treatment: transparency means the
        # response form is whatever the far end of the chain produces, which a
        # beat-fidelity master must never see raw (an inline DONE would trip
        # its "must not surface IO_REQ_DONE" contract) — so normalise it.
        if isinstance(other, (IoV2BigPacket, IoV2Any)) or other == IoV2BigPacket.tag:
            from utils.io_v2_beat_adapter import IoV2BeatAdapter
            return IoV2BeatAdapter(parent, name, beat_width=self.beat_width)
        # IoV2Beat slave with a DIFFERENT width (same-width peers bound directly
        # above): both sides speak the beat sub-protocol, only the granularity
        # changes. Insert the width-conversion adapter, which repacks the
        # per-beat streams in both directions (N narrow beats <-> one wide
        # beat) so each side runs at its own beat width — the two bindings then
        # show different per-cycle occupancies. The wider width must be an
        # integer multiple of the narrower one (checked by the adapter).
        if isinstance(other, IoV2Beat):
            from utils.io_v2_beat_width_adapter import IoV2BeatWidthAdapter
            return IoV2BeatWidthAdapter(parent, name,
                input_width=self.beat_width, output_width=other.beat_width)
        return super().bridge_to(other, parent, name)


class IoV2Any(Signature):
    """io_v2 port that is deliberately protocol-transparent.

    Declared by pass-through forwarders — the remapper, limiter, fifo,
    traffic stubs and clock bridge — that carry a request and relay its
    response 1:1 without caring which of the three v2 response forms
    (sync DONE / async big-packet / beat stream) flows through them.

    Unlike the retired :class:`IoV2BigPacket`, this is an *explicit,
    intentional* "any": the strict-protocol policy allows it, but only where
    transparency is the genuine behaviour, not as a lazy default for a leaf
    model that should commit to a concrete contract. A transparent forwarder
    declares ``IoV2Any`` on BOTH its input (slave) and output (master) ports.

    As a master it binds directly to every io_v2 slave: whatever the upstream
    sent, it forwards, and whatever form the downstream answers with, it
    relays back. As a slave it binds directly to big-packet / single-req
    masters (they tolerate every response form that can emerge from the
    chain), but a beat-fidelity :class:`IoV2Beat` master inserts its general
    normalising adapter in front of it — transparency means the response
    form is unknown, and a beat master must never see a raw inline ``DONE``.

    ``beat_tolerant`` marks a *terminal* master that natively consumes raw
    per-beat response streams of any width (e.g. the traffic generator,
    which measures a NoC's bandwidth and must observe the stream
    unmodified). Such a master binds directly to an :class:`IoV2Beat`
    slave instead of getting the (single-outstanding, stream-folding)
    collapse adapter a plain transparent forwarder needs.
    """

    tag = 'io_v2'

    def __init__(self, beat_tolerant: bool = False):
        self.beat_tolerant = beat_tolerant

    def label(self):
        if self.beat_tolerant:
            return 'io_v2 (any/beat-tolerant)'
        return 'io_v2 (any/transparent)'

    def is_compatible(self, other):
        if isinstance(other, str):
            return other == self.tag
        return isinstance(other, (IoV2Any, IoV2BigPacket, IoV2SingleReq,
                                  IoV2Sync))

    def bridge_to(self, other, parent, name):
        # A beat-tolerant terminal master consumes the raw beat stream
        # itself: direct bind, no adapter (see the class docstring).
        if self.beat_tolerant and isinstance(other, IoV2Beat):
            return None
        # A beat slave streams per-beat responses and requires beat-form
        # writes — the one response form a transparent chain cannot promise
        # its (unknown) upstream master tolerates. Insert the collapse
        # adapter, exactly as the retired IoV2BigPacket master did: it
        # forwards the access as beats and folds the response stream back
        # into a single reply.
        if isinstance(other, IoV2Beat):
            from utils.io_v2_beat_collapse_adapter import IoV2BeatCollapseAdapter
            return IoV2BeatCollapseAdapter(parent, name, beat_width=other.beat_width)
        # Everything else: transparent, forwards any io_v2 form 1:1 with no
        # adapter.
        if self.is_compatible(other):
            return None
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
