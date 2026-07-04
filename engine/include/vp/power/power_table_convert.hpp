/*
 * Copyright (C) 2026 GreenWaves Technologies, SAS, ETH Zurich and
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
 * Authors: Germain Haugou (germain.haugou@gmail.com)
 */

#pragma once

#include <cstring>
#include <vector>
#include <vp/vp.hpp>

namespace vp
{
    /**
     * @brief Find a named power source in a generated model config
     *
     * The model config struct must carry the shared power table fields generated
     * from vp.power_config.PowerSourceConfig, i.e. "size_t power_count;" and
     * "const PowerSourceConfig *power;".
     *
     * @param cfg  The generated model config struct
     * @param name Name of the power source to look for
     * @return The power source config, or NULL if not found
     */
    template<typename PCFG>
    inline auto power_source_config_get(const PCFG &cfg, const char *name) -> decltype(cfg.power)
    {
        for (size_t i = 0; i < cfg.power_count; i++)
        {
            if (strcmp(cfg.power[i].name, name) == 0)
            {
                return &cfg.power[i];
            }
        }
        return NULL;
    }

    /**
     * @brief Declare a new power source from a generated PowerSourceConfig
     *
     * Bridge between the generated power table structs (vp.power_config on the
     * Python side) and the power framework. The generated structs are per-target
     * build artifacts, so this helper is duck-typed on the expected field layout
     * (dynamic_unit, dynamic/dynamic_count, leakage/leakage_count with
     * temp/volt/freq/value entries).
     *
     * A NULL config declares an inert source, like the JSON path with a missing
     * config: all accounting calls on the source are no-ops.
     *
     * @param power  The block power (this->power of the model)
     * @param name   Name of the power source
     * @param source Power source to be declared
     * @param cfg    Generated power source config, or NULL
     * @param trace  Optional power trace where the source should account power
     * @return int   0 if the source was properly created, -1 otherwise
     */
    template<typename CFG>
    inline int new_power_source_from_config(vp::BlockPower &power, std::string name,
        vp::PowerSource *source, const CFG *cfg, vp::PowerTrace *trace=NULL)
    {
        if (cfg == NULL)
        {
            return power.new_power_source(name, source, vp::PowerSourceTable(), trace);
        }

        std::vector<vp::PowerTableEntry> dynamic(cfg->dynamic_count);
        std::vector<vp::PowerTableEntry> leakage(cfg->leakage_count);

        for (size_t i = 0; i < cfg->dynamic_count; i++)
        {
            dynamic[i] = { cfg->dynamic[i].temp, cfg->dynamic[i].volt,
                cfg->dynamic[i].freq, cfg->dynamic[i].value };
        }
        for (size_t i = 0; i < cfg->leakage_count; i++)
        {
            leakage[i] = { cfg->leakage[i].temp, cfg->leakage[i].volt,
                cfg->leakage[i].freq, cfg->leakage[i].value };
        }

        vp::PowerSourceTable table = { cfg->dynamic_unit,
            dynamic.data(), dynamic.size(), leakage.data(), leakage.size() };

        return power.new_power_source(name, source, table, trace);
    }
};
