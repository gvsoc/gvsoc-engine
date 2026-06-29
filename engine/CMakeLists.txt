cmake_minimum_required(VERSION 3.10)
project(gvsoc-engine LANGUAGES CXX C)

set(GVSOC_ENGINE_CXX_SRCS
    "src/component.cpp"
    "src/ports.cpp"
    "src/top.cpp"
    "src/block.cpp"
    "src/register.cpp"
    "src/signal.cpp"
    "src/queue.cpp"
    "src/mapping_tree.cpp"
    "src/debug_mem.cpp"
    "src/proxy.cpp"
    "src/launcher.cpp"
    "src/launcher_client.cpp"
    "src/proxy_client.cpp"
    "src/memcheck.cpp"
    "src/jsmn.cpp"
    "src/json.cpp"
    "src/trace/event.cpp"
    "src/trace/trace.cpp"
    "src/trace/vcd.cpp"
    "src/trace/trace_engine.cpp"
    "src/clock/clock_engine.cpp"
    "src/clock/clock_event.cpp"
    "src/clock/block_clock.cpp"
    "src/time/block_time.cpp"
    "src/time/time_engine.cpp"
    "src/time/time_event.cpp"
    "src/power/power_table.cpp"
    "src/power/power_engine.cpp"
    "src/power/block_power.cpp"
    "src/power/power_trace.cpp"
    "src/power/power_source.cpp"
    "src/stats.cpp"
    )

set(GVSOC_ENGINE_INC_DIRS "include")

# ==================
# Optimized launcher
# ==================

if(${BUILD_OPTIMIZED})
    add_library(gvsoc SHARED ${GVSOC_ENGINE_CXX_SRCS} ${GVSOC_ENGINE_C_SRCS})
    target_compile_options(gvsoc PRIVATE -fno-stack-protector)
    target_include_directories(gvsoc PUBLIC ${GVSOC_ENGINE_INC_DIRS})
    target_link_libraries(gvsoc PUBLIC z pthread ${CMAKE_DL_LIBS})
    set_target_properties(gvsoc PROPERTIES OUTPUT_NAME "pulpvp")
    install(TARGETS gvsoc
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        RUNTIME DESTINATION bin
        INCLUDES DESTINATION include
        )

    # The default launcher is always SystemC-free, so a plain run never drags in the
    # libsystemc runtime dependency.
    add_executable(gvsoc_launcher "src/main.cpp")
    target_link_libraries(gvsoc_launcher PRIVATE gvsoc z pthread ${CMAKE_DL_LIBS})

    install(TARGETS gvsoc_launcher
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        RUNTIME DESTINATION bin
        INCLUDES DESTINATION include
        )

    # When $SYSTEMC_HOME is set, also build a SystemC-linked variant, gvsoc_launcher_sc.
    # gvrun selects it only for targets that require SystemC.
    if(DEFINED ENV{SYSTEMC_HOME})
        message(STATUS "SYSTEMC")
        add_executable(gvsoc_launcher_sc "src/main.cpp" "src/systemc_driver.cpp")
        target_compile_options(gvsoc_launcher_sc PRIVATE -DVP_USE_SYSTEMC=1)
        target_include_directories(gvsoc_launcher_sc PRIVATE $ENV{SYSTEMC_HOME}/include)
        target_link_directories(gvsoc_launcher_sc PRIVATE $ENV{SYSTEMC_HOME}/lib64/)
        target_link_libraries(gvsoc_launcher_sc PRIVATE systemc gvsoc z pthread ${CMAKE_DL_LIBS})
        install(TARGETS gvsoc_launcher_sc
            LIBRARY DESTINATION lib
            ARCHIVE DESTINATION lib
            RUNTIME DESTINATION bin
            INCLUDES DESTINATION include
            )
    endif()
endif()

# ==============
# Asserts launcher
# ==============
# Same as the optimized variant (fully optimized, no trace/profile/memcheck
# overhead) but with assertions active: NDEBUG is undefined so libc assert()
# is kept, and VP_ASSERT_ACTIVE enables the gv_assert() macro.

if(${BUILD_ASSERT})
    add_library(gvsoc_asserts SHARED ${GVSOC_ENGINE_CXX_SRCS} ${GVSOC_ENGINE_C_SRCS})
    target_compile_options(gvsoc_asserts PRIVATE -fno-stack-protector -UNDEBUG)
    target_compile_definitions(gvsoc_asserts PRIVATE -DVP_ASSERT_ACTIVE=1)
    target_include_directories(gvsoc_asserts PUBLIC ${GVSOC_ENGINE_INC_DIRS})
    target_link_libraries(gvsoc_asserts PUBLIC z pthread ${CMAKE_DL_LIBS})
    set_target_properties(gvsoc_asserts PROPERTIES OUTPUT_NAME "pulpvp-asserts")
    install(TARGETS gvsoc_asserts
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        RUNTIME DESTINATION bin
        INCLUDES DESTINATION include
        )

    add_executable(gvsoc_launcher_asserts "src/main.cpp")
    target_compile_options(gvsoc_launcher_asserts PRIVATE -UNDEBUG)
    target_compile_definitions(gvsoc_launcher_asserts PRIVATE -DVP_ASSERT_ACTIVE=1)
    target_link_libraries(gvsoc_launcher_asserts PRIVATE gvsoc_asserts z pthread ${CMAKE_DL_LIBS})
    install(TARGETS gvsoc_launcher_asserts
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        RUNTIME DESTINATION bin
        INCLUDES DESTINATION include
        )

    # SystemC-linked variant, selected by gvrun for SystemC targets.
    if(DEFINED ENV{SYSTEMC_HOME})
        add_executable(gvsoc_launcher_asserts_sc "src/main.cpp" "src/systemc_driver.cpp")
        target_compile_options(gvsoc_launcher_asserts_sc PRIVATE -DVP_USE_SYSTEMC=1 -UNDEBUG)
        target_compile_definitions(gvsoc_launcher_asserts_sc PRIVATE -DVP_ASSERT_ACTIVE=1)
        target_include_directories(gvsoc_launcher_asserts_sc PRIVATE $ENV{SYSTEMC_HOME}/include)
        target_link_directories(gvsoc_launcher_asserts_sc PRIVATE $ENV{SYSTEMC_HOME}/lib64/)
        target_link_libraries(gvsoc_launcher_asserts_sc PRIVATE systemc gvsoc_asserts z pthread ${CMAKE_DL_LIBS})
        install(TARGETS gvsoc_launcher_asserts_sc
            LIBRARY DESTINATION lib
            ARCHIVE DESTINATION lib
            RUNTIME DESTINATION bin
            INCLUDES DESTINATION include
            )
    endif()
endif()

if(${BUILD_OPTIMIZED_M32})
    add_library(gvsoc_m32 SHARED ${GVSOC_ENGINE_CXX_SRCS} ${GVSOC_ENGINE_C_SRCS})
    target_include_directories(gvsoc_m32 PUBLIC ${GVSOC_ENGINE_INC_DIRS})
    target_link_libraries(gvsoc_m32 PUBLIC z pthread ${CMAKE_DL_LIBS})
    set_target_properties(gvsoc_m32 PROPERTIES OUTPUT_NAME "pulpvp-m32")
    install(TARGETS gvsoc_m32
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        INCLUDES DESTINATION include
        )
    target_compile_options(gvsoc_m32 PRIVATE -m32 -D__M32_MODE__)
    target_link_options(gvsoc_m32 PRIVATE -m32)
endif()

# ==============
# Profile launcher
# ==============

if(${BUILD_PROFILE})
    add_library(gvsoc_profile SHARED ${GVSOC_ENGINE_CXX_SRCS} ${GVSOC_ENGINE_C_SRCS})
    target_compile_options(gvsoc_profile PRIVATE -fno-stack-protector)
    target_include_directories(gvsoc_profile PUBLIC ${GVSOC_ENGINE_INC_DIRS})
    target_link_libraries(gvsoc_profile PUBLIC z pthread ${CMAKE_DL_LIBS})
    set_target_properties(gvsoc_profile PROPERTIES OUTPUT_NAME "pulpvp-profile")
    install(TARGETS gvsoc_profile
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        RUNTIME DESTINATION bin
        INCLUDES DESTINATION include
        )

    # TODO VP_TRACE_ACTIVE should be removed as soon as traces have been switch to events
    target_compile_definitions(gvsoc_profile PRIVATE -DCONFIG_GVSOC_EVENT_ACTIVE=1 -DVP_TRACE_ACTIVE=1 -DCONFIG_GVSOC_STATS_ACTIVE=1 -DVP_ASSERT_ACTIVE=1)
    target_compile_options(gvsoc_profile PRIVATE -UNDEBUG)

    add_executable(gvsoc_launcher_profile "src/main.cpp")
    target_link_libraries(gvsoc_launcher_profile PRIVATE gvsoc_profile z pthread ${CMAKE_DL_LIBS})
    install(TARGETS gvsoc_launcher_profile
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        RUNTIME DESTINATION bin
        INCLUDES DESTINATION include
        )
    # TODO VP_TRACE_ACTIVE should be removed as soon as traces have been switch to events
    target_compile_definitions(gvsoc_launcher_profile PRIVATE -DCONFIG_GVSOC_EVENT_ACTIVE=1 -DVP_TRACE_ACTIVE=1 -DCONFIG_GVSOC_STATS_ACTIVE=1 -DVP_ASSERT_ACTIVE=1)
    target_compile_options(gvsoc_launcher_profile PRIVATE -UNDEBUG)

    # SystemC-linked variant, selected by gvrun for SystemC targets.
    if(DEFINED ENV{SYSTEMC_HOME})
        add_executable(gvsoc_launcher_profile_sc "src/main.cpp" "src/systemc_driver.cpp")
        target_compile_options(gvsoc_launcher_profile_sc PRIVATE -DVP_USE_SYSTEMC=1 -UNDEBUG)
        target_include_directories(gvsoc_launcher_profile_sc PRIVATE $ENV{SYSTEMC_HOME}/include)
        target_link_directories(gvsoc_launcher_profile_sc PRIVATE $ENV{SYSTEMC_HOME}/lib64/)
        target_link_libraries(gvsoc_launcher_profile_sc PRIVATE systemc gvsoc_profile z pthread ${CMAKE_DL_LIBS})
        install(TARGETS gvsoc_launcher_profile_sc
            LIBRARY DESTINATION lib
            ARCHIVE DESTINATION lib
            RUNTIME DESTINATION bin
            INCLUDES DESTINATION include
            )
        # TODO VP_TRACE_ACTIVE should be removed as soon as traces have been switch to events
        target_compile_definitions(gvsoc_launcher_profile_sc PRIVATE -DCONFIG_GVSOC_EVENT_ACTIVE=1 -DVP_TRACE_ACTIVE=1 -DCONFIG_GVSOC_STATS_ACTIVE=1 -DVP_ASSERT_ACTIVE=1)
    endif()
endif()

# ==============
# Debug launcher
# ==============

if(${BUILD_DEBUG})
    add_library(gvsoc_debug SHARED ${GVSOC_ENGINE_CXX_SRCS} ${GVSOC_ENGINE_C_SRCS})
    target_include_directories(gvsoc_debug PUBLIC ${GVSOC_ENGINE_INC_DIRS})
    target_link_libraries(gvsoc_debug PUBLIC z pthread ${CMAKE_DL_LIBS})
    set_target_properties(gvsoc_debug PROPERTIES OUTPUT_NAME "pulpvp-debug")
    install(TARGETS gvsoc_debug
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        RUNTIME DESTINATION bin
        INCLUDES DESTINATION include
        )
    target_compile_definitions(gvsoc_debug PRIVATE -DCONFIG_GVSOC_EVENT_ACTIVE=1 -DVP_TRACE_ACTIVE=1 -DVP_MEMCHECK_ACTIVE=1 -DVP_ASSERT_ACTIVE=1)
    target_compile_options(gvsoc_debug PRIVATE -UNDEBUG)

    add_executable(gvsoc_launcher_debug "src/main.cpp")
    target_link_libraries(gvsoc_launcher_debug PRIVATE gvsoc_debug z pthread ${CMAKE_DL_LIBS})
    install(TARGETS gvsoc_launcher_debug
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        RUNTIME DESTINATION bin
        INCLUDES DESTINATION include
        )
    target_compile_definitions(gvsoc_launcher_debug PRIVATE -DCONFIG_GVSOC_EVENT_ACTIVE=1 -DVP_TRACE_ACTIVE=1 -DVP_MEMCHECK_ACTIVE=1 -DVP_ASSERT_ACTIVE=1)
    target_compile_options(gvsoc_launcher_debug PRIVATE -UNDEBUG)

    # SystemC-linked variant, selected by gvrun for SystemC targets.
    if(DEFINED ENV{SYSTEMC_HOME})
        add_executable(gvsoc_launcher_debug_sc "src/main.cpp" "src/systemc_driver.cpp")
        target_compile_options(gvsoc_launcher_debug_sc PRIVATE -DVP_USE_SYSTEMC=1 -UNDEBUG)
        target_include_directories(gvsoc_launcher_debug_sc PRIVATE $ENV{SYSTEMC_HOME}/include)
        target_link_directories(gvsoc_launcher_debug_sc PRIVATE $ENV{SYSTEMC_HOME}/lib64/)
        target_link_libraries(gvsoc_launcher_debug_sc PRIVATE systemc gvsoc_debug z pthread ${CMAKE_DL_LIBS})
        install(TARGETS gvsoc_launcher_debug_sc
            LIBRARY DESTINATION lib
            ARCHIVE DESTINATION lib
            RUNTIME DESTINATION bin
            INCLUDES DESTINATION include
            )
        target_compile_definitions(gvsoc_launcher_debug_sc PRIVATE -DCONFIG_GVSOC_EVENT_ACTIVE=1 -DVP_TRACE_ACTIVE=1 -DVP_MEMCHECK_ACTIVE=1 -DVP_ASSERT_ACTIVE=1)
    endif()
endif()

if(${BUILD_DEBUG_M32})
    add_library(gvsoc_debug_m32 SHARED ${GVSOC_ENGINE_CXX_SRCS} ${GVSOC_ENGINE_C_SRCS})
    target_include_directories(gvsoc_debug_m32 PUBLIC ${GVSOC_ENGINE_INC_DIRS})
    target_link_libraries(gvsoc_debug_m32 PUBLIC z pthread ${CMAKE_DL_LIBS})
    set_target_properties(gvsoc_debug_m32 PROPERTIES OUTPUT_NAME "pulpvp-debug-m32")
    install(TARGETS gvsoc_debug_m32
        LIBRARY DESTINATION lib
        ARCHIVE DESTINATION lib
        INCLUDES DESTINATION include
        )
    target_compile_definitions(gvsoc_debug_m32 PRIVATE -DCONFIG_GVSOC_EVENT_ACTIVE=1 -DVP_TRACE_ACTIVE=1 -DVP_MEMCHECK_ACTIVE=1 -DVP_ASSERT_ACTIVE=1)
    target_compile_options(gvsoc_debug_m32 PRIVATE -m32 -D__M32_MODE__ -UNDEBUG)
    target_link_options(gvsoc_debug_m32 PRIVATE "-m32")
endif()
