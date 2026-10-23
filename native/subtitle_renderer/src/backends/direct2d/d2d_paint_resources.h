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
    std::uint64_t *brushCreated = nullptr,
    float layoutScale = 1.0f
);

// 扫字线羽化 mask 画刷：把逐像素 alpha 贴图（BGRA 预乘、行距 = width*4）
// 上传为位图并包成 CLAMP + 最近邻的画刷，平移到 (originX, originY) 实现
// 与 mask 矩形的 1:1 对齐（无过滤、无外扩）。像素生成（Bayer 抖动距离场）
// 留在调用方，这里只负责 D2D 资源构造。
Microsoft::WRL::ComPtr<ID2D1BitmapBrush> createScanlineMaskBrush(
    ID2D1DeviceContext *context,
    const std::uint8_t *pixels,
    UINT32 width,
    UINT32 height,
    float originX,
    float originY,
    const D2DDevice &device
);

// 扫字线烈焰棒图样位图（BGRA 预乘、行距 = width*4）：逐帧生成后
// 交给 DrawBitmap 等比缩放到锋面目标矩形。像素生成留在 d2d_fx.cpp，这里
// 只负责 D2D 资源构造。
Microsoft::WRL::ComPtr<ID2D1Bitmap> createScanlineColumnBitmap(
    ID2D1DeviceContext *context,
    const std::uint8_t *pixels,
    UINT32 width,
    UINT32 height,
    const D2DDevice &device
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
    float canvasDy,
    float layoutScale = 1.0f
);

Microsoft::WRL::ComPtr<ID2D1Bitmap1> loadWicBitmap(
    ID2D1DeviceContext *context,
    const std::wstring &path,
    // 可选染色：非空时按 strength 把图像像素向 tint 混合（alpha 不变），
    // 镜像 Python painter 的 _tinted（SourceIn 叠色）语义。
    const RgbaColor *tint = nullptr,
    int tintStrengthPct = 100,
    // true: 逐通道乘色（保留素材内部明暗/描边，谱线层用）；
    // false: 压向目标色的混合（普通上色用）。
    bool multiplyTint = false
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
