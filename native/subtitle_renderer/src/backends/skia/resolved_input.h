#pragma once
#include "core/resolved_scene.h"
#include <QtCore/QJsonObject>
namespace krok::subtitle::native {
::subtitle::skia::ResolvedSubtitleInput resolvedSkiaInput(const QJsonObject& ir);
}
