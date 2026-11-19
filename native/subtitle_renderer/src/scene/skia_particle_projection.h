#pragma once

#include "../model/render_scene.h"
#include "../protocol/render_config.h"

namespace krok::subtitle::native::scene {
void projectSkiaSingParticles(const protocol::RenderConfig&, const protocol::TimingLine&,
                              const protocol::ResolvedStyle&, int timingOffset, bool extended, RenderScene&, TextLine&);
}
