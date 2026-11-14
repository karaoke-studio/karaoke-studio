#pragma once
#include "../model/paint_types.h"

namespace krok::subtitle::native::effects {
struct ScanlineImageDecor {
    bool tint = false;
    RgbaColor tintColor{255, 255, 255, 255};
    // 上色强度（0~100）：预乘空间按比例混合原色与目标色，alpha 保留。
    int tintStrength = 100;
    int strokeWidth = 0;      // 0 = 不画
    RgbaColor strokeColor{255, 255, 255, 255};
    int stroke2Width = 0;
    RgbaColor stroke2Color{255, 255, 255, 255};
    bool glow = false;
    int glowRadius = 0;
    RgbaColor glowColor{255, 255, 255, 255};
    bool shadow = false;
    RgbaColor shadowColor{255, 255, 255, 255};
    float shadowOffsetX = 0.0f;
    float shadowOffsetY = 0.0f;

    bool operator==(const ScanlineImageDecor &) const = default;
};

struct ScanlineImageComposite {
    /// BGRA 预乘像素（含 margin 外扩）。
    std::vector<std::uint8_t> pixels;
    int width = 0;
    int height = 0;
    int margin = 0;
};

/// 把一帧（ARGB32_Premultiplied 的 QImage）按取色层级烘成装饰整图：
/// 发光 → 阴影 → 描边2 → 描边 → 本体（实色上色时本体为实色剪影）。
/// 纯 CPU（形态学/盒式模糊/合成），可在 BeginDraw 会话内调用，随后用
/// CreateBitmap 上传。镜像 Python _decorated_scanline_frame + band
/// _edge_source 的算法口径（膨胀=可分离 max、发光=3 趟盒式模糊）。
/// ``frame`` 为 ARGB32_Premultiplied 的原始像素（小端内存序即 BGRA 字节
/// 序）；``bytesPerLine`` 允许与 width*4 不同（行对齐）。刻意不依赖
/// Qt 头：本模块保持 Qt 无关（2026-10 实测新引 <QImage> 会把 Qt 的
/// ``emit`` 等关键字宏带进来，与文件里既有的同名局部变量冲突）。
ScanlineImageComposite scanlineImageCompositePixels(
    const std::uint8_t *frame,
    int bytesPerLine,
    int width,
    int height,
    const ScanlineImageDecor &decor
);


}
