// SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
//
// SPDX-License-Identifier: Apache-2.0
//
// Authors: Germain Haugou (germain.haugou@gmail.com)

/*
 * Backdoor debug-memory access.
 *
 * Debug tools (gvcontrol proxy mem_read/mem_write, GDB server) must be able
 * to access memories while the simulation is paused. The timed io_v2 path
 * cannot serve them: buffering routers answer GRANTED/DENIED and complete
 * from clock events, which never fire while paused.
 *
 * Instead, components implementing vp::DebugMemIf form an out-of-band,
 * zero-time access path:
 *
 *  - Terminal components (memories) implement debug_mem_access() as a direct
 *    access to their backing storage, and keep the default
 *    debug_mem_regions() which advertises them as one flat region.
 *  - Interconnects override debug_mem_regions() to walk their mappings and
 *    recurse into the component bound behind each output port (resolved
 *    through MasterPort::get_final_ports()), composing address translations.
 *
 * An entry component (typically the router receiving the proxy command or
 * the one bound to a core's debug port) builds a DebugMemMap once, on first
 * debug access: a flat, sorted list of regions in its own address space,
 * each pointing to a terminal DebugMemIf. Accesses are then a region lookup
 * plus a virtual call into the terminal — no timing, no simulation advance.
 *
 * Regions whose target does not implement the interface are left unmapped
 * and accesses to them fail, so tools get an error instead of a hang.
 * The map is built once; runtime remapping is not supported.
 */

#pragma once

#include <stdint.h>
#include <vector>

namespace vp
{

class DebugMemIf;

/**
 * @brief One flat backdoor region, in the entry component's address space.
 */
struct DebugMemRegion
{
    // Base address in the entry component's address space
    uint64_t base;
    // Region size in bytes
    uint64_t size;
    // Terminal provider serving this region
    DebugMemIf *target;
    // Target-local address corresponding to `base`. A backdoor access at
    // entry address A is served at target address target_offset + (A - base).
    uint64_t target_offset;
};

/**
 * @brief Out-of-band debug-memory access interface
 *
 * Components supporting backdoor debug accesses inherit this class and
 * return it from vp::Block::debug_mem_if().
 */
class DebugMemIf
{
public:
    virtual ~DebugMemIf() = default;

    /**
     * @brief Zero-time backdoor access at this component's local address
     *
     * The caller must hold the engine lock. Implementations must not apply
     * any timing, must not schedule events and must not advance the
     * simulation in any way.
     *
     * @param addr Local address of the access.
     * @param data Data buffer to be read or written.
     * @param size Size of the access in bytes.
     * @param is_write True for a write, false for a read.
     * @return 0 on success, -1 on error (unmapped, out of bounds,
     *     unsupported).
     */
    virtual int debug_mem_access(uint64_t addr, uint8_t *data, uint64_t size,
        bool is_write) = 0;

    /**
     * @brief Emit the flat backdoor regions behind a local window
     *
     * Emits regions covering the local window [local_base, local_base +
     * window_size), which the caller exposes at entry_base in the entry
     * component's address space (local address A maps to entry address
     * entry_base + (A - local_base)).
     *
     * The default implementation emits this component as a single terminal
     * region. Interconnects override it to recurse into the components bound
     * behind their output ports, with their address translation applied.
     *
     * @param regions Vector where regions are accumulated.
     * @param local_base Base of the window, in this component's address space.
     * @param window_size Size of the window in bytes.
     * @param entry_base Address of the window in the entry address space.
     * @param depth Recursion depth, capped to break routing-graph cycles.
     */
    virtual void debug_mem_regions(std::vector<DebugMemRegion> &regions,
        uint64_t local_base, uint64_t window_size, uint64_t entry_base, int depth);

    // Recursion limit for debug_mem_regions, to break routing-graph cycles
    static constexpr int MAX_DEPTH = 32;
};

/**
 * @brief Flat backdoor address map of an entry component
 *
 * Holds the sorted, non-overlapping flat regions collected from a root
 * DebugMemIf. Built once, lazily, on first debug access.
 */
class DebugMemMap
{
public:
    /**
     * @brief Build the map by collecting the root's regions
     *
     * Regions are inserted in emission order with first-inserted-wins
     * clipping, so providers must emit higher-priority windows first (e.g.
     * explicit router mappings before catch-all ones).
     *
     * @param root Entry component whose address space the map describes.
     */
    void build(DebugMemIf *root);

    bool is_built() const { return this->built; }

    /**
     * @brief Backdoor access at an entry-space address
     *
     * Splits the access over the regions it spans and forwards each part to
     * its terminal target.
     *
     * @return 0 on success, -1 if any part hits a gap or the target fails.
     */
    int access(uint64_t addr, uint8_t *data, uint64_t size, bool is_write);

private:
    void insert(const DebugMemRegion &region);
    const DebugMemRegion *find(uint64_t addr) const;

    // Sorted by base, non-overlapping
    std::vector<DebugMemRegion> regions;
    bool built = false;
};

}; // namespace vp
