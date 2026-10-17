#pragma once

// 2026-09 逐字几何特效与装饰粒子的确定性求值——与 Python 侧镜像：
//   - 逐字几何：krok_helper/subtitle_render/engine/render/elements/horizontal/
//     transitions.py 的 _geo_char_state / fx_unit_hash
//   - 粒子轨迹：krok_helper/subtitle_render/engine/render/effects/particles.py
//     的 burst_particles_at
// 改动任一侧的编排参数（时长/行程/缓动/哈希）都必须双侧同步。

#include <cstdint>
#include <string>
#include <vector>

#include "d2d_backend.h"

#include <d2d1_2.h>

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

/// 粒子 sprite 名（star4 / ring / note / pixel / petal / snow / snow_solid）。
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
    if (kind == "flame_alt") {
        return "flame_alt";
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
    if (kind == "assemble" || kind == "dissolve") {
        return "pixel";
    }
    if (kind == "petal") {
        return "petal";
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
