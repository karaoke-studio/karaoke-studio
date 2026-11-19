add_executable(krok_skia_subtitle_renderer
    src/skia/sidecar_main.cpp
    src/commands/skia/command_router.cpp src/commands/skia/frame_commands.cpp
    src/commands/skia/text_commands.cpp
    src/commands/skia/posix_ring_owner.cpp
    src/scene/skia_ir_contract.cpp src/scene/skia_ir_projection.cpp
    src/scene/skia_lower_projection.cpp src/scene/skia_particle_projection.cpp
    src/scene/skia_text_projection.cpp src/backends/skia/resolved_input.cpp src/backends/skia/resolved_wipe.cpp
    src/backends/skia/resolved_glyph.cpp src/backends/skia/resolved_ruby.cpp src/backends/skia/image_fill_cache.cpp
    src/protocol/render_config_parser.cpp src/protocol/json_value.cpp src/protocol/json_protocol.cpp
    src/runtime/shared_frame_ring.cpp src/runtime/checksum.cpp
    src/diagnostics/shared_frame_metadata_json.cpp)
target_link_libraries(krok_skia_subtitle_renderer PRIVATE subtitle_skia_backend subtitle_scene_adapter Qt6::Core Qt6::Gui)
target_compile_definitions(krok_skia_subtitle_renderer PRIVATE KROK_SKIA_FINGERPRINT="${KROK_SKIA_FINGERPRINT}")
krok_strict_float(krok_skia_subtitle_renderer)
if(TARGET subtitle_skia_gpu)
    target_compile_definitions(krok_skia_subtitle_renderer PRIVATE KROK_SKIA_SIDECAR_GPU)
endif()
