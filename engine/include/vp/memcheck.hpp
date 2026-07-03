/*
 * Copyright (C) 2020 GreenWaves Technologies, SAS, ETH Zurich and
 *                    University of Bologna
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/*
 * Authors: Germain Haugou, GreenWaves Technologies (germain.haugou@greenwaves-technologies.com)
 */

#pragma once

#include <stdint.h>
#include <string>
#include <map>
#include <vector>

namespace gv
{
    class Controller;
};

namespace vp
{

    /**
     * @brief Descriptor of one tracked allocation.
     *
     * Buffer IDs are monotonic and never reused, so a stale ID kept in a register or
     * in the memory shadow keeps identifying its buffer after it has been freed,
     * which is what allows precise use-after-free reports.
     */
    class MemCheckBuffer
    {
    public:
        uint32_t id = 0;
        // Region (declared heap) this buffer was allocated from
        int mem_id = -1;
        // Base address and size, in the global address space seen by the allocator
        uint64_t base = 0;
        uint64_t size = 0;
        bool freed = false;
        // Guest PC and return address of the allocation and free calls, for reports
        uint64_t alloc_pc = 0;
        uint64_t alloc_ra = 0;
        uint64_t free_pc = 0;
        uint64_t free_ra = 0;
    };

    /**
     * @brief One checked memory region, identified by its mem_id.
     *
     * Regions are declared by the platform (utils.memcheck_regions component),
     * with the name and full range of each allocator-backed memory. They are used
     * to name the region a buffer belongs to (and the one a faulty access lands
     * in) and to report allocations against unknown mem_ids. All checking is
     * provenance-based: accesses without a buffer ID are not checked, since the
     * allocator's own metadata traffic (free-list headers inside free chunks)
     * cannot be told apart from application accesses.
     */
    class MemCheckRegion
    {
    public:
        std::string name;
        // Full range of the region, as declared by the platform
        uint64_t base = 0;
        uint64_t size = 0;
        // Live buffers, indexed by base address
        std::map<uint64_t, MemCheckBuffer *> live_buffers;
    };

    /**
     * @brief Description of the last memcheck fault, for front-ends.
     *
     * Filled when a fault is detected; queried by the proxy "memcheck_status"
     * command so consoles and GUIs can render a structured report and navigate to
     * the fault. The buffer details can be retrieved from the registry with
     * buffer_id.
     */
    class MemCheckFaultInfo
    {
    public:
        bool valid = false;
        // Engine time of the faulting access
        int64_t time = 0;
        // Component path of the faulting core
        std::string core;
        // Faulting instruction
        uint64_t pc = 0;
        // Access description
        uint64_t addr = 0;
        uint8_t size = 0;
        bool is_write = false;
        // One of: overflow, underflow, use-after-free, cross-region,
        // uninit-branch, uninit-address
        std::string kind;
        // Faulty buffer, 0 for uninitialized-value faults
        uint32_t buffer_id = 0;
        // Full formatted report
        std::string message;
    };

    /**
     * @brief Global allocation registry for memory checking.
     *
     * One instance per top-level, reached through vp::Block::get_memcheck(). Only
     * accessed from the simulation thread, hence lock-free.
     *
     * Allocators running on the target declare their allocations here (through the
     * ISS semihosting calls). Each allocation gets a buffer ID which the ISS
     * propagates through registers and memory alongside the pointer. Components
     * owning the canonical offset of a region (memories, interconnect inputs) call
     * check_access() on each access to detect overflows, underflows, use-after-free
     * and cross-region accesses, all reported with real physical addresses.
     */
    class MemCheck
    {
    public:
        MemCheck(gv::Controller *launcher);

        /**
         * @brief Declare a checked region.
         *
         * Called at construction by the platform declaration component with the
         * region's name and full range.
         */
        void declare_region(int mem_id, const std::string &name, uint64_t base,
            uint64_t size);

        /**
         * @brief Register an allocation and return its buffer ID.
         *
         * Returns 0 if mem_id does not match any declared region.
         */
        uint32_t alloc(int mem_id, uint64_t base, uint64_t size, uint64_t pc, uint64_t ra);

        /**
         * @brief Mark the allocation at this base address as freed.
         *
         * Returns the ID of the freed buffer, or 0 if no live buffer starts at this
         * address (invalid or double free).
         */
        uint32_t free(int mem_id, uint64_t base, uint64_t size, uint64_t pc, uint64_t ra);

        /**
         * @brief Check one access against the registry.
         *
         * Called by the core models at issue time, with the address folded to its
         * canonical (global) form when the master has aliases.
         *
         * @param addr      Global address of the access.
         * @param size      Access size in bytes.
         * @param buffer_id Provenance of the address, 0 if unknown (no check).
         * @param is_write  True for writes, false for reads.
         * @param error     Filled with the error report when the check fails.
         *
         * @return true if the access is valid, false otherwise.
         */
        bool check_access(uint64_t addr, uint64_t size, uint32_t buffer_id,
            bool is_write, std::string &error);

        MemCheckBuffer *get_buffer(uint32_t id);
        MemCheckRegion *get_region(int mem_id);
        // Find the declared region containing this address, for report naming
        MemCheckRegion *get_region_at(uint64_t addr);

        /**
         * @brief Pause the simulation on a fault when a front-end is attached.
         *
         * When a proxy client is connected (gvsoc-gui3 opens an in-process proxy,
         * gvconsole connects to one), the simulation is stopped like on a
         * watchpoint hit so the fault can be inspected and execution resumed.
         *
         * @return true if a front-end was attached and the simulation stopped;
         *         false in batch mode, where the caller applies the werror policy.
         */
        bool fault_stop();

        // Last detected fault. check_access() fills the access description on
        // failure; the reporting core completes it (core, pc, time) before
        // stopping. Overwritten by each new fault.
        MemCheckFaultInfo fault;

    private:
        std::string buffer_desc(MemCheckBuffer *buffer);
        void fault_fill(const char *kind, uint64_t addr, uint64_t size,
            bool is_write, uint32_t buffer_id, const std::string &message);

        gv::Controller *launcher;
        // Descriptors indexed by ID. Entry 0 is unused since ID 0 means "no buffer".
        std::vector<MemCheckBuffer *> buffers;
        std::map<int, MemCheckRegion> regions;
    };
};
