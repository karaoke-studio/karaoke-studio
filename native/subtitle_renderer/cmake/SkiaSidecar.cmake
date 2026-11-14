add_executable(krok_skia_subtitle_renderer
    src/skia/sidecar_main.cpp
    src/commands/skia/command_router.cpp src/commands/skia/frame_commands.cpp
    src/scene/skia_ir_projection.cpp
    src/protocol/render_config_parser.cpp src/protocol/json_value.cpp src/protocol/json_protocol.cpp
    src/runtime/shared_frame_ring.cpp src/runtime/checksum.cpp
    src/diagnostics/shared_frame_metadata_json.cpp)
target_link_libraries(krok_skia_subtitle_renderer PRIVATE subtitle_skia_backend subtitle_scene_adapter Qt6::Core Qt6::Gui)
target_compile_definitions(krok_skia_subtitle_renderer PRIVATE KROK_SKIA_FINGERPRINT="${KROK_SKIA_FINGERPRINT}")
krok_strict_float(krok_skia_subtitle_renderer)
