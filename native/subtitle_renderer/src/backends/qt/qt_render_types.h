#pragma once

#include "../../protocol/render_config.h"

#include <QtCore/QHash>
#include <QtCore/QPointF>
#include <QtCore/QSet>
#include <QtCore/QString>
#include <QtGui/QFont>
#include <QtGui/QImage>
#include <QtGui/QPainterPath>

#include <cstddef>
#include <utility>
#include <vector>

namespace krok::subtitle::native::legacy_qt {

using protocol::ResolvedStyle;
using protocol::RubyAnnotation;
using protocol::TimingLine;

struct LineLayout {
    QString text;
    QFont font;
    QPainterPath path;
    std::vector<double> charLefts;
    std::vector<double> charWidths;
    std::vector<QFont> charFonts;
    const ResolvedStyle *lineStyle = nullptr;
    // Pointers into RenderConfig::resolvedStyles, which is frozen for render_frame.
    std::vector<const ResolvedStyle *> charStyles;
    double x = 0.0;
    double baselineY = 0.0;
    double width = 0.0;
    double height = 0.0;
    double ascent = 0.0;
    double descent = 0.0;
    double afterClipExtent = 0.0;
    bool hasInlineStyles = false;
};

struct LineDiagnostics {
    int lane = 0;
    double lineX = 0.0;
    double lineWidth = 0.0;
    double baselineY = 0.0;
    double afterClipLeft = 0.0;
    double afterClipRight = 0.0;
    double afterClipTop = 0.0;
    double afterClipHeight = 0.0;
};

struct DisplayLineRef {
    const TimingLine *line = nullptr;
    int lane = 0;
    int displayStartMs = 0;
    int displayEndMs = 0;
};

struct RubyDiagnostics {
    QString kanji;
    QString reading;
    std::vector<int> indices;
    double x = 0.0;
    double baselineY = 0.0;
    double targetWidth = 0.0;
    double readingWidth = 0.0;
    double progress = 0.0;
    double afterClipLeft = 0.0;
    double afterClipRight = 0.0;
    double afterClipTop = 0.0;
    double afterClipHeight = 0.0;
};

struct RubyLayerImage {
    QImage image;
    QPointF offset;
};

struct TextLayerImage {
    QImage image;
    QPointF offset;
};

struct GlyphRunRef {
    std::size_t start = 0;
    std::size_t end = 0;
};

struct RubyGroupInfo {
    std::vector<int> indices;
    RubyAnnotation ruby;
};

struct RubyUnitLayout {
    QString text;
    std::pair<int, int> interval;
    double x = 0.0;
    double width = 0.0;
};

struct LineCharTransition {
    QString phase;
    QString effect;
    double progress = 1.0;
    int startMs = 0;
    int endMs = 0;
};

struct AnimationState {
    double opacity = 1.0;
    double dx = 0.0;
    double dy = 0.0;
    double rotation = 0.0;
    double scaleX = 1.0;
    double scaleY = 1.0;
    double skewY = 0.0;
};

struct ImageFillCacheEntry {
    QString key;
    QImage image;
};

struct GlowBitmapCacheEntry {
    QString key;
    QImage image;
};

struct TextLayerCacheEntry {
    QString key;
    TextLayerImage layer;
};

struct LayoutCacheEntry {
    QString key;
    LineLayout layout;
};

struct GlowBitmapCacheKeyParts {
    QString key;
    QString shapeKey;
    QString checksum;
    int radius = 1;
    int width = 0;
    int height = 0;
    int format = 0;
};

struct GlowBitmapCacheMissDiagnostic {
    QString scope;
    QString category;
    int radius = 1;
    int width = 0;
    int height = 0;
    int format = 0;
    QString checksum;
};

struct GlowLayerImage {
    QImage image;
    QPointF offset;
};

struct GlowBitmapCacheStats {
    int hits = 0;
    int misses = 0;
    int shapeMisses = 0;
    int contentVariantMisses = 0;
    int evictedKeyMisses = 0;
    QSet<QString> seenKeys;
    QSet<QString> seenShapes;
    QHash<QString, int> missesByScope;
    std::vector<GlowBitmapCacheMissDiagnostic> recentMisses;
};

struct TextLayerCacheStats {
    int hits = 0;
    int misses = 0;
};

struct LayoutCacheStats {
    int hits = 0;
    int misses = 0;
};

struct RenderDiagnostics {
    int visibleLines = 0;
    bool hasFirstLine = false;
    double lineX = 0.0;
    double lineWidth = 0.0;
    double baselineY = 0.0;
    double afterClipLeft = 0.0;
    double afterClipRight = 0.0;
    double afterClipTop = 0.0;
    double afterClipHeight = 0.0;
    std::vector<LineDiagnostics> lines;
    std::vector<RubyDiagnostics> rubies;
};

struct RenderResult {
    QImage image;
    RenderDiagnostics diagnostics;
};

struct RangeFrameResult {
    int tMs = 0;
    double renderMs = 0.0;
    QString checksum;
    int visibleLines = 0;
    QImage image;
};

}  // namespace krok::subtitle::native::legacy_qt
