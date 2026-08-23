#pragma once

#include "qt_render_types.h"

namespace krok::subtitle::native::legacy_qt {

RenderResult renderFrame(const protocol::RenderConfig &cfg, int tMs);

}  // namespace krok::subtitle::native::legacy_qt
