/*
 * Copyright (C) 2020 GreenWaves Technologies, SAS, ETH Zurich and University of Bologna
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


#ifndef __VP_PROXY_HPP__
#define __VP_PROXY_HPP__

#include <mutex>
#include <vp/controller.hpp>

namespace gv {

class GvProxy;

// A proxy session is a pure command issuer + wire-notification sender; it is not a Gvsoc_user.
// The engine's events reach it as wire notifications pushed by the controller through GvProxy
// (step_end / notify_running / notify_stopped / send_quit / notify_syscall_stop), so the in-process
// Gvsoc_user interface stays free for actual embedders (e.g. the GUI's trace database).
class GvProxySession
{
public:
    GvProxySession(GvProxy *proxy, int req_fd, int reply_fd);
    void wait();
    // Write a raw notification line to this session's reply stream. Caller must hold proxy->mutex.
    void notify(const std::string &msg);
    // Send the reply for a finished asynchronous step. Called by the controller via
    // GvProxy::step_end with this session as the step data. When `reason` is non-empty the step
    // was interrupted before completion (e.g. simulation stopped); the reply then carries a
    // `step_stopped=<reason>` field so the front-end can explain why.
    void send_step_reply(const std::string &reason = "");

private:
    void proxy_loop();

    Logger logger;
    GvProxy *proxy;
    std::thread *loop_thread;
    int socket_fd;
    int reply_fd;
    Gvsoc *gvsoc;
    // Request id of the in-flight async step (shared-session path), used to format its reply.
    std::string step_req;
};

class GvProxy
{
public:
    GvProxy(vp::TimeEngine *engine, vp::Component *top, int req_pipe=-1, int reply_pipe=-1);
    int open(int port, int *out_port);
    void send_quit(int status);
    int join();
    bool send_payload(FILE *reply_file, std::string req, uint8_t *payload, int size);
    void wait_connected();
    // Push engine events to every session over the wire. The controller calls these so a session
    // never has to be a Gvsoc_user. notify_running/stopped use the "req=-1;msg=running=1" /
    // "msg=stopped=<t>" format the proxy clients already parse; notify_syscall_stop sends
    // "req=-1;msg=syscall_stop". step_end routes a completed async step to the session that issued
    // it (`data` is the GvProxySession pointer passed as the step data).
    void notify_running();
    void notify_stopped();
    void notify_syscall_stop();
    void step_end(void *data, const std::string &reason = "");
    // Tell every connected session that the engine's set of registered binaries changed, as a
    // "req=-1;msg=binaries_changed" notification. It carries no data: a client re-queries the list
    // with the get_binaries command. Used for binaries added while a client is already connected
    // (e.g. via semi-hosting); clients connecting later get the current set from get_binaries.
    void notify_binaries_changed();
    // Shut the proxy down: stop accepting connections and force any session blocked reading its
    // socket to return so its thread exits. Must be called before process teardown — a thread
    // blocked in fgets() holds the FILE lock, which deadlocks the stdio cleanup run by exit().
    void stop();

    // Use to notify to loop thread to exit
    std::mutex mutex;
    // Set to true qhen proxy side has finished, which means engine can exit
    bool has_finished = false;
    // Exit status sent by proxy to be returned to main
    int exit_status;
    std::condition_variable cond;
    vp::Component *top;


  private:
    void send_reply(std::string msg);

    void listener(void);

    int telnet_socket = -1;
    int socket_port;
    // Set during stop() so the listener thread returns quietly instead of logging an error when
    // its accept() is interrupted by the socket being closed.
    bool stopping = false;

    std::thread *listener_thread = nullptr;

    std::vector<int> sockets;

    Logger logger;
    std::vector<GvProxySession *> sessions;
    int req_pipe;
    int reply_pipe;
};
}

extern gv::GvProxy *proxy;


#endif
