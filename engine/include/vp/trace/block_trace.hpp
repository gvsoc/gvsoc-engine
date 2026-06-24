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

#ifndef __VP_TRACE_block_trace_HPP__
#define __VP_TRACE_block_trace_HPP__

#include "vp/trace/trace.hpp"
#include "vp/assert.hpp"

using namespace std;

namespace vp
{
    class trace;
    class TraceEngine;
    class Block;

    typedef enum
    {
        ERROR,
        WARNING,
        INFO,
        DEBUG,
        TRACE
    } TraceLevel;

    class BlockTrace
    {

        friend class vp::Component;
        friend class vp::Block;

    public:
        BlockTrace(vp::Block *parent, Block &top, vp::TraceEngine *engine);

        void new_trace(std::string name, Trace *trace, TraceLevel level = vp::TraceLevel::DEBUG);

        void new_trace_event(std::string name, Trace *trace, int width);

        void new_trace_event_string(std::string name, Trace *trace);

        void new_trace_event_real(std::string name, Trace *trace);

        inline TraceEngine *get_trace_engine();

        /**
         * @brief Declare a memory access made by this block, for watchpoint matching.
         *
         * Any master (CPU core, DMA, accelerator) calls this on each memory access it issues so the
         * generic, central watchpoint facility can match it and stop the simulation on a hit —
         * regardless of which master made the access. It is gated by a fast flag, so it is ~free when
         * no watchpoint is set.
         */
        inline void declare_access(uint64_t addr, uint64_t size, bool is_write);

        /**
         * @brief Register this block as a watchpoint-capable master.
         *
         * Makes the block appear in the engine's master list (queryable by a front-end) so a
         * watchpoint can be scoped to it. Call once, e.g. from the block's reset(), so the master is
         * known before the simulation runs. Idempotent; declare_access also calls it as a fallback.
         */
        inline void register_as_master();

        /**
         * @brief Assertion check reported through the block's trace.
         *
         * If the condition is false, the optional printf-style message is
         * printed exactly like a trace line — with timestamp, cycle stamp and
         * the owning block's instance path — and the simulation aborts.
         *
         * It is only active in the "asserts" and "debug" build variants (which
         * define VP_ASSERT_ACTIVE). In every other variant it is an empty inline
         * method, so the call has no cost (for side-effect-free conditions the
         * optimizer removes it entirely).
         *
         *     this->traces.assert(idx < size, "idx=%d out of range %d", idx, size);
         *     _this->traces.assert(cond);   // from a static handler
         *
         * @param cond Condition that must hold.
         * @param fmt  Optional printf-style message describing the assertion.
         */
#ifdef VP_ASSERT_ACTIVE
        void assert(bool cond, const char *fmt = "", ...);
#else
        inline void assert(bool, const char * = "", ...) {}
#endif

        std::map<std::string, Trace *> traces;
        std::map<std::string, Trace *> trace_events;

    protected:
    private:
        void reg_trace(Trace *trace, int event);

        Block &top;

        vp::TraceEngine *engine = NULL;
        // Set once this block has registered itself as a watchpoint-capable master (see
        // declare_access), so we only register it with the engine on its first declared access.
        bool master_registered = false;
    };

};

#endif
