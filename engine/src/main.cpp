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

#include <gv/gvsoc.hpp>
#include <chrono>
#include <stdlib.h>
#include <dlfcn.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>
#include <vp/json.hpp>
#ifdef VP_USE_SYSTEMC
#include "systemc_driver.hpp"
#endif



int main(int argc, char *argv[])
{
    char *config_path = NULL;
    char *runtime_config_path = NULL;
    bool open_proxy = false;

    for (int i=1; i<argc; i++)
    {
        if (strncmp(argv[i], "--config=", 9) == 0)
        {
            config_path = &argv[i][9];
        }
        else if (strncmp(argv[i], "--runtime-config=", 17) == 0)
        {
            runtime_config_path = &argv[i][17];
        }
        else if (strcmp(argv[i], "--proxy") == 0)
        {
            open_proxy = true;
        }
    }

    if (config_path == NULL)
    {
        fprintf(stderr, "No configuration specified, please specify through option --config=<config path>.\n");
        return -1;
    }


#ifdef VP_USE_SYSTEMC
    // In case GVSOC was compiled with SystemC, check if we have at least one SystemC component
    // and if so, forward the launch to the dedicated SystemC launcher
    if (requires_systemc(config_path))
    {
        return systemc_launcher(config_path);
    }
#endif

    gv::GvsocConf conf = { .config_path=config_path };
    if (runtime_config_path != NULL)
    {
        conf.runtime_config_path = runtime_config_path;
    }
    gv::Gvsoc *gvsoc = gv::gvsoc_new(&conf);
    gvsoc->open();

    if (conf.proxy_socket != -1)
    {
        printf("Opened proxy on socket %d\n", conf.proxy_socket);
    }

    gvsoc->start();

    // GVSOC_SIMSPEED set: report the host time spent simulating (from run to
    // the end of the simulation, without the loading of the models) and the
    // simulated time, for simulation speed measurements.
    bool simspeed = getenv("GVSOC_SIMSPEED") != NULL;
    auto run_start = std::chrono::steady_clock::now();

    gvsoc->run();

    gvsoc->quit(0);
    int retval = gvsoc->join();

    double host_s = std::chrono::duration<double>(std::chrono::steady_clock::now() - run_start).count();
    int64_t sim_ps = gvsoc->stop();
    if (simspeed)
    {
        fprintf(stderr, "[gvsoc simspeed] sim_time_ps=%lld host_time_s=%.6f\n", (long long)sim_ps, host_s);
    }
    gvsoc->close();

    return retval;
}
