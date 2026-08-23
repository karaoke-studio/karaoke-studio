#pragma once

#include "../render_backend.h"
#include "../../protocol/render_config.h"

namespace krok::subtitle::native::legacy_qt {

krok::subtitle::native::RenderScene gpuSceneFromConfig(
    const protocol::RenderConfig &config
);

}  // namespace krok::subtitle::native::legacy_qt
