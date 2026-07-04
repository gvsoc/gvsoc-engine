/*
 * Copyright (C) 2020 GreenWaves Technologies, SAS, SAS, ETH Zurich and University of Bologna
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


#include <pthread.h>
#include <signal.h>
#include <unistd.h>
#include <algorithm>
#include <cstdio>

#include <stdexcept>
#include <vp/vp.hpp>
#include <gv/gvsoc.hpp>
#include <vp/proxy.hpp>
#include <vp/controller.hpp>
#include <vp/proxy_client.hpp>
#include <vp/top.hpp>

static pthread_t sigint_thread;

// Global signal handler to catch sigint when we are in C world and after
// the engine has started.
// Just few pthread functions are signal-safe so just forward the signal to
// the sigint thread so that he can properly stop the engine
static void sigint_handler(int s)
{
    pthread_kill(sigint_thread, SIGINT);
}

// This thread takes care of properly stopping the engine when ctrl C is hit
// so that the python world can properly close everything.
// On first SIGINT, request a gentle quit via stop_req.
// If the engine doesn't respond within 100ms (e.g. a model is stuck in an
// infinite loop), warn the user and wait for a second Ctrl-C to force exit.
void *gv::Controller::signal_routine(void *__this)
{
    Controller *launcher = (Controller *)__this;
    sigset_t sigs_to_catch;
    int caught;
    sigemptyset(&sigs_to_catch);
    sigaddset(&sigs_to_catch, SIGINT);

    do
    {
        sigwait(&sigs_to_catch, &caught);

        vp::TimeEngine *engine = launcher->handler->get_time_engine();

        if (engine->finished_get())
        {
            // Second Ctrl-C after quit was already requested — force exit
            fprintf(stderr, "\n[\033[31mFATAL\033[0m] Forced exit\n");
            _exit(1);
        }

        // First Ctrl-C: gentle quit (sets stop_req + finished)
        engine->quit(-1);

        // Wait 100ms for the engine to process stop_req and exit cleanly
        struct timespec ts = {0, 100000000};
        nanosleep(&ts, NULL);

        // If exec() hasn't returned after 100ms, the engine is likely stuck
        // in a model callback. Warn the user.
        if (!engine->run_returned)
        {
            fprintf(stderr, "\n[\033[33mWARNING\033[0m] Engine is not responding "
                "(likely stuck in a model). Press Ctrl-C again to force quit.\n");
        }

    } while (1);
    return NULL;
}

gv::Controller::Controller()
: logger("LAUNCHER")
{

}

void gv::Controller::syscall_stop_handle()
{
    for (gv::ControllerClient *client: this->clients)
    {
        if (client->user)
        {
            client->user->handle_syscall_stop();
        }
    }

    // Proxy clients are not Gvsoc_users: pause the engine (the last-added client is the proxy's
    // gating one) and push the stop to them over the wire.
    if (this->proxy && !this->clients.empty())
    {
        this->stop(this->clients.back());
        this->proxy->notify_syscall_stop();
    }
}

void gv::Controller::declare_binary(const std::string &path)
{
    // Accumulate the binary (de-duplicated). On a genuinely new one, tell connected proxy clients
    // the set changed so they re-query get_binaries(); a client connecting later picks it up from
    // that query instead. No-op beyond accumulation when no proxy is enabled.
    for (auto &known: this->declared_binaries)
    {
        if (known == path)
        {
            return;
        }
    }
    this->declared_binaries.push_back(path);
    if (this->proxy)
    {
        this->proxy->notify_binaries_changed();
    }
}

std::vector<std::string> gv::Controller::get_binaries()
{
    return this->declared_binaries;
}

void gv::Controller::register_core(vp::Component *core)
{
    for (auto c: this->cores)
    {
        if (c == core)
        {
            return;
        }
    }
    this->cores.push_back(core);
}

std::vector<vp::Component *> gv::Controller::get_cores()
{
    return this->cores;
}

void gv::Controller::init(gv::GvsocConf *conf)
{
    if (!this->is_init)
    {
        this->is_init = true;
        this->conf = conf;
        this->is_async = conf == NULL || conf->api_mode == gv::Api_mode::Api_mode_async;
        pthread_mutex_init(&this->mutex, NULL);
        pthread_mutex_init(&this->lock_mutex, NULL);
        pthread_cond_init(&this->cond, NULL);

        if (conf)
        {
            // Keep a copy of the configuration path: the GvsocConf object belongs to the
            // caller and may not outlive this call, and restart needs the path to
            // instantiate the system again.
            this->config_path = conf->config_path;

            this->handler = new vp::Top(conf->config_path, this->is_async, this);

            this->instance = this->handler->top_instance;
            this->instance->set_launcher(this);

            js::Config *gv_config = this->handler->gv_config;

            // When the platform contains SystemC components, the time engine must be driven
            // by the SystemC kernel rather than by the internal engine thread. The external
            // driver (the gvsoc_launcher in synchronous mode, or the GUI process via
            // systemc_gui_start) takes over running the engine.
            this->systemc_enabled = gv_config->get_child_bool("systemc");

            this->proxy = NULL;
            // The proxy is enabled either through the platform config ("proxy/enabled") or
            // programmatically through GvsocConf (an in-process host, e.g. the GUI). Each session
            // owns a control client that gates the engine against the always-on host (the GUI's
            // database client driving the engine through the gvsoc API, or the standalone main.cpp
            // client).
            bool conf_proxy = this->conf && this->conf->proxy_enabled;
            if (gv_config->get_child_bool("proxy/enabled") || conf_proxy)
            {
                // A programmatic in-process host (e.g. the GUI, via GvsocConf) always wants an
                // ephemeral port and no startup wait (the console is an optional panel opened on
                // demand). A config-enabled (standalone) proxy waits for a connection and takes its
                // listening port from the config.
                int in_port = conf_proxy ? 0 : gv_config->get_child_int("proxy/port");
                this->proxy_wait = !conf_proxy;
                int out_port;
                this->proxy = new GvProxy(this->handler->get_time_engine(), instance, -1, -1);

                if (this->proxy->open(in_port, &out_port))
                {
                    throw runtime_error("Failed to start proxy");
                }

                this->conf->proxy_socket = out_port;
            }

            this->step_block = new vp::Block(NULL, "stepper", this->handler->get_time_engine(),
                this->handler->get_trace_engine(), this->handler->get_power_engine());
        }
    }
}

void gv::Controller::open(ControllerClient *client)
{
    if (this->is_async)
    {
        // Create the sigint thread so that we can properly close simulation
        // in case ctrl C is hit.
        sigset_t sigs_to_block;
        sigemptyset(&sigs_to_block);
        sigaddset(&sigs_to_block, SIGINT);
        pthread_sigmask(SIG_BLOCK, &sigs_to_block, NULL);
        pthread_create(&sigint_thread, NULL, signal_routine, (void *)this);

        signal(SIGINT, sigint_handler);

        // In asynchronous mode, a dedicated thread is running the time engine, unless the
        // platform uses SystemC: in that case the engine is driven by an external SystemC
        // kernel (e.g. the GUI process), which becomes the engine thread itself.
        if (!this->systemc_enabled)
        {
            this->engine_thread = new std::thread(&gv::Controller::engine_routine, this);
#ifndef __APPLE__
            pthread_setname_np(this->engine_thread->native_handle(), "engine");
#endif
        }
    }
    else
    {
        // It is also considered always running since anyway the engine is making progress
        // only when the external loop is running it.
        this->client_run(client);
    }

    this->instance->build_all();
}

void gv::Controller::bind(gv::Gvsoc_user *user, ControllerClient *client)
{
    this->user = user;
    this->handler->get_time_engine()->bind_to_launcher(user);
}

void gv::Controller::start(ControllerClient *client)
{
    this->handler->start();
    this->instance->reset_all(true);
    this->instance->reset_all(false);

    // Now that all initialization are done, wait for at least one proxy connection before
    // running. Skipped for an in-process host (proxy_wait false), where the console is an optional
    // panel opened on demand and must not block engine startup.
    if (this->proxy && this->proxy_wait)
    {
        this->proxy->wait_connected();
    }

    if (!this->is_async)
    {
        // The main loop, either internally in asynchronous or externally in synchronous mode,
        // is by default having the lock so that any client must take it to stop the engine.
        // The main loop will release the lock after a stop request and when it is waiting that
        // that the simulation becomes runnable again.
        pthread_mutex_lock(&this->mutex);
    }

    // Now simulation can start
    this->check_run();
}

void gv::Controller::close(ControllerClient *client)
{
    // Tear down the proxy first so no session thread is left blocked reading its socket: such a
    // thread holds a stdio FILE lock that would deadlock the stdio cleanup run at process exit.
    if (this->proxy)
    {
        this->proxy->stop();
    }

    ((vp::Top *)this->handler)->get_trace_engine()->flush();

    this->instance->stop_all();

    vp::Top *top = (vp::Top *)this->handler;

    // Dump stats before unbuild_all() to avoid reading freed memory.
    // Skip if the user already dumped explicitly via semihosting.
    if (top->get_stats_engine()->is_enabled() && !top->get_stats_engine()->has_dumped())
    {
        top->get_stats_engine()->dump("stats.txt");
    }

    // Same for the power report: dump a whole-run report unless the
    // application already captured its own windows (magic triggers).
    top->get_power_engine()->close();

    this->instance->unbuild_all();

    delete top;
}

void gv::Controller::restart(ControllerClient *client)
{
    // Restart is not supported when the engine is driven by an external SystemC kernel since
    // SystemC time cannot be rewound in-process. The proxy is supported: it only keeps a pointer
    // to the system (repointed below) and its sessions re-fetch the engine each command.
    if (this->systemc_enabled)
    {
        fprintf(stderr, "Ignoring restart request, it is not supported on SystemC platforms\n");
        return;
    }

    this->logger.info("Restarting system\n");

    // Teardown, mirroring close(). The engine is locked so the engine thread is parked
    // outside any model code and will pick up the new system once it resumes.
    // The stats dump done in close() is skipped on purpose since restart discards the run.
    this->handler->get_trace_engine()->flush();
    this->instance->stop_all();
    this->instance->unbuild_all();

    // Cancel any pending step before the step block is destroyed. Heap-allocated
    // asynchronous step events are leaked since only their callback may free them,
    // and we don't want step-end notifications to fire during restart.
    while (this->step_block->time.first_event)
    {
        this->step_block->time.first_event->cancel();
    }

    // Preallocated client step events are attached to the step block of the system
    // being destroyed and must be created again on the new one.
    for (ControllerClient *current: this->clients)
    {
        delete current->step_event;
        current->step_event = NULL;
    }
    delete this->step_block;

    delete this->instance;
    delete this->handler;

    // Back to the state of a freshly opened simulation
    this->is_sim_finished = false;
    this->notified_finish = false;
    this->retval = -1;
    this->clients_want_run_prev = false;

    // A restarted system is paused at time 0: drop every client's run request so nothing resumes on
    // its own. The always-on host re-runs explicitly after restart (the GUI calls run() again); a
    // proxy session (e.g. the console) stays parked until it issues run. Without this, a client that
    // was running before the restart would keep the engine advancing immediately after rebuild.
    this->run_count = 0;
    for (ControllerClient *current: this->clients)
    {
        current->running = false;
    }

    // Rebuild, mirroring init(), open() and start(). The engine and sigint threads are
    // not recreated, they stay around and dereference the new handler once resumed.
    this->handler = new vp::Top(this->config_path, this->is_async, this);
    this->instance = this->handler->top_instance;
    this->instance->set_launcher(this);

    // The proxy holds a pointer to the system being destroyed; repoint it at the rebuilt one so
    // proxy sessions (e.g. the GUI console) keep working across the restart.
    if (this->proxy)
    {
        this->proxy->top = this->instance;
    }

    this->step_block = new vp::Block(NULL, "stepper", this->handler->get_time_engine(),
        this->handler->get_trace_engine(), this->handler->get_power_engine());

    for (ControllerClient *current: this->clients)
    {
        this->client_step_event_create(current);
    }

    // Re-apply the user bindings to the new system
    if (this->user)
    {
        this->handler->get_time_engine()->bind_to_launcher(this->user);
    }
    if (this->vcd_user)
    {
        this->instance->traces.get_trace_engine()->set_vcd_user(this->vcd_user);
    }

    this->instance->build_all();

    // This declares and registers trace events again to the VCD user, with the same
    // paths as the previous system
    this->handler->start();
    this->instance->reset_all(true);
    this->instance->reset_all(false);
}

int64_t gv::Controller::run_sync()
{
    // Since the stop mechanism is lock-free, we need to issue memory barrier to see latest
    // version of running field
    __sync_synchronize();

    // Wait until the simulation becomes runnable.
    // Simulation can be stopped if any client stopped it or locked it.
    while (!this->running)
    {
        pthread_cond_wait(&this->cond, &this->mutex);
    }

    this->logger.info("Running time engine\n");

    // Now run the engine, this will execute events until a stop request is issued.
    // The engine will then execute events until the current timestamp is over and will then return.
    int64_t time = this->handler->get_time_engine()->run();

    // We handle the simulation termination only once to notify everyone.
    // Then simulation can freely continue if needed
    if (!this->notified_finish && this->handler->get_time_engine()->finished_get())
    {
        this->notified_finish = true;
        this->sim_finished(this->handler->get_time_engine()->stop_status);
    }

    return time;
}

void gv::Controller::run_async(ControllerClient *client)
{
    this->client_run(client);
}

void gv::Controller::client_run(ControllerClient *client)
{
    if (!client->running)
    {
        client->running = true;
        this->run_count++;
        this->logger.info("Turn client to running (client: %s, run_count: %d)\n",
            client->name.c_str(), this->run_count);
        this->check_run();
    }
}

void gv::Controller::client_stop(ControllerClient *client)
{
    if (client->running)
    {
        client->running = false;
        this->run_count--;
        this->logger.info("Turn client to stopped (client: %s, run_count: %d)\n",
            client->name.c_str(), this->run_count);
        this->check_run();
    }
}


void gv::Controller::flush(ControllerClient *client)
{
    this->handler->flush();
}

void gv::Controller::sim_finished(int status)
{
    this->logger.info("Simulation has finished\n");

    // When simulation is over, we record the status, mark simulation as finished and notified
    // every client.
    if (!this->is_sim_finished)
    {
        this->is_sim_finished = true;
        this->retval = status;


        // Cancel any pending step. Dequeue before exec: asynchronous step events delete
        // themselves in their callback, so exec must be the last access to the event.
        while (this->step_block->time.first_event)
        {
            vp::TimeEvent *event = this->step_block->time.first_event;
            event->cancel();
            event->exec();
        }

        for (gv::ControllerClient *client: this->clients)
        {
            client->sim_finished(status);
        }

        // Proxy clients (e.g. gvconsole) are not Gvsoc_users; push the exit to them over the wire.
        if (this->proxy)
        {
            this->proxy->send_quit(status);
        }

        pthread_cond_broadcast(&this->cond);
    }
}

void gv::Controller::sim_stopped()
{
    this->logger.info("Simulation has stopped\n");

    // Notify every client that the simulation paused (resumable), mirroring sim_finished which
    // notifies a definitive end via has_ended.
    for (gv::ControllerClient *client: this->clients)
    {
        if (client->user)
        {
            client->user->has_stopped();
        }
    }
}

int gv::Controller::join(ControllerClient *client)
{
    // Make the calling client runnable so that it does not prevent simulation from running for
    // other clients
    this->client_run(client);

    // Wait simulation is over
    while(!this->is_sim_finished)
    {
        if (this->is_async)
        {
            pthread_cond_wait(&this->cond, &this->mutex);
        }
        else
        {
            this->run_sync();
        }
    }

    // Wait for each client to finish and collect status from everyone
    int retval = this->retval;
    for (gv::ControllerClient *wait_client: this->clients)
    {
        while (!wait_client->has_quit)
        {
            pthread_cond_wait(&this->cond, &this->mutex);
        }

        if (wait_client->status != 0)
        {
            retval = wait_client->status;
        }
    }

    return retval;
}

int64_t gv::Controller::stop(ControllerClient *client)
{
    this->client_stop(client);

    // If this client had a cycle-step outstanding, interrupt it so its front-end unblocks instead
    // of staying stuck until the (now never-reached) target cycle. This method runs with the engine
    // locked/halted, so canceling the pending clock event here is safe.
    this->abort_step_cycles(client, "simulation stopped");

    // Since this method must be called with the engine locked, we are sure it is already stopped
    // thus we can safely return the current time as the time where it was stopped.
    return this->handler->get_time_engine()->get_time();
}

void gv::Controller::step_sync(int64_t duration, ControllerClient *client)
{
    this->step_until_sync(this->handler->get_time_engine()->get_time() + duration, client);
}

void gv::Controller::step_async(int64_t duration, ControllerClient *client, bool wait, void *request)
{
    this->step_until_async(this->handler->get_time_engine()->get_time() + duration, client, wait, request);
}

void gv::Controller::step_cycles_async(int clock_id, int64_t count, ControllerClient *client,
    bool wait, void *request)
{
    vp::ClockEngine *clock = this->handler->get_time_engine()->get_clock_engines()[clock_id];

    // Carry the controller/client/request/clock into the C-style clock callback. Freed in the
    // handler. The backing event is filled in just below.
    StepCyclesReq *req = new StepCyclesReq{ this, client, request, clock, nullptr };

    // Enqueue a delayed clock event at current_cycle + count in the domain itself. Its handler
    // pauses the engine on a clean cycle boundary (end of the timestamp) and invokes our callback.
    // The clock engine allocates one event per step, so several clients can have a cycle-step
    // outstanding on the same domain at once.
    req->event = clock->step_cycles(count, &Controller::step_cycles_async_handler, (void *)req);

    // Remember the outstanding step on the client so it can be interrupted if the client is stopped
    // before the target cycle is reached (see abort_step_cycles).
    client->step_cycles_pending = req;

    this->client_run(client);

    if (wait)
    {
        // Block until the handler stops the client (step reached) or the sim ended.
        while (client->running && !this->is_sim_finished)
        {
            pthread_cond_wait(&this->cond, &this->mutex);
        }
    }
}

void gv::Controller::step_cycles_async_handler(void *arg)
{
    StepCyclesReq *req = (StepCyclesReq *)arg;
    Controller *_this = req->controller;
    ControllerClient *client = req->client;
    void *request = req->request;
    _this->logger.info("Step cycles handler\n");

    // The step reached its target: it is no longer outstanding, so a concurrent stop must not also
    // try to abort it.
    client->step_cycles_pending = nullptr;

    _this->client_stop(client);

    for (gv::ControllerClient *c: _this->clients)
    {
        if (c->user)
        {
            c->user->handle_step_end(request);
        }
    }

    // Route the step-end reply to the proxy session that issued the step (same mechanism as the
    // time-based step). No-op unless the proxy shares the host client.
    if (_this->proxy)
    {
        _this->proxy->step_end(request);
    }

    delete req;
}

void gv::Controller::abort_step_cycles(ControllerClient *client, const std::string &reason)
{
    StepCyclesReq *req = client->step_cycles_pending;
    if (req == nullptr)
    {
        return;
    }
    client->step_cycles_pending = nullptr;

    this->logger.info("Aborting step cycles (client: %s, reason: %s)\n", client->name.c_str(),
        reason.c_str());

    // Cancel this step's own event so it does not fire (and send a second, spurious reply) when the
    // engine is later resumed. Other clients' steps on the same domain have their own events and are
    // left untouched.
    req->clock->cancel_step_cycles(req->event);

    void *request = req->request;

    for (gv::ControllerClient *c: this->clients)
    {
        if (c->user)
        {
            c->user->handle_step_end(request);
        }
    }

    // Route the interrupted step-end reply to the issuing proxy session, carrying the reason so the
    // front-end can explain why the step did not complete. Same routing as the normal step-end.
    if (this->proxy)
    {
        this->proxy->step_end(request, reason);
    }

    delete req;
}

void gv::Controller::step_async_handler(vp::Block *__this, vp::TimeEvent *event)
{
    Controller *_this = (Controller *)event->get_args()[0];
    ControllerClient *client = (ControllerClient *)event->get_args()[1];
    _this->logger.info("Step handler\n");
    _this->client_stop(client);

    for (gv::ControllerClient *client: _this->clients)
    {
        if (client->user)
        {
            client->user->handle_step_end(event->get_args()[2]);
        }
    }

    // For a shared proxy session (no client user of its own), the step data is the session; route
    // the step-end reply to it. No-op unless the proxy shares the host client.
    if (_this->proxy)
    {
        _this->proxy->step_end(event->get_args()[2]);
    }

    delete event;
}

void gv::Controller::step_sync_handler(vp::Block *__this, vp::TimeEvent *event)
{
    Controller *_this = (Controller *)event->get_args()[0];
    ControllerClient *client = (ControllerClient *)event->get_args()[1];
    _this->logger.info("Step handler\n");
    _this->handler->get_time_engine()->pause();
}

void gv::Controller::step_until_sync(int64_t end_time, ControllerClient *client)
{
    // Only go a step if the end time is not the present time
    if (end_time > this->instance->time.get_engine()->get_time())
    {
        // The idea to implement the step is to enqueue a time event which will stop the engine
        // when the step timestamp is reached.
        int64_t time = -1;

        // Enqueue the event which will stop the engine exactly when we need
        client->step_event->enqueue(end_time - this->instance->time.get_engine()->get_time());

        // Now let the engine run until we reach the end of step.
        // We may stop several times if other clients ask to, so we need to iterate as long
        // as needed.
        while (this->handler->get_time_engine()->get_time() < end_time && !this->is_sim_finished)
        {
            time = this->run_sync();

        }
    }
}

void gv::Controller::step_until_async(int64_t end_time, ControllerClient *client, bool wait, void *request)
{
    vp::TimeEvent *event = new vp::TimeEvent(this->step_block);
    event->set_callback(this->step_async_handler);
    event->get_args()[0] = this;
    event->get_args()[1] = client;
    event->get_args()[2] = request;

    event->enqueue(end_time - this->instance->time.get_engine()->get_time());

    this->client_run(client);

    if (wait)
    {
        while (this->handler->get_time_engine()->get_time() < end_time && !this->is_sim_finished)
        {
            pthread_cond_wait(&this->cond, &this->mutex);
        }
    }
}

void gv::Controller::wait_runnable()
{
    while (!this->is_runnable())
    {
        pthread_cond_wait(&this->cond, &this->mutex);
    }
}

bool gv::Controller::is_runnable()
{
    return this->run_count == this->clients.size() && this->lock_count == 0;
}

void gv::Controller::check_run()
{
    pthread_mutex_lock(&this->lock_mutex);
    // Simulation can run if all clients are runnable and no one locked the engine.
    bool should_run = this->run_count == this->clients.size() && this->lock_count == 0;

    // Detect the genuine "all clients want to run" -> "paused" transition so we can notify
    // clients with has_stopped(). We key on run_count (the persistent run/stop state) rather
    // than the running flag, so transient engine_lock command locks (which force running false
    // but leave run_count untouched) don't trigger a spurious has_stopped. We do NOT exclude the
    // finished case: it also fires once at the finishing transition (redundant with has_ended but
    // harmless), and crucially it keeps firing for genuine pauses after the run has ended, which a
    // !is_sim_finished guard would suppress for good.
    bool clients_want_run = this->clients.size() > 0 &&
        this->run_count == this->clients.size();
    bool notify_stopped = this->clients_want_run_prev && !clients_want_run;
    // Symmetric paused->running edge, used to push a run notification over the proxy.
    bool notify_running = !this->clients_want_run_prev && clients_want_run;
    this->clients_want_run_prev = clients_want_run;

    this->logger.info("Checking engine (should_run: %d, running: %d, run_count: %d, nb_clients: %d,"
        " lock_count: %d, finished: %d)\n",
        should_run, this->running, this->run_count, this->clients.size(), this->lock_count,
        this->is_sim_finished);

    if (should_run != this->running)
    {
        if (should_run)
        {
            this->logger.info("Enqueue running\n");
            this->running = true;
        }
        else
        {
            this->logger.info("Enqueue stop\n");
            this->running = false;
            // Since the mechanism is lock-free and to make sure the engine thread sees the engine
            // not running after the stop, we need to issue memory barrier
            __sync_synchronize();
            this->handler->get_time_engine()->pause();
        }
        pthread_cond_broadcast(&this->cond);
    }
    pthread_mutex_unlock(&this->lock_mutex);

    // Notify after releasing lock_mutex: user callbacks must not run under the engine lock.
    if (notify_stopped)
    {
        this->sim_stopped();
    }

    // Push run-state changes over the proxy so connected front-ends (GUI button, console prompt)
    // stay in sync regardless of which one drove the engine. No-op unless the proxy shares the
    // host client (see GvProxy::notify_*).
    if (this->proxy)
    {
        if (notify_running) this->proxy->notify_running();
        if (notify_stopped) this->proxy->notify_stopped();
    }
}

void gv::Controller::client_quit(gv::ControllerClient *client)
{
    // Just need to wake other clients, the launcher client code takes care of the rest
    pthread_cond_broadcast(&this->cond);
}

gv::Io_binding *gv::Controller::io_bind(gv::Io_user *user, std::string comp_name, std::string itf_name, ControllerClient *client)
{
    return (gv::Io_binding *)this->instance->external_bind(comp_name, itf_name, (void *)user);
}

gv::Wire_binding *gv::Controller::wire_bind(gv::Wire_user *user, std::string comp_name, std::string itf_name, ControllerClient *client)
{
    return (gv::Wire_binding *)this->instance->external_bind(comp_name, itf_name, (void *)user);
}

void gv::Controller::vcd_bind(gv::Vcd_user *user, ControllerClient *client)
{
    // Remember the user so that it can be bound again to the new trace engine on restart
    this->vcd_user = user;
    this->instance->traces.get_trace_engine()->set_vcd_user(user);
}

void gv::Controller::stdout_bind(gv::Stdout_user *user, ControllerClient *client)
{
    this->stdout_user = user;
}

void gv::Controller::stdout_dump(int64_t timestamp, const std::string &path, const char *data,
    int size)
{
    // Always echo to the host terminal so the launching console / CI keep seeing the output.
    fwrite(data, 1, size, stdout);
    fflush(stdout);

    // Additionally deliver to a bound consumer (e.g. the GUI output panel), if any.
    if (this->stdout_user)
    {
        this->stdout_user->stdout_dump(timestamp, path, data, size);
    }
}

void gv::Controller::vcd_enable(ControllerClient *client)
{
    this->instance->traces.get_trace_engine()->set_global_enable(1);
}

void gv::Controller::vcd_disable(ControllerClient *client)
{
    this->instance->traces.get_trace_engine()->set_global_enable(0);
}

int gv::Controller::event_subscribe(std::string pattern,
    gv::Vcd::MatchKind kind, ControllerClient *client)
{
    return this->instance->traces.get_trace_engine()->event_subscribe(pattern, kind);
}

int gv::Controller::event_unsubscribe(std::string pattern,
    gv::Vcd::MatchKind kind, ControllerClient *client)
{
    return this->instance->traces.get_trace_engine()->event_unsubscribe(pattern, kind);
}


static std::vector<std::string> split(const std::string& s, char delimiter)
{
   std::vector<std::string> tokens;
   std::string token;
   std::istringstream tokenStream(s);
   while (std::getline(tokenStream, token, delimiter))
   {
      tokens.push_back(token);
   }
   return tokens;
}



void gv::Controller::update(int64_t timestamp, ControllerClient *client)
{
    this->handler->get_time_engine()->update(timestamp);
}



void *gv::Controller::get_component(std::string path, ControllerClient *client)
{
    return this->instance->get_block_from_path(split(path, '/'));
}

void gv::Controller::engine_routine()
{
    // The main loop, either internally in asynchronous or externally in synchronous mode,
    // is by default having the lock so that any client must take it to stop the engine.
    // The main loop will release the lock after a stop request and when it is waiting that
    // that the simulation becomes runnable again.
    pthread_mutex_lock(&this->mutex);

    while(1)
    {
        this->run_sync();
    }
}


void gv::Controller::lock()
{
    pthread_mutex_lock(&this->mutex);
}

void gv::Controller::unlock()
{
    pthread_mutex_unlock(&this->mutex);
}

void gv::Controller::engine_lock()
{
    this->logger.info("Engine lock (lock_count: %d)\n", this->lock_count);

    pthread_mutex_lock(&this->lock_mutex);

    this->lock_count++;
    this->running = false;
    __sync_synchronize();

    this->handler->get_time_engine()->pause();

    pthread_mutex_unlock(&this->lock_mutex);

    this->lock();
}

void gv::Controller::engine_unlock()
{
    this->logger.info("Engine unlock (lock_count: %d)\n", this->lock_count);

    pthread_mutex_lock(&this->lock_mutex);
    this->lock_count--;
    pthread_mutex_unlock(&this->lock_mutex);

    this->check_run();

    pthread_cond_broadcast(&this->cond);
    this->unlock();
}

double gv::Controller::get_instant_power(double &dynamic_power, double &static_power, ControllerClient *client)
{
    return this->instance->power.get_instant_power(dynamic_power, static_power);
}

double gv::Controller::get_average_power(double &dynamic_power, double &static_power, ControllerClient *client)
{
    return this->instance->power.get_average_power(dynamic_power, static_power);
}

void gv::Controller::report_start(ControllerClient *client)
{
    this->instance->power.get_engine()->start_capture();
}

void gv::Controller::report_stop(ControllerClient *client)
{
    this->instance->power.get_engine()->stop_capture();
}

gv::PowerReport *gv::Controller::report_get(ControllerClient *client)
{
    return this->instance->power.get_report();
}



extern "C" int gv_api_version()
{
    return GV_API_VERSION;
}

gv::Gvsoc *gv::gvsoc_new(gv::GvsocConf *conf, std::string name)
{
    if (conf && conf->proxy_socket != -1)
    {
        return new Gvsoc_proxy_client(conf);
    }
    else
    {
        return new gv::ControllerClient(conf, name);
    }
}

void gv::Controller::client_step_event_create(ControllerClient *client)
{
    // In synchronous mode, since only one thread is allowed to do the step, we use a single
    // event and preallocate it for performance reason.
    client->step_event = new vp::TimeEvent(this->step_block);
    client->step_event->set_callback(this->step_sync_handler);
    client->step_event->get_args()[0] = this;
    client->step_event->get_args()[1] = client;
}

void gv::Controller::register_client(ControllerClient *client)
{
    this->client_step_event_create(client);

    // Add client to list of current clients
    this->clients.push_back(client);

    // And check if simulation must be stopped because of the client we added
    this->check_run();
}

void gv::Controller::unregister_client(ControllerClient *client)
{
    // Stop the client
    this->client_stop(client);

    // Remove it from list of current clients
    this->clients.erase(
        std::remove(this->clients.begin(), this->clients.end(), client),
        this->clients.end());

    // And check if simulation must be resumed because of the client we removed
    this->check_run();
}
