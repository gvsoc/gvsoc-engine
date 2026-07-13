/*
 * Copyright (C) 2026 ETH Zurich and University of Bologna
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

#include <cstdlib>
#include <fstream>
#include <stdexcept>

#include <vp/runtime_config.hpp>

void vp::RuntimeConfig::load(std::string path)
{
    this->path = path;

    std::ifstream file(path);
    if (!file.is_open())
    {
        // No runtime values for this run, all baked defaults apply
        return;
    }

    std::string line;
    int line_number = 0;
    while (std::getline(file, line))
    {
        line_number++;

        if (line.empty() || line[0] == '#')
        {
            continue;
        }

        size_t pos = line.find('=');
        if (pos == std::string::npos || pos == 0)
        {
            throw std::invalid_argument("Malformed runtime config line (file: " + path
                + ", line: " + std::to_string(line_number) + "): " + line);
        }

        std::string key = line.substr(0, pos);
        if (this->entries.count(key))
        {
            throw std::invalid_argument("Duplicated runtime config key (file: " + path
                + ", line: " + std::to_string(line_number) + "): " + key);
        }

        this->entries[key] = { line.substr(pos + 1) };
    }
}

const std::string *vp::RuntimeConfig::get(const std::string &key)
{
    auto it = this->entries.find(key);
    if (it == this->entries.end())
    {
        return nullptr;
    }

    it->second.consumed = true;
    return &it->second.value;
}

void vp::RuntimeConfig::check_all_consumed()
{
    for (auto &entry : this->entries)
    {
        if (!entry.second.consumed)
        {
            throw std::invalid_argument("Runtime config key was not consumed by any "
                "component, this usually means a typo or a stale file (file: " + this->path
                + ", key: " + entry.first + ")");
        }
    }
}

int64_t vp::RuntimeConfig::to_int64(const std::string &value)
{
    return strtoll(value.c_str(), NULL, 0);
}

double vp::RuntimeConfig::to_double(const std::string &value)
{
    return strtod(value.c_str(), NULL);
}

bool vp::RuntimeConfig::to_bool(const std::string &value)
{
    return value == "true" || value == "True" || value == "1";
}
