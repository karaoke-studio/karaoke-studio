#pragma once

#include "../model/render_types.h"

#include <QtCore/QHash>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <QtCore/QString>

#include <algorithm>
#include <cmath>
#include <memory>
#include <optional>
#include <utility>
#include <vector>

namespace krok::subtitle::native::protocol {

struct TimingChar {
    QString text;
    int startMs = 0;
    std::optional<int> resolvedEndMs;
    std::optional<int> pauseReleaseMs;
    bool explicitStart = false;
    bool explicitEnd = false;
    QString roleLabel;
    // Shared pointer into RenderConfig::vectorGlyphs (schema 2 dedup table).
    std::shared_ptr<const krok::subtitle::native::VectorGlyph> vectorGlyph;
    std::optional<krok::subtitle::native::BitmapGuide> bitmapGuide;
};

struct ResolvedLineLayout {
    bool present = false;
    QString lineYPosition = QStringLiteral("bottom");
    int lineYMarginPx = 80;
    int lineGapPx = 90;
    QString smartHorizontal = QStringLiteral("equal_margins");
    int horizontalMarginPx = 50;
    std::vector<QString> lineAlignments{QStringLiteral("left"), QStringLiteral("right")};
    bool dualLineLayout = true;
    QString lineHorizontalLayout = QStringLiteral("asymmetric");
    QString row1Align = QStringLiteral("left");
    int row1OffsetX = 50;
    int row1OffsetY = 0;
    QString row2Align = QStringLiteral("right");
    int row2OffsetX = -50;
    int row2OffsetY = 0;
    int letterSpacingPx = 0;
    int spaceWidthPercent = 20;
    bool allowBiting = false;
    int rubyIntervalPx = 0;
    QString rubyAlignment = QStringLiteral("auto");
    int rubyGapPx = 0;
};

struct TimingLine {
    std::vector<TimingChar> chars;
    int endMs = 0;
    QString singerLabel;
    int singerId = -1;
    int sourceIndex = 0;
    int sourceLineIndex = 0;
    // Loader-stamped identity from TimingLine.track_line_index; -1 = unknown.
    int trackLineIndex = -1;
    int pageIndex = -1;
    int pageLineCount = 0;
    // Sayatoo signal lamps (every lit style) attach only to each section's
    // first page's first line; Python stamps the flag so both backends share
    // one decision. Absent field parses as true to keep legacy per-line IRs.
    bool signalHead = false;
    // Python 在源加载入口已把整行时间戳严格逆序的行镜像理顺为顺序，仅保留
    // 本标记让走字反向（横排 rtl 翻转 / 竖排自下而上），与 Painter 同口径。
    bool wipeReverse = false;
    int sourceOffsetMs = 0;
    int lane = 0;
    double layoutOffsetX = 0.0;
    double layoutOffsetY = 0.0;
    std::vector<krok::subtitle::native::PlacementWindow> placementWindows;
    std::optional<int> displayStartMs;
    std::optional<int> displayEndMs;
    std::optional<double> guideAnchorLeft;
    std::optional<double> guideAnchorRight;
    bool centerOverride = false;
    QString entryAnimation = QStringLiteral("none");
    int entryDurationMs = 0;
    QString exitAnimation = QStringLiteral("none");
    int exitDurationMs = 0;
    QString karaokeAnimation = QStringLiteral("none");
    ResolvedLineLayout layout;
};

struct RubyAnnotation {
    QString kanji;
    QString reading;
    std::vector<int> readingPartMs;
    std::vector<QString> readingParts;
    int posStartMs = 0;
    int posEndMs = 0;
    int sourceIndex = 0;
    int sourceOffsetMs = 0;
    // Loader-resolved owning line plus the line-local, half-open target range;
    // -1 = unresolved, in which case the historical text search runs (see
    // rubyTargetIndices). Source ownership is always enforced, while line
    // ownership is enforced whenever both sides carry a resolved identity.
    int targetLineIndex = -1;
    int targetCharStart = -1;
    int targetCharEnd = -1;
};

struct PaintFillSpec {
    QString mode = QStringLiteral("solid");
    QString color = QStringLiteral("#FFFFFF");
    QString startColor = QStringLiteral("#FFFFFF");
    QString endColor = QStringLiteral("#FFFFFF");
    std::vector<std::pair<double, QString>> gradientStops;
    QString splitTopColor = QStringLiteral("#FFFFFF");
    QString splitBottomColor = QStringLiteral("#FFFFFF");
    int splitPositionPct = 50;
    std::vector<std::pair<double, QString>> splitStops;
    QString imagePath;
    int imageScalePct = 100;
};

struct ResolvedStyle {
    QString fontFamily = QStringLiteral("UD Digi Kyokasho N-B");
    QString fontFamilyLatin;
    int fontSizePx = 100;
    std::optional<int> latinFontSizePx;
    int fontWeight = 400;
    std::optional<int> latinFontWeight;
    bool italic = false;
    bool allowBiting = false;
    bool affectsRubyAnchor = true;
    int spaceWidthPercent = 20;
    int letterSpacingPx = 0;
    QString baseColor = QStringLiteral("#FFFFFF");
    QString fillColor = QStringLiteral("#FF5A6F");
    QString beforeStrokeColor = QStringLiteral("#222222");
    QString afterStrokeColor = QStringLiteral("#222222");
    QString beforeStroke2Color = QStringLiteral("#000000");
    QString afterStroke2Color = QStringLiteral("#000000");
    QString beforeShadowColor = QStringLiteral("#000000");
    QString afterShadowColor = QStringLiteral("#000000");
    PaintFillSpec baseFill;
    PaintFillSpec afterFill;
    PaintFillSpec beforeStrokeFill;
    PaintFillSpec afterStrokeFill;
    PaintFillSpec beforeStroke2Fill;
    PaintFillSpec afterStroke2Fill;
    PaintFillSpec beforeShadowFill;
    PaintFillSpec afterShadowFill;
    QString rubyColor = QStringLiteral("#FF5A6F");
    QString rubyBaseColor = QStringLiteral("#FFFFFF");
    QString rubyFillColor = QStringLiteral("#FF5A6F");
    QString rubyBeforeStrokeColor = QStringLiteral("#222222");
    QString rubyAfterStrokeColor = QStringLiteral("#222222");
    QString rubyBeforeStroke2Color = QStringLiteral("#000000");
    QString rubyAfterStroke2Color = QStringLiteral("#000000");
    QString rubyBeforeShadowColor = QStringLiteral("#000000");
    QString rubyAfterShadowColor = QStringLiteral("#000000");
    PaintFillSpec rubyBaseFill;
    PaintFillSpec rubyAfterFill;
    PaintFillSpec rubyBeforeStrokeFill;
    PaintFillSpec rubyAfterStrokeFill;
    PaintFillSpec rubyBeforeStroke2Fill;
    PaintFillSpec rubyAfterStroke2Fill;
    PaintFillSpec rubyBeforeShadowFill;
    PaintFillSpec rubyAfterShadowFill;
    int strokeWidthPx = 9;
    int stroke2WidthPx = 0;
    // Tracked separately from the width because ``stroke2WidthPx`` is zeroed as
    // soon as the flag turns off: an unset ruby stroke2 has to inherit the main
    // text's *flag*, which a zeroed width can no longer distinguish from a
    // deliberate 0 px. Defaults to true so an absent key keeps the old
    // "no key means do not zero" behaviour (and matches Style.stroke2_enabled).
    bool stroke2Enabled = true;
    // The width before the flag zeroes it. Ruby inherits the flag and the width
    // along separate chains (N3 resolves UseEdge2 and EdgeSize2 independently),
    // so switching the main text's stroke2 off must not erase the width an
    // explicitly enabled ruby still inherits.
    int stroke2RawWidthPx = 0;
    QString decorationKind = QStringLiteral("shadow");
    int glowRadiusPx = 10;
    int glowBeforeRadiusPx = 10;
    int glowAfterRadiusPx = 10;
    int glowConcentrationLevel = 0;
    int shadowOffsetX = 0;
    int shadowOffsetY = 1;
    std::optional<int> rubyShadowOffsetX;
    std::optional<int> rubyShadowOffsetY;
    int rubyFontSizePx = 30;
    QString rubyFontFamily;
    QString rubyFontFamilyLatin;
    std::optional<int> rubyFontWeight;
    std::optional<int> rubyLatinFontSizePx;
    std::optional<int> rubyLatinFontWeight;
    bool rubyFontFollowMain = true;
    int rubyGapPx = 8;
    int rubyIntervalPx = 0;
    QString rubyAlignment = QStringLiteral("auto");
    QString rubyMainProgressMode = QStringLiteral("checkpoint_segments");
    bool rubyHorizontalGradientWithMain = true;
    // These ruby fallbacks are "unset" until a scheme sets them explicitly.
    // Empty/nullopt means "follow the effective (role) main text", resolved in
    // applyGpuResolvedStyle against the role-resolved style — mirroring the CPU
    // painter's lazy _ruby_* helpers and the existing rubyShadowOffset pattern.
    // Baking them from the *global* main during base parsing made a role that
    // overrode its main decoration/glow keep the global default for ruby.
    std::optional<int> rubyStrokeWidthPx;
    std::optional<bool> rubyStroke2Enabled;
    std::optional<int> rubyStroke2WidthPx;
    QString rubyDecorationKind;
    std::optional<int> rubyGlowBeforeRadiusPx;
    std::optional<int> rubyGlowAfterRadiusPx;
    std::optional<int> rubyGlowConcentrationLevel;
    bool litEnabled = false;
    QString litStyle = QStringLiteral("volume");
    int litNumber = 4;
    int litSize = 32;
    int litOffsetX = 0;
    int litOffsetY = -24;
    int litTracking = 0;
    QString litFillColor = QStringLiteral("#0000FF");
    QString litStrokeColor = QStringLiteral("#FFFFFF");
    int litStrokeWidth = 2;
    int litStrokeSoften = 0;
    int litOpacityPct = 100;
    int litEdgeBrightnessPct = 60;
    bool litShadow = true;
    int litTimeOffsetMs = 0;
    int litWaitingTimeMs = 0;
    QString litTransitionMode = QStringLiteral("fade");
    int litTransitionRatioPct = 67;
    int litTransitionAngleDeg = 0;
    int litTransitionDistance = 0;
    int signalsDurationMs = 4000;
    int volumeSize = 48;
    int volumeOffsetX = 0;
    int volumeOffsetY = 0;
    int volumeColumnWidth = 12;
    int volumeColumnCount = 4;
    int volumeColumnSpacing = 0;
    int volumeAlign = 1;
    double volumeRatio = 3.0;
    QString volumeFillColor = QStringLiteral("#FFFFFF");
    QString volumeStrokeColor = QStringLiteral("#0000FF");
    QString volumeOverlayFillColor = QStringLiteral("#0000FF");
    QString volumeOverlayStrokeColor = QStringLiteral("#FFFFFF");
    int volumeFlashTimes = 3;
    double volumeFlashDurationRatio = 1.0;
    int volumeTransitionRatioPct = 67;
    bool hasMainKaraokeColors = false;
    bool hasRubyKaraokeColors = false;
};

struct RenderConfig {
    int width = 1920;
    int height = 1080;
    int fps = 60;
    // 预览缩放：布局仍在 width/height 逻辑坐标系计算，光栅化画布按 dpr 缩放
    // （对应 Python 侧 preview_render_target_size + setDevicePixelRatio 的语义）。
    double dpr = 1.0;

    int physicalWidth() const { return std::max(1, static_cast<int>(std::lround(width * dpr))); }
    int physicalHeight() const { return std::max(1, static_cast<int>(std::lround(height * dpr))); }
    ResolvedStyle baseStyle;
    QString layoutSemantics = QStringLiteral("legacy");
    int lineYMarginPx = 80;
    int lineGapPx = 90;
    int lineLeadInMs = 1800;
    int lineTailMs = 1000;
    int lineProtectMs = 0;
    int lineLaneGapMs = 300;
    int lineContinuitySnapMs = 800;
    int linePairSecondDelayMs = 3000;
    int lineMaxHoldMs = 12000;
    int sectionGapMs = 4000;
    QString lineYPosition = QStringLiteral("bottom");
    QString lineHorizontalLayout = QStringLiteral("asymmetric");
    QString sectionEndingMode = QStringLiteral("hold");
    bool syncEnding = false;
    int upperLineLeftMarginPx = 50;
    int lowerLineRightMarginPx = 50;
    int horizontalMarginPx = 50;
    QString smartHorizontal = QStringLiteral("equal_margins");
    std::vector<QString> lineAlignments{QStringLiteral("left"), QStringLiteral("right")};
    bool dualLineLayout = true;
    bool rightToLeft = false;
    bool vertical = false;
    int viewportScalePct = 100;
    int viewportRotationDeg = 0;
    int viewportOffsetX = 0;
    int viewportOffsetY = 0;
    QString viewportAlign = QStringLiteral("center");
    QString entryAnim = QStringLiteral("none");
    int entryLeadMs = 300;
    QString exitAnim = QStringLiteral("none");
    int exitFadeMs = 300;
    QString karaokeAnim = QStringLiteral("inherit");
    int timingOffsetMs = 0;
    int primaryTrackOffsetMs = 0;
    QJsonObject singerStyleOverrides;
    QJsonObject customStyleSchemes;
    // Built during configure. render_frame must not insert here because LineLayout stores pointers into this QHash.
    QHash<QString, ResolvedStyle> resolvedStyles;
    // Render IR schema 2: deduplicated vector guide glyph outlines. Characters
    // reference entries through ``vector_glyph_id`` and share the same immutable
    // object, so a thousand inline guide glyphs pay for one outline only.
    QHash<QString, std::shared_ptr<const krok::subtitle::native::VectorGlyph>> vectorGlyphs;
    std::vector<TimingLine> lines;
    std::vector<RubyAnnotation> rubies;
    // Render IR schema: ``titles`` is an array of per-entry title payloads
    // (multi-title). Python and sidecar ship together, so the legacy single
    // ``title`` object key is not read anymore.
    QJsonArray titles;
};

}  // namespace krok::subtitle::native::protocol
