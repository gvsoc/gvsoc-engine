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

#include <vp/vp.hpp>
#include <vp/itf/clk.hpp>
#include <vp/trace/trace_engine.hpp>
#include <vector>
#include <thread>
#include <set>
#include <string.h>

int64_t vp::TraceEngine::event_declare(Event *event)
{
    this->events[event->path_get()] = event;
    this->events_array.push_back((vp::Trace *)event);
    this->is_event.push_back(true);
    return this->events_array.size() - 1;
}

vp::Event_file *vp::TraceEngine::get_event_file(std::string path)
{
    auto it = event_files.find(path);
    if (it != event_files.end())
    {
        return it->second;
    }

    std::string format = this->config->get_child_str("**/events/format");

    if (format == "vcd")
    {
        Event_file *event_file = new Vcd_file(path);
        this->event_files[path] = event_file;
        return event_file;
    }
    else
    {
      throw std::invalid_argument("Unknown trace format (name: " + format + ")\n");
    }

    return NULL;
}

bool vp::TraceEngine::event_active_get(std::string full_path, std::string &file_path)
{
    bool enabled = false;
    file_path = this->active_events[full_path];
    if (file_path != "")
    {
    	enabled = true;
    }
    else
    {
        for (auto &x : events_path_regex)
        {
            if ((x.second->is_path && x.second->path == full_path) || regexec(x.second->regex, full_path.c_str(), 0, NULL, 0) == 0)
            {
                file_path = x.second->file_path;
               	enabled = true;
            }
        }
    }

    if (!enabled)
    {
        return false;
    }

    for (auto &x : this->events_exclude_path_regex)
    {
        if (regexec(x.second->regex, full_path.c_str(), 0, NULL, 0) == 0)
        {
            return false;
        }
    }

    return true;
}

void vp::TraceEngine::check_event_active(vp::Event *event)
{
    std::string path = event->path_get();
   	std::string file_path;
    if (this->event_active_get(path, file_path))
	{
	    event->enable_set(true, this->get_event_file(file_path));
        // Pin the event as a synthetic permanent subscriber so the GUI's
        // event_unsubscribe (and the matching enable_set(false)) doesn't tear
        // down a stream the --event=/include_raw filter wants to keep alive.
        event->subscriber_count++;
	}
	else
	{
	    event->enable_set(false);
	}
}

// Enable / disable matching events for the bound Vcd_user. The engine
// currently supports a single Vcd_user at a time, so the `user` argument
// must match `this->vcd_user`; future multi-user work will lift that
// constraint.
//
// Returns the number of events whose enable state transitioned.
static bool path_matches(const std::string &path, const std::string &pattern,
    gv::Vcd::MatchKind kind, regex_t *compiled_regex)
{
    switch (kind)
    {
        case gv::Vcd::MatchKind::Exact:
            return path == pattern;
        case gv::Vcd::MatchKind::Prefix:
            return path.compare(0, pattern.size(), pattern) == 0;
        case gv::Vcd::MatchKind::Regex:
            return compiled_regex && regexec(compiled_regex, path.c_str(),
                0, NULL, 0) == 0;
    }
    return false;
}

int vp::TraceEngine::event_subscribe(std::string pattern, gv::Vcd::MatchKind kind)
{
    // No Vcd_user bound (--vcd-only mode, proxy dynamic subscribe) routes
    // through the file dumper. Default sink is all.vcd, same as the legacy
    // conf_trace() path. With a Vcd_user bound (GUI mode), file is NULL and
    // events flow exclusively through the Vcd_user pipeline.
    vp::Event_file *file_dst = (this->vcd_user == NULL)
        ? this->get_event_file("all.vcd")
        : NULL;

    regex_t compiled_regex;
    bool have_regex = false;
    if (kind == gv::Vcd::MatchKind::Regex)
    {
        if (regcomp(&compiled_regex, pattern.c_str(), REG_EXTENDED | REG_NOSUB) != 0)
        {
            return 0;
        }
        have_regex = true;
    }

    int count = 0;
    for (size_t i = 0; i < this->events_array.size(); i++)
    {
        vp::Trace *trace = this->events_array[i];
        if (!trace) continue;

        if (this->is_event[i])
        {
            // vp::Event entry (used by vp::Signal). Path getter is path_get();
            // activation flips enable_set(true) on the 0->1 refcount edge.
            vp::Event *event = (vp::Event *)trace;
            if (path_matches(event->path_get(), pattern, kind,
                have_regex ? &compiled_regex : NULL))
            {
                if (event->subscriber_count++ == 0)
                {
                    event->enable_set(true, file_dst);
                }
                count++;
            }
        }
        else
        {
            // Legacy vp::Trace entry (vp::Register::reg_event, new_trace_event,
            // event_imiss, pcer_*, …). Path getter is get_full_path();
            // activation flips set_event_active(true) which wires
            // dump_callback so subsequent vp::Trace::event() / event_real() /
            // event_string() calls push values into the trace pipe. With a
            // Vcd_user bound we also invoke event_enable on the 0->1 edge
            // so the GUI's TraceCapturer allocates a DbTrace for the id.
            if (path_matches(trace->get_full_path(), pattern, kind,
                have_regex ? &compiled_regex : NULL))
            {
                if (trace->subscriber_count++ == 0)
                {
                    // Allocate the streaming handle and mark the enable in one call.
                    if (this->vcd_user && trace->user_trace == NULL)
                    {
                        this->event_enable_now(trace, true);
                    }
                    trace->set_event_active(true, file_dst);
                }
                count++;
            }
        }
    }

    if (have_regex) regfree(&compiled_regex);
    return count;
}

int vp::TraceEngine::event_unsubscribe(std::string pattern, gv::Vcd::MatchKind kind)
{
    regex_t compiled_regex;
    bool have_regex = false;
    if (kind == gv::Vcd::MatchKind::Regex)
    {
        if (regcomp(&compiled_regex, pattern.c_str(), REG_EXTENDED | REG_NOSUB) != 0)
        {
            return 0;
        }
        have_regex = true;
    }

    int count = 0;
    for (size_t i = 0; i < this->events_array.size(); i++)
    {
        vp::Trace *trace = this->events_array[i];
        if (!trace) continue;

        if (this->is_event[i])
        {
            vp::Event *event = (vp::Event *)trace;
            if (path_matches(event->path_get(), pattern, kind,
                have_regex ? &compiled_regex : NULL))
            {
                // Mismatched unsubscribe (count already 0) is silently
                // ignored — keeps the counter sane.
                if (event->subscriber_count > 0)
                {
                    if (--event->subscriber_count == 0)
                    {
                        event->enable_set(false);
                    }
                    count++;
                }
            }
        }
        else
        {
            if (path_matches(trace->get_full_path(), pattern, kind,
                have_regex ? &compiled_regex : NULL))
            {
                if (trace->subscriber_count > 0)
                {
                    if (--trace->subscriber_count == 0)
                    {
                        trace->set_event_active(false);
                    }
                    count++;
                }
            }
        }
    }

    if (have_regex) regfree(&compiled_regex);
    return count;
}

void vp::TraceEngine::check_trace_active(vp::Trace *trace, int event)
{
    std::string full_path = trace->get_full_path();

    trace->set_active(false);

    if (event)
    {
    	std::string file_path;
	    if (this->event_active_get(full_path, file_path))
		{
		    trace->set_event_active(true, this->get_event_file(file_path));
            // Same pin-via-refcount idea as check_event_active(): the
            // --event=/include_raw filter is a synthetic permanent subscriber.
            trace->subscriber_count++;
		}
		else
		{
		    trace->set_event_active(false);
		}
    }
    else
    {
        for (auto &x : this->trace_regexs)
        {
            if (regexec(x.second->regex, full_path.c_str(), 0, NULL, 0) == 0)
            {
                std::string file_path = x.second->file_path;
                if (file_path == "")
                {
                    trace->trace_file = stdout;
                }
                else
                {
                    if (this->trace_files.count(file_path) == 0)
                    {
                        FILE *file = fopen(file_path.c_str(), "w");
                        if (file == NULL)
                            throw std::logic_error("Unable to open file: " + file_path);
                        this->trace_files[file_path] = file;
                    }
                    trace->trace_file = this->trace_files[file_path];
                }
                trace->set_active(true);
            }
        }

        for (auto &x : this->trace_exclude_regexs)
        {
            if (regexec(x.second->regex, full_path.c_str(), 0, NULL, 0) == 0)
            {
                if (event)
                    trace->set_event_active(false);
                else
                    trace->set_active(false);
            }
        }
    }
}

const char *vp::TraceEngine::get_string(const char *str)
{
    const char *result = this->strings[str];
    if (result == NULL)
    {
        // If the string is not yet known, allocate it
        result = strdup(str);
        this->strings[result] = result;
    }
    return result;
}

void vp::TraceEngine::check_traces()
{
    for (auto x : this->traces_array)
    {
        this->check_trace_active(x, x->is_event);
    }
    for (size_t i = 0; i < this->events_array.size(); i++)
    {
        Trace *trace = this->events_array[i];
        if (trace && !this->is_event[i])
        {
            this->check_trace_active(trace, trace->is_event);
        }
    }
}

void vp::TraceEngine::reg_trace(vp::Trace *trace, int event, string path, string name)
{
    if (event)
    {
        this->events_array.push_back(trace);
        this->is_event.push_back(false);
        trace->id = this->events_array.size() - 1;
    }
    else
    {
        this->traces_array.push_back(trace);
        trace->id = this->traces_array.size() - 1;
    }

    int len = path.size() + name.size() + 1;

    if (len > max_path_len)
        max_path_len = len;

    string full_path;

    if (name[0] != '/')
        full_path = path + "/" + name;
    else
        full_path = name;

    traces_map[full_path] = trace;
    trace->set_full_path(full_path);
    trace->is_event = event;

    trace->trace_file = stdout;

    this->check_trace_active(trace, event);

    if (trace->get_event_active())
    {
        fprintf(this->trace_file, "%s %s ", path.c_str(), name.c_str());
        if (trace->type == gv::Vcd_event_type_real || trace->width <= 1)
        {
            fprintf(this->trace_file, "%s\n", name.c_str());
        }
        else
        {
            fprintf(this->trace_file, "%s[%d:0]\n", name.c_str(), trace->width-1);
        }
    }
}

vp::TraceEngine::TraceEngine(js::Config *config)
    : config(config), vcd_user(NULL)
{
    pthread_mutex_init(&mutex, NULL);
    pthread_cond_init(&cond, NULL);

    for (int i = 0; i < TRACE_EVENT_NB_BUFFER; i++)
    {
        event_buffers.push(new char[TRACE_EVENT_BUFFER_SIZE]);
    }
    current_buffer = event_buffers.front();
    current_buffer_event = current_buffer;
    event_buffers.pop();
    current_buffer_size = 0;
    current_buffer_remaining_size = TRACE_EVENT_BUFFER_SIZE;
    this->use_external_dumper = config->get_child_bool("events/use-external-dumper");

    this->global_enable = config->get_child_bool("events/enabled");

    thread = new std::thread(&TraceEngine::vcd_routine, this);

#ifndef __APPLE__
    pthread_setname_np(thread->native_handle(), "event_parser");
#endif

    this->trace_format = TRACE_FORMAT_LONG;
    std::string path = "trace_file.txt";
    this->trace_file = fopen(path.c_str(), "w");
    if (this->trace_file == NULL)
    {
        throw std::runtime_error("Error while opening VCD file (path: " + path + ", error: " + strerror(errno) + ")\n");
    }

    for (auto x : config->get("traces/include_regex")->get_elems())
    {
        std::string trace_path = x->get_str();
        this->add_trace_path(0, trace_path);
    }
    for (auto x : config->get("events/include_regex")->get_elems())
    {
        std::string trace_path = x->get_str();
        this->add_trace_path(1, trace_path);
    }
    for (auto x : config->get("events/include_raw")->get_elems())
    {
        std::string file_path, trace_path;

        std::string path = x->get_str();
        int pos = path.find('@');
        if (pos != std::string::npos)
        {
            file_path = path.substr(pos + 1);
            trace_path = path.substr(0, pos);
        }
        else
        {
            file_path = "all.vcd";
            trace_path = path;
        }

        this->active_events[x->get_str()] = std::string(file_path);
    }

    this->werror = config->get_child_bool("werror");
    this->set_trace_level(config->get_child_str("traces/level").c_str());

    this->active_warnings.resize(vp::Trace::WARNING_TYPE_UNCONNECTED_DEVICE + 1);
    this->active_warnings[vp::Trace::WARNING_TYPE_UNCONNECTED_DEVICE] = config->get_child_bool("wunconnected-device");

    this->active_warnings.resize(vp::Trace::WARNING_TYPE_UNCONNECTED_PADFUN + 1);
    this->active_warnings[vp::Trace::WARNING_TYPE_UNCONNECTED_PADFUN] = config->get_child_bool("wunconnected-padfun");

    this->trace_float_hex = config->get_child_bool("traces/float_hex");
    string format = config->get_child_str("traces/format");

    if (format == "short")
    {
        this->trace_format = TRACE_FORMAT_SHORT;
    }
    else
    {
        this->trace_format = TRACE_FORMAT_LONG;
    }

    this->memcheck_enabled = config->get("memcheck")->get_bool();
}

void vp::TraceEngine::init(vp::Component *top)
{
    this->top = top;
    auto vcd_traces = config->get("events/traces");

    if (vcd_traces != NULL)
    {
        for (auto x : vcd_traces->get_childs())
        {
            std::string type = x.second->get_child_str("type");
            std::string path = x.second->get_child_str("path");

            if (type == "string")
            {
                vp::Trace *trace = new vp::Trace();
                top->traces.new_trace_event_string(path, trace);
            }
            else if (type == "int")
            {
                vp::Trace *trace = new vp::Trace();
                top->traces.new_trace_event(path, trace, 32);
                this->init_traces.push_back(trace);
            }
        }
    }
}

void vp::TraceEngine::start()
{
    if (this->use_external_dumper && this->vcd_user)
    {
        // Declare every event (vp::Event + legacy vp::Trace) to the Vcd_user
        // so it can populate its signal-discovery registry (the GUI's signal
        // browser, an FST dumper's symbol table, …) for *every* signal the
        // platform exposes, independently of whether the signal is currently
        // streaming. event_declare is a no-op by default so legacy Vcd_users
        // (which only override event_register) keep working unchanged.
        //
        // Streaming is NOT enabled here. Events flow once a subscriber bumps
        // the per-event refcount through event_subscribe(). The legacy
        // --event=PAT / events/include_raw filter is already a "pinned"
        // subscriber: check_event_active / check_trace_active activate the
        // event AND bump subscriber_count at trace registration time. For
        // those filter-pinned entries we additionally need to register the
        // trace with the Vcd_user (which wasn't bound when reg_trace ran) so
        // its event_update_*() calls have a handle to dispatch on.
        for (size_t i = 0; i < this->events_array.size(); i++)
        {
            Trace *trace = this->events_array[i];
            if (!trace) continue;

            if (this->is_event[i])
            {
                vp::Event *event = (vp::Event *)trace;
                event->declare_to(this->vcd_user);
                // Filter-pinned vp::Event: enable_set(true) already ran (in
                // check_event_active), but vcd_user was NULL back then. Re-run
                // it now WITHOUT bumping subscriber_count so the vcd_user side
                // (external_trace via event_register) gets wired up.
                if (event->subscriber_count > 0 && event->active_get())
                {
                    event->enable_set(true);
                }
            }
            else
            {
                std::string clock_trace_name = "";
                if (trace->comp && trace->comp->clock.get_engine())
                {
                    clock_trace_name =
                        trace->comp->clock.get_engine()->clock_trace.get_path();
                }
                int width = trace->type == gv::Vcd_event_type_real ? 8 :
                            trace->type == gv::Vcd_event_type_string ? 0 :
                            trace->width;
                this->vcd_user->event_declare(trace->get_full_path(),
                    trace->type, width, "", clock_trace_name);
                // Filter-pinned legacy vp::Trace: set_event_active(true, file)
                // already ran (in check_trace_active). Wire it up to vcd_user
                // so streamed values reach the GUI in addition to the file.
                if (trace->subscriber_count > 0 && trace->user_trace == NULL)
                {
                    this->event_enable_now(trace, true);
                }
            }
        }
    }
    for (auto x : this->init_traces)
    {
        x->event_highz();
    }
}



void vp::TraceEngine::add_exclude_path(int events, const char *path)
{
    regex_t *regex = new regex_t();

    if (events)
    {
        char *delim = (char *)::index(path, '@');
        if (delim)
        {
            *delim = 0;
        }

        if (this->events_path_regex.count(path) > 0)
        {
            delete this->events_path_regex[path];
            this->events_path_regex.erase(path);
        }
        else
        {
            this->events_exclude_path_regex[path] = new trace_regex(path, regex, "");
        }
    }
    else
    {
        char *delim = (char *)::index(path, ':');
        if (delim)
        {
            *delim = 0;
        }
        if (this->trace_regexs.count(path) > 0)
        {
            delete this->trace_regexs[path];
            this->trace_regexs.erase(path);
        }
        else
        {
            this->trace_exclude_regexs[path] = new trace_regex(path, regex, "");
        }
    }

    regcomp(regex, path, 0);
}



void vp::TraceEngine::add_path(int events, const char *path, bool is_path)
{
    regex_t *regex = new regex_t();

    if (events)
    {
        const char *file_path = "all.vcd";
        char *delim = (char *)::index(path, '@');
        if (delim)
        {
            *delim = 0;
            file_path = delim + 1;
        }

        if (this->events_exclude_path_regex.count(path) > 0)
        {
            delete this->events_exclude_path_regex[path];
            this->events_exclude_path_regex.erase(path);
        }

        this->events_path_regex[path] = new trace_regex(path, regex, file_path, is_path);
    }
    else
    {
        std::string file_path = "";
        char *dup_path = strdup(path);
        char *sep = strchr(dup_path, ':');
        if (sep)
        {
            *sep = 0;
            file_path = sep + 1;
            path = dup_path;
        }

        if (this->trace_exclude_regexs.count(path) > 0)
        {
            delete this->trace_exclude_regexs[path];
            this->trace_exclude_regexs.erase(path);
        }

        this->trace_regexs[path] = new trace_regex(path, regex, file_path);
    }

    regcomp(regex, path, 0);
}

void vp::TraceEngine::add_trace_path(int events, std::string path)
{
    this->add_path(events, path.c_str());
}

void vp::TraceEngine::add_exclude_trace_path(int events, std::string path)
{
    this->add_exclude_path(events, path.c_str());
}

void vp::TraceEngine::add_paths(int events, int nb_path, const char **paths)
{
    for (int i = 0; i < nb_path; i++)
    {
        add_path(events, paths[i]);
    }
}



void vp::TraceEngine::set_trace_level(const char *trace_level)
{
    if (strcmp(trace_level, "error") == 0)
    {
        this->trace_level = vp::ERROR;
    }
    else if (strcmp(trace_level, "warning") == 0)
    {
        this->trace_level = vp::WARNING;
    }
    else if (strcmp(trace_level, "info") == 0)
    {
        this->trace_level = vp::INFO;
    }
    else if (strcmp(trace_level, "debug") == 0)
    {
        this->trace_level = vp::DEBUG;
    }
    else if (strcmp(trace_level, "trace") == 0)
    {
        this->trace_level = vp::TRACE;
    }
}
