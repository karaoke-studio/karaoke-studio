#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QString>
#include <memory>

namespace subtitle::skia { class PreparedScene; }
namespace krok::subtitle::preview {
struct PreviewSceneResult {
    std::shared_ptr<const ::subtitle::skia::PreparedScene> scene;
    QString error;
};

PreviewSceneResult preparePreviewScene(const QByteArray& ir);
}
