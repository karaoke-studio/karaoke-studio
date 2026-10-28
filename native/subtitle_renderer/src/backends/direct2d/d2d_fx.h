#pragma once

// 2026-09 逐字几何特效与装饰粒子的确定性求值——与 Python 侧镜像：
//   - 逐字几何：krok_helper/subtitle_render/engine/render/elements/horizontal/
//     transitions.py 的 _geo_char_state / fx_unit_hash
//   - 粒子轨迹：krok_helper/subtitle_render/engine/render/effects/particles.py
//     的 burst_particles_at
//   - 2026-10 自定义图片扫字线的取色层级装饰：krok_helper/subtitle_render/
//     engine/render/elements/scanline_image.py 的 _decorated_scanline_frame
// 改动任一侧的编排参数（时长/行程/缓动/哈希）都必须双侧同步。

#include <cstdint>
#include <string>
#include <vector>

#include "d2d_backend.h"

#include <d2d1_2.h>

namespace krok::subtitle::native {
struct PaintStyle;
struct RgbaColor;
}

namespace krok::subtitle::native::direct2d {

/// 与 transitions.fx_unit_hash 逐位一致的整数哈希 → [0,1)。
float fxUnitHash(std::uint32_t index, std::uint32_t salt);

struct GeoCharState {
    float opacity = 1.0f;
    float dx = 0.0f;
    float dy = 0.0f;
    float rotation = 0.0f;
    float scaleX = 1.0f;
    float scaleY = 1.0f;
    // glow_in / glow_out 的辉光状态（多级描边光晕；见 kGlowHaloStrokes）。
    float glowAlpha = 0.0f;
    float glowRadiusEm = 0.0f;
};

/// 逐字几何特效（tracking_in / wave_in / scatter_out / converge_out）。
/// ``fontPx`` 为该行样式字号（D2D 行空间，已含 layoutReferenceScale）；
/// ``startMs`` 为编排窗口起点（入场=显示起点，退场=退场起点）。
/// 编排与 char_fade 同构（350ms 错峰 + 250ms 行程），「入场/退场动画
/// 时长」仅作 >0 的播放门，值不进本函数。
/// ``hasCenters`` 为真时 converge_out 用真实字符/行中心，否则 index 估算。
GeoCharState geoCharState(
    const std::string &effect,
    float fontPx,
    int index,
    int count,
    int tMs,
    int startMs,
    int durationMs,
    bool exitPhase,
    float charCenterX,
    float lineCenterX,
    bool hasCenters
);

/// 下落风格入场粒子的提前发射量（镜像 particles.fall_entry_lead_ms）：
/// 入场 burst 自显示起点前 20% 粒子寿命开始（花瓣/羽毛 340ms、雪花
/// 300ms），文字逐字渐显窗口不变。其余档位返回 0。
int fallEntryLeadMs(const std::string &effect);

/// 字符中心原点的复合矩阵（镜像 Python character_transform 的 center 分支
/// 点序：T(-c) → Scale → Rotation → T(c+dx)）。
D2D1_MATRIX_3X2_F geoCharMatrix(
    const GeoCharState &state,
    float centerX,
    float centerY
);

struct FxParticle {
    float x = 0.0f;
    float y = 0.0f;
    float rotationDeg = 0.0f;
    float sizePx = 0.0f;
    float alpha = 0.0f;
    // 火焰火舌的速度拉伸（sprite 双轴缩放倍率；其余 kind 恒 1）：
    // 越升越窄越长，镜像 Python ParticleDraw.stretch_x/stretch_y。
    float stretchX = 1.0f;
    float stretchY = 1.0f;
};

/// 一个 ParticleBurst 在 tMs 的全部粒子（镜像 particles.burst_particles_at）。
std::vector<FxParticle> burstParticlesAt(
    const ParticleBurst &burst,
    int tMs,
    float originX,
    float originY,
    float boxW,
    float boxH
);

/// 扫字线烈焰棒的一帧程序化图样（BGRA 预乘，镜像 Python
/// scanline.render_scanline_column_image）。``width``/``height`` 为内部
/// 栅格尺寸，绘制端按目标矩形缩放。
std::vector<std::uint8_t> scanlineColumnPixels(
    const std::string &shape,
    const RgbaColor &outer,
    const RgbaColor &inner,
    const RgbaColor &core,
    const PaintStyle *fill,
    int timeMs,
    unsigned width,
    unsigned height,
    float gradientLeft,
    float gradientWidth
);

/// 自定义图片扫字线一帧的「取色层级」装饰参数（颜色/几何都已解析成
/// 具体数值：效果几何按 图片高/字号 同比缩放后的**源图像素**值）。
/// 与 Python scanline_image._DecorPlan 一一对应。
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

/// 粒子 sprite 名（star4 / ring / note / pixel / petal / feather / bolt /
/// snow / snow_solid）。
inline const char *fxSpriteForKind(const std::string &kind) {
    if (kind == "ripple") {
        return "ring";
    }
    if (kind == "note") {
        return "note";
    }
    if (kind == "heart") {
        return "heart";
    }
    if (kind == "flame") {
        return "flame";
    }
    if (kind == "flame_tall") {
        return "flame_tall";
    }
    if (kind == "flame_wide") {
        return "flame_wide";
    }
    if (kind == "flame_curl") {
        return "flame_curl";
    }
    if (kind == "flame_alt") {
        return "flame_alt";
    }
    if (kind == "flame_alt_b") {
        return "flame_alt_b";
    }
    if (kind == "fireball") {
        return "flame";
    }
    if (kind == "flash") {
        return "star4";
    }
    if (kind == "shockwave") {
        return "ring";
    }
    if (kind == "spark") {
        return "dot";
    }
    if (kind == "flame_glow") {
        return "flame";
    }
    if (kind == "ember") {
        return "dot";
    }
    if (kind == "ash") {
        return "ash";
    }
    if (kind == "smoke") {
        return "smoke";
    }
    if (kind == "assemble" || kind == "dissolve") {
        return "pixel";
    }
    if (kind == "petal") {
        return "petal";
    }
    if (kind == "feather") {
        return "feather";
    }
    if (kind == "lightning" || kind == "lightning_glow") {
        return "bolt";
    }
    if (kind == "snow") {
        return "snow";
    }
    if (kind == "snow_solid") {
        return "snow_solid";
    }
    return "star4";
}

/// 辉光浮现/消散的多级圆角描边层（镜像 transitions.GLOW_HALO_STROKES）：
/// 宽度递增强度递减，叠出高斯感弥散晕。
struct GlowHaloStroke {
    float widthEm;
    float strength;
};
inline constexpr GlowHaloStroke kGlowHaloStrokes[] = {
    {0.10f, 0.42f},
    {0.22f, 0.24f},
    {0.38f, 0.14f},
    {0.58f, 0.08f},
    {0.82f, 0.045f},
};
// 辉光浮现/消散 = 细密横向回声（镜像 transitions.GLOW_ECHO_*）：
// 每侧 N 个字形重影副本、固定间距、亮度几何衰减。
inline constexpr int kGlowEchoCopies = 14;
inline constexpr float kGlowEchoPitchEm = 0.12f;
inline constexpr float kGlowEchoDecay = 0.82f;

/// 唱字描边闪光常量（镜像 painter.STROKE_FLASH_*）。
inline constexpr int kStrokeFlashMs = 240;
inline constexpr float kStrokeFlashAlpha = 0.85f;
inline constexpr float kStrokeFlashWidthBoost = 0.6f;
inline constexpr float kStrokeFlashMinWidthEm = 0.03f;

}  // namespace krok::subtitle::native::direct2d
