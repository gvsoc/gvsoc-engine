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

#include <inttypes.h>
#include <stdarg.h>
#include <stdio.h>
#include "vp/memcheck.hpp"
#include "vp/controller.hpp"


static std::string vp_memcheck_format(const char *fmt, ...)
{
    char buffer[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buffer, sizeof(buffer), fmt, ap);
    va_end(ap);
    return std::string(buffer);
}


vp::MemCheck::MemCheck(gv::Controller *launcher)
: launcher(launcher)
{
    // ID 0 means "no buffer", keep the slot unused
    this->buffers.push_back(NULL);
}


bool vp::MemCheck::fault_stop()
{
    // Stop like a watchpoint hit when a front-end is attached (gvsoc-gui3 opens
    // an in-process proxy, gvconsole connects to one), so the fault can be
    // inspected and execution resumed. In batch mode the caller applies the
    // werror policy instead.
    if (this->launcher != NULL && this->launcher->has_frontend())
    {
        this->launcher->syscall_stop_handle();
        return true;
    }
    return false;
}


void vp::MemCheck::declare_region(int mem_id, const std::string &name, uint64_t base,
    uint64_t size)
{
    vp::MemCheckRegion &region = this->regions[mem_id];

    if (!name.empty())
    {
        region.name = name;
    }
    region.base = base;
    region.size = size;
}


uint32_t vp::MemCheck::alloc(int mem_id, uint64_t base, uint64_t size, uint64_t pc,
    uint64_t ra)
{
    if (this->regions.count(mem_id) == 0)
    {
        return 0;
    }

    vp::MemCheckRegion &region = this->regions[mem_id];

    vp::MemCheckBuffer *buffer = new vp::MemCheckBuffer();
    buffer->id = this->buffers.size();
    buffer->mem_id = mem_id;
    buffer->base = base;
    buffer->size = size;
    buffer->alloc_pc = pc;
    buffer->alloc_ra = ra;

    this->buffers.push_back(buffer);
    region.live_buffers[base] = buffer;

    return buffer->id;
}


uint32_t vp::MemCheck::free(int mem_id, uint64_t base, uint64_t size, uint64_t pc,
    uint64_t ra)
{
    if (this->regions.count(mem_id) == 0)
    {
        return 0;
    }

    vp::MemCheckRegion &region = this->regions[mem_id];

    auto it = region.live_buffers.find(base);
    if (it == region.live_buffers.end())
    {
        return 0;
    }

    vp::MemCheckBuffer *buffer = it->second;
    buffer->freed = true;
    buffer->free_pc = pc;
    buffer->free_ra = ra;

    region.live_buffers.erase(it);

    return buffer->id;
}


vp::MemCheckBuffer *vp::MemCheck::get_buffer(uint32_t id)
{
    if (id == 0 || id >= this->buffers.size())
    {
        return NULL;
    }
    return this->buffers[id];
}


vp::MemCheckRegion *vp::MemCheck::get_region(int mem_id)
{
    auto it = this->regions.find(mem_id);
    return it == this->regions.end() ? NULL : &it->second;
}


vp::MemCheckRegion *vp::MemCheck::get_region_at(uint64_t addr)
{
    for (auto &it : this->regions)
    {
        if (addr >= it.second.base && addr < it.second.base + it.second.size)
        {
            return &it.second;
        }
    }
    return NULL;
}


std::string vp::MemCheck::buffer_desc(vp::MemCheckBuffer *buffer)
{
    std::string region_name = "??";
    auto it = this->regions.find(buffer->mem_id);
    if (it != this->regions.end())
    {
        region_name = it->second.name;
    }

    std::string desc = vp_memcheck_format(
        "buffer #%" PRIu32 " (region: %s, base: 0x%" PRIx64 ", size: 0x%" PRIx64
        ", allocated at pc 0x%" PRIx64 " ra 0x%" PRIx64,
        buffer->id, region_name.c_str(), buffer->base, buffer->size,
        buffer->alloc_pc, buffer->alloc_ra);

    if (buffer->freed)
    {
        desc += vp_memcheck_format(", freed at pc 0x%" PRIx64 " ra 0x%" PRIx64,
            buffer->free_pc, buffer->free_ra);
    }

    return desc + ")";
}


void vp::MemCheck::fault_fill(const char *kind, uint64_t addr, uint64_t size,
    bool is_write, uint32_t buffer_id, const std::string &message)
{
    this->fault.valid = true;
    this->fault.kind = kind;
    this->fault.addr = addr;
    this->fault.size = size;
    this->fault.is_write = is_write;
    this->fault.buffer_id = buffer_id;
    this->fault.message = message;
    // The reporting core completes time, core path and pc before stopping
    this->fault.time = 0;
    this->fault.core = "";
    this->fault.pc = 0;
}


bool vp::MemCheck::check_access(uint64_t addr, uint64_t size,
    uint32_t buffer_id, bool is_write, std::string &error)
{
    // No provenance: no check. Accesses without a buffer ID can not be told apart
    // from the allocator's own metadata traffic (free-list headers live inside the
    // free chunks of the heap), so flagging heap accesses which fall outside live
    // buffers would raise false positives on every allocator operation. Coverage
    // relies on the provenance following the pointers, which the free calls
    // deliberately strip ("laundering") before the allocator recycles a chunk.
    if (buffer_id == 0)
    {
        return true;
    }

    vp::MemCheckBuffer *buffer = this->get_buffer(buffer_id);
    if (buffer == NULL)
    {
        return true;
    }

    const char *dir = is_write ? "write" : "read";

    this->fault.valid = false;

    if (buffer->freed)
    {
        error = vp_memcheck_format(
            "Invalid %s of %" PRIu64 " bytes at 0x%" PRIx64
            " through a pointer to freed ", dir, size, addr)
            + this->buffer_desc(buffer);
        this->fault_fill("use-after-free", addr, size, is_write, buffer_id, error);
        return false;
    }

    if (addr < buffer->base || addr + size > buffer->base + buffer->size)
    {
        // When the access lands in another declared region than the buffer's one,
        // report it as a cross-region access naming both
        vp::MemCheckRegion *buffer_region = this->get_region(buffer->mem_id);
        vp::MemCheckRegion *landing_region = this->get_region_at(addr);
        if (landing_region != NULL && landing_region != buffer_region)
        {
            error = vp_memcheck_format(
                "Invalid %s of %" PRIu64 " bytes at 0x%" PRIx64
                " in region %s through a pointer to another region's ",
                dir, size, addr, landing_region->name.c_str())
                + this->buffer_desc(buffer);
            this->fault_fill("cross-region", addr, size, is_write, buffer_id, error);
            return false;
        }

        bool after = addr >= buffer->base + buffer->size;
        uint64_t distance = after ? addr - (buffer->base + buffer->size)
            : buffer->base - addr;
        error = vp_memcheck_format(
            "Invalid %s of %" PRIu64 " bytes at 0x%" PRIx64
            ", %" PRIu64 " bytes %s ", dir, size, addr, distance,
            after ? "after" : "before") + this->buffer_desc(buffer);
        this->fault_fill(after ? "overflow" : "underflow", addr, size, is_write,
            buffer_id, error);
        return false;
    }

    return true;
}
