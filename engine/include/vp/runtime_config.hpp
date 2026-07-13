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

#pragma once

#include <string>
#include <unordered_map>

namespace vp {

/**
 * @brief Per-run runtime configuration values
 *
 * Holds the key/value file which gvrun regenerates at every invocation with
 * the values of the config fields marked runtime (``Annotated[T, Runtime]``
 * on the Python side). Keys are ``<component path>/<field>`` (component path
 * without leading '/'); a list field's own key carries the element count and
 * its elements follow as ``<component path>/<field>/<index>/<subfield>``.
 *
 * The engine overlays these values onto the typed config structs of the
 * compiled component tree at construction (see
 * ``Component::apply_runtime_overrides``), so runtime values never enter
 * the compiled tree nor its signature.
 */
class RuntimeConfig
{
public:
    /**
     * @brief Load a key/value file
     *
     * A missing file is valid and leaves the store empty (all baked defaults
     * apply). A malformed line or a duplicated key raises.
     *
     * @param path Path of the file.
     */
    void load(std::string path);

    /**
     * @brief Get the raw value of a key
     *
     * The key is marked consumed so that leftover keys can be reported at
     * the end of the tree construction.
     *
     * @param key The key.
     * @return The value, or nullptr if the key is absent.
     */
    const std::string *get(const std::string &key);

    /**
     * @brief Raise if any key was never consumed
     *
     * Called once the whole component tree has been built. A leftover key
     * means a typo or a stale file and is reported as a fatal error.
     */
    void check_all_consumed();

    static int64_t to_int64(const std::string &value);
    static double to_double(const std::string &value);
    static bool to_bool(const std::string &value);

private:
    struct Entry
    {
        std::string value;
        bool consumed = false;
    };

    std::unordered_map<std::string, Entry> entries;
    std::string path;
};

} // namespace vp
