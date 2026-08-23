#pragma once

#include "../render_backend.h"
#include "d2d_device.h"

#include <d2d1_2.h>
#include <wrl/client.h>

namespace krok::subtitle::native::direct2d {

Microsoft::WRL::ComPtr<ID2D1PathGeometry> vectorGlyphGeometry(
    ID2D1Factory1 *factory,
    const VectorGlyph &glyph,
    float pixelSize,
    const D2DDevice &device
);

bool paintNeedsBodyProtection(const PaintStyle &paint);

Microsoft::WRL::ComPtr<ID2D1Geometry> outsideStrokeGeometry(
    ID2D1Factory1 *factory,
    ID2D1Geometry *body,
    float width,
    const D2DDevice &device
);

Microsoft::WRL::ComPtr<ID2D1Geometry> widenedStrokeGeometry(
    ID2D1Factory1 *factory,
    ID2D1Geometry *body,
    float width,
    const D2DDevice &device
);

}  // namespace krok::subtitle::native::direct2d
