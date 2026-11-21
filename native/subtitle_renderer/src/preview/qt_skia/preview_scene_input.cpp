#include "preview_scene_input.h"
#include "backends/skia/resolved_input.h"
#include "core/prepared_scene.h"
#include <QtCore/QJsonDocument>
#include <exception>

namespace krok::subtitle::preview {
PreviewSceneResult preparePreviewScene(const QByteArray& ir) {
    const auto document = QJsonDocument::fromJson(ir);
    if (!document.isObject()) return {nullptr, QStringLiteral("Skia 预览场景必须为 JSON 对象")};
    try {
        return {std::make_shared<const ::subtitle::skia::PreparedScene>(
            ::krok::subtitle::native::resolvedSkiaInput(document.object())), {}};
    } catch (const std::exception& error) {
        return {nullptr, QString::fromUtf8(error.what())};
    }
}
}
