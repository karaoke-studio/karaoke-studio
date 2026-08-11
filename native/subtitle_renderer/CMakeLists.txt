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
    src/protocol/json_protocol.cpp
    src/protocol/json_protocol.h
    src/runtime/shared_frame_ring.cpp
    src/runtime/shared_frame_ring.h
    src/backends/render_backend.h
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
