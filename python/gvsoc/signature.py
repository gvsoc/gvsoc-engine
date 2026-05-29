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

    def is_compatible(self, other):
        """Return True if a master with ``self`` can bind directly to a slave with ``other``."""
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

    def is_compatible(self, other):
        # IoV2Sync is a tighter version of the same response surface; a
        # big-packet master is already prepared to handle the sync DONE
        # response form, so this binds directly.
        return isinstance(other, (IoV2BigPacket, IoV2Sync))


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

    def is_compatible(self, other):
        return isinstance(other, IoV2Beat) and other.beat_width == self.beat_width

    def bridge_to(self, other, parent, name):
        # Same-mode peer (IoV2Beat <-> IoV2Beat, same beat_width): no adapter.
        if self.is_compatible(other):
            return None
        # Mismatched mode (IoV2Beat master <-> IoV2BigPacket / IoV2Sync slave,
        # or legacy ``'io_v2'`` string slave that is by default big-packet):
        # the adapter normalises the slave's response into a uniform per-beat
        # stream. The legacy string is the historic v2 default and matches
        # IoV2BigPacket semantically — the slave is free to answer in any of
        # the three response forms, including the sync DONE that beat-fidelity
        # masters cannot consume directly. ``IoV2Sync`` is a strict subset
        # of that surface (DONE only), so the same adapter handles it.
        if isinstance(other, (IoV2BigPacket, IoV2Sync)) or other == IoV2BigPacket.tag:
            from utils.io_v2_beat_adapter import IoV2BeatAdapter
            return IoV2BeatAdapter(parent, name, beat_width=self.beat_width)
        # IoV2Beat <-> IoV2Beat with differing widths is a SoC design error,
        # not a missing adapter.
        return super().bridge_to(other, parent, name)
