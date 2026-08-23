#include "qt_frame_renderer.h"

#include "qt_display_plan.h"
#include "qt_line_painter.h"

#include <QtGui/QPainter>

namespace krok::subtitle::native::legacy_qt {

using protocol::RenderConfig;

RenderResult renderFrame(const RenderConfig &cfg, int tMs) {
    RenderResult result{
        QImage(cfg.physicalWidth(), cfg.physicalHeight(), QImage::Format_ARGB32_Premultiplied),
        RenderDiagnostics{},
    };
    result.image.fill(Qt::transparent);

    QPainter painter(&result.image);
    painter.setRenderHints(QPainter::Antialiasing | QPainter::TextAntialiasing | QPainter::SmoothPixmapTransform);
    if (cfg.dpr != 1.0) {
        painter.scale(cfg.dpr, cfg.dpr);
    }

    const std::vector<DisplayLineRef> visibleLines = visibleDisplayLines(cfg, tMs);
    result.diagnostics.visibleLines = static_cast<int>(visibleLines.size());

    for (const DisplayLineRef &displayLine : visibleLines) {
        if (displayLine.line == nullptr) {
            continue;
        }
        paintLine(
            painter,
            cfg,
            *displayLine.line,
            tMs,
            displayLine.lane,
            result.diagnostics.visibleLines,
            &result.diagnostics
        );
    }

    painter.end();
    return result;
}

}  // namespace krok::subtitle::native::legacy_qt
