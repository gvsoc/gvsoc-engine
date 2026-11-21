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

#include <cstdint>
#include <cstring>

// Layout of the structures below, compiled into each platform tree library
// (vp_platform_tree_version) and checked by the engine when it loads one, so
// that a tree built against another layout is refused instead of misread.
// Bump it at each change of these structures, with the GUI mirror of them
// (TREE_LAYOUT_VERSION in gvsoc/gui src/model_graph.cpp).
//
// All the strings of the tree (component, module, port, mapping, runtime field
// and signature names) are indexes in the string table of the tree
// (vp_platform_tree_strings), -1 for none.
#define VP_COMPONENT_TREE_VERSION 4

namespace vp {

/**
 * @brief A port-to-port binding between two components.
 *
 * Components and ports are given by index: a component is -1 for the component
 * holding the binding ("self"), otherwise the index of the child in its children
 * array, and a port is the index of the port in the ports array of that
 * component node. The signatures are indexes in the string table of the tree,
 * only used by tools like the GUI.
 */
struct TreeBinding
{
    int master_comp;                // -1 for self, otherwise child index
    int master_port;                // Index in the ports of the master node
    int slave_comp;                 // -1 for self, otherwise child index
    int slave_port;                 // Index in the ports of the slave node
    int master_signature;           // Index in the string table, or -1
    int slave_signature;            // Index in the string table, or -1
};

/**
 * @brief Type of a runtime-settable config field.
 */
enum RuntimeFieldType
{
    RUNTIME_STRING,
    RUNTIME_BOOL,
    RUNTIME_INT64,
    RUNTIME_DOUBLE,
    RUNTIME_LIST,
};

/**
 * @brief Metadata for one run-time-settable field of a component config.
 *
 * Generated at compile time from fields marked ``Annotated[T, Runtime]``.
 * At component construction, the engine uses this table to overlay the
 * field's value from the per-run runtime config file onto the component's
 * typed config struct (see vp::RuntimeConfig).
 *
 * For RUNTIME_LIST fields, ``offset`` is the offset of the ``<name>_count``
 * member, ``ptr_offset`` the offset of the element array pointer, and
 * ``elem_fields`` describes the fields of one element struct (scalars and
 * strings only). The list members are left zero for scalar fields.
 */
struct RuntimeField
{
    int name;                       // Field name (last element of the runtime config key), index in the string table
    unsigned int offset;            // Offset of the field (or list count) inside the config struct
    RuntimeFieldType type;          // Type of the overlay
    unsigned int ptr_offset = 0;    // For lists: offset of the element array pointer
    unsigned int elem_size = 0;     // For lists: size of one element struct
    const RuntimeField *elem_fields = nullptr; // For lists: element field descriptors
    int num_elem_fields = 0;        // For lists: number of element field descriptors
};

/**
 * @brief One address-range mapping of an interconnect component.
 *
 * Generated from config fields named ``mappings`` whose entries carry
 * name/base/size. Purely informational for tools (the models read their
 * typed configs directly); exported so the GUI can display memory maps.
 */
struct TreeMapping
{
    int name;                   // Target port name, index in the string table
    uint64_t base;              // Base address
    uint64_t size;              // Range size in bytes (0: catch-all)
};

/**
 * @brief Node in the compiled component tree.
 *
 * Generated per-target by Python. Describes the hierarchy of components
 * with their typed configurations, module names, and bindings.
 * The engine walks this tree when instantiating the system.
 */
struct ComponentTreeNode
{
    int name;                            // Component name (e.g. "rom", "bank_0"), index in the string table
    const void *config;                  // Pointer to config struct (static or constexpr), or nullptr
    const ComponentTreeNode *children;   // Array of child nodes (nullptr if leaf)
    int num_children;                    // Number of children
    int vp_component;                    // Module name (.so), index in the string table, or -1 for default
    const TreeBinding *bindings;         // Array of bindings, or nullptr
    int num_bindings;                    // Number of bindings
    const RuntimeField *runtime_fields;  // Runtime-settable field table, or nullptr
    int num_runtime_fields;              // Number of runtime-settable fields
    const TreeMapping *mappings;         // Address mappings (interconnects), or nullptr
    int num_mappings;                    // Number of address mappings
    const int *ports;                    // Names of the component ports, indexes in the string table, or nullptr
    int num_ports;                       // Number of ports

    /**
     * @brief Find a child node by name.
     * @return Pointer to the child node, or nullptr if not found.
     */
    const ComponentTreeNode *find_child(const char *const *strings, const char *child_name) const
    {
        if (children == nullptr) return nullptr;
        for (int i = 0; i < num_children; i++)
        {
            if (strcmp(strings[children[i].name], child_name) == 0)
                return &children[i];
        }
        return nullptr;
    }
};

} // namespace vp

/**
 * @brief Entry point for the per-target compiled tree.
 *
 * Each target .so exports this function. Returns the root of the tree.
 * Returns nullptr if the target has no compiled tree.
 */
extern "C" const vp::ComponentTreeNode *vp_get_platform_tree();

/**
 * @brief String table of the tree, indexed by the string fields of the tree
 * structures. The number of strings is returned in nb_strings.
 */
extern "C" const char *const *vp_platform_tree_strings(int *nb_strings);
