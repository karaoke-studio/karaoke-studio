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
option(KROK_BUILD_NATIVE_TESTS "Build native boundary and deterministic effect tests" ON)
if(KROK_BUILD_NATIVE_TESTS)
    enable_testing()
    add_executable(subtitle_scene_tests tests/scene_tests.cpp)
    target_link_libraries(subtitle_scene_tests PRIVATE subtitle_scene)
    add_test(NAME subtitle_scene_tests COMMAND subtitle_scene_tests)
    add_executable(subtitle_effect_samples tests/effect_samples.cpp)
    target_link_libraries(subtitle_effect_samples PRIVATE subtitle_scene)
endif()

if(KROK_BUILD_LEGACY_SIDECAR)
    add_library(subtitle_scene_adapter STATIC
        src/scene/paint_projection.cpp
        src/scene/style_projection.cpp
        src/scene/line_layout_projection.cpp)
    target_link_libraries(subtitle_scene_adapter PUBLIC subtitle_scene Qt6::Core Qt6::Gui)
    if(MSVC)
        target_compile_options(subtitle_scene_adapter PRIVATE /utf-8 /O2)
    endif()
endif()
