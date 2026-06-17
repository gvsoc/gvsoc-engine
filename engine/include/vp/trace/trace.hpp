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

#ifndef __VP_TRACE_TRACE_HPP__
#define __VP_TRACE_TRACE_HPP__

#include "vp/trace/event_dumper.hpp"
#include <functional>
#include <stdarg.h>
#include <vector>
#include <string_view>

namespace vp {

	class TraceEngine;
	class Trace;
	class Event;

	using TraceParseCallback = uint8_t *(*)(uint8_t *buffer, bool &unlock);
    using TraceDumpCallback = void (*)(vp::TraceEngine*, vp::Trace*, int64_t, int64_t, uint8_t*, uint8_t*);

    using EventParseCallback = uint8_t *(*)(uint8_t *buffer, bool &unlock);
    using EventDumpCallback = void (*)(vp::Event*, uint8_t*, int64_t, uint8_t*);

#define BUFFER_SIZE (1 << 16)

class TraceEngine;
class Component;
class Trace;
class Block;

#ifdef CONFIG_GVSOC_EVENT_ACTIVE
class Event
{
public:
    Event(vp::Block &parent, const char *name, int width=64,
        gv::Vcd_event_type type=gv::Vcd_event_type_logical,
        const char *description=nullptr);
    // To be removed
    inline void dump_value(uint8_t *value, int64_t time_delay=0);
    // 4-state variant: `flags` is a per-bit mask parallel to `value`, same
    // width. For each bit, (flag=0,val=0/1) means logical 0/1, (flag=1,val=0)
    // means X, (flag=1,val=1) means Z.
    inline void dump_value(uint8_t *value, uint8_t *flags, int64_t time_delay=0);
    inline void dump(uint8_t *value, int64_t time_delay=0);
    void dump_next(uint8_t *value, int64_t cycles=1, int64_t time_delay=0);
    inline void dump(const char *value, int64_t time_delay=0);
    inline void dump_highz(int64_t time_delay=0);
    void dump_highz_next();
    std::string path_get();
    void enable_set(bool enabled, vp::Event_file *file=NULL);
    // Register the owner's current-value storage so the value can be replayed
    // on the enable 0->1 edge. Set by vp::Signal to its `value` member, which
    // it keeps up to date on every set() regardless of activation. A late
    // subscriber then sees the current state (e.g. a clock period set once at
    // reset) instead of nothing. NULL (the default) disables the replay for
    // raw events that don't register storage.
    void value_storage_set(uint8_t *value) { this->enable_value = value; }
    // Declare this event to the given Vcd_user (no enablement / streaming
    // wiring). Invoked once per Vcd_user from TraceEngine::start() so the
    // Vcd_user sees every declared signal independently of the
    // regex / include_raw enable filter.
    void declare_to(gv::Vcd_user *user);
    inline bool active_get() { return this->dump_callback != NULL; }
    bool dump_next_values();
    void next_set(vp::Event *next) { this->next = next; }
    vp::Event *next_get() { return this->next; }
    // Cycle at which the pending deferred value (dump_next / dump_highz_next)
    // must be flushed. Used by the clock engine to arm its trace-flush event.
    int64_t next_value_cyclestamp_get() { return this->next_value_cyclestamp; }

    gv::Vcd_event_type type;
    int width;
    const char *description;
    // Number of live subscribers via gv::Vcd::event_subscribe(). The engine
    // owns this counter: ++ on each subscribe match, -- on each unsubscribe
    // match. The 0->1 transition triggers enable_set(true); the 1->0
    // transition triggers enable_set(false). Default 0 means events are
    // suppressed (dump_callback stays NULL) and produce no DB / file output.
    int subscriber_count = 0;
private:
    static void dump_string(vp::Event *event, uint8_t *value, int64_t time_delay, uint8_t *flags);
    static void dump_1(vp::Event *event, uint8_t *value, int64_t time_delay, uint8_t *flags);
    static void dump_8(vp::Event *event, uint8_t *value, int64_t time_delay, uint8_t *flags);
    static void dump_16(vp::Event *event, uint8_t *value, int64_t time_delay, uint8_t *flags);
    static void dump_32(vp::Event *event, uint8_t *value, int64_t time_delay, uint8_t *flags);
    static void dump_64(vp::Event *event, uint8_t *value, int64_t time_delay, uint8_t *flags);
    static void next_value_fill_string(vp::Event *event, uint8_t *value, uint8_t *flags);
    static void next_value_fill_1(vp::Event *event, uint8_t *value, uint8_t *flags);
    static void next_value_fill_8(vp::Event *event, uint8_t *value, uint8_t *flags);
    static void next_value_fill_32(vp::Event *event, uint8_t *value, uint8_t *flags);
    static void next_value_fill_16(vp::Event *event, uint8_t *value, uint8_t *flags);
    static void next_value_fill_64(vp::Event *event, uint8_t *value, uint8_t *flags);
    static uint8_t *parse_string(uint8_t *buffer, bool &unlock);
    static uint8_t *parse_1(uint8_t *buffer, bool &unlock);
    static uint8_t *parse_8(uint8_t *buffer, bool &unlock);
    static uint8_t *parse_16(uint8_t *buffer, bool &unlock);
    static uint8_t *parse_32(uint8_t *buffer, bool &unlock);
    static uint8_t *parse_64(uint8_t *buffer, bool &unlock);

    vp::Block &parent;
    const char *name;
    vp::Event_file *file = NULL;
    EventDumpCallback dump_callback = NULL;
    EventParseCallback parse_callback;
    void (*next_value_fill_callback)(vp::Event *event, uint8_t *value, uint8_t *flags) = NULL;
    int64_t id;
    // Set by enable_set(true) when streaming to a Vcd_user; stay NULL for an event that is never
    // enabled, so enable_set(false) (called by check_event_active during init) skips the disable
    // marker instead of dereferencing a wild pointer.
    void *external_trace = NULL;
    gv::Vcd_user *vcd_user = NULL;
    uint8_t *next_value;
    uint8_t *next_flags;
    vp::Event *next;
    bool has_next_value = false;
    int64_t next_value_cyclestamp;
    // Current-value storage registered via value_storage_set(), replayed on
    // the enable 0->1 edge. NULL when the owner opted out.
    uint8_t *enable_value = NULL;
    // Set on the first dump_value() so the enable replay only fires once a
    // real value has been produced (avoids replaying uninitialized storage).
    bool has_value = false;
    // True when the current value is high-Z (released). The value storage
    // cannot encode Z, so the enable replay needs this to re-emit Z (rather
    // than the stale value bytes) to a late subscriber. `next_is_highz` carries
    // it through a deferred (release_next) dump.
    bool is_highz = false;
    bool next_is_highz = false;
};
#else
class Event {
public:
    Event(vp::Block &parent, std::string_view name, int width=64,
        gv::Vcd_event_type type=gv::Vcd_event_type_logical,
        const char *description=nullptr) {}
    void dump_value(uint8_t *value, int64_t time_delay=0) {}
    void dump_value(uint8_t *value, uint8_t *flags, int64_t time_delay=0) {}
    void dump(uint8_t *value, int64_t time_delay=0) {}
    void dump_next(uint8_t *value, int64_t cycles=1, int64_t time_delay=0) {}
    void dump(const char *value, int64_t time_delay=0) {}
    void dump_highz(int64_t time_delay=0) {}
    void dump_highz_next() {}
    std::string path_get() {return "";}
    void enable_set(bool enabled, vp::Event_file *file=NULL) {}
    void value_storage_set(uint8_t *value) {}
    void declare_to(gv::Vcd_user *user) {}
    inline bool active_get() { return false; }

    gv::Vcd_event_type type=gv::Vcd_event_type_logical;
    int width=0;
    int subscriber_count = 0;
};
#endif

class Trace {

    friend class BlockTrace;
    friend class TraceEngine;

  public:
    static const int LEVEL_ERROR = 0;
    static const int LEVEL_WARNING = 1;
    static const int LEVEL_INFO = 2;
    static const int LEVEL_DEBUG = 3;
    static const int LEVEL_TRACE = 4;

    typedef enum {
        WARNING_TYPE_UNCONNECTED_DEVICE,
        WARNING_TYPE_UNCONNECTED_PADFUN
    } warning_type_e;

    inline void msg(int level, const char *fmt, ...);
    inline void msg(const char *fmt, ...);
    inline void user_msg(const char *fmt, ...);
    inline void warning(const char *fmt, ...);
    inline void warning(warning_type_e type, const char *fmt, ...);
    void force_warning(const char *fmt, ...);
    void force_warning(warning_type_e type, const char *fmt, ...);
    void force_warning_no_error(const char *fmt, ...);
    void force_warning_no_error(warning_type_e type, const char *fmt, ...);
    inline void fatal(const char *fmt, ...);
    void assert_fail(const char *fmt, va_list ap);

    inline void event_highz(int64_t cycle_delay = 0, int64_t time_delay = 0);
    inline void event(uint8_t *value, int64_t cycle_delay = 0, int64_t time_delay = 0);
    inline void event_string(const char *value, bool realloc);
    inline void event_real(double value);

    void register_callback(std::function<void()> callback) { this->callbacks.push_back(callback); }

    inline std::string get_name() { return this->name; }

    void set_full_path(std::string path) { this->full_path = path; }
    std::string get_full_path() { return this->full_path; }

    void dump_header();
    void dump_warning_header();
    void dump_fatal_header();

    void set_active(bool active);
    void set_event_active(bool active, Event_file *file=NULL);
    vp::Trace *next_get() { return this->next; }

#ifndef VP_TRACE_ACTIVE
    inline bool get_active() { return false; }
    inline bool get_active(int level) { return false; }
    inline bool get_event_active() { return false; }
#else
    inline bool get_active() { return is_active; }
    bool get_active(int level);
    inline bool get_event_active() { return this->dump_callback != NULL; }
#endif
    bool is_active = false;

    int width;
    int id;
    void *user_trace = NULL;
    FILE *trace_file = stdout;
    int is_event;
    gv::Vcd_event_type type;
    // Same role as vp::Event::subscriber_count — refcount of live
    // gv::Vcd::event_subscribe() callers matching this legacy trace's path.
    int subscriber_count = 0;

  protected:
    int level;
    Component *comp = NULL;
    TraceDumpCallback dump_callback = NULL;
    TraceParseCallback parse_callback;
    bool is_event_active = false;
    std::string name;
    std::string path;
    Trace *next;
    std::string full_path;
    std::vector<std::function<void()>> callbacks;
    Event_file *file;
};

// the static_cast<vp_trace&> is here to fix a weird issue with the -Wnonnull
// warning on GCC11. GCC says that trace_ptr is null, but we verified in the if
// condition that it is not null.
// The static_cast is used to avoid disabling the warning completely.
#define vp_assert_always(cond, trace_ptr, msg...)                                                  \
    if (!(cond)) {                                                                                 \
        if (trace_ptr) {                                                                           \
            vp::Trace *trace_p = trace_ptr;                                                        \
            (static_cast<vp::Trace &>(*trace_p)).fatal(msg);                                       \
        } else {                                                                                   \
            fprintf(stdout, "ASSERT FAILED: ");                                                    \
            fprintf(stdout, msg);                                                                  \
            abort();                                                                               \
        }                                                                                          \
    }

#define vp_warning_always(trace_ptr, msg...)                                                       \
    if (trace_ptr)                                                                                 \
        ((vp::Trace *)(trace_ptr))->force_warning(msg);                                            \
    else {                                                                                         \
        fprintf(stdout, "WARNING: ");                                                              \
        fprintf(stdout, msg);                                                                      \
        abort();                                                                                   \
    }

#ifndef VP_TRACE_ACTIVE
#define vp_assert(cond, trace, msg...)
#else
#define vp_assert(cond, trace_ptr, msg...) vp_assert_always(cond, trace_ptr, msg)
#endif

void fatal(const char *fmt, ...);

}; // namespace vp

#endif
