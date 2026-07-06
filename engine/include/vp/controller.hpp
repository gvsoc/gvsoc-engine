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

#include <vp/vp.hpp>
#include <thread>
#include <mutex>
#include <condition_variable>
#include <queue>


namespace gv {

    class ControllerClient;
    class Controller;

    extern gv::Controller controller;

    // Context carried into ClockEngine::step_cycles' C-style callback (void(*)(void*)), which cannot
    // use TimeEvent::get_args() like the time-step path. Heap-allocated by step_cycles_async and
    // freed by step_cycles_async_handler.
    struct StepCyclesReq
    {
        Controller *controller;
        ControllerClient *client;
        void *request;
        // Clock domain the step event is enqueued in, and the event itself, so the step can be
        // canceled on abort. `event` is owned by the clock engine and freed when it fires.
        vp::ClockEngine *clock;
        vp::ClockEvent *event;
    };

    class Logger
    {
    public:
        Logger(std::string module);
        inline void info(const char *fmt, ...);

    private:
        std::string module;
    };

    class Controller
    {
        friend class ControllerClient;

    public:
        Controller();
        void init(gv::GvsocConf *conf);

        static Controller &get() {
            static Controller controller;
            return controller;
        }

        // Open launcher. This instantiate the system and setup simulation loop
        void open(ControllerClient *client);
        // Bind a user controller so that it is notified about event enqueueuing
        void bind(gv::Gvsoc_user *user, ControllerClient *client);
        // Start the system. This build the whole system, call start on it, reset it,
        // and wait proxy connection if needed and check if simulation can be run
        void start(ControllerClient *client);
        // Close system models
        void close(ControllerClient *client);
        // Tear down the whole system and instantiate it again from the stored configuration,
        // leaving the simulation stopped at time 0 as after a fresh open/start.
        // Must be called with the engine locked (see ControllerClient::restart).
        void restart(ControllerClient *client);
        // Run events until a stop request is received.
        int64_t run_sync();
        // Ask internal loop to run events until a stop request is received
        void run_async(ControllerClient *client);
        // Flush system models
        void flush(ControllerClient *client);
        // Terminate simulation. THis will mark it as over and will notified all clients
        void sim_finished(int status);
        // Notify clients that the simulation has paused (stopped but resumable), as opposed to
        // sim_finished which notifies a definitive end.
        void sim_stopped();
        // BLock caller untiul simulation is over and every client is over
        int join(ControllerClient *client);
        // Lock launcher. This is a simple mutex, can be used to enter the launcher when we are
        // sure the engine is stopped, e.g. for a step or a run.
        void lock();
        // Unlock launcher
        void unlock();
        // Stop the engine and lock the launcher. Can be used either when engine is running or
        // stopped but is much expensive that the simple lock
        void engine_lock();
        // Unlock the launcher and run the engine if needed.
        void engine_unlock();
        // Stop the client. This will prevent simulation from running until client is run again
        int64_t stop(ControllerClient *client);
        // Run simulation for specified duration in synchronous mode
        void step_sync(int64_t duration, ControllerClient *client);
        // Run simulation for specified duration in asynchronous mode
        void step_async(int64_t duration, ControllerClient *client, bool wait, void *request);
        // Step a clock domain by exactly count cycles in asynchronous mode, using a clock event
        // enqueued in the domain itself (cycle-accurate, immune to frequency changes).
        void step_cycles_async(int clock_id, int64_t count, ControllerClient *client, bool wait,
            void *request);
        // Run simulation until specified timestamp is reached, in synchronous mode
        void step_until_sync(int64_t timestamp, ControllerClient *client);
        // Run simulation until specified timestamp is reached, in asynchronous mode
        void step_until_async(int64_t timestamp, ControllerClient *client, bool wait, void *request);
        // Run simulation for specified duration in asynchronous mode and blocks caller until
        // step is reached
        void wait_runnable();
        // Must be called when client wants to quit. This will unblock other clients waiting for its
        // termination in the join method
        void client_quit(gv::ControllerClient *client);
        // Add a client. This may stop simulation.
        void register_client(ControllerClient *client);
        // Remove a client. This may stop simulation.
        void unregister_client(ControllerClient *client);
        // Can be called to handle a semi-hosting stop
        void syscall_stop_handle();

        // True when an interactive front-end is attached (a proxy is open:
        // gvsoc-gui3 opens an in-process one, gvconsole connects to one). Used to
        // pause on faults (e.g. memcheck) instead of exiting.
        bool has_frontend() { return this->proxy != NULL; }

        // Register an ELF binary a model has gained access to (statically at reset, or dynamically at
        // run time e.g. via semi-hosting). The path is accumulated in the engine and connected proxy
        // clients are notified that the binary set changed so they can re-query get_binaries() and
        // (e.g. the console) auto-load symbols. Model code must reach this through the component
        // (comp->get_launcher()) rather than Controller::get(): the latter's inline singleton
        // resolves to a separate per-.so instance.
        void declare_binary(const std::string &path);
        // The ELF binaries registered so far (snapshot copy). Queried by proxy clients via the
        // get_binaries proxy command.
        std::vector<std::string> get_binaries();

        // Register a CPU core component (each ISS registers itself), and query the registered cores.
        // Used by proxy clients (e.g. the console) to set breakpoints/watchpoints on every core
        // rather than just one, since each core only checks its own.
        void register_core(vp::Component *core);
        std::vector<vp::Component *> get_cores();

        double get_instant_power(double &dynamic_power, double &static_power, ControllerClient *client);
        double get_average_power(double &dynamic_power, double &static_power, ControllerClient *client);
        void report_start(ControllerClient *client);
        void report_stop(ControllerClient *client);
        gv::PowerReport *report_get(ControllerClient *client);


        void update(int64_t timestamp, ControllerClient *client);

        gv::Io_binding *io_bind(gv::Io_user *user, std::string comp_name, std::string itf_name, ControllerClient *client);
        gv::Wire_binding *wire_bind(gv::Wire_user *user, std::string comp_name, std::string itf_name, ControllerClient *client);

        void vcd_bind(gv::Vcd_user *user, ControllerClient *client);
        // Bind a user controller to receive simulated-software console output.
        void stdout_bind(gv::Stdout_user *user, ControllerClient *client);
        // Forward a chunk of console output: always echoed to host stdout (so the launching
        // terminal / CI keep working), and additionally delivered to the bound Stdout_user, if any.
        // Reached by models through comp->get_launcher() (see declare_binary note above).
        void stdout_dump(int64_t timestamp, const std::string &path, const char *data, int size);
        void vcd_enable(ControllerClient *client);
        void vcd_disable(ControllerClient *client);
        int event_subscribe(std::string pattern,
            gv::Vcd::MatchKind kind, ControllerClient *client);
        int event_unsubscribe(std::string pattern,
            gv::Vcd::MatchKind kind, ControllerClient *client);
        int trace_subscribe(std::string pattern,
            gv::Vcd::MatchKind kind, ControllerClient *client);
        int trace_unsubscribe(std::string pattern,
            gv::Vcd::MatchKind kind, ControllerClient *client);
        void trace_level_set(std::string level, ControllerClient *client);
        void *get_component(std::string path, ControllerClient *client);

        vp::Top *top_get() { return this->handler; }
        ControllerClient *default_client_get() { return this->clients[0]; }

    protected:
        bool is_init = false;

    private:
        // Tells if no client is preventing simulation from being run.
        bool is_runnable();
        // Thread engine entry in asynchronous mode
        void engine_routine();
        static void *signal_routine(void *__this);
        // Check if simulation must be running or stopped depending on client state (stopped/running
        // and lock count)
        void check_run();
        // Mark client as runnable, and enable simulation if needed
        void client_run(ControllerClient *client);
        // Mark client as stopped, and stop simulation if needed
        void client_stop(ControllerClient *client);
        // Static handler used as time event callback, used to stop engine when a step is reached,
        // for asynchronous mode
        static void step_async_handler(vp::Block *__this, vp::TimeEvent *event);
        // Static handler used as time event callback, used to stop engine when a step is reached,
        // for synchronous mode
        static void step_sync_handler(vp::Block *__this, vp::TimeEvent *event);
        // Static handler invoked by ClockEngine::step_cycles when the requested cycle count is
        // reached. Mirrors step_async_handler: stops the client and routes the step-end reply.
        static void step_cycles_async_handler(void *arg);
        // Interrupt the cycle-step outstanding for `client`, if any (the requested count was not
        // reached): cancel its clock event and route an aborted step-end reply carrying `reason`,
        // so a front-end blocked on the step unblocks and can explain why it stopped. No-op if the
        // client has no step pending. Called when a client is stopped with a step outstanding (e.g.
        // toolbar stop); a global stop such as a breakpoint would call this for every client. Must
        // be called with the engine locked/halted (the clock event is canceled).
        void abort_step_cycles(ControllerClient *client, const std::string &reason);
        // Allocate and setup the preallocated synchronous step event of a client.
        // Used when the client registers and again on restart, since step events are
        // attached to the step block of the current system.
        void client_step_event_create(ControllerClient *client);

        // Internal logger
        Logger logger;
        // Main GVSOC controller configuration
        gv::GvsocConf *conf;
        // Path of the configuration the system was instantiated from. Copied from the
        // configuration at init time since the GvsocConf object belongs to the caller and may
        // not outlive init(). Used to instantiate the system again on restart.
        std::string config_path;
        // Top model class containing all engines
        vp::Top *handler;
        // Simulation retval set when simulation terminates
        int retval = -1;
        // User launcher to be notified when an event is enqueued
        gv::Gvsoc_user *user = NULL;
        // User notified about VCD events. Remembered so that it can be bound again to the new
        // trace engine on restart.
        gv::Vcd_user *vcd_user = NULL;
        // User notified about simulated-software console output. Persists across restart since it is
        // owned by the controller, not the trace engine.
        gv::Stdout_user *stdout_user = NULL;
        // Tell if main controller is asynchronous
        bool is_async;
        // True when the configuration contains SystemC components. In this case the engine
        // is not driven by the internal engine thread but by an external SystemC kernel
        // (e.g. the GUI process), so the auto engine thread is not started.
        bool systemc_enabled = false;
        // Thread running the engine in asynchronous mode
        std::thread *engine_thread;
        // Thread handling ctrlC
        std::thread *signal_thread;
        // Top system instance
        vp::Component *instance;
        // Proxy
        GvProxy *proxy;
        // ELF binaries registered by models via declare_binary; the authoritative list returned to
        // proxy clients by get_binaries(). Accumulated here (not in the proxy) so it survives the
        // proxy not yet existing at declaration time and covers binaries added during execution.
        std::vector<std::string> declared_binaries;
        // CPU core components registered via register_core; returned to proxy clients by get_cores().
        std::vector<vp::Component *> cores;
        // When a proxy is enabled, tells whether start() must block until a client connects. True
        // for a config-enabled (standalone) proxy; false for an in-process host (the GUI) so the
        // engine can start without an attached console.
        bool proxy_wait = true;
        // Tell if time engine should be running. In asynchronous mode, there might be a delay
        // with the actual state of the engine
        bool running = false;
        // List of current clients
        std::vector<ControllerClient *> clients;
        // True if simulation has terminated
        bool is_sim_finished = false;
        // Previous "all clients want to run" state, used by check_run to fire has_stopped only
        // on the genuine runnable -> paused transition (ignores transient engine_lock command
        // locks, which don't change run_count).
        bool clients_want_run_prev = false;
        // Mutex used for managing simulation state, i.e. the fields running, run_count and
        // lock_count
        pthread_mutex_t lock_mutex;
        // Time engine mutex. The engine owns the mutex when it is running. Any actor must take
        // it before accessing the models
        pthread_mutex_t mutex;
        // Time engine condition used for various wakeup
        pthread_cond_t cond;
        // Number of runnable clients
        int run_count = 0;
        // Number of locked clients
        int lock_count = 0;
        // True when simulation termination has been received and notified to clients
        bool notified_finish = false;
        vp::Block *step_block;
    };

    class ControllerClient : public gv::Gvsoc
    {
        friend class Controller;

    public:
        ControllerClient(gv::GvsocConf *conf, std::string name="main");
        ~ControllerClient();

        void open() override;
        void bind(gv::Gvsoc_user *user) override;
        void close() override;
        void run() override;
        bool get_memcheck_fault(gv::MemcheckFault &out) override;
        void start() override;
        void restart() override;
        void flush() override;
        int64_t stop() override;
        double get_instant_power(double &dynamic_power, double &static_power) override;
        double get_average_power(double &dynamic_power, double &static_power) override;
        void report_start() override;
        void report_stop() override;
        gv::PowerReport *report_get() override;
        void step(int64_t duration, bool wait=false, void *data=NULL) override;
        // Step clock domain clock_id by exactly count cycles (proxy-internal, not part of the public
        // Gvsoc embedding API). Async only; the proxy always uses async.
        void step_cycles(int clock_id, int64_t count, bool wait=false, void *data=NULL);
        void step_until(int64_t timestamp, bool wait=false, void *data=NULL) override;
        int join() override;
        void lock() override;
        void unlock() override;
        void update(int64_t timestamp) override;
        gv::Io_binding *io_bind(gv::Io_user *user, std::string comp_name, std::string itf_name) override;
        gv::Wire_binding *wire_bind(gv::Wire_user *user, std::string comp_name, std::string itf_name) override;
        void vcd_bind(gv::Vcd_user *user) override;
        void stdout_bind(gv::Stdout_user *user) override;
        void vcd_enable() override;
        void vcd_disable() override;
        int event_subscribe(std::string pattern,
            gv::Vcd::MatchKind kind = gv::Vcd::MatchKind::Exact) override;
        int event_unsubscribe(std::string pattern,
            gv::Vcd::MatchKind kind = gv::Vcd::MatchKind::Exact) override;
        int trace_subscribe(std::string pattern,
            gv::Vcd::MatchKind kind = gv::Vcd::MatchKind::Regex) override;
        int trace_unsubscribe(std::string pattern,
            gv::Vcd::MatchKind kind = gv::Vcd::MatchKind::Regex) override;
        void trace_level_set(std::string level) override;
        void *get_component(std::string path) override;
        void wait_runnable() override;
        void terminate() override;
        void quit(int status) override;
        int64_t get_time() override;
        int64_t get_next_event_time() override;

        // Called by launcher when simulation is over to notify each client
        void sim_finished(int status);

    private:

        // Internal logger
        Logger logger;
        // Client name used for debugging
        std::string name;
        // True when the client has quit
        bool has_quit = false;
        // Return status given when the client has quit
        int status;
        // Tell if the client is runnable or stopped
        bool running = false;
        // Tell if the client is synchronous or asynchronous
        bool async;
        // User controller to be notified when simulation has ended
        gv::Gvsoc_user *user = NULL;
        // Step event used to stop engine when stepping in synchronous mode
        vp::TimeEvent *step_event = NULL;
        // Cycle-step this client currently has outstanding, or nullptr. At most one per client
        // (the client blocks on the reply before issuing another); different clients can each have
        // one on their own clock domain. Set by step_cycles_async, cleared on normal completion
        // (step_cycles_async_handler) or interruption (abort_step_cycles). Under the engine mutex.
        StepCyclesReq *step_cycles_pending = nullptr;
    };

};

inline void gv::Logger::info(const char *fmt, ...)
{
// #ifdef VP_TRACE_ACTIVE
    // fprintf(stderr, "[\033[34m%s\033[0m] ", this->module.c_str());
    // va_list ap;
    // va_start(ap, fmt);
    // if (vfprintf(stderr, fmt, ap) < 0) {}
    // va_end(ap);
// #endif
}
