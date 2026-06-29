/*
 * Copyright (C) 2020  GreenWaves Technologies, SAS, SAS, ETH Zurich and University of Bologna
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

#pragma once

/*
 * Assertions are provided as the vp::BlockTrace method assert(), reached through
 * a block's `traces` member:
 *
 *     this->traces.assert(cond);
 *     this->traces.assert(cond, "idx=%d out of range %d", idx, size);
 *     _this->traces.assert(cond, "...");   // from a static handler holding a Block*
 *
 * On failure it reports through the block's own trace, so it is printed exactly
 * like a trace line — with timestamp, cycle stamp and the failing component
 * instance path — and then aborts. See vp::BlockTrace::assert() in
 * vp/trace/block_trace.hpp.
 *
 * It is only compiled in for the "asserts" and "debug" build variants (which
 * define VP_ASSERT_ACTIVE); in every other variant the method is an empty inline
 * and the call has no cost.
 *
 * This header is included by vp/trace/block_trace.hpp, before the BlockTrace
 * class is declared. We drop any libc assert() macro here so that the `assert`
 * method name parses correctly; libc's <cassert>/<assert.h> remains
 * re-includable afterwards for code that wants the standard assert().
 */

#ifdef assert
#undef assert
#endif
