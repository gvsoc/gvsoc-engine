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
    set(GAPY_TARGETS_DIR_OPT ${GAPY_TARGETS_DIR_OPT} --target-dir=${subdir})
endforeach()

# Extra modules are only visible to the config generation and to the generated
# components, not to the modules models. This keeps the models compile commands
# stable when extra modules are given (e.g. by testsuite builds), so that they
# do not get recompiled and reinstalled.
foreach(subdir ${GVSOC_EXTRA_MODULES})
    set(GAPY_TARGETS_DIR_OPT ${GAPY_TARGETS_DIR_OPT} --target-dir=${subdir})
endforeach()

set(TARGET_CONFIG_DIR ${CMAKE_BINARY_DIR}/gvsoc/configs)
file(MAKE_DIRECTORY ${TARGET_CONFIG_DIR})

set(COMPONENTS)
set(TREE_SOURCES)

if(DEFINED GVSOC_TARGETS)

    string(REPLACE " "  ";" GVSOC_TARGETS_LIST "${GVSOC_TARGETS}")
    foreach(target ${GVSOC_TARGETS_LIST})

        string(REPLACE ","  "_" TARGET_NAME "${target}")
        string(REPLACE "/"  "." TARGET_NAME "${TARGET_NAME}")
        string(REPLACE ":"  "_" TARGET_NAME "${TARGET_NAME}")
        set(TARGET_CONFIG ${TARGET_CONFIG_DIR}/${TARGET_NAME}.config)
        list(APPEND TREE_SOURCES ${TARGET_CONFIG_DIR}/${TARGET_NAME}.tree.cpp)

        message(STATUS "Generating GVSOC config to ${TARGET_CONFIG} with command: ")

        # Gapy is used to extract the list of components out of the specified target

        # Targets carrying gvrun qualifiers (attr./config.) must be generated with
        # gvrun: gapy cannot apply those qualifiers, so the generated tree would
        # not match the runtime systree.
        if(DEFINED ENV{USE_GVRUN} OR target MATCHES ":(attr|config)\\.")
            set(GAPY_CMD gvrun --target=${target} ${GAPY_TARGETS_DIR_OPT}
                --platform=gvsoc
                --builddir=${CMAKE_CURRENT_BINARY_DIR}
                --installdir=${CMAKE_INSTALL_PREFIX}/models
                --component-file=${TARGET_CONFIG} components
                --py-stack
            )
        else()
            set(GAPY_CMD gapy --target=${target} ${GAPY_TARGETS_DIR_OPT}
                --platform=gvsoc
                --builddir=${CMAKE_CURRENT_BINARY_DIR}
                --installdir=${CMAKE_INSTALL_PREFIX}/models
                --component-file=${TARGET_CONFIG} components
                --py-stack
            )
        endif()

        string(REPLACE ";"  " " GAPY_CMD_STR "    ${GAPY_CMD}")
        message(STATUS ${GAPY_CMD_STR})

        # We generate first the list of components for the specified target
        execute_process (
            COMMAND ${GAPY_CMD} RESULT_VARIABLE ret
        )
        if(NOT ret EQUAL "0")
            message( FATAL_ERROR "Caught error while generating gvsoc config")
        endif()

        import_kconfig(CONFIG_ ${TARGET_CONFIG} "COMPONENTS")

    endforeach()

else()
    set(CONFIG_BUILD_ALL 1)
endif()

# install sub folder
set(GVSOC_MODELS_INSTALL_FOLDER       "models")
set(GVSOC_MODELS_OPTIM_INSTALL_FOLDER "")
set(GVSOC_MODELS_PROFILE_INSTALL_FOLDER "profile")
set(GVSOC_MODELS_DEBUG_INSTALL_FOLDER "debug")
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
option(BUILD_OPTIMIZED_M32 "build GVSOC with optimizations in 32bits mode"     OFF)
option(BUILD_DEBUG_M32     "build GVSOC with debug information in 32bits mode" OFF)

SET(CMAKE_INSTALL_RPATH "${CMAKE_INSTALL_PREFIX}/lib")

# set target types
vp_set_target_types(
    BUILD_DEBUG ${BUILD_DEBUG}
    BUILD_PROFILE ${BUILD_PROFILE}
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
    vp_model(NAME ${component}
        SOURCES "${SRCS_LIST}"
        INCLUDES "${GVSOC_EXTRA_MODULES}"
    )
    # Link any libraries declared from the generator via Component.add_libraries().
    if(NOT "${CONFIG_LIBS_${component}}" STREQUAL "")
        string(REPLACE " " ";" LIBS_LIST "${CONFIG_LIBS_${component}}")
        vp_model_link_libraries(NAME ${component}
            LIBRARY "${LIBS_LIST}"
        )
    endif()
endforeach()

# =============================================
# Per-target platform tree shared libraries
# =============================================
# Each target generates a <target>.tree.cpp during gapy components.
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
    target_include_directories(${lib_target} PRIVATE
        ${CMAKE_CURRENT_BINARY_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/engine/include)
    install(TARGETS ${lib_target}
        LIBRARY DESTINATION lib)
    # Install the SHA256 sidecar written by tree_gen.generate_tree_cpp.
    # Used by the launcher (runner_gvrun2) to detect a stale .so when the
    # runtime systree has been reshaped (e.g. via --attribute) and
    # transparently fall back to the JSON-based instantiation path.
    install(FILES "${tree_src}.sig"
        DESTINATION lib
        RENAME "libplatform_tree_${lib_name}.so.sig"
        OPTIONAL)
endforeach()

install(DIRECTORY python/ DESTINATION python)

install(
    FILES python/gvsoc/gvsoc_control.py
    DESTINATION  python/gv
    )
