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
#include <QtGui/QGuiApplication>
#include <QtGui/QImage>
#include <QtGui/QLinearGradient>
#include <QtGui/QPainter>
#include <QtGui/QPainterPath>
#include <QtGui/QPen>

#include <algorithm>
#include <cstdint>
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
    int rubyFontSizePx = 35;
    QString baseColor = QStringLiteral("#FFFFFF");
    QString fillColor = QStringLiteral("#FF5A6F");
    QString strokeColor = QStringLiteral("#222222");
    int strokeWidthPx = 9;
    int lineLeadInMs = 1800;
    int lineTailMs = 1000;
    std::vector<TimingLine> lines;
    std::vector<RubyAnnotation> rubies;
};

QString stringValue(const QJsonObject &object, const QString &key, const QString &fallback = {}) {
    const auto value = object.value(key);
    return value.isString() ? value.toString() : fallback;
}

int intValue(const QJsonObject &object, const QString &key, int fallback = 0) {
    const auto value = object.value(key);
    return value.isDouble() ? value.toInt() : fallback;
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
    cfg.rubyFontSizePx = std::max(1, intValue(style, QStringLiteral("ruby_font_size_px"), cfg.rubyFontSizePx));
    cfg.baseColor = stringValue(style, QStringLiteral("base_color"), cfg.baseColor);
    cfg.fillColor = stringValue(style, QStringLiteral("fill_color"), cfg.fillColor);
    cfg.strokeColor = stringValue(style, QStringLiteral("stroke_color"), cfg.strokeColor);
    cfg.strokeWidthPx = std::max(0, intValue(style, QStringLiteral("stroke_width_px"), cfg.strokeWidthPx));
    cfg.lineLeadInMs = std::max(0, intValue(style, QStringLiteral("line_lead_in_ms"), cfg.lineLeadInMs));
    cfg.lineTailMs = std::max(0, intValue(style, QStringLiteral("line_tail_ms"), cfg.lineTailMs));

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

void paintLine(QPainter &painter, const RenderConfig &cfg, const TimingLine &line, int tMs, int lane, int visibleCount) {
    const QString text = lineText(line);
    if (text.isEmpty()) {
        return;
    }

    QFont font(cfg.fontFamily, cfg.fontSizePx);
    font.setWeight(QFont::DemiBold);
    QFont rubyFont(cfg.fontFamily, cfg.rubyFontSizePx);
    rubyFont.setWeight(QFont::DemiBold);

    const double y = cfg.height * 0.68 + lane * cfg.fontSizePx * 1.35;
    const double approximateWidth = text.size() * cfg.fontSizePx * 0.84;
    const double x = (cfg.width - approximateWidth) * 0.5;
    const QPointF baseline(x, y);

    QPainterPath beforePath;
    beforePath.addText(baseline, font, text);

    QString afterText;
    for (const auto &ch : line.chars) {
        if (ch.startMs <= tMs) {
            afterText += ch.text;
        }
    }
    QPainterPath afterPath;
    if (!afterText.isEmpty()) {
        afterPath.addText(baseline, font, afterText);
    }

    const QColor stroke(cfg.strokeColor);
    const QColor base(cfg.baseColor);
    const QColor fill(cfg.fillColor);
    const double strokeWidth = std::max(1, cfg.strokeWidthPx);

    painter.strokePath(beforePath, QPen(stroke, strokeWidth, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
    painter.fillPath(beforePath, QBrush(base));

    if (!afterPath.isEmpty()) {
        QLinearGradient gradient(QPointF(x, y - cfg.fontSizePx), QPointF(x + approximateWidth, y));
        gradient.setColorAt(0.0, fill);
        gradient.setColorAt(1.0, QColor(80, 190, 255));
        painter.strokePath(afterPath, QPen(stroke.darker(120), strokeWidth, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
        painter.fillPath(afterPath, QBrush(gradient));
    }

    if (!cfg.rubies.empty()) {
        const RubyAnnotation &ruby = cfg.rubies[static_cast<std::size_t>(visibleCount) % cfg.rubies.size()];
        if (!ruby.reading.isEmpty()) {
            QPainterPath rubyPath;
            rubyPath.addText(QPointF(x + approximateWidth * 0.12, y - cfg.fontSizePx * 1.05), rubyFont, ruby.reading);
            painter.strokePath(rubyPath, QPen(stroke, 4.0, Qt::SolidLine, Qt::RoundCap, Qt::RoundJoin));
            painter.fillPath(rubyPath, QBrush(QColor(QStringLiteral("#FFFFFF"))));
        }
    }
}

QImage renderFrame(const RenderConfig &cfg, int tMs) {
    QImage image(cfg.width, cfg.height, QImage::Format_ARGB32_Premultiplied);
    image.fill(Qt::transparent);

    QPainter painter(&image);
    painter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing | QPainter::SmoothPixmapTransform);

    int lane = 0;
    int visibleCount = 0;
    for (const auto &line : cfg.lines) {
        if (!lineVisible(line, tMs, cfg)) {
            continue;
        }
        paintLine(painter, cfg, line, tMs, lane, visibleCount);
        lane = std::min(lane + 1, 2);
        ++visibleCount;
    }

    painter.end();
    return image;
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
        out.insert(QStringLiteral("error"), QStringLiteral("output_path is required for C1 smoke render"));
        return out;
    }

    QImage image = renderFrame(*config, tMs);
    const bool saved = image.save(outputPath);
    QJsonObject out = response(saved, QStringLiteral("frame_ready"));
    out.insert(QStringLiteral("t_ms"), tMs);
    out.insert(QStringLiteral("width"), image.width());
    out.insert(QStringLiteral("height"), image.height());
    out.insert(QStringLiteral("output_path"), outputPath);
    out.insert(QStringLiteral("checksum"), QString::number(imageChecksum(image)));
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
