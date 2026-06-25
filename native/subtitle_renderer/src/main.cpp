#include <QtCore/QByteArray>
#include <QtCore/QFile>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QPointF>
#include <QtCore/QTextStream>
#include <QtGui/QBrush>
#include <QtGui/QColor>
#include <QtGui/QFont>
#include <QtGui/QFontMetricsF>
#include <QtGui/QGuiApplication>
#include <QtGui/QImage>
#include <QtGui/QPainter>
#include <QtGui/QPainterPath>
#include <QtGui/QPen>

#include <algorithm>
#include <cstdint>
#include <cmath>
#include <iostream>
#include <optional>
#include <vector>

namespace {

constexpr int kProtocolSchema = 1;

struct TimingChar {
    QString text;
    int startMs = 0;
    std::optional<int> pauseReleaseMs;
    QString roleLabel;
};

struct TimingLine {
    std::vector<TimingChar> chars;
    int endMs = 0;
    QString singerLabel;
    int singerId = -1;
};

struct RubyAnnotation {
    QString kanji;
    QString reading;
    std::vector<int> readingPartMs;
    int posStartMs = 0;
    int posEndMs = 0;
};

struct RenderConfig {
    int width = 1920;
    int height = 1080;
    int fps = 60;
    QString fontFamily = QStringLiteral("UD Digi Kyokasho N-B");
    int fontSizePx = 100;
    int fontWeight = 400;
    int letterSpacingPx = 0;
    QString baseColor = QStringLiteral("#FFFFFF");
    QString fillColor = QStringLiteral("#FF5A6F");
    QString beforeStrokeColor = QStringLiteral("#222222");
    QString afterStrokeColor = QStringLiteral("#222222");
    QString beforeStroke2Color = QStringLiteral("#000000");
    QString afterStroke2Color = QStringLiteral("#000000");
    int strokeWidthPx = 9;
    int stroke2WidthPx = 0;
    int lineYMarginPx = 80;
    int lineGapPx = 90;
    int lineLeadInMs = 1800;
    int lineTailMs = 1000;
    QString lineYPosition = QStringLiteral("bottom");
    bool dualLineLayout = true;
    bool rightToLeft = false;
    std::vector<TimingLine> lines;
    std::vector<RubyAnnotation> rubies;
};

struct LineLayout {
    QString text;
    QFont font;
    QPainterPath path;
    std::vector<double> charLefts;
    std::vector<double> charWidths;
    double x = 0.0;
    double baselineY = 0.0;
    double width = 0.0;
    double height = 0.0;
    double ascent = 0.0;
    double descent = 0.0;
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
};

struct RenderResult {
    QImage image;
    RenderDiagnostics diagnostics;
};

QString stringValue(const QJsonObject &object, const QString &key, const QString &fallback = {}) {
    const auto value = object.value(key);
    return value.isString() ? value.toString() : fallback;
}

int intValue(const QJsonObject &object, const QString &key, int fallback = 0) {
    const auto value = object.value(key);
    return value.isDouble() ? value.toInt() : fallback;
}

QString paintFillColor(const QJsonObject &object, const QString &fallback) {
    return stringValue(object, QStringLiteral("color"), fallback);
}

QString karaokeLayerColor(
    const QJsonObject &style,
    const QString &stateKey,
    const QString &layerKey,
    const QString &fallback
) {
    const QJsonObject colors = style.value(QStringLiteral("karaoke_colors")).toObject();
    const QJsonObject state = colors.value(stateKey).toObject();
    return paintFillColor(state.value(layerKey).toObject(), fallback);
}

QJsonObject response(bool ok, const QString &event) {
    QJsonObject out;
    out.insert(QStringLiteral("ok"), ok);
    out.insert(QStringLiteral("event"), event);
    return out;
}

void writeJson(const QJsonObject &object) {
    const QJsonDocument doc(object);
    std::cout << doc.toJson(QJsonDocument::Compact).constData() << std::endl;
}

std::uint64_t imageChecksum(const QImage &image) {
    const uchar *data = image.constBits();
    const qsizetype size = image.sizeInBytes();
    std::uint64_t hash = 1469598103934665603ull;
    const qsizetype step = std::max<qsizetype>(1, size / 4096);
    for (qsizetype i = 0; i < size; i += step) {
        hash ^= static_cast<std::uint64_t>(data[i]);
        hash *= 1099511628211ull;
    }
    return hash;
}

std::vector<int> parseIntArray(const QJsonArray &items) {
    std::vector<int> out;
    out.reserve(static_cast<std::size_t>(items.size()));
    for (const auto &item : items) {
        if (item.isDouble()) {
            out.push_back(item.toInt());
        }
    }
    return out;
}

std::optional<RenderConfig> parseConfig(const QJsonObject &ir, QString *error) {
    if (ir.value(QStringLiteral("schema")).toInt() != kProtocolSchema) {
        *error = QStringLiteral("unsupported Render IR schema");
        return std::nullopt;
    }

    RenderConfig cfg;
    const QJsonObject screen = ir.value(QStringLiteral("screen")).toObject();
    cfg.width = std::max(1, intValue(screen, QStringLiteral("width"), cfg.width));
    cfg.height = std::max(1, intValue(screen, QStringLiteral("height"), cfg.height));
    cfg.fps = std::max(1, intValue(screen, QStringLiteral("fps"), cfg.fps));

    const QJsonObject style = ir.value(QStringLiteral("style")).toObject();
    cfg.fontFamily = stringValue(style, QStringLiteral("font_family"), cfg.fontFamily);
    cfg.fontSizePx = std::max(1, intValue(style, QStringLiteral("font_size_px"), cfg.fontSizePx));
    cfg.fontWeight = std::clamp(intValue(style, QStringLiteral("font_weight"), cfg.fontWeight), 1, 999);
    cfg.letterSpacingPx = intValue(style, QStringLiteral("letter_spacing_px"), cfg.letterSpacingPx);
    cfg.baseColor = stringValue(style, QStringLiteral("base_color"), cfg.baseColor);
    cfg.fillColor = stringValue(style, QStringLiteral("fill_color"), cfg.fillColor);
    const QString strokeColor = stringValue(style, QStringLiteral("stroke_color"), cfg.beforeStrokeColor);
    cfg.beforeStrokeColor = strokeColor;
    cfg.afterStrokeColor = strokeColor;
    cfg.strokeWidthPx = std::max(0, intValue(style, QStringLiteral("stroke_width_px"), cfg.strokeWidthPx));
    cfg.stroke2WidthPx = std::max(0, intValue(style, QStringLiteral("stroke2_width_px"), cfg.stroke2WidthPx));
    cfg.lineYMarginPx = std::max(0, intValue(style, QStringLiteral("line_y_margin_px"), cfg.lineYMarginPx));
    cfg.lineGapPx = std::max(0, intValue(style, QStringLiteral("line_gap_px"), cfg.lineGapPx));
    cfg.lineLeadInMs = std::max(0, intValue(style, QStringLiteral("line_lead_in_ms"), cfg.lineLeadInMs));
    cfg.lineTailMs = std::max(0, intValue(style, QStringLiteral("line_tail_ms"), cfg.lineTailMs));
    cfg.lineYPosition = stringValue(style, QStringLiteral("line_y_position"), cfg.lineYPosition);
    cfg.dualLineLayout = style.value(QStringLiteral("dual_line_layout")).isBool()
        ? style.value(QStringLiteral("dual_line_layout")).toBool()
        : cfg.dualLineLayout;
    cfg.rightToLeft = style.value(QStringLiteral("right_to_left")).isBool()
        ? style.value(QStringLiteral("right_to_left")).toBool()
        : cfg.rightToLeft;
    cfg.baseColor = karaokeLayerColor(style, QStringLiteral("before"), QStringLiteral("text"), cfg.baseColor);
    cfg.fillColor = karaokeLayerColor(style, QStringLiteral("after"), QStringLiteral("text"), cfg.fillColor);
    cfg.beforeStrokeColor = karaokeLayerColor(style, QStringLiteral("before"), QStringLiteral("stroke"), cfg.beforeStrokeColor);
    cfg.afterStrokeColor = karaokeLayerColor(style, QStringLiteral("after"), QStringLiteral("stroke"), cfg.afterStrokeColor);
    cfg.beforeStroke2Color = karaokeLayerColor(style, QStringLiteral("before"), QStringLiteral("stroke2"), cfg.beforeStroke2Color);
    cfg.afterStroke2Color = karaokeLayerColor(style, QStringLiteral("after"), QStringLiteral("stroke2"), cfg.beforeStroke2Color);

    const QJsonObject track = ir.value(QStringLiteral("track")).toObject();
    const QJsonArray lines = track.value(QStringLiteral("lines")).toArray();
    cfg.lines.reserve(static_cast<std::size_t>(lines.size()));
    for (const auto &lineValue : lines) {
        const QJsonObject lineObject = lineValue.toObject();
        TimingLine line;
        line.endMs = intValue(lineObject, QStringLiteral("end_ms"), 0);
        line.singerLabel = stringValue(lineObject, QStringLiteral("singer_label"));
        line.singerId = intValue(lineObject, QStringLiteral("singer_id"), -1);

        const QJsonArray chars = lineObject.value(QStringLiteral("chars")).toArray();
        line.chars.reserve(static_cast<std::size_t>(chars.size()));
        for (const auto &charValue : chars) {
            const QJsonObject charObject = charValue.toObject();
            TimingChar ch;
            ch.text = stringValue(charObject, QStringLiteral("text"));
            ch.startMs = intValue(charObject, QStringLiteral("start_ms"), 0);
            if (charObject.value(QStringLiteral("pause_release_ms")).isDouble()) {
                ch.pauseReleaseMs = charObject.value(QStringLiteral("pause_release_ms")).toInt();
            }
            ch.roleLabel = stringValue(charObject, QStringLiteral("role_label"));
            line.chars.push_back(ch);
        }
        cfg.lines.push_back(line);
    }

    const QJsonArray rubies = track.value(QStringLiteral("rubies")).toArray();
    cfg.rubies.reserve(static_cast<std::size_t>(rubies.size()));
    for (const auto &rubyValue : rubies) {
        const QJsonObject rubyObject = rubyValue.toObject();
        RubyAnnotation ruby;
        ruby.kanji = stringValue(rubyObject, QStringLiteral("kanji"));
        ruby.reading = stringValue(rubyObject, QStringLiteral("reading"));
        ruby.readingPartMs = parseIntArray(rubyObject.value(QStringLiteral("reading_part_ms")).toArray());
        ruby.posStartMs = intValue(rubyObject, QStringLiteral("pos_start_ms"), 0);
        ruby.posEndMs = intValue(rubyObject, QStringLiteral("pos_end_ms"), 0);
        cfg.rubies.push_back(ruby);
    }

    return cfg;
}

QString lineText(const TimingLine &line) {
    QString text;
    for (const auto &ch : line.chars) {
        text += ch.text;
    }
    return text;
}

int lineStartMs(const TimingLine &line) {
    if (line.chars.empty()) {
        return 0;
    }
    return line.chars.front().startMs;
}

bool lineVisible(const TimingLine &line, int tMs, const RenderConfig &cfg) {
    if (line.chars.empty()) {
        return false;
    }
    const int start = lineStartMs(line) - cfg.lineLeadInMs;
    const int end = std::max(line.endMs, line.chars.back().startMs) + cfg.lineTailMs;
    return start <= tMs && tMs <= end;
}

int charEndMs(const TimingLine &line, std::size_t index) {
    if (index >= line.chars.size()) {
        return 0;
    }
    const TimingChar &ch = line.chars[index];
    if (ch.pauseReleaseMs.has_value()) {
        return std::max(ch.startMs, ch.pauseReleaseMs.value());
    }
    if (index + 1 < line.chars.size()) {
        return std::max(ch.startMs, line.chars[index + 1].startMs);
    }
    if (line.endMs > ch.startMs) {
        return line.endMs;
    }
    return ch.startMs + 1;
}

double progressRatio(int startMs, int endMs, int tMs) {
    if (endMs <= startMs) {
        return tMs >= startMs ? 1.0 : 0.0;
    }
    const double raw = static_cast<double>(tMs - startMs) / static_cast<double>(endMs - startMs);
    return std::clamp(raw, 0.0, 1.0);
}

QColor colorValue(const QString &value, const QColor &fallback) {
    const QColor color(value);
    return color.isValid() ? color : fallback;
}

QFont buildLineFont(const RenderConfig &cfg) {
    QFont font(cfg.fontFamily);
    font.setPixelSize(cfg.fontSizePx);
    font.setWeight(static_cast<QFont::Weight>(std::clamp(cfg.fontWeight, 1, 999)));
    if (cfg.letterSpacingPx != 0) {
        font.setLetterSpacing(QFont::AbsoluteSpacing, cfg.letterSpacingPx);
    }
    return font;
}

double visualStrokeExtent(const RenderConfig &cfg) {
    return std::ceil((std::max(cfg.strokeWidthPx, 0) + std::max(cfg.stroke2WidthPx, 0)) / 2.0);
}

double baselineYForLine(const RenderConfig &cfg, const QFontMetricsF &metrics, int lane, int visibleLineCount) {
    const double pad = visualStrokeExtent(cfg);
    if (cfg.dualLineLayout && visibleLineCount >= 2) {
        const double mainHeight = metrics.ascent() + metrics.descent() + pad * 2.0;
        const double mainAscent = metrics.ascent() + pad;
        const double mainDescent = metrics.descent() + pad;
        double upperBaseline = 0.0;
        double lowerBaseline = 0.0;
        if (cfg.lineYPosition == QStringLiteral("top")) {
            upperBaseline = cfg.lineYMarginPx + mainAscent;
            lowerBaseline = upperBaseline + mainHeight + cfg.lineGapPx;
        } else if (cfg.lineYPosition == QStringLiteral("center")) {
            const double totalHeight = mainHeight * 2.0 + cfg.lineGapPx;
            const double upperMainTop = std::floor((cfg.height - totalHeight) / 2.0);
            upperBaseline = upperMainTop + mainAscent;
            lowerBaseline = upperBaseline + mainHeight + cfg.lineGapPx;
        } else {
            lowerBaseline = cfg.height - cfg.lineYMarginPx - mainDescent;
            upperBaseline = lowerBaseline - mainHeight - cfg.lineGapPx;
        }
        return (cfg.dualLineLayout && std::min(lane, 1) == 1) ? lowerBaseline : upperBaseline;
    }

    if (cfg.lineYPosition == QStringLiteral("top")) {
        return cfg.lineYMarginPx + pad + metrics.ascent();
    }
    if (cfg.lineYPosition == QStringLiteral("center")) {
        const double blockHeight = metrics.height() + pad * 2.0;
        return std::floor((cfg.height - blockHeight) / 2.0) + pad + metrics.ascent();
    }
    return cfg.height - cfg.lineYMarginPx - pad - metrics.descent();
}

LineLayout layoutLine(const RenderConfig &cfg, const TimingLine &line, int lane, int visibleLineCount) {
    const QString text = lineText(line);

    LineLayout layout;
    layout.text = text;
    layout.font = buildLineFont(cfg);

    const QFontMetricsF metrics(layout.font);
    layout.ascent = metrics.ascent();
    layout.descent = metrics.descent();
    layout.height = metrics.height();

    layout.charWidths.reserve(line.chars.size());
    double totalWidth = 0.0;
    for (const auto &ch : line.chars) {
        const double width = std::max(1.0, metrics.horizontalAdvance(ch.text));
        layout.charWidths.push_back(width);
        totalWidth += width;
    }
    totalWidth += std::max(0, static_cast<int>(line.chars.size()) - 1) * cfg.letterSpacingPx;
    layout.width = std::max(1.0, totalWidth);
    layout.x = (cfg.width - layout.width) * 0.5;

    layout.baselineY = baselineYForLine(cfg, metrics, lane, visibleLineCount);

    layout.charLefts.resize(line.chars.size());
    if (cfg.rightToLeft) {
        double cursor = layout.x + layout.width;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            cursor -= layout.charWidths[i];
            layout.charLefts[i] = cursor;
            cursor -= cfg.letterSpacingPx;
        }
    } else {
        double cursor = layout.x;
        for (std::size_t i = 0; i < line.chars.size(); ++i) {
            layout.charLefts[i] = cursor;
            cursor += layout.charWidths[i] + cfg.letterSpacingPx;
        }
    }

    // C2 keeps one complete line path for both before/after layers. Karaoke
    // progress is expressed only by clipping the after layer, not by rebuilding
    // a prefix string path that can drift under kerning/shaping.
    layout.path.addText(QPointF(layout.x, layout.baselineY), layout.font, text);
    return layout;
}

std::optional<QRectF> afterClipRect(const RenderConfig &cfg, const TimingLine &line, const LineLayout &layout, int tMs) {
    if (line.chars.empty()) {
        return std::nullopt;
    }

    double clipEdge = cfg.rightToLeft ? layout.x + layout.width : layout.x;
    bool hasProgress = false;
    for (std::size_t i = 0; i < line.chars.size(); ++i) {
        const int start = line.chars[i].startMs;
        const int end = charEndMs(line, i);
        const double left = layout.charLefts[i];
        const double right = left + layout.charWidths[i];

        if (tMs < start) {
            break;
        }

        hasProgress = true;
        const double ratio = progressRatio(start, end, tMs);
        if (ratio < 1.0) {
            clipEdge = cfg.rightToLeft
                ? right - layout.charWidths[i] * ratio
                : left + layout.charWidths[i] * ratio;
            break;
        }

        clipEdge = cfg.rightToLeft ? left : right;
    }
    if (!hasProgress) {
        return std::nullopt;
    }

    const double strokePad = std::ceil(cfg.strokeWidthPx / 2.0);
    const double top = layout.baselineY - layout.ascent - strokePad;
    const double height = layout.height + strokePad * 2.0;
    if (cfg.rightToLeft) {
        const double left = std::clamp(clipEdge, layout.x, layout.x + layout.width);
        return QRectF(left, top, layout.x + layout.width - left, height);
    }
    const double right = std::clamp(clipEdge, layout.x, layout.x + layout.width);
    return QRectF(layout.x, top, right - layout.x, height);
}

void paintKaraokePath(QPainter &painter, const QPainterPath &path, const QColor &fill, const QColor &stroke, const QColor &stroke2, const RenderConfig &cfg) {
    if (cfg.stroke2WidthPx > 0) {
        painter.strokePath(path, QPen(stroke2, cfg.strokeWidthPx + cfg.stroke2WidthPx, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    if (cfg.strokeWidthPx > 0) {
        painter.strokePath(path, QPen(stroke, cfg.strokeWidthPx, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    painter.fillPath(path, QBrush(fill));
}

void paintKaraokeStrokes(QPainter &painter, const QPainterPath &path, const QColor &stroke, const QColor &stroke2, const RenderConfig &cfg) {
    if (cfg.stroke2WidthPx > 0) {
        painter.strokePath(path, QPen(stroke2, cfg.strokeWidthPx + cfg.stroke2WidthPx, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
    if (cfg.strokeWidthPx > 0) {
        painter.strokePath(path, QPen(stroke, cfg.strokeWidthPx, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    }
}

void paintLine(QPainter &painter, const RenderConfig &cfg, const TimingLine &line, int tMs, int lane, int visibleCount, int visibleLineCount, RenderDiagnostics *diagnostics) {
    const QString text = lineText(line);
    if (text.isEmpty()) {
        return;
    }

    const LineLayout layout = layoutLine(cfg, line, lane, visibleLineCount);

    const QColor base = colorValue(cfg.baseColor, QColor(QStringLiteral("#FFFFFF")));
    const QColor fill = colorValue(cfg.fillColor, QColor(QStringLiteral("#FF5A6F")));
    const QColor beforeStroke = colorValue(cfg.beforeStrokeColor, QColor(QStringLiteral("#222222")));
    const QColor afterStroke = colorValue(cfg.afterStrokeColor, beforeStroke);
    const QColor beforeStroke2 = colorValue(cfg.beforeStroke2Color, QColor(QStringLiteral("#000000")));
    const QColor afterStroke2 = colorValue(cfg.afterStroke2Color, beforeStroke2);

    paintKaraokePath(painter, layout.path, base, beforeStroke, beforeStroke2, cfg);

    const auto clip = afterClipRect(cfg, line, layout, tMs);
    if (clip.has_value() && clip->width() > 0.0) {
        painter.save();
        painter.setClipRect(*clip, Qt::IntersectClip);
        paintKaraokeStrokes(painter, layout.path, afterStroke, afterStroke2, cfg);
        painter.fillPath(layout.path, QBrush(fill));
        painter.restore();
    }

    if (diagnostics != nullptr && !diagnostics->hasFirstLine) {
        diagnostics->hasFirstLine = true;
        diagnostics->lineX = layout.x;
        diagnostics->lineWidth = layout.width;
        diagnostics->baselineY = layout.baselineY;
        if (clip.has_value()) {
            diagnostics->afterClipLeft = clip->left();
            diagnostics->afterClipRight = clip->right();
            diagnostics->afterClipTop = clip->top();
            diagnostics->afterClipHeight = clip->height();
        } else {
            diagnostics->afterClipLeft = layout.x;
            diagnostics->afterClipRight = layout.x;
            const double strokePad = std::ceil(cfg.strokeWidthPx / 2.0);
            diagnostics->afterClipTop = layout.baselineY - layout.ascent - strokePad;
            diagnostics->afterClipHeight = layout.height + strokePad * 2.0;
        }
    }
    (void)visibleCount;
}

RenderResult renderFrame(const RenderConfig &cfg, int tMs) {
    RenderResult result{
        QImage(cfg.width, cfg.height, QImage::Format_ARGB32_Premultiplied),
        RenderDiagnostics{},
    };
    result.image.fill(Qt::transparent);

    QPainter painter(&result.image);
    painter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing | QPainter::SmoothPixmapTransform);

    std::vector<const TimingLine *> visibleLines;
    visibleLines.reserve(cfg.lines.size());
    for (const auto &line : cfg.lines) {
        if (lineVisible(line, tMs, cfg)) {
            visibleLines.push_back(&line);
        }
    }
    result.diagnostics.visibleLines = static_cast<int>(visibleLines.size());
    if (cfg.dualLineLayout && visibleLines.size() > 2) {
        visibleLines.resize(2);
    }

    int lane = 0;
    int visibleCount = 0;
    for (const TimingLine *line : visibleLines) {
        paintLine(painter, cfg, *line, tMs, lane, visibleCount, result.diagnostics.visibleLines, &result.diagnostics);
        lane = std::min(lane + 1, 2);
        ++visibleCount;
    }

    painter.end();
    return result;
}

QJsonObject handleConfigure(const QJsonObject &request, std::optional<RenderConfig> *config) {
    QString error;
    auto parsed = parseConfig(request.value(QStringLiteral("ir")).toObject(), &error);
    if (!parsed.has_value()) {
        QJsonObject out = response(false, QStringLiteral("configure"));
        out.insert(QStringLiteral("error"), error);
        return out;
    }
    *config = parsed;
    QJsonObject out = response(true, QStringLiteral("configured"));
    out.insert(QStringLiteral("width"), parsed->width);
    out.insert(QStringLiteral("height"), parsed->height);
    out.insert(QStringLiteral("fps"), parsed->fps);
    out.insert(QStringLiteral("line_count"), static_cast<int>(parsed->lines.size()));
    out.insert(QStringLiteral("ruby_count"), static_cast<int>(parsed->rubies.size()));
    return out;
}

QJsonObject handleRenderFrame(const QJsonObject &request, const std::optional<RenderConfig> &config) {
    if (!config.has_value()) {
        QJsonObject out = response(false, QStringLiteral("render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("renderer is not configured"));
        return out;
    }

    const int tMs = intValue(request, QStringLiteral("t_ms"), 0);
    const QString outputPath = stringValue(request, QStringLiteral("output_path"));
    if (outputPath.isEmpty()) {
        QJsonObject out = response(false, QStringLiteral("render_frame"));
        out.insert(QStringLiteral("error"), QStringLiteral("output_path is required for native smoke render"));
        return out;
    }

    RenderResult rendered = renderFrame(*config, tMs);
    QImage &image = rendered.image;
    const bool saved = image.save(outputPath);
    QJsonObject out = response(saved, QStringLiteral("frame_ready"));
    out.insert(QStringLiteral("t_ms"), tMs);
    out.insert(QStringLiteral("width"), image.width());
    out.insert(QStringLiteral("height"), image.height());
    out.insert(QStringLiteral("output_path"), outputPath);
    out.insert(QStringLiteral("checksum"), QString::number(imageChecksum(image)));
    out.insert(QStringLiteral("visible_lines"), rendered.diagnostics.visibleLines);
    if (rendered.diagnostics.hasFirstLine) {
        out.insert(QStringLiteral("line_x"), rendered.diagnostics.lineX);
        out.insert(QStringLiteral("line_width"), rendered.diagnostics.lineWidth);
        out.insert(QStringLiteral("baseline_y"), rendered.diagnostics.baselineY);
        out.insert(QStringLiteral("after_clip_left"), rendered.diagnostics.afterClipLeft);
        out.insert(QStringLiteral("after_clip_right"), rendered.diagnostics.afterClipRight);
        out.insert(QStringLiteral("after_clip_top"), rendered.diagnostics.afterClipTop);
        out.insert(QStringLiteral("after_clip_height"), rendered.diagnostics.afterClipHeight);
    }
    if (!saved) {
        out.insert(QStringLiteral("error"), QStringLiteral("failed to save output image"));
    }
    return out;
}

QJsonObject parseErrorResponse(const QString &message) {
    QJsonObject out = response(false, QStringLiteral("parse_error"));
    out.insert(QStringLiteral("error"), message);
    return out;
}

}  // namespace

int main(int argc, char **argv) {
    qputenv("QT_QPA_PLATFORM", qgetenv("QT_QPA_PLATFORM").isEmpty() ? QByteArray("offscreen") : qgetenv("QT_QPA_PLATFORM"));
    QGuiApplication app(argc, argv);

    QJsonObject ready = response(true, QStringLiteral("ready"));
    ready.insert(QStringLiteral("schema"), kProtocolSchema);
    ready.insert(QStringLiteral("qt"), QString::fromLatin1(qVersion()));
    writeJson(ready);

    std::optional<RenderConfig> config;
    QTextStream input(stdin, QIODevice::ReadOnly);
    while (!input.atEnd()) {
        const QString line = input.readLine().trimmed();
        if (line.isEmpty()) {
            continue;
        }

        QJsonParseError parseError;
        const QJsonDocument doc = QJsonDocument::fromJson(line.toUtf8(), &parseError);
        if (parseError.error != QJsonParseError::NoError || !doc.isObject()) {
            writeJson(parseErrorResponse(parseError.errorString()));
            continue;
        }

        const QJsonObject request = doc.object();
        const QString command = stringValue(request, QStringLiteral("cmd"));
        if (command == QStringLiteral("configure")) {
            writeJson(handleConfigure(request, &config));
        } else if (command == QStringLiteral("render_frame")) {
            writeJson(handleRenderFrame(request, config));
        } else if (command == QStringLiteral("shutdown")) {
            writeJson(response(true, QStringLiteral("shutdown")));
            return 0;
        } else {
            QJsonObject out = response(false, QStringLiteral("unknown_command"));
            out.insert(QStringLiteral("error"), QStringLiteral("unknown command: ") + command);
            writeJson(out);
        }
    }

    return 0;
}
