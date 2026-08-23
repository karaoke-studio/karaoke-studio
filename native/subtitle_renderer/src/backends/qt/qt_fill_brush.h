#pragma once

#include "../../protocol/render_config.h"

#include <QtCore/QRectF>
#include <QtGui/QBrush>

namespace krok::subtitle::native::legacy_qt {

QBrush brushForFill(
    const protocol::PaintFillSpec &fill,
    const QRectF &rect
);

}  // namespace krok::subtitle::native::legacy_qt
