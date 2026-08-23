#include "qt_frame_diagnostics_json.h"

#include "../backends/qt/qt_render_cache.h"
#include "../backends/qt/qt_render_types.h"
#include "../runtime/checksum.h"

#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <QtGui/QImage>

namespace krok::subtitle::native::diagnostics {

using legacy_qt::GlowBitmapCacheMissDiagnostic;
using legacy_qt::LineDiagnostics;
using legacy_qt::RenderDiagnostics;
using legacy_qt::RubyDiagnostics;
using legacy_qt::glowBitmapCacheSize;
using legacy_qt::glowBitmapCacheStats;
using legacy_qt::layoutCacheSize;
using legacy_qt::layoutCacheStats;
using legacy_qt::textLayerCacheSize;
using legacy_qt::textLayerCacheStats;
using runtime::imageChecksum;

void appendQtFrameDiagnostics(
    QJsonObject *out,
    int tMs,
    const QImage &image,
    const RenderDiagnostics &diagnostics,
    double renderMs
) {
    out->insert(QStringLiteral("t_ms"), tMs);
    out->insert(QStringLiteral("width"), image.width());
    out->insert(QStringLiteral("height"), image.height());
    out->insert(QStringLiteral("checksum"), QString::number(imageChecksum(image)));
    out->insert(QStringLiteral("render_ms"), renderMs);
    out->insert(QStringLiteral("visible_lines"), diagnostics.visibleLines);
    out->insert(QStringLiteral("glow_cache_hits"), glowBitmapCacheStats().hits);
    out->insert(QStringLiteral("glow_cache_misses"), glowBitmapCacheStats().misses);
    out->insert(QStringLiteral("glow_cache_shape_misses"), glowBitmapCacheStats().shapeMisses);
    out->insert(QStringLiteral("glow_cache_content_variant_misses"), glowBitmapCacheStats().contentVariantMisses);
    out->insert(QStringLiteral("glow_cache_evicted_key_misses"), glowBitmapCacheStats().evictedKeyMisses);
    out->insert(QStringLiteral("glow_cache_size"), glowBitmapCacheSize());
    out->insert(QStringLiteral("text_layer_cache_hits"), textLayerCacheStats().hits);
    out->insert(QStringLiteral("text_layer_cache_misses"), textLayerCacheStats().misses);
    out->insert(QStringLiteral("text_layer_cache_size"), textLayerCacheSize());
    out->insert(QStringLiteral("layout_cache_hits"), layoutCacheStats().hits);
    out->insert(QStringLiteral("layout_cache_misses"), layoutCacheStats().misses);
    out->insert(QStringLiteral("layout_cache_size"), layoutCacheSize());
    QJsonObject missesByScope;
    const auto scopeKeys = glowBitmapCacheStats().missesByScope.keys();
    for (const QString &scope : scopeKeys) {
        missesByScope.insert(scope, glowBitmapCacheStats().missesByScope.value(scope));
    }
    out->insert(QStringLiteral("glow_cache_misses_by_scope"), missesByScope);
    QJsonArray recentGlowMisses;
    const auto &misses = glowBitmapCacheStats().recentMisses;
    const std::size_t start = misses.size() > 8 ? misses.size() - 8 : 0;
    for (std::size_t index = start; index < misses.size(); ++index) {
        const GlowBitmapCacheMissDiagnostic &miss = misses[index];
        QJsonObject item;
        item.insert(QStringLiteral("scope"), miss.scope);
        item.insert(QStringLiteral("category"), miss.category);
        item.insert(QStringLiteral("radius"), miss.radius);
        item.insert(QStringLiteral("width"), miss.width);
        item.insert(QStringLiteral("height"), miss.height);
        item.insert(QStringLiteral("format"), miss.format);
        item.insert(QStringLiteral("checksum"), miss.checksum);
        recentGlowMisses.append(item);
    }
    out->insert(QStringLiteral("glow_cache_recent_misses"), recentGlowMisses);
    QJsonArray lineDiagnostics;
    for (const LineDiagnostics &line : diagnostics.lines) {
        QJsonObject item;
        item.insert(QStringLiteral("lane"), line.lane);
        item.insert(QStringLiteral("line_x"), line.lineX);
        item.insert(QStringLiteral("line_width"), line.lineWidth);
        item.insert(QStringLiteral("baseline_y"), line.baselineY);
        item.insert(QStringLiteral("after_clip_left"), line.afterClipLeft);
        item.insert(QStringLiteral("after_clip_right"), line.afterClipRight);
        item.insert(QStringLiteral("after_clip_top"), line.afterClipTop);
        item.insert(QStringLiteral("after_clip_height"), line.afterClipHeight);
        lineDiagnostics.append(item);
    }
    out->insert(QStringLiteral("line_diagnostics"), lineDiagnostics);
    QJsonArray rubyDiagnostics;
    for (const RubyDiagnostics &ruby : diagnostics.rubies) {
        QJsonObject item;
        item.insert(QStringLiteral("kanji"), ruby.kanji);
        item.insert(QStringLiteral("reading"), ruby.reading);
        QJsonArray indices;
        for (int index : ruby.indices) {
            indices.append(index);
        }
        item.insert(QStringLiteral("indices"), indices);
        item.insert(QStringLiteral("x"), ruby.x);
        item.insert(QStringLiteral("baseline_y"), ruby.baselineY);
        item.insert(QStringLiteral("target_width"), ruby.targetWidth);
        item.insert(QStringLiteral("reading_width"), ruby.readingWidth);
        item.insert(QStringLiteral("progress"), ruby.progress);
        item.insert(QStringLiteral("after_clip_left"), ruby.afterClipLeft);
        item.insert(QStringLiteral("after_clip_right"), ruby.afterClipRight);
        item.insert(QStringLiteral("after_clip_top"), ruby.afterClipTop);
        item.insert(QStringLiteral("after_clip_height"), ruby.afterClipHeight);
        rubyDiagnostics.append(item);
    }
    out->insert(QStringLiteral("ruby_diagnostics"), rubyDiagnostics);
    if (diagnostics.hasFirstLine) {
        out->insert(QStringLiteral("line_x"), diagnostics.lineX);
        out->insert(QStringLiteral("line_width"), diagnostics.lineWidth);
        out->insert(QStringLiteral("baseline_y"), diagnostics.baselineY);
        out->insert(QStringLiteral("after_clip_left"), diagnostics.afterClipLeft);
        out->insert(QStringLiteral("after_clip_right"), diagnostics.afterClipRight);
        out->insert(QStringLiteral("after_clip_top"), diagnostics.afterClipTop);
        out->insert(QStringLiteral("after_clip_height"), diagnostics.afterClipHeight);
    }
}

}  // namespace krok::subtitle::native::diagnostics
