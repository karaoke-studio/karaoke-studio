#include "paint_projection.h"
#include <QtGui/QColor>
#include <algorithm>
#include <cmath>
#include <QtCore/QFileInfo>
#include <stdexcept>

namespace krok::subtitle::native::scene {
using protocol::ResolvedStyle;
using protocol::ResolvedLineLayout;
using protocol::PaintFillSpec;

krok::subtitle::native::RgbaColor projectColor(const QString &value, const QString &fallback) {
    if (value.startsWith("linear(")) {
        const auto parts = value.mid(7, value.size() - 8).split(',');
        if (!value.endsWith(')') || parts.size() != 4) throw std::runtime_error("线性颜色格式无效");
        std::array<float, 4> channels;
        for (int i = 0; i < 4; ++i) {
            bool valid = false;
            const double number = parts[i].toDouble(&valid);
            if (!valid || !std::isfinite(number) || number < 0 || number > (i == 3 ? 1 : 65504))
                throw std::runtime_error("线性颜色通道必须为有限的 RGB 强度和 0–1 不透明度");
            channels[i] = float(number);
        }
        const auto byte = [](float c) { return std::uint8_t(std::lround(std::clamp(c, 0.f, 1.f) * 255.f)); };
        return {byte(linearToSrgb(channels[0])), byte(linearToSrgb(channels[1])),
                byte(linearToSrgb(channels[2])), byte(channels[3]), channels};
    }
    QColor color(value);
    if (!color.isValid()) {
        color = QColor(fallback);
    }
    return krok::subtitle::native::RgbaColor{
        static_cast<std::uint8_t>(color.red()),
        static_cast<std::uint8_t>(color.green()),
        static_cast<std::uint8_t>(color.blue()),
        static_cast<std::uint8_t>(color.alpha()),
    };
}

krok::subtitle::native::PaintStyle projectPaint(
    const PaintFillSpec &source,
    const QString &fallback
) {
    using krok::subtitle::native::PaintStop;
    using krok::subtitle::native::PaintStyle;
    PaintStyle paint;
    paint.mode = source.mode.toStdString();
    paint.color = projectColor(source.color, fallback);
    paint.imagePath = source.imagePath.toStdWString();
    paint.imageScale = static_cast<float>(
        std::clamp(source.imageScalePct, 1, 1000) / 100.0
    );
    if (!source.imagePath.isEmpty()) {
        const QFileInfo info(source.imagePath);
        if (info.exists() && info.isFile()) {
            paint.imageModifiedMs = static_cast<std::uint64_t>(
                std::max<qint64>(info.lastModified().toMSecsSinceEpoch(), 0)
            );
            paint.imageSize = static_cast<std::uint64_t>(
                std::max<qint64>(info.size(), 0)
            );
        }
    }
    const auto &sourceStops = source.mode == QStringLiteral("split_vertical")
        ? source.splitStops
        : source.gradientStops;
    paint.stops.reserve(sourceStops.size());
    for (const auto &[position, color] : sourceStops) {
        paint.stops.push_back(PaintStop{
            static_cast<float>(std::clamp(position / 100.0, 0.0, 1.0)),
            projectColor(color, source.color),
        });
    }
    if (paint.stops.empty()) {
        if (source.mode == QStringLiteral("split_vertical")) {
            paint.stops = {
                {0.0f, projectColor(source.splitTopColor, source.color)},
                {
                    static_cast<float>(std::clamp(
                        source.splitPositionPct / 100.0, 0.0, 1.0
                    )),
                    projectColor(source.splitBottomColor, source.color),
                },
                {1.0f, projectColor(source.splitBottomColor, source.color)},
            };
        } else {
            paint.stops = {
                {0.0f, projectColor(source.startColor, source.color)},
                {1.0f, projectColor(source.endColor, source.color)},
            };
        }
    }
    return paint;
}

}  // namespace krok::subtitle::native::scene
