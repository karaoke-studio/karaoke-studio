#include "commands/skia/command_router.h"
#include "protocol/json_protocol.h"
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
    ready.insert("profiles", QJsonArray{"p04_ascii_solid_wipe", "p05_resolved_horizontal", "p05_single_line_placement", "p05_inline_roles", "p05_fixed_row_grid", "p05_page_collisions", "p05_smart_horizontal", "p05_segmented_wipe", "p05_resolved_ruby"});
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
    ready.insert("vertical_ruby_schema", 1);
    ready.insert("vector_guide_schema", 1);
    ready.insert("primary_solid_outline_schema", 1);
    ready.insert("secondary_solid_outline_schema", 1);
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
    auto profiles = ready.value("profiles").toArray();
    profiles.append("p05_upper_ruby");
    profiles.append("p05_overlap_wipe");
    profiles.append("p05_lower_ruby");
    profiles.append("p05_ruby_markers");
    profiles.append("p05_ruby_units");
    profiles.append("p05_font_fallback");
    profiles.append("p05_mixed_fonts");
    profiles.append("p05_standalone_marks");
    profiles.append("p07_no_wipe");
    profiles.append("p07_zero_ruby_no_wipe");
    profiles.append("p05_horizontal_rtl");
    profiles.append("p05_horizontal_rtl_cells");
    profiles.append("p05_vertical_solid");
    profiles.append("p05_vertical_japanese_glyphs");
    profiles.append("p05_vertical_ruby");
    profiles.append("p05_vector_guides");
    profiles.append("p06_primary_solid_outline");
    profiles.append("p06_secondary_solid_outline");
    profiles.append("p05_horizontal_rtl_kana_cells");
    profiles.append("p05_horizontal_rtl_kana_marks");
    profiles.append("p05_reverse_wipe");
    profiles.append("p05_reverse_overlap");
    ready.insert("ruby_unit_schema", 1);
    ready.insert("ruby_marker_schema", 1);
    ready.insert("lower_ruby_schema", 1);
    ready.insert("overlap_schema", 1);
    ready.insert("profiles", profiles);
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
