#pragma once
#include "../model/paint_types.h"
#include "../protocol/render_config.h"

namespace krok::subtitle::native::scene {
RgbaColor projectColor(const QString& value, const QString& fallback);
PaintStyle projectPaint(const protocol::PaintFillSpec& source, const QString& fallback);
}
