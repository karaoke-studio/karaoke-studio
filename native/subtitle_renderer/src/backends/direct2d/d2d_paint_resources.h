#pragma once

#include "../render_backend.h"
#include "d2d_device.h"

#include <d2d1_2.h>
#include <wrl/client.h>

#include <cstdint>
#include <string>
#include <vector>

namespace krok::subtitle::native::direct2d {

Microsoft::WRL::ComPtr<ID2D1Brush> createPaintBrush(
    ID2D1DeviceContext *context,
    const PaintStyle &paint,
    const D2D1_RECT_F &rect,
    const RgbaColor &fallback,
    const D2DDevice &device,
    ID2D1Bitmap1 *image = nullptr,
    float canvasDx = 0.0f,
    float canvasDy = 0.0f,
    std::uint64_t *brushCreated = nullptr
);

D2D1_RECT_F rubyPaintBounds(
    const PaintStyle &paint,
    const D2D1_RECT_F &localBounds,
    const D2D1_RECT_F &horizontalBounds
);

void updatePaintBrush(
    ID2D1Brush *brush,
    const PaintStyle &paint,
    const D2D1_RECT_F &rect,
    float canvasDx,
    float canvasDy
);

Microsoft::WRL::ComPtr<ID2D1Bitmap1> loadWicBitmap(
    ID2D1DeviceContext *context,
    const std::wstring &path
);

// 动图（GIF）解码结果：逐帧全尺寸合成位图 + 每帧时长（ms，已钳 ≥10）。
// 帧数由调用方限制（与 Python 侧 GUIDE_ANIM_MAX_FRAMES 保持一致）。
struct AnimatedBitmapFrames {
    std::vector<Microsoft::WRL::ComPtr<ID2D1Bitmap1>> bitmaps;
    std::vector<int> delaysMs;
};

AnimatedBitmapFrames loadWicAnimatedBitmaps(
    ID2D1DeviceContext *context,
    const std::wstring &path,
    int maxFrames
);

}  // namespace krok::subtitle::native::direct2d
