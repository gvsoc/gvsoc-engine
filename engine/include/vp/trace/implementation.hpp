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


#ifndef __VP_TRACE_IMPLEMENTATION_HPP__
#define __VP_TRACE_IMPLEMENTATION_HPP__

#include "vp/trace/trace_engine.hpp"
#include "vp/clock/clock_engine.hpp"

namespace vp {

    inline TraceEngine *BlockTrace::get_trace_engine()
    {
        return this->engine;
    }

    inline void BlockTrace::register_as_master()
    {
        if (!this->master_registered)
        {
            this->master_registered = true;
            this->engine->register_master(this->top.get_path());
        }
    }

    inline void BlockTrace::declare_access(uint64_t addr, uint64_t size, bool is_write)
    {
        // Fallback registration in case the master did not register at reset; one-time per block, the
        // bool check afterwards is negligible.
        this->register_as_master();
        // Fast path: nothing more to do unless a watchpoint is set. The owning block is the master
        // that made the access, so check_access knows which master hit.
        if (this->engine->watchpoints_active)
        {
            this->engine->check_access(&this->top, addr, size, is_write);
        }
    }
    #ifdef CONFIG_GVSOC_EVENT_ACTIVE
    inline void vp::Event::dump_value(uint8_t *value, int64_t time_delay)
    {
        // Mark that a value has been produced even while inactive, so a later
        // enable can replay the owner's current value (see enable_set).
        this->has_value = true;
        this->is_highz = false;
        EventDumpCallback callback = (EventDumpCallback)this->dump_callback;

        if (callback)
        {
            callback(this, value, time_delay, NULL);
        }
    }
    inline void vp::Event::dump_value(uint8_t *value, uint8_t *flags, int64_t time_delay)
    {
        this->has_value = true;
        this->is_highz = false;
        EventDumpCallback callback = (EventDumpCallback)this->dump_callback;

        if (callback)
        {
            callback(this, value, time_delay, flags);
        }
    }
    inline void vp::Event::dump_highz(int64_t time_delay)
    {
        // Record the high-Z state even while inactive, so a later enable can
        // replay it (see enable_set) — e.g. a signal reset to high-Z before the
        // GUI subscribes.
        this->has_value = true;
        this->is_highz = true;
        EventDumpCallback callback = (EventDumpCallback)this->dump_callback;

        if (callback)
        {
            // Under the 4-state (value_bit, flag_bit) encoding used by the
            // GUI, Z is (flag=1, value=1) per bit. Set both to all-1s so the
            // downstream painters render this as Z rather than X.
            uint64_t highz = (uint64_t)-1;
            callback(this, (uint8_t *)&highz, time_delay, (uint8_t *)&highz);
        }
    }

    inline void vp::Event::dump(const char *value, int64_t time_delay)
    {
        EventDumpCallback callback = (EventDumpCallback)this->dump_callback;

        if (callback)
        {
            callback(this, (uint8_t *)value, time_delay, NULL);
        }
    }
    inline void vp::Event::dump(uint8_t *value, int64_t time_delay)
    {
        this->dump_value(value, time_delay);
    }

    #endif

  inline void vp::Trace::event_highz(int64_t cycle_delay, int64_t time_delay)
  {
  #ifdef VP_TRACE_ACTIVE
    TraceDumpCallback callback = (TraceDumpCallback)this->dump_callback;

    if (callback && this->comp)
    {
      // 4-state encoding: Z is (flag=1, value=1) per bit. Keep this in sync
      // with vp::Event::dump_highz / dump_highz_next so logic_box etc. pick
      // the Z (yellow inactive-line) branch instead of X (red).
      uint64_t highz = (uint64_t)-1;
      callback(this->comp->traces.get_trace_engine(), this, comp->time.get_time() + time_delay,
        comp->clock.get_engine() ? comp->clock.get_cycles() + cycle_delay : -1, (uint8_t *)&highz,
        (uint8_t *)&highz);
    }
  #endif
  }

  inline void vp::Trace::event(uint8_t *value, int64_t cycle_delay, int64_t time_delay)
  {
  #ifdef VP_TRACE_ACTIVE

    TraceDumpCallback callback = (TraceDumpCallback)this->dump_callback;

    if (callback && this->comp)
    {
      uint64_t zero = (uint64_t)0;
      callback(this->comp->traces.get_trace_engine(), this, comp->time.get_time() + time_delay,
        comp->clock.get_engine() ? comp->clock.get_cycles() + cycle_delay: -1, value, (uint8_t *)&zero);
    }
  #endif
  }

  inline void vp::Trace::event_string(const char *value, bool realloc)
  {
  #ifdef VP_TRACE_ACTIVE

    TraceDumpCallback callback = (TraceDumpCallback)this->dump_callback;
    if (callback && this->comp)
    {
      uint64_t zero = (uint64_t)0;
      callback(this->comp->traces.get_trace_engine(), this, comp->time.get_time(),
        comp->clock.get_engine() ? comp->clock.get_cycles() : -1, (uint8_t *)value,
        0);
    }
  #endif
  }

  inline void vp::Trace::event_real(double value)
  {
  #ifdef VP_TRACE_ACTIVE

    TraceDumpCallback callback = (TraceDumpCallback)this->dump_callback;
    if (callback && this->comp)
    {
      uint64_t zero = (uint64_t)0;
      callback(this->comp->traces.get_trace_engine(), this, comp->time.get_time(), comp->clock.get_engine() ? comp->clock.get_cycles() : -1, (uint8_t *)&value, (uint8_t *)&zero);
    }
  #endif
  }


  inline void vp::Trace::user_msg(const char *fmt, ...) {
    #if 0
    fprintf(trace_file, "%ld: %ld: [\033[34m%-*.*s\033[0m] ", comp->clock.get_engine()->time.get_time(), comp->clock.get_engine()->get_cycles(), max_trace_len, max_trace_len, comp->get_path());
    va_list ap;
    va_start(ap, fmt);
    if (vfprintf(trace_file, format, ap) < 0) {}
    va_end(ap);
    #endif
  }

  inline void vp::Trace::fatal(const char *fmt, ...)
  {
    dump_fatal_header();
    va_list ap;
    va_start(ap, fmt);
    if (vfprintf(this->trace_file, fmt, ap) < 0) {}
    va_end(ap);
    // abort() does not flush stdio; without this the message is lost when
    // stdout is a pipe (block-buffered), e.g. under gvrun output capture
    fflush(this->trace_file);
    abort();
  }

  inline void vp::Trace::warning(const char *fmt, ...) {
  #ifdef VP_TRACE_ACTIVE
  	if ((is_active || stream_active) && comp->traces.get_trace_engine()->get_trace_level() >= this->level)
    {
      if (stream_active)
      {
        va_list ap;
        va_start(ap, fmt);
        this->stream_msg(vp::Trace::LEVEL_WARNING, fmt, ap);
        va_end(ap);
      }
      if (!is_active)
      {
        return;
      }
      dump_warning_header();
      va_list ap;
      va_start(ap, fmt);
      if (vfprintf(this->trace_file, fmt, ap) < 0) {}
      va_end(ap);


      if (comp->traces.get_trace_engine()->get_werror())
      {
        exit(1);
      }
    }
  #endif
  }

  inline void vp::Trace::warning(vp::Trace::warning_type_e type, const char *fmt, ...) {
  #ifdef VP_TRACE_ACTIVE
  	if ((is_active || stream_active) && comp->traces.get_trace_engine()->get_trace_level() >= vp::Trace::LEVEL_WARNING)
    {
      if (comp->traces.get_trace_engine()->is_warning_active(type))
      {
        if (stream_active)
        {
          va_list ap;
          va_start(ap, fmt);
          this->stream_msg(vp::Trace::LEVEL_WARNING, fmt, ap);
          va_end(ap);
        }
        if (!is_active)
        {
          return;
        }
        dump_warning_header();
        va_list ap;
        va_start(ap, fmt);
        if (vfprintf(this->trace_file, fmt, ap) < 0) {}
        va_end(ap);

        if (comp->traces.get_trace_engine()->get_werror())
        {
          exit(1);
        }
      }
    }
  #endif
  }

  inline void vp::Trace::msg(const char *fmt, ...)
  {
  #ifdef VP_TRACE_ACTIVE
  	if ((is_active || stream_active) && comp->traces.get_trace_engine()->get_trace_level() >= this->level)
    {
      if (stream_active)
      {
        va_list ap;
        va_start(ap, fmt);
        this->stream_msg(this->level, fmt, ap);
        va_end(ap);
      }
      if (is_active)
      {
        dump_header();
        va_list ap;
        va_start(ap, fmt);
        if (vfprintf(this->trace_file, fmt, ap) < 0) {}
        va_end(ap);
      }
    }
  #endif
  }

  inline void vp::Trace::msg(int level, const char *fmt, ...)
  {
  #ifdef VP_TRACE_ACTIVE
    if ((is_active || stream_active) && comp->traces.get_trace_engine()->get_trace_level() >= level)
    {
      if (stream_active)
      {
        va_list ap;
        va_start(ap, fmt);
        this->stream_msg(level, fmt, ap);
        va_end(ap);
      }
      if (!is_active)
      {
        return;
      }
      dump_header();
      if (level == vp::Trace::LEVEL_ERROR)
      {
        fprintf(this->trace_file, "\033[31m");
      }
      else if (level == vp::Trace::LEVEL_WARNING)
      {
        fprintf(this->trace_file, "\033[33m");
      }
      va_list ap;
      va_start(ap, fmt);
      if (vfprintf(this->trace_file, fmt, ap) < 0) {}
      va_end(ap);
      if (level == vp::Trace::LEVEL_ERROR || level == vp::Trace::LEVEL_WARNING)
      {
        fprintf(this->trace_file, "\033[0m");
      }
    }
  #endif
  }


};

#endif
