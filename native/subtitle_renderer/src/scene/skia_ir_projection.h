#pragma once
#include "../model/render_scene.h"
#include <QtCore/QJsonObject>
namespace krok::subtitle::native::scene {
RenderScene skiaSceneFromIr(const QJsonObject& ir);
}
