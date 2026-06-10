// SPDX-FileCopyrightText: 2026 ETH Zurich, University of Bologna and EssilorLuxottica SAS
//
// SPDX-License-Identifier: Apache-2.0
//
// Authors: Germain Haugou (germain.haugou@gmail.com)

#include <vp/debug_mem.hpp>

#include <algorithm>

// Saturating end-of-region, since the root window is [0, ~0ULL)
static inline uint64_t region_end(uint64_t base, uint64_t size)
{
    uint64_t end = base + size;
    return end < base ? UINT64_MAX : end;
}

void vp::DebugMemIf::debug_mem_regions(std::vector<DebugMemRegion> &regions,
    uint64_t local_base, uint64_t window_size, uint64_t entry_base, int depth)
{
    regions.push_back({ entry_base, window_size, this, local_base });
}

void vp::DebugMemMap::build(vp::DebugMemIf *root)
{
    this->regions.clear();

    std::vector<DebugMemRegion> collected;
    root->debug_mem_regions(collected, 0, ~0ULL, 0, 0);

    for (const DebugMemRegion &region : collected)
    {
        this->insert(region);
    }

    std::sort(this->regions.begin(), this->regions.end(),
        [](const DebugMemRegion &a, const DebugMemRegion &b) { return a.base < b.base; });

    this->built = true;
}

void vp::DebugMemMap::insert(const DebugMemRegion &region)
{
    if (region.size == 0)
    {
        return;
    }

    // First-inserted wins: clip the new region against every region already
    // in the map, which may split it into several fragments.
    std::vector<DebugMemRegion> pending = { region };

    for (const DebugMemRegion &existing : this->regions)
    {
        std::vector<DebugMemRegion> next;

        for (const DebugMemRegion &frag : pending)
        {
            uint64_t frag_end = region_end(frag.base, frag.size);
            uint64_t exist_end = region_end(existing.base, existing.size);

            if (frag_end <= existing.base || exist_end <= frag.base)
            {
                // No overlap
                next.push_back(frag);
                continue;
            }

            if (frag.base < existing.base)
            {
                // Keep the part below the existing region
                next.push_back({ frag.base, existing.base - frag.base,
                    frag.target, frag.target_offset });
            }

            if (frag_end > exist_end)
            {
                // Keep the part above the existing region
                next.push_back({ exist_end, frag_end - exist_end,
                    frag.target, frag.target_offset + (exist_end - frag.base) });
            }
        }

        pending = next;

        if (pending.empty())
        {
            return;
        }
    }

    for (const DebugMemRegion &frag : pending)
    {
        this->regions.push_back(frag);
    }
}

const vp::DebugMemRegion *vp::DebugMemMap::find(uint64_t addr) const
{
    // First region strictly above addr, then step back
    auto it = std::upper_bound(this->regions.begin(), this->regions.end(), addr,
        [](uint64_t addr, const DebugMemRegion &region) { return addr < region.base; });

    if (it == this->regions.begin())
    {
        return NULL;
    }

    const DebugMemRegion *region = &*(it - 1);

    if (addr >= region_end(region->base, region->size))
    {
        return NULL;
    }

    return region;
}

int vp::DebugMemMap::access(uint64_t addr, uint8_t *data, uint64_t size, bool is_write)
{
    while (size > 0)
    {
        const DebugMemRegion *region = this->find(addr);
        if (region == NULL)
        {
            return -1;
        }

        uint64_t offset = addr - region->base;
        uint64_t iter_size = std::min(size, region->size - offset);

        if (region->target->debug_mem_access(region->target_offset + offset,
            data, iter_size, is_write))
        {
            return -1;
        }

        addr += iter_size;
        data += iter_size;
        size -= iter_size;
    }

    return 0;
}
