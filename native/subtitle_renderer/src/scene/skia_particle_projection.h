#pragma once

#include "../model/render_scene.h"
#include "../protocol/render_config.h"

namespace krok::subtitle::native::scene {
void projectSkiaParticles(const protocol::RenderConfig&, const protocol::TimingLine&,
                          const protocol::ResolvedStyle&, int timingOffset, int version, RenderScene&, TextLine&);
}
