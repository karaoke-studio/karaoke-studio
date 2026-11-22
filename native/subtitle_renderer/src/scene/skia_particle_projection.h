#pragma once

#include "../model/render_scene.h"
#include "../protocol/render_config.h"
#include <QtCore/QJsonArray>

namespace krok::subtitle::native::scene {
bool hasFadedScanlineSingingParticles(const protocol::RenderConfig&, const protocol::TimingLine&,
                                     const protocol::ResolvedStyle&, const QJsonArray& features);
void projectSkiaParticles(const protocol::RenderConfig&, const protocol::TimingLine&,
                          const protocol::ResolvedStyle&, int timingOffset, int version, const QJsonArray& features,
                          RenderScene&, TextLine&);
}
