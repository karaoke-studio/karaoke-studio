#include "commands/skia/command_router.h"
#include "protocol/json_protocol.h"
#include "scene/skia_ir_contract.h"
#include <QtCore/QCoreApplication>
#include <QtCore/QJsonArray>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    using namespace krok::subtitle::native;
    auto ready = protocol::response(true, "ready");
    ready.insert("backend", "skia");
    ready.insert("schema", 3);
    ready.insert("selection_version", 1);
    ready.insert("shared_memory_lifecycle_schema", 1);
    ready.insert("profiles", scene::skiaIrProfiles());
    QJsonArray targets{"raster"};
#ifdef KROK_SKIA_SIDECAR_GPU
    targets.append("gpu");
#endif
    ready.insert("execution_targets", targets);
    ready.insert("pixel_formats", QJsonArray{"rgba8888", "bgra8888_premultiplied"});
    ready.insert("text_metrics_schema", 1);
    ready.insert("cell_glyph_schema", 1);
    ready.insert("kana_mark_schema", 1);
    ready.insert("vertical_metrics_schema", 1);
    ready.insert("empty_cell_schema", 1);
    ready.insert("vertical_solid_schema", 2);
    ready.insert("vertical_japanese_glyph_schema", 1);
    ready.insert("vertical_ruby_schema", 2);
    ready.insert("vector_guide_schema", 1);
    ready.insert("primary_solid_outline_schema", 1);
    ready.insert("secondary_solid_outline_schema", 1);
    ready.insert("line_fade_schema", 1);
    ready.insert("line_motion_schema", 1);
    ready.insert("character_fade_schema", 1);
    ready.insert("scanline_band_schema", scene::kScanlineBandSchema);
    ready.insert("character_transform_schema", scene::kCharacterTransformSchema);
    ready.insert("sing_twinkle_schema", scene::kSingingParticleSchema);
    ready.insert("font_chain_schema", 1);
    ready.insert("mixed_font_schema", 1);
    ready.insert("standalone_mark_schema", 1);
    ready.insert("no_wipe_schema", 1);
    ready.insert("zero_ruby_no_wipe_schema", 1);
    ready.insert("horizontal_rtl_schema", 1);
    ready.insert("horizontal_rtl_cell_schema", 1);
    ready.insert("horizontal_rtl_kana_schema", 1);
    ready.insert("body_phase_order_schema", 1);
    ready.insert("reverse_wipe_schema", 1);
    ready.insert("reverse_overlap_schema", 1);
    ready.insert("resolved_layout_schema", 2);
    ready.insert("resolved_wipe_schema", 1);
    ready.insert("resolved_ruby_schema", 1);
    ready.insert("upper_ruby_schema", 1);
    ready.insert("ruby_unit_schema", 1);
    ready.insert("ruby_marker_schema", 1);
    ready.insert("lower_ruby_schema", 1);
    ready.insert("overlap_schema", 1);
    ready.insert("fingerprint", KROK_SKIA_FINGERPRINT);
    protocol::writeJson(ready);
    commands::SkiaCommandRouter router;
    std::string line;
    while (std::getline(std::cin, line)) {
        QJsonObject error;
        const auto request = protocol::parseRequestLine(QString::fromUtf8(line), &error);
        if (!request) protocol::writeJson(error);
        else if (!router.dispatch(*request)) break;
    }
    router.close();
}
