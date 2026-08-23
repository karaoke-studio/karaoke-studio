cmake_minimum_required(VERSION 3.24)

project(krok_subtitle_renderer LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Emit compile_commands.json so VS Code IntelliSense picks up the exact Qt
# include flags instead of relying on a hand-maintained c_cpp_properties path.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

find_package(Qt6 REQUIRED COMPONENTS Core Gui Widgets)

# Version fingerprint: the native sidecar must be built against the same Qt that
# PyQt6 bundles, otherwise the sidecar and the Python fallback renderer rasterize
# glyphs differently (hinting / stroke AA / metrics drift across minor versions).
# The run script passes the PyQt6 Qt version via -DKROK_EXPECTED_QT_VERSION; a
# mismatch is a hard error rather than a silent pixel divergence.
if(DEFINED KROK_EXPECTED_QT_VERSION AND NOT KROK_EXPECTED_QT_VERSION STREQUAL "")
    if(NOT Qt6_VERSION VERSION_EQUAL KROK_EXPECTED_QT_VERSION)
        message(FATAL_ERROR
            "Native Qt ${Qt6_VERSION} != PyQt6 Qt ${KROK_EXPECTED_QT_VERSION}. "
            "Rebuild against the matching Qt so native/Python pixels stay aligned "
            "(see docs/字幕渲染核心C++化方案.md §7 Qt 版本指纹).")
    endif()
endif()

add_executable(krok_subtitle_renderer
    src/main.cpp
    src/diagnostics/qt_frame_diagnostics_json.cpp
    src/diagnostics/qt_frame_diagnostics_json.h
    src/protocol/json_protocol.cpp
    src/protocol/json_protocol.h
    src/protocol/json_value.cpp
    src/protocol/json_value.h
    src/protocol/render_config.h
    src/protocol/render_config_parser.cpp
    src/protocol/render_config_parser.h
    src/runtime/checksum.cpp
    src/runtime/checksum.h
    src/runtime/gpu_preview_worker_pool.cpp
    src/runtime/gpu_preview_worker_pool.h
    src/runtime/render_job_runtime.cpp
    src/runtime/render_job_runtime.h
    src/runtime/render_runtime.cpp
    src/runtime/render_runtime.h
    src/runtime/shared_frame_ring.cpp
    src/runtime/shared_frame_ring.h
    src/backends/render_backend.h
    src/backends/qt/gpu_scene_projection.cpp
    src/backends/qt/gpu_scene_projection.h
    src/backends/qt/qt_cached_line_layout.cpp
    src/backends/qt/qt_cached_line_layout.h
    src/backends/qt/qt_cached_ruby_layer.cpp
    src/backends/qt/qt_cached_ruby_layer.h
    src/backends/qt/qt_cached_text_layer.cpp
    src/backends/qt/qt_cached_text_layer.h
    src/backends/qt/qt_character_animation.cpp
    src/backends/qt/qt_character_animation.h
    src/backends/qt/qt_clip_geometry.cpp
    src/backends/qt/qt_clip_geometry.h
    src/backends/qt/qt_display_plan.cpp
    src/backends/qt/qt_display_plan.h
    src/backends/qt/qt_fill_brush.cpp
    src/backends/qt/qt_fill_brush.h
    src/backends/qt/qt_font_factory.cpp
    src/backends/qt/qt_font_factory.h
    src/backends/qt/qt_frame_renderer.cpp
    src/backends/qt/qt_frame_renderer.h
    src/backends/qt/qt_glyph_run.cpp
    src/backends/qt/qt_glyph_run.h
    src/backends/qt/qt_line_layout.cpp
    src/backends/qt/qt_line_layout.h
    src/backends/qt/qt_line_painter.cpp
    src/backends/qt/qt_line_painter.h
    src/backends/qt/qt_render_cache.cpp
    src/backends/qt/qt_render_cache.h
    src/backends/qt/qt_render_types.h
    src/backends/qt/qt_ruby_diagnostics.cpp
    src/backends/qt/qt_ruby_diagnostics.h
    src/backends/qt/qt_ruby_layout.cpp
    src/backends/qt/qt_ruby_layout.h
    src/backends/qt/qt_ruby_painter.cpp
    src/backends/qt/qt_ruby_painter.h
    src/backends/qt/qt_ruby_target.cpp
    src/backends/qt/qt_ruby_target.h
    src/backends/qt/qt_ruby_timing.cpp
    src/backends/qt/qt_ruby_timing.h
    src/backends/qt/qt_ruby_wipe.cpp
    src/backends/qt/qt_ruby_wipe.h
    src/backends/qt/qt_style_metrics.cpp
    src/backends/qt/qt_style_metrics.h
    src/backends/qt/qt_text_layer.cpp
    src/backends/qt/qt_text_layer.h
    src/backends/qt/qt_transformed_text.cpp
    src/backends/qt/qt_transformed_text.h
    src/backends/qt/qt_utopia_painter.cpp
    src/backends/qt/qt_utopia_painter.h
    src/backends/direct2d/d2d_backend.cpp
    src/backends/direct2d/d2d_backend.h
    src/backends/direct2d/d2d_device.cpp
    src/backends/direct2d/d2d_device.h
    src/backends/direct2d/native_preview_surface.cpp
    src/backends/direct2d/native_preview_surface.h
)

target_link_libraries(krok_subtitle_renderer
    PRIVATE
        Qt6::Core
        Qt6::Gui
        Qt6::Widgets
)

target_compile_definitions(krok_subtitle_renderer
    PRIVATE
        NOMINMAX
        WIN32_LEAN_AND_MEAN
        KROK_GPU_DIAGNOSTICS=1
)

if(WIN32)
    target_link_libraries(krok_subtitle_renderer
        PRIVATE
            d2d1
            d3d11
            dwrite
            dcomp
            dxgi
            ole32
            windowscodecs
    )
else()
    message(FATAL_ERROR "The Direct2D GPU subtitle renderer probe is Windows-only")
endif()

if(MSVC)
    target_compile_options(krok_subtitle_renderer PRIVATE /utf-8 /O2)
else()
    target_compile_options(krok_subtitle_renderer PRIVATE -O2)
endif()
