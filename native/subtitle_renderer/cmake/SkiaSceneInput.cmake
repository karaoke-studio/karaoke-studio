# Project canonical IR into Skia input for both the sidecar and the Quick host.
add_library(subtitle_skia_ir STATIC
    src/scene/skia_ir_contract.cpp src/scene/skia_ir_projection.cpp
    src/scene/skia_lower_projection.cpp src/scene/skia_particle_projection.cpp
    src/scene/skia_text_projection.cpp src/backends/skia/resolved_input.cpp src/backends/skia/resolved_wipe.cpp
    src/backends/skia/resolved_sources.cpp src/backends/skia/resolved_role_paints.cpp
    src/backends/skia/resolved_signal.cpp src/backends/skia/resolved_line_band.cpp
    src/backends/skia/resolved_glyph.cpp src/backends/skia/resolved_bitmap_guide.cpp
    src/backends/skia/resolved_ruby.cpp src/backends/skia/image_fill_cache.cpp
    src/backends/text_semantics.cpp
    src/protocol/render_config_parser.cpp src/protocol/json_value.cpp)
target_link_libraries(subtitle_skia_ir PUBLIC subtitle_skia_scene_input Qt6::Core
                                       PRIVATE subtitle_scene_adapter Qt6::Gui)
krok_strict_float(subtitle_skia_ir)
