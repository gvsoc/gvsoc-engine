cmake_minimum_required(VERSION 3.16.3)

set (CMAKE_CXX_STANDARD 17)
set (CMAKE_CXX_STANDARD_REQUIRED ON)

set(CMAKE_INSTALL_MESSAGE LAZY)

if (NOT CMAKE_SKIP_INSTALL_RPATH)

# use, i.e. don't skip the full RPATH for the build tree
set(CMAKE_SKIP_BUILD_RPATH FALSE)

# when building, don't use the install RPATH already
# (but later on when installing)
set(CMAKE_BUILD_WITH_INSTALL_RPATH FALSE)
set(CMAKE_INSTALL_RPATH "${CMAKE_INSTALL_PREFIX}/lib")

# add the automatically determined parts of the RPATH
# which point to directories outside the build tree to the install RPATH
set(CMAKE_INSTALL_RPATH_USE_LINK_PATH TRUE)

endif()


project(gvsoc-core)

# 2.2 Misc
#
# import_kconfig(<prefix> <kconfig_fragment> [<keys>])
#
# Parse a KConfig fragment (typically with extension .config) and
# introduce all the symbols that are prefixed with 'prefix' into the
# CMake namespace. List all created variable names in the 'keys'
# output variable if present.
function(import_kconfig prefix kconfig_fragment list_name)
  # Parse the lines prefixed with 'prefix' in ${kconfig_fragment}
  file(
    STRINGS
    ${kconfig_fragment}
    DOT_CONFIG_LIST
    REGEX "^${prefix}"
    ENCODING "UTF-8"
  )

  foreach (CONFIG ${DOT_CONFIG_LIST})
    # CONFIG could look like: CONFIG_NET_BUF=y

    # Match the first part, the variable name
    string(REGEX MATCH "[^=]+" CONF_VARIABLE_NAME ${CONFIG})

    # Match the second part, variable value
    string(REGEX MATCH "=(.+$)" CONF_VARIABLE_VALUE ${CONFIG})
    # The variable name match we just did included the '=' symbol. To just get the
    # part on the RHS we use match group 1
    set(CONF_VARIABLE_VALUE ${CMAKE_MATCH_1})

    if("${CONF_VARIABLE_VALUE}" MATCHES "^\"(.*)\"$") # Is surrounded by quotes
      set(CONF_VARIABLE_VALUE ${CMAKE_MATCH_1})
    endif()

    if("${CONF_VARIABLE_NAME}" STREQUAL "CONFIG_COMPONENTS")
        string(REPLACE " "  ";" COMPONENT_LIST "${CONF_VARIABLE_VALUE}")
        foreach (COMPONENT ${COMPONENT_LIST})
            list(FIND ${list_name} "${COMPONENT}" INDEX)

            # Check if "Orange" was not found and append it
            if(INDEX EQUAL -1)
                list(APPEND ${list_name} "${COMPONENT}")
            endif()

            set(${list_name} "${${list_name}}" PARENT_SCOPE)
        endforeach()
    else()
        set("${CONF_VARIABLE_NAME}" "${CONF_VARIABLE_VALUE}" PARENT_SCOPE)
    endif()

    list(APPEND keys "${CONF_VARIABLE_NAME}")
  endforeach()

  foreach(outvar ${ARGN})
    set(${outvar} "${keys}" PARENT_SCOPE)
  endforeach()
endfunction()

# GVSOC needs to be given a config file containing all the components to be compiled

foreach(subdir ${GVSOC_MODULES})
    set(GVRUN_TARGETS_DIR_OPT ${GVRUN_TARGETS_DIR_OPT} --target-dir=${subdir})
endforeach()

# Extra modules are only visible to the config generation and to the generated
# components, not to the modules models. This keeps the models compile commands
# stable when extra modules are given (e.g. by testsuite builds), so that they
# do not get recompiled and reinstalled.
foreach(subdir ${GVSOC_EXTRA_MODULES})
    set(GVRUN_TARGETS_DIR_OPT ${GVRUN_TARGETS_DIR_OPT} --target-dir=${subdir})
endforeach()

# Target directories (e.g. of a test) only bring targets and the components
# compiled from their own sources. Unlike extra modules, they are not on the
# include path of the other generated components, which then keep the same
# compile command and are not rebuilt and reinstalled, while other simulations
# may be loading them, when a test builds its target in the shared build.
set(GVSOC_TARGETS_DIRS_REAL)
foreach(subdir ${GVSOC_TARGETS_DIRS})
    set(GVRUN_TARGETS_DIR_OPT ${GVRUN_TARGETS_DIR_OPT} --target-dir=${subdir})
    get_filename_component(subdir_real "${subdir}" REALPATH)
    list(APPEND GVSOC_TARGETS_DIRS_REAL "${subdir_real}")
endforeach()

set(TARGET_CONFIG_DIR ${CMAKE_BINARY_DIR}/gvsoc/configs)
file(MAKE_DIRECTORY ${TARGET_CONFIG_DIR})

set(COMPONENTS)
set(TREE_SOURCES)

# Lists the components of a target on a platform and generates its compiled tree
# (<config>.tree.cpp). The RTL platform (the board running the RTL simulator) gets
# its own tree, named with a .rtl suffix. The command is only queued here: the
# queued commands all run in parallel afterwards (gvsoc_run_target_components).
set(TARGET_CONFIGS)
set(GVSOC_GEN_COMMANDS_FILE ${TARGET_CONFIG_DIR}/components_commands.txt)
file(WRITE ${GVSOC_GEN_COMMANDS_FILE} "")

macro(gvsoc_target_components target platform suffix)
    string(REPLACE ","  "_" TARGET_NAME "${target}")
    string(REPLACE "/"  "." TARGET_NAME "${TARGET_NAME}")
    string(REPLACE ":"  "_" TARGET_NAME "${TARGET_NAME}")
    set(TARGET_CONFIG ${TARGET_CONFIG_DIR}/${TARGET_NAME}${suffix}.config)
    list(APPEND TREE_SOURCES ${TARGET_CONFIG_DIR}/${TARGET_NAME}${suffix}.tree.cpp)
    list(APPEND TARGET_CONFIGS ${TARGET_CONFIG})

    # gvrun extracts the list of components out of the specified target
    set(GVRUN_CMD gvrun --target=${target} ${GVRUN_TARGETS_DIR_OPT}
        --platform=${platform}
        --builddir=${CMAKE_CURRENT_BINARY_DIR}
        --installdir=${CMAKE_INSTALL_PREFIX}/models
        --component-file=${TARGET_CONFIG} components
        --py-stack
    )

    string(JOIN "\t" GVRUN_CMD_LINE ${GVRUN_CMD})
    file(APPEND ${GVSOC_GEN_COMMANDS_FILE} "${GVRUN_CMD_LINE}\n")
endmacro()

# Runs the queued component generations, in parallel (GVSOC_GEN_JOBS jobs, the
# number of cores by default), then imports the components of each target
macro(gvsoc_run_target_components)
    if(NOT DEFINED GVSOC_GEN_JOBS)
        cmake_host_system_information(RESULT GVSOC_GEN_JOBS QUERY NUMBER_OF_LOGICAL_CORES)
    endif()

    message(STATUS "Generating the components of the targets (${GVSOC_GEN_JOBS} jobs), "
        "commands in ${GVSOC_GEN_COMMANDS_FILE}")

    execute_process (
        COMMAND ${CMAKE_CURRENT_SOURCE_DIR}/bin/gvsoc-gen-components
            --jobs=${GVSOC_GEN_JOBS}
            --times=${TARGET_CONFIG_DIR}/components_times.json
            ${GVSOC_GEN_COMMANDS_FILE}
        RESULT_VARIABLE ret
    )
    if(NOT ret EQUAL "0")
        message( FATAL_ERROR "Caught error while generating gvsoc config")
    endif()

    foreach(target_config ${TARGET_CONFIGS})
        import_kconfig(CONFIG_ ${target_config} "COMPONENTS")
    endforeach()
endmacro()

if(DEFINED GVSOC_TARGETS)

    string(REPLACE " "  ";" GVSOC_TARGETS_LIST "${GVSOC_TARGETS}")
    foreach(target ${GVSOC_TARGETS_LIST})
        gvsoc_target_components(${target} gvsoc "")
    endforeach()

    # Targets also run with --platform rtl, by an RTL simulator running inside
    # GVSoC (gvsoc_svsim, Verilator). Those run by an external simulator (VCS,
    # Questa) through their own runner do not need it.
    if(DEFINED GVSOC_RTL_TARGETS)
        string(REPLACE " "  ";" GVSOC_RTL_TARGETS_LIST "${GVSOC_RTL_TARGETS}")
        foreach(target ${GVSOC_RTL_TARGETS_LIST})
            gvsoc_target_components(${target} rtl ".rtl")
        endforeach()
    endif()

    gvsoc_run_target_components()

else()
    set(CONFIG_BUILD_ALL 1)
endif()

# install sub folder
set(GVSOC_MODELS_INSTALL_FOLDER       "models")
set(GVSOC_MODELS_OPTIM_INSTALL_FOLDER "")
set(GVSOC_MODELS_PROFILE_INSTALL_FOLDER "profile")
set(GVSOC_MODELS_DEBUG_INSTALL_FOLDER "debug")
set(GVSOC_MODELS_ASSERT_INSTALL_FOLDER "asserts")
set(GVSOC_MODELS_SV_INSTALL_FOLDER    "sv")
set(GVSOC_MODELS_OPTIM_M32_INSTALL_FOLDER "m32")
set(GVSOC_MODELS_DEBUG_M32_INSTALL_FOLDER "debug_m32")

# ================
# Utility includes
# ================
include(cmake/vp_model.cmake)

# =======
# Options
# =======
option(BUILD_OPTIMIZED     "build GVSOC with optimizations"                    ON)
option(BUILD_PROFILE       "build GVSOC with profile information"              ON)
option(BUILD_DEBUG         "build GVSOC with debug information"                ON)
option(BUILD_ASSERT        "build GVSOC with assertions enabled"               ON)
option(BUILD_OPTIMIZED_M32 "build GVSOC with optimizations in 32bits mode"     OFF)
option(BUILD_DEBUG_M32     "build GVSOC with debug information in 32bits mode" OFF)

SET(CMAKE_INSTALL_RPATH "${CMAKE_INSTALL_PREFIX}/lib")

# set target types
vp_set_target_types(
    BUILD_DEBUG ${BUILD_DEBUG}
    BUILD_PROFILE ${BUILD_PROFILE}
    BUILD_ASSERT ${BUILD_ASSERT}
    BUILD_OPTIMIZED ${BUILD_OPTIMIZED}
    BUILD_OPTIMIZED_M32 ${BUILD_OPTIMIZED_M32}
    BUILD_DEBUG_M32 ${BUILD_DEBUG_M32}
    )

set(CMAKE_POSITION_INDEPENDENT_CODE ON)

install(DIRECTORY bin/ DESTINATION bin USE_SOURCE_PERMISSIONS)

if (NOT APPLE)
    # SCHEREMO: DPI is not supported on anything but Linux anyway / no EDA tools on Apple
    add_subdirectory(dpi-wrapper)
endif()

# The model config headers (<model/path>/<name>_config.hpp) are generated in this
# binary dir. The engine libraries export it, so that every model linking them finds
# them whatever the build layout.
set(GVSOC_ENGINE_GEN_DIR ${CMAKE_CURRENT_BINARY_DIR})
add_subdirectory(engine)

set(is_first true)
set(module_index 0)

foreach(subdir ${GVSOC_MODULES})

    if(NOT is_first)
        if(IS_DIRECTORY "${subdir}")
            message(STATUS "subdir: ${subdir} bin: ${CMAKE_CURRENT_BINARY_DIR}/module_${module_index}")

            if(EXISTS "${subdir}/CMakeLists.txt")
                add_subdirectory(
                    "${subdir}"
                    "${CMAKE_CURRENT_BINARY_DIR}/module_${module_index}"
                )
                math(EXPR module_index "${module_index} + 1")
            endif()
        endif()
    endif()

    set(is_first false)
endforeach()

foreach(component ${COMPONENTS})
    string(REPLACE " " ";" SRCS_LIST "${CONFIG_SRCS_${component}}")

    # Target directories holding one of the component sources
    set(component_includes ${GVSOC_EXTRA_MODULES})
    foreach(dir ${GVSOC_TARGETS_DIRS_REAL})
        foreach(src ${SRCS_LIST})
            get_filename_component(src_real "${src}" REALPATH)
            string(FIND "${src_real}" "${dir}/" pos)
            if(pos EQUAL 0)
                list(APPEND component_includes "${dir}")
                break()
            endif()
        endforeach()
    endforeach()

    # Libraries declared from the generator via Component.add_libraries():
    # each one found on the CMake search paths is linked by its full path,
    # and the include directory next to it is searched for its headers.
    # Those outside the compiler's default paths need it, as Homebrew's on
    # macOS (/opt/homebrew/lib, /opt/homebrew/include). One not found is
    # linked by name, as before.
    set(LIBS_LIST "")
    set(LIBS_INCLUDES "")
    if(NOT "${CONFIG_LIBS_${component}}" STREQUAL "")
        string(REPLACE " " ";" LIBS_NAMES "${CONFIG_LIBS_${component}}")
        foreach(lib ${LIBS_NAMES})
            if(IS_ABSOLUTE "${lib}" OR lib MATCHES "^-")
                list(APPEND LIBS_LIST "${lib}")
                continue()
            endif()
            string(MAKE_C_IDENTIFIER "GVSOC_MODEL_LIB_${lib}" lib_var)
            find_library(${lib_var} NAMES ${lib})
            if(${lib_var})
                list(APPEND LIBS_LIST "${${lib_var}}")
                get_filename_component(lib_dir "${${lib_var}}" DIRECTORY)
                get_filename_component(lib_inc "${lib_dir}/../include" ABSOLUTE)
                if(IS_DIRECTORY "${lib_inc}" AND NOT lib_inc IN_LIST CMAKE_CXX_IMPLICIT_INCLUDE_DIRECTORIES)
                    list(APPEND LIBS_INCLUDES "${lib_inc}")
                endif()
            else()
                list(APPEND LIBS_LIST "${lib}")
            endif()
        endforeach()
        list(REMOVE_DUPLICATES LIBS_INCLUDES)
    endif()

    vp_model(NAME ${component}
        SOURCES "${SRCS_LIST}"
        INCLUDES "${component_includes}"
    )
    if(NOT "${LIBS_LIST}" STREQUAL "")
        vp_model_link_libraries(NAME ${component}
            LIBRARY "${LIBS_LIST}"
        )
    endif()
    # As system directories: searched after the model ones, so that a header
    # of a library prefix shared by many packages never shadows a model one
    if(NOT "${LIBS_INCLUDES}" STREQUAL "")
        foreach(TARGET_TYPE IN LISTS VP_TARGET_TYPES)
            if(TARGET ${component}${TARGET_TYPE})
                target_include_directories(${component}${TARGET_TYPE} SYSTEM PRIVATE ${LIBS_INCLUDES})
            endif()
        endforeach()
    endif()
endforeach()

# =============================================
# Per-target platform tree shared libraries
# =============================================
# Each target generates a <target>.tree.cpp during gvrun components.
# Compile each into a shared library installed alongside the models.
# TREE_SOURCES is built from the current GVSOC_TARGETS loop — do NOT
# glob ${TARGET_CONFIG_DIR}/*.tree.cpp here: stale files from previous
# builds (e.g. a prior test run) would be compiled against the current
# target's regenerated headers and fail with "too many initializers"
# when the Config layouts differ.
foreach(tree_src ${TREE_SOURCES})
    get_filename_component(tree_name ${tree_src} NAME)       # e.g. "acu.acu.tree.cpp"
    string(REGEX REPLACE "\\.tree\\.cpp$" "" target_name "${tree_name}")  # e.g. "acu.acu"
    string(REGEX REPLACE "[^a-zA-Z0-9_]" "_" lib_name "${target_name}")    # e.g. "acu_acu"
    set(lib_target "platform_tree_${lib_name}")

    add_library(${lib_target} SHARED ${tree_src})
    # Force the ".so" suffix on all platforms (macOS would otherwise emit
    # ".dylib"). The library is dlopen'd by explicit path from
    # runner_gvrun2.py, which assumes a ".so" name regardless of host.
    set_target_properties(${lib_target} PROPERTIES SUFFIX ".so")
    vp_export_symbols(${lib_target} vp_get_platform_tree vp_platform_tree_version)
    target_include_directories(${lib_target} PRIVATE
        ${CMAKE_CURRENT_BINARY_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/engine/include)
    install(TARGETS ${lib_target}
        LIBRARY DESTINATION lib)
endforeach()

install(DIRECTORY python/ DESTINATION python)

install(
    FILES python/gvsoc/gvsoc_control.py
    DESTINATION  python/gv
    )
