#pragma once

#include "../render_backend.h"
#include "d2d_device.h"
#include "d2d_stroke_outline.h"

#include <d2d1_2.h>

#include <string>
#include <wrl/client.h>

namespace krok::subtitle::native::direct2d {

Microsoft::WRL::ComPtr<ID2D1PathGeometry> vectorGlyphGeometry(
    ID2D1Factory1 *factory,
    const VectorGlyph &glyph,
    float pixelSize,
    const D2DDevice &device
);

// 指示灯「星型 / 音符」路径（0..size 单位坐标，布尔并集轮廓与
// QPainterPath::united() 同口径）；边长比例镜像 Painter 的
// _lit_star_note_path。
Microsoft::WRL::ComPtr<ID2D1PathGeometry> lampShapeGeometry(
    ID2D1Factory1 *factory,
    const std::string &litStyle,
    float size,
    const D2DDevice &device
);


bool paintNeedsBodyProtection(const PaintStyle &paint);

Microsoft::WRL::ComPtr<ID2D1Geometry> outsideStrokeGeometry(
    ID2D1Factory1 *factory,
    ID2D1Geometry *body,
    float width,
    const D2DDevice &device,
    std::shared_ptr<StrokeOutlineCache> *cache = nullptr
);

Microsoft::WRL::ComPtr<ID2D1Geometry> widenedStrokeGeometry(
    ID2D1Factory1 *factory,
    ID2D1Geometry *body,
    float width,
    const D2DDevice &device
);

}  // namespace krok::subtitle::native::direct2d
