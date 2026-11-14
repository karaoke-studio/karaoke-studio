# Shared numeric code must produce the same floats on every platform. Clang contracts a*b+c
# into FMA by default and arm64 always has FMA while the x64 baseline does not, so the same
# source would round differently on Apple Silicon. MSVC's default /fp:precise never contracts.
function(krok_strict_float target)
    if(CMAKE_CXX_COMPILER_ID MATCHES "Clang|GNU")
        if(CMAKE_CXX_COMPILER_FRONTEND_VARIANT STREQUAL "MSVC")
            target_compile_options(${target} PRIVATE /clang:-ffp-contract=off)
        else()
            target_compile_options(${target} PRIVATE -ffp-contract=off)
        endif()
    endif()
endfunction()

# Scene values and deterministic effects have no renderer or platform dependencies.
add_library(subtitle_scene STATIC
    src/effects/unit_hash.cpp
    src/effects/noise.cpp
    src/effects/geometry.cpp
    src/effects/particles.cpp
    src/effects/particles/decoration.cpp
    src/effects/particles/energy.cpp
    src/effects/particles/falling.cpp
    src/effects/particles/scanline.cpp
    src/effects/scanline_column.cpp
    src/effects/scanline_image.cpp)
target_include_directories(subtitle_scene PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}/src")
if(MSVC)
    target_compile_options(subtitle_scene PUBLIC /utf-8 PRIVATE /O2)
endif()
krok_strict_float(subtitle_scene)
option(KROK_BUILD_NATIVE_TESTS "Build native boundary and deterministic effect tests" ON)
find_package(Threads REQUIRED)
add_library(subtitle_session_runtime STATIC
    src/runtime/generation_cancellation.cpp
    src/runtime/owner_backend_session.cpp)
target_link_libraries(subtitle_session_runtime PUBLIC subtitle_scene Threads::Threads)
if(KROK_BUILD_NATIVE_TESTS)
    enable_testing()
    add_executable(subtitle_scene_tests tests/scene_tests.cpp)
    target_link_libraries(subtitle_scene_tests PRIVATE subtitle_scene)
    add_test(NAME subtitle_scene_tests COMMAND subtitle_scene_tests)
    add_executable(subtitle_effect_samples tests/effect_samples.cpp)
    target_link_libraries(subtitle_effect_samples PRIVATE subtitle_scene)
    krok_strict_float(subtitle_effect_samples)
    add_executable(subtitle_owner_session_tests tests/owner_session_tests.cpp)
    target_link_libraries(subtitle_owner_session_tests PRIVATE subtitle_session_runtime)
    add_test(NAME subtitle_owner_session_tests COMMAND subtitle_owner_session_tests)
    set_tests_properties(subtitle_scene_tests subtitle_owner_session_tests PROPERTIES TIMEOUT 60)
endif()

if(KROK_BUILD_SCENE_ADAPTER)
    add_library(subtitle_scene_adapter STATIC
        src/scene/paint_projection.cpp
        src/scene/style_projection.cpp
        src/scene/line_layout_projection.cpp)
    target_link_libraries(subtitle_scene_adapter PUBLIC subtitle_scene Qt6::Core Qt6::Gui)
    krok_strict_float(subtitle_scene_adapter)
    if(MSVC)
        target_compile_options(subtitle_scene_adapter PRIVATE /utf-8 /O2)
    endif()
endif()
