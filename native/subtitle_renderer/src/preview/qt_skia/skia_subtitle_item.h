#pragma once
#include <QtQuick/QQuickRhiItem>
#include <QtQml/qqmlregistration.h>

namespace krok::subtitle::preview {
// Host-side subtitle preview surface. It renders on Qt's scene graph render thread into the
// texture QQuickRhiItem owns, so frames reach the screen without a CPU readback. Subtitle
// drawing itself comes from the shared Skia core; this type only bridges Qt and the GPU.
class SkiaSubtitleItem : public QQuickRhiItem {
    Q_OBJECT
    QML_ELEMENT
public:
    explicit SkiaSubtitleItem(QQuickItem* parent = nullptr);
protected:
    QQuickRhiItemRenderer* createRenderer() override;
};
}
