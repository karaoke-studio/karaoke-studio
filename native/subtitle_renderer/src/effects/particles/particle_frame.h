#pragma once
#include "../particles.h"
#include <algorithm>
#include <cmath>
namespace krok::subtitle::native::effects::particles {
struct Frame {
 const ParticleBurst& burst;
 float tau, life, size, travel, emitX, originX, originY, boxW, boxH;
};
// 羽毛/闪电（镜像 particles.py FEATHER_FADE_START /
// LIGHTNING_GLOW_ALPHA）。
constexpr float kFeatherFadeStart = 0.85f;
constexpr float kLightningGlowAlpha = 0.22f;
constexpr float kLightningGlowSizeScale = 1.35f;
// 汇聚式下落（镜像 particles._fall_progress）：三次 Hermite，
// 初速 0.2、接近落点减速到 0；末段透明度随速度渐隐。
constexpr float kFallInitialV = 0.20f;
constexpr float kFallStartMin = 0.55f;
constexpr float kFallStartSpan = 0.45f;
constexpr float kFallSpeedFadeStart = 0.55f;
inline float fallProgress(float p) {
    const float v = std::clamp(p, 0.0f, 1.0f);
    return (kFallInitialV - 2.0f) * v * v * v
        + (3.0f - 2.0f * kFallInitialV) * v * v
        + kFallInitialV * v;
}
inline float fallVelocity(float p) {
    const float v = std::clamp(p, 0.0f, 1.0f);
    return 3.0f * (kFallInitialV - 2.0f) * v * v
        + 2.0f * (3.0f - 2.0f * kFallInitialV) * v
        + kFallInitialV;
}
inline float fallSpeedFade(float p) {
    const float v = std::clamp(p, 0.0f, 1.0f);
    if (v <= kFallSpeedFadeStart) {
        return 1.0f;
    }
    const float speed = fallVelocity(v) / 1.45f;
    float q = std::clamp(speed / 0.55f, 0.0f, 1.0f);
    return q * q * (3.0f - 2.0f * q);
}
// 走字风（镜像 particles.py）：下落粒子向右后方漂 + 固定后倾角。
constexpr float kPetalWindFraction = 0.55f;
constexpr float kSnowWindFraction = 0.45f;
constexpr float kFeatherWindFraction = 0.65f;
constexpr float kFallLeanDeg = -22.0f;
constexpr float kSnowLeanDeg = -30.0f;
constexpr float kFeatherLeanDeg = -32.0f;
// AE Box emitter + 连续出生（Particular 雪/花瓣教程）：出入场每颗在
// 0~45% 寿命内错峰出生，晚出生的从更低处开始；唱字档只做小幅错峰。
constexpr float kFallSpawnSpread = 0.45f;
constexpr float kFallSpawnSpreadSing = 0.18f;
// AE 下落特效景深（Particular 多层 far/mid/near + 随机 size）：每颗
// 用 u4 固定 0.5~1.6× 的深度尺寸，远小近大。
constexpr float kPetalSizeMin = 0.45f;
constexpr float kPetalSizeSpan = 1.20f;
constexpr float kSnowSizeMin = 0.40f;
constexpr float kSnowSizeSpan = 0.88f;
constexpr float kFeatherSizeMin = 0.45f;
constexpr float kFeatherSizeSpan = 1.20f;
// 音符/爱心四档尺寸景深（镜像 particles.PARTICLE_DEPTH_TIERS）：按
// (i + seed>>5) 轮换到不同粒子，与横向出生点解耦（2026-10-22 经验 §3.5）。
constexpr float kParticleDepthTiers[] = {
    0.72f, 0.92f, 1.12f, 1.34f,
};
// 扫字线特效四档尺寸（镜像 particles.SCANLINE_FX_DEPTH_TIERS）：与上面
// 音符/爱心那套既有档位刻意分开——两端旧档值不同，新档统一取这一份。
constexpr float kScanlineDepthTiers[] = {
    0.60f, 0.85f, 1.15f, 1.45f,
};
// 落雷簇状重击时刻（镜像 particles.LIGHTNING_STRIKE_BIRTHS /
// LIGHTNING_STRIKE_JITTER）：真实闪电两三连击 + 停顿 + 补击。
constexpr float kLightningStrikeBirths[] = {
    0.00f, 0.03f, 0.10f, 0.15f, 0.38f, 0.42f, 0.51f, 0.69f, 0.73f,
};
constexpr float kLightningStrikeJitter = 0.035f;

constexpr float kStarYTop = -0.62f;
constexpr float kStarYSpan = 0.92f;
constexpr float kStarYSplitU = 0.62f / 0.92f;

inline float starYFraction(float u) {
    return kStarYTop + kStarYSpan * u;
}

inline int starQuadrant(bool left, bool top) {
    return (left ? 2 : 0) + (top ? 1 : 0);
}

inline float starYResample(
    float u2, float alt6, float alt7, int prevQ, bool left
) {
    if (starQuadrant(left, starYFraction(u2) < 0.0f) != prevQ) {
        return u2;
    }
    if (starQuadrant(left, starYFraction(alt6) < 0.0f) != prevQ) {
        return alt6;
    }
    if (starQuadrant(left, starYFraction(alt7) < 0.0f) != prevQ) {
        return alt7;
    }
    if (prevQ & 1) {
        // 上一颗在上 → 取下半段（frac ∈ [0, +0.30]，必为下侧）。
        return kStarYSplitU + (1.0f - kStarYSplitU) * alt6;
    }
    // 上一颗在下 → 取上半段（frac ∈ [-0.62, 0)，必为上侧）。
    return kStarYSplitU * alt7;
}


std::vector<FxParticle> scanline(const Frame&);
std::vector<FxParticle> decoration(const Frame&);
std::vector<FxParticle> energy(const Frame&);
std::vector<FxParticle> falling(const Frame&);
}
