cmake_minimum_required(VERSION 3.24)

project(krok_qpainter_parallel_probe LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

find_package(Qt6 REQUIRED COMPONENTS Core Gui)

add_executable(krok_qpainter_parallel_probe
    src/main.cpp
)

target_link_libraries(krok_qpainter_parallel_probe
    PRIVATE
        Qt6::Core
        Qt6::Gui
)

if(MSVC)
    target_compile_options(krok_qpainter_parallel_probe PRIVATE /utf-8 /O2)
else()
    target_compile_options(krok_qpainter_parallel_probe PRIVATE -O2)
endif()
