#include "d2d_fx.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::direct2d {
namespace {

// 幅度参数（与 transitions.py 顶部常量镜像）。
constexpr float kTrackingSpreadEm = 1.6f;
constexpr float kWaveAmplitudeEm = 0.5f;
constexpr float kWaveAmplitudeMinPx = 24.0f;
constexpr float kScatterTravelEm = 2.2f;
constexpr float kScatterShrink = 0.4f;
constexpr float kConvergeShrink = 0.2f;
// 编排与 Python char_fade 完全同构（镜像 transitions.CHAR_FADE_* 常量）：
// 350ms 内逐字错峰、每字 250ms 走完行程；激活门 = entry/exitDurationMs>0
//（专门的播放时间，窗口判定同 char_fade 的 600ms）。
constexpr int kCharFadeIntroDelayMs = 350;
constexpr int kCharFadeInOutTimeMs = 250;
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

float clampedRatio(int elapsedMs, int durationMs) {
    if (durationMs <= 0) {
        return 1.0f;
    }
    const float value = static_cast<float>(elapsedMs)
        / static_cast<float>(durationMs);
    return std::clamp(value, 0.0f, 1.0f);
}

float centerNorm(int index, int count) {
    if (count <= 1) {
        return 0.0f;
    }
    const float half = static_cast<float>(count - 1) / 2.0f;
    return (static_cast<float>(index) - half) / half;
}

}  // namespace

float fxUnitHash(std::uint32_t index, std::uint32_t salt) {
    std::uint32_t h = index * 0x85EBCA77u + salt * 0xC2B2AE3Du;
    h ^= h >> 15;
    h *= 0x2545F491u;
    h ^= h >> 13;
    return static_cast<float>(h & 0xFFFFFFu) / 16777216.0f;
}

// 星光族（sparkle/twinkle）纵向锚点偏置 + 连续象限去重——镜像
// particles.star_y_fraction / star_y_resample（2026-10 用户口径：尽量多
// 出现在主文字上侧，锚点只稍微溢出字形顶 ≈12% 行高，连续两颗不落同
// 象限）。
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

int fallEntryTextDelayMs(const std::string &effect) {
    // 镜像 transitions.FALL_ENTRY_TEXT_DELAY_MS：文字渐显延后 20% 粒子寿命。
    if (effect == "petal" || effect == "feather") {
        return 340;
    }
    if (effect == "snow" || effect == "snow_solid") {
        return 300;
    }
    return 0;
}

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
) {
    GeoCharState state;
    const float font = std::max(fontPx, 1.0f);
    // 编排总时长 = 「入场/退场动画时长」旋钮（调用方门保证 >0）；
    // 错峰:行程保持 7:5（默认 600ms 时即 350/250）。
    const int totalMs = std::clamp(durationMs, 120, 3000);
    const int staggerMs = totalMs * 7 / 12;
    const int travelMs = std::max(totalMs - staggerMs, 40);
    // 下落风格入场：文字逐字渐显整体延后到粒子已开始下落之后；退场与
    // 其余档位不加延迟。
    const int textDelayMs = exitPhase ? 0 : fallEntryTextDelayMs(effect);
    const auto staggerProgress = [&]() {
        const int step = count <= 1 ? 0 : staggerMs / (count - 1);
        return clampedRatio(
            tMs - startMs - step * index - textDelayMs, travelMs
        );
    }();
    // 入场透明度 4 倍速上升 + smoothstep 运动包络：字符在行程前 1/4 即
    // 全不透明，位移仍有 8 成以上，保证「先清晰可见、再走完行程」。
    const auto smoothstep = [](float p) {
        return p * p * (3.0f - 2.0f * p);
    };

    if (effect == "tracking_in") {
        const float p = staggerProgress;
        const float q = smoothstep(p);
        const float spread = font * kTrackingSpreadEm;
        state.dx = centerNorm(index, count) * spread * (1.0f - q);
        state.opacity = std::min(p * 4.0f, 1.0f);
        return state;
    }
    if (effect == "wave_in") {
        const float p = staggerProgress;
        const float q = smoothstep(p);
        const float amplitude = std::max(font * kWaveAmplitudeEm, kWaveAmplitudeMinPx);
        state.dy = amplitude * (1.0f - q);
        state.opacity = std::min(p * 4.0f, 1.0f);
        return state;
    }
    if (effect == "scatter_out") {
        const float p = staggerProgress;
        const double theta = static_cast<double>(
            fxUnitHash(static_cast<std::uint32_t>(index), 1u)
        ) * 2.0 * 3.14159265358979323846;
        const float speed = 0.55f + 0.45f * fxUnitHash(
            static_cast<std::uint32_t>(index), 2u
        );
        const float spin = (
            fxUnitHash(static_cast<std::uint32_t>(index), 3u) - 0.5f
        ) * 720.0f;
        const float travel = font * kScatterTravelEm * speed
            * (1.0f - (1.0f - p) * (1.0f - p));
        const float scale = 1.0f - kScatterShrink * p;
        state.opacity = (1.0f - p) * (1.0f - p);
        state.dx = static_cast<float>(std::cos(theta)) * travel;
        state.dy = static_cast<float>(std::sin(theta)) * travel;
        state.rotation = spin * p;
        state.scaleX = scale;
        state.scaleY = scale;
        return state;
    }
    if (effect == "stretch_in") {
        // 逐字拉伸入场（光条凝聚，Aegisub \fscx+\blur+\t 同款语言）。
        const float p = staggerProgress;
        state.opacity = std::min(p * 4.0f, 1.0f);
        const float q = smoothstep(p);
        state.glowAlpha = (1.0f - q) * std::sqrt(1.0f - q);
        state.glowRadiusEm = 1.2f - 0.5f * q;
        state.scaleX = 1.0f + 2.2f * (1.0f - q);
        return state;
    }
    if (effect == "stretch_out") {
        // 逐字拉伸退场：反向——清晰字形横向拉伸成光条弥散，同时淡出。
        const float p = staggerProgress;
        const float q = smoothstep(p);
        state.opacity = 1.0f - q;
        constexpr float pi = 3.14159265358979323846f;
        state.glowAlpha = std::sin(pi * q) * 0.9f;
        state.glowRadiusEm = 0.7f + 0.8f * q;
        state.scaleX = 1.0f + 2.2f * q;
        return state;
    }
    if (effect == "glow_in" || effect == "glow_out") {
        // 辉光浮现/消散：整行同步（无逐字错峰）横向拉伸 + 高强度
        // 大半径光晕（逐字拉伸的整行版；曲线与 Python 镜像）。
        const float p = clampedRatio(tMs - startMs, travelMs);
        const float q = smoothstep(p);
        if (effect == "glow_in") {
            state.opacity = std::min(p * 3.0f, 1.0f);
            state.glowAlpha = (1.0f - q) * std::sqrt(1.0f - q);
            state.glowRadiusEm = 1.0f - 0.25f * q;
            state.scaleX = 1.0f + 0.6f * (1.0f - q);
        } else {
            state.opacity = 1.0f - q;
            constexpr float pi = 3.14159265358979323846f;
            state.glowAlpha = std::sin(pi * q);
            state.glowRadiusEm = 1.0f + 0.9f * q;
            state.scaleX = 1.0f + 0.6f * q;
        }
        return state;
    }
    if (effect == "flame") {
        // 火焰档文字本体（镜像 transitions.py flame 分支）：参考既有
        // stretch_in/out 的光条凝聚/弥散语言，但延后到火烧到后段（62%）
        // 才发生——入场前 62% 隐形，后 38% 由 3.2× 横向光条收束成字形；
        // 退场前 62% 清晰，后 38% 横向拉伸成光条淡出。不叠 stretch 的
        // 辉光层（火焰粒子本身已是高亮层）。
        const float p = staggerProgress;
        const float raw = std::clamp((p - 0.62f) / 0.38f, 0.0f, 1.0f);
        const float q = smoothstep(raw);
        if (exitPhase) {
            state.opacity = 1.0f - q;
            state.scaleX = 1.0f + 2.2f * q;
        } else {
            state.opacity = std::min(raw * 4.0f, 1.0f);
            state.scaleX = 1.0f + 2.2f * (1.0f - q);
        }
        return state;
    }
    if (effect == "sparkle" || effect == "ripple" || effect == "note"
        || effect == "heart" || effect == "petal"
        || effect == "snow" || effect == "snow_solid"
        || effect == "feather" || effect == "lightning") {
        // 粒子类出入场动画的本体：文字逐字显形（入场 4 倍速透明度，
        // 粒子拼接同款编排），退场逐字淡出；粒子由 planner 叠加。
        const float p = staggerProgress;
        if (exitPhase) {
            const float q = smoothstep(p);
            state.opacity = 1.0f - q;
        } else {
            state.opacity = std::min(p * 4.0f, 1.0f);
        }
        return state;
    }
    if (effect == "assemble_in") {
        // 粒子拼接：粒子飞入期间字形未成形，抵达后显形。
        const float p = staggerProgress;
        const float appear = smoothstep(p);
        state.opacity = std::max(0.0f, (appear - 0.45f) / 0.55f);
        return state;
    }
    if (effect == "dissolve_out") {
        // 粒子消散：字形先行散去，粒子随后飞离。
        const float p = staggerProgress;
        const float vanish = std::clamp(p / 0.55f, 0.0f, 1.0f);
        state.opacity = 1.0f - vanish * vanish * (3.0f - 2.0f * vanish);
        return state;
    }
    // converge_out
    const float p = staggerProgress;
    const float eased = p * p * (3.0f - 2.0f * p);
    if (hasCenters) {
        state.dx = (lineCenterX - charCenterX) * eased;
    } else {
        const float halfWidthEm = static_cast<float>(count) * 0.5f;
        state.dx = -centerNorm(index, count) * font * halfWidthEm * eased;
    }
    const float scale = 1.0f - kConvergeShrink * eased;
    state.opacity = 1.0f - eased;
    state.scaleX = scale;
    state.scaleY = scale;
    return state;
}

D2D1_MATRIX_3X2_F geoCharMatrix(
    const GeoCharState &state,
    float centerX,
    float centerY
) {
    return D2D1::Matrix3x2F::Translation(-centerX, -centerY)
        * D2D1::Matrix3x2F::Scale(state.scaleX, state.scaleY)
        * D2D1::Matrix3x2F::Rotation(state.rotation)
        * D2D1::Matrix3x2F::Translation(
            centerX + state.dx, centerY + state.dy
        );
}

std::vector<FxParticle> burstParticlesAt(
    const ParticleBurst &burst,
    int tMs,
    float originX,
    float originY,
    float boxW,
    float boxH
) {
    std::vector<FxParticle> out;
    if (tMs < burst.startMs || tMs > burst.endMs) {
        return out;
    }
    const int life = std::max(burst.endMs - burst.startMs, 1);
    const float tau = static_cast<float>(tMs - burst.startMs);
    const float size = burst.sizePx;
    const float travel = burst.travelPx;

    if (burst.kind == "ripple") {
        // 水波纹（镜像 particles.burst_particles_at）：3 枚细描边圆环错峰
        // 扩散（第 i 环延迟 150ms），「自小圈快速扩开 → 满径时淡出」。
        const int rings = std::max(burst.count, 1);
        for (int i = 0; i < rings; ++i) {
            const float delay = static_cast<float>(i) * 150.0f;
            const float lifeI = std::max(
                static_cast<float>(life) - delay, 1.0f
            );
            const float p = std::clamp((tau - delay) / lifeI, 0.0f, 1.0f);
            if (p <= 0.0f) {
                continue;
            }
            const float eased = 1.0f - (1.0f - p) * (1.0f - p);
            const float scale = size * (0.25f + 0.85f * eased);
            out.push_back(FxParticle{
                originX, originY, 0.0f, scale, (1.0f - p) * (1.0f - p),
            });
        }
        return out;
    }

    const std::uint32_t seed = burst.seed;
    int starPrevQ = -1;  // 星光族象限链：-1 = 尚无上一颗
    out.reserve(static_cast<std::size_t>(std::max(burst.count, 0)));
    for (int i = 0; i < burst.count; ++i) {
        const std::uint32_t index = seed + static_cast<std::uint32_t>(i);
        if (burst.kind == "sparkle" || burst.kind == "twinkle") {
            // 整句扫过（镜像 particles.burst_particles_at sparkle 分支）；
            // 唱字星光（twinkle）2026-10 起并入同一运动学（字框内小扫过）。
            const float u1 = fxUnitHash(index, 1u);
            const bool left = (u1 - 0.5f) < 0.0f;
            const float u2 = starYResample(
                fxUnitHash(index, 2u),
                fxUnitHash(index, 6u),
                fxUnitHash(index, 7u),
                starPrevQ,
                left
            );
            const float yOff = starYFraction(u2) * boxH;
            starPrevQ = starQuadrant(left, yOff < 0.0f);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            float delay;
            if (burst.sweep > 0) {
                delay = u1 * 350.0f + u5 * 90.0f;
            } else if (burst.sweep < 0) {
                delay = (1.0f - u1) * 350.0f + u5 * 90.0f;
            } else {
                // 短窗口（如 120ms 退场）不能让随机出生延迟吞掉整组粒子：
                // 钳到「寿命 - 最短可见寿命」（镜像 particles.py 的
                // SPARKLE_BIRTH_JITTER_MS / SPARKLE_MIN_VISIBLE_MS）。
                delay = std::min(
                    u5 * 130.0f,
                    std::max(
                        static_cast<float>(life) - 40.0f, 0.0f
                    )
                );
            }
            const float denom = std::max(
                static_cast<float>(life) - delay, 1.0f
            );
            const float p = std::clamp((tau - delay) / denom, 0.0f, 1.0f);
            if (p <= 0.0f) {
                continue;
            }
            const float sizeI = size * (0.65f + 0.70f * u3);
            const float sizeF = sizeI * (0.15f + 0.30f * u4);
            const float scale = sizeI + (sizeF - sizeI) * p;
            const float driftX = (u4 - 0.5f) * travel * 0.35f * p;
            const float driftY = -(0.30f + 0.70f * u2) * travel * 0.45f * p;
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW + driftX,
                originY + yOff + driftY,
                u3 * 90.0f + 60.0f * p,
                scale,
                1.0f - p,
            });
        } else if (burst.kind == "twinkle_classic") {
            // 旧版唱字星光（2026-10 运动学改造前的形态，逐字复刻自被
            // 2775dc99 删除的 twinkle 分支；镜像 particles.py 同名分支）：
            // 位置对称铺满整个字框、随机固定旋转、随机寿命（300–450ms）
            // 与峰值大小，sin 包络「出生→放大→熄灭」的原地闪烁。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float lifeI = 300.0f + 150.0f * u4;
            const float spread = std::max(
                static_cast<float>(life) - lifeI, 0.0f
            );
            const float delay = u3 * spread * 0.8f;
            const float p = std::clamp((tau - delay) / lifeI, 0.0f, 1.0f);
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            constexpr float pi = 3.14159265358979323846f;
            const float envelope = std::sin(pi * p);
            const float peak = size * (0.65f + 0.70f * u4);
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.95f,
                originY + (u2 - 0.5f) * boxH * 0.85f,
                u3 * 120.0f - 30.0f,
                peak * (0.30f + 0.70f * envelope),
                envelope,
            });
        } else if (burst.kind == "assemble" || burst.kind == "dissolve") {
            // 粒子拼接/消散（镜像 particles.burst_particles_at）：
            // 拼接=自随机方向远端飞向字形内随机落点后缩小熄灭；
            // 消散=自字形内随机起点飞向随机方向远端并淡出。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float delay = burst.kind == "assemble"
                ? u3 * 90.0f : u3 * 70.0f;
            const float p = std::clamp(
                (tau - delay) / std::max(
                    static_cast<float>(life) - delay, 1.0f
                ),
                0.0f, 1.0f
            );
            if (p <= 0.0f) {
                continue;
            }
            if (burst.kind == "assemble" && p >= 1.0f) {
                continue;
            }
            const float eased = p * p * (3.0f - 2.0f * p);
            constexpr float pi = 3.14159265358979323846f;
            const float theta = u2 * 2.0f * pi;
            const float speed = 0.6f + 0.5f * u4;
            const float landX = (u1 - 0.5f) * boxW * 0.9f;
            const float landY = (u4 - 0.5f) * boxH * 0.8f;
            if (burst.kind == "assemble") {
                const float startX = landX + std::cos(theta) * travel * speed;
                const float startY = landY
                    + std::sin(theta) * travel * speed * 0.7f;
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * eased,
                    originY + startY + (landY - startY) * eased,
                    (u3 - 0.5f) * 240.0f * (1.0f - eased),
                    size * (0.9f - 0.35f * eased),
                    std::min(p * 6.0f, 1.0f) * (1.0f - p * p),
                });
            } else {
                out.push_back(FxParticle{
                    originX + landX
                        + std::cos(theta) * travel * speed * eased,
                    originY + landY
                        + std::sin(theta) * travel * speed * 0.7f * eased,
                    (u3 - 0.5f) * 240.0f * eased,
                    size * (0.75f + 0.25f * (1.0f - eased)),
                    (1.0f - p) * (1.0f - p * 0.5f),
                });
            }
        } else if (burst.kind == "note" || burst.kind == "heart") {
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float delay = u3 * static_cast<float>(life) * 0.35f;
            const float p = std::clamp(
                (tau - delay)
                    / std::max(static_cast<float>(life) - delay, 1.0f),
                0.0f,
                1.0f
            );
            if (p <= 0.0f) {
                continue;
            }
            constexpr float pi = 3.14159265358979323846f;
            const float rise = travel * (1.0f - (1.0f - p) * (1.0f - p));
            const float sway = std::sin((u2 + p * 1.5f) * 2.0f * pi)
                * size * 0.4f;
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.8f + sway,
                originY - boxH * 0.35f - rise,
                (u2 - 0.5f) * 28.0f,
                size * (0.70f + 0.60f * u1)
                    * (0.85f + 0.15f * std::sin(pi * p)),
                1.0f - std::sqrt(p) * p,
            });
        } else if (burst.kind == "flame" || burst.kind == "flame_alt") {
            // 火焰 v3（镜像 particles.burst_particles_at flame 分支）：
            // 独立寿命 + 错峰出生形成连续火流；加速上腾 p^1.35（浮力）+
            // 双频湍流横摆；尺寸快涨后收（Aeg \fscx 60→240→40 包络）；
            // 高频闪烁 + 长灭；越升越窄越长（stretchX/Y 速度拉伸）。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            const float birth = u3 * life * 0.34f;
            const float lifeI = std::min(
                life * (0.62f + 0.38f * u5),
                std::max(life - birth, 1.0f)
            );
            const float p = (tau - birth) / lifeI;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float rise = travel * std::pow(p, 1.35f);
            const float sway = (
                std::sin((u2 + p * 1.7f) * 2.0f * pi) * 0.45f
                + std::sin((u4 * 0.7f + p * 3.3f) * 2.0f * pi) * 0.18f
            ) * size * (0.35f + 0.65f * p);
            const float grow = 1.0f - std::pow(
                1.0f - std::min(p / 0.16f, 1.0f), 2.0f
            );
            // std::max(..., 0) 避免负数进入 pow；p>0.5 后才开始“冷却收缩”。
            const float cool = 1.0f - 0.50f * std::pow(
                std::max((p - 0.50f) / 0.50f, 0.0f), 1.3f
            );
            const float flicker = 0.70f + 0.30f * std::sin(
                (p * 5.5f + u4 * 3.0f) * 2.0f * pi
            );
            const float alpha = std::min(p / 0.08f, 1.0f)
                * std::pow(1.0f - p, 0.45f) * flicker;
            const float lean = std::cos((u2 + p * 1.7f) * 2.0f * pi) * 16.0f
                + (u4 - 0.5f) * 30.0f
                + std::sin((u5 + p * 2.1f) * 2.0f * pi) * 8.0f;
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.90f + sway,
                originY + (0.04f + 0.52f * u2) * boxH - rise,
                lean,
                size * (0.35f + 0.85f * grow) * cool
                    * (0.60f + 0.80f * u1),
                alpha,
                1.0f - 0.35f * p,
                1.0f + 1.05f * p,
            });
        } else if (burst.kind == "fireball") {
            // 火球起爆（镜像 particles fireball 分支）：径向炸开 + 快速
            // 膨胀收缩，配合 spark/shockwave 做点燃/熄灭冲击。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            const float birth = u3 * life * 0.10f;
            const float p = std::clamp(
                (tau - birth) / std::max(life - birth, 1.0f),
                0.0f, 1.0f
            );
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float eased = 1.0f - (1.0f - p) * (1.0f - p);
            const float angle = u1 * 2.0f * pi;
            const float spread = travel * (0.35f + 0.55f * u2) * eased;
            out.push_back(FxParticle{
                originX + std::cos(angle) * spread,
                originY + std::sin(angle) * spread * 0.55f
                    - travel * 0.80f * eased,
                u4 * 360.0f + p * (120.0f + 160.0f * u5),
                size * (0.35f + 0.95f * eased) * (1.0f - 0.15f * p),
                std::min(p / 0.06f, 1.0f) * std::pow(1.0f - p, 1.6f),
            });
        } else if (burst.kind == "flash") {
            // 冲击白闪（镜像 particles flash 分支）。
            const float u1 = fxUnitHash(index, 1u);
            const float p = tau / life;
            if (p >= 1.0f) {
                continue;
            }
            out.push_back(FxParticle{
                originX, originY,
                u1 * 90.0f + p * 30.0f,
                size * (0.45f + 0.95f * p),
                std::pow(1.0f - p, 1.5f),
            });
        } else if (burst.kind == "shockwave") {
            // 冲击环（镜像 particles shockwave 分支）。
            const float u1 = fxUnitHash(index, 1u);
            const float p = tau / life;
            if (p >= 1.0f) {
                continue;
            }
            const float eased = 1.0f - (1.0f - p) * (1.0f - p);
            out.push_back(FxParticle{
                originX, originY,
                (u1 - 0.5f) * 20.0f,
                size * (0.25f + 1.25f * eased),
                std::pow(1.0f - p, 2.0f) * 0.65f,
            });
        } else if (burst.kind == "spark") {
            // 飞散电火花/火星（镜像 particles spark 分支）。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            constexpr float pi = 3.14159265358979323846f;
            const float delay = u3 * 60.0f;
            const float p = std::clamp(
                (tau - delay) / std::max(life - delay, 1.0f),
                0.0f, 1.0f
            );
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float angle = u1 * 2.0f * pi;
            const float speed = travel * (0.60f + 0.90f * u2);
            out.push_back(FxParticle{
                originX + std::cos(angle) * speed * p,
                originY + std::sin(angle) * speed * p * 0.60f
                    + travel * 0.55f * p * p,
                p * 360.0f * (u4 - 0.5f),
                size * (0.70f + 0.60f * u4) * (1.0f - 0.40f * p),
                std::min(p / 0.05f, 1.0f) * std::pow(1.0f - p, 1.2f),
            });
        } else if (burst.kind == "flame_glow") {
            // 火焰背光（镜像 particles flame_glow 分支）：大尺寸低透明度
            // 火形慢升、缓闪，画在文字之下（front=False）做灼烧辉光。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            constexpr float pi = 3.14159265358979323846f;
            const float birth = u3 * life * 0.25f;
            const float lifeI = std::min(
                life * (0.70f + 0.30f * u4),
                std::max(life - birth, 1.0f)
            );
            const float p = (tau - birth) / lifeI;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float rise = travel * std::pow(p, 1.05f);
            const float sway = std::sin((u2 + p * 1.2f) * 2.0f * pi)
                * size * 0.18f;
            const float glowPulse = 0.72f + 0.28f * std::sin(
                (p * 2.5f + u2) * 2.0f * pi
            );
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.70f + sway,
                originY + (0.05f + 0.25f * u2) * boxH - rise,
                (u4 - 0.5f) * 10.0f,
                size * (0.82f + 0.18f * std::sin(pi * p))
                    * (0.85f + 0.30f * u1),
                std::min(p / 0.15f, 1.0f)
                    * std::pow(1.0f - p, 1.2f) * glowPulse * 0.18f,
                1.0f,
                1.0f + 0.25f * p,
            });
        } else if (burst.kind == "lightning" || burst.kind == "lightning_glow") {
            // 闪电（镜像 particles.burst_particles_at lightning 分支）：
            // 每颗=一次 100~240ms 的短放电，窗口内随机时刻重击；**生长**
            // 用 stretchY 0.18→1.10 并锚住顶端/底端（AE Trim Paths /
            // Direction 关键帧效果）。核心 bolt 在前；同轨迹放大副本
            // （lightning_glow）在后当辉光。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            const float birth = u3 * life * 0.72f;
            const float lifeI = std::min(
                100.0f + 140.0f * u4,
                std::max(static_cast<float>(life) - birth, 1.0f)
            );
            const float p = (tau - birth) / lifeI;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float growP = std::min(p / 0.45f, 1.0f);
            const float grow = 0.18f + 0.92f * growP;
            const float anchor = burst.kind == "lightning_glow"
                ? size / kLightningGlowSizeScale
                : size;
            const float direction = u5 >= 0.25f ? 1.0f : -1.0f;
            const float strobe = 0.55f + 0.45f * std::abs(
                std::sin((p * 2.6f + u5 * 2.0f) * pi)
            );
            float alpha = std::min(p / 0.06f, 1.0f)
                * std::pow(1.0f - p, 0.65f) * strobe;
            if (burst.kind == "lightning_glow") {
                alpha *= kLightningGlowAlpha;
            }
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 1.10f,
                originY - boxH * 0.08f
                    + (u2 - 0.5f) * boxH * 0.10f
                    + direction * 0.5f * anchor * (grow - 1.0f),
                (u4 - 0.5f) * 20.0f,
                size,
                alpha,
                1.15f,
                grow,
            });
        } else if (burst.kind == "ember") {
            // 余烬火星（镜像 particles ember 分支）：小圆点快速上蹿、
            // 受“重力”减速、横向漂移 + 双频摆动 + 高频闪烁。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            const float birth = u3 * life * 0.55f;
            const float lifeI = std::min(
                life * (0.50f + 0.50f * u5),
                std::max(life - birth, 1.0f)
            );
            const float p = (tau - birth) / lifeI;
            if (p <= 0.0f || p >= 1.0f) {
                continue;
            }
            const float rise = travel * (1.0f - std::pow(1.0f - p, 2.0f));
            const float drift = (u1 - 0.5f) * travel * 0.55f * p;
            const float sway = std::sin((u2 + p * 2.2f) * 2.0f * pi)
                * size * 1.3f;
            const float twinkle = 0.55f + 0.45f * std::sin(
                (p * 9.0f + u2 * 4.0f) * 2.0f * pi
            );
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.75f + drift + sway,
                originY + (0.10f + 0.30f * u4) * boxH - rise,
                p * 360.0f * (u4 - 0.5f),
                size * (0.55f + 0.55f * u1) * (1.0f - 0.55f * p),
                std::min(p / 0.08f, 1.0f)
                    * std::pow(1.0f - p, 0.9f) * twinkle,
            });
        } else if (burst.kind == "ash") {
            // 灰烬（镜像 particles.burst_particles_at ash 分支）：自字框
            // 出生后飘落 + 小幅摇摆 + 边落边缩小熄灭 + 慢自旋。
            constexpr float pi = 3.14159265358979323846f;
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float delay = u3 * life * 0.20f;
            const float p = std::clamp(
                (tau - delay) / std::max(life - delay, 1.0f), 0.0f, 1.0f
            );
            if (p <= 0.0f) {
                continue;
            }
            const float fall = (0.35f + 0.45f * u2) * boxH * p;
            const float sway =
                std::sin((u2 + p * 1.2f) * 2.0f * pi) * size * 0.80f;
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.8f + sway,
                originY + (u2 - 0.3f) * boxH * 0.4f + fall,
                (u4 - 0.5f) * 120.0f + p * (u3 - 0.5f) * 180.0f,
                size * (0.5f + 0.6f * u1) * (1.0f - 0.45f * p),
                std::min(p * 5.0f, 1.0f) * std::pow(1.0f - p, 1.15f),
            });
        } else if (burst.kind == "petal") {
            // 花瓣飘落（镜像 particles.burst_particles_at petal 分支）：
            // sin 横向摇摆 + 单调竖直行程 + 连续翻转；位置偏移全部取
            // box_w/box_h 比例（星光/音符同约定，不用字号倍数行程——
            // 初版 travel=1.6em 出生过高飘进行间距，2026-10 用户口径）。
            // sweep>0 飘入 ease-out 汇拢、sweep<0 飘散 smoothstep 离场、
            // sweep==0 唱字飘动小行程缓起缓落。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            const float spawn = u3;
            // AE Box emitter + 连续出生：每颗在 0~45% 寿命内错峰出生，
            // 不做首帧预滚（避免入场粒子直接出现在文字内部）；最大尺寸
            // 档按 burst seed 轮换到不同粒子（序列扰动）。
            const int particleCount = std::max(burst.count, 1);
            const int depthSlot = (
                i + static_cast<int>(
                    burst.seed % static_cast<std::uint32_t>(particleCount)
                )
            ) % particleCount;
            const float delay = spawn * life
                * (burst.sweep == 0 ? kFallSpawnSpreadSing : kFallSpawnSpread);
            if (tau < delay) {
                continue;
            }
            const float base = std::clamp(
                (tau - delay)
                    / std::max(static_cast<float>(life) - delay, 1.0f),
                0.0f,
                1.0f
            );
            const float p = base;
            if (p <= 0.0f) {
                continue;
            }
            constexpr float pi = 3.14159265358979323846f;
            const float flutter = std::sin(
                (u2 + p * (0.40f + 0.25f * u5)) * 2.0f * pi
            ) * size * (0.22f + 0.10f * u4);
            const float wind = kPetalWindFraction * boxW * p;
            const float depth = particleCount <= 1
                ? 0.5f
                : std::clamp(
                    (static_cast<float>(depthSlot) + 0.30f * u4)
                        / static_cast<float>(particleCount - 1),
                    0.0f,
                    1.0f
                );
            const float sizeI = size
                * (kPetalSizeMin + kPetalSizeSpan * depth);
            const float spin = u3 * 360.0f
                + p * (160.0f + 240.0f * u5) * (u4 >= 0.5f ? 1.0f : -1.0f)
                + kFallLeanDeg;
            // 不再落到字底：终点在字框下方 1.1~2.3 box_h 的不可观测区，
            // 最后 15% 才淡出；位置随机、不汇聚。
            const float holdFade = p <= 0.85f
                ? 1.0f
                : std::pow(
                    std::max((1.0f - p) / 0.15f, 0.0f), 1.2f
                );
            if (burst.sweep > 0) {
                // 花瓣飘入：非零初速 + 重力加速，穿过文字落到不可观测区。
                const float fall = fallProgress(p);
                const float landX = (u1 - 0.5f) * boxW * 0.85f;
                const float landY = (0.42f + 0.16f * u4) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 0.7f;
                const float startY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * fall
                        + wind + flutter,
                    originY + relY,
                    spin,
                    sizeI * (0.85f + 0.15f * std::sin(pi * p)),
                    std::min(p * 4.0f, 1.0f) * speedFade,
                });
            } else if (burst.sweep < 0) {
                // 花瓣飘散：起始高度铺满字框上半部，落到不可观测区；
                // 终点向右、非零初速 + 重力加速。
                const float fall = fallProgress(p);
                const float startX = (u1 - 0.5f) * boxW * 0.85f;
                const float endX = startX + (0.5f + 0.9f * u5) * boxW;
                const float startY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
                const float landY = (0.50f + 0.20f * u4) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + startX + (endX - startX) * fall
                        + wind + flutter,
                    originY + relY,
                    spin,
                    sizeI * (0.90f + 0.10f * (1.0f - p)),
                    std::min(p * 4.0f, 1.0f) * speedFade,
                });
            } else {
                // 花瓣飘动：字框上半出生，落到不可观测区再淡出。
                const float fall = fallProgress(p);
                const float startY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
                const float landY = (0.42f + 0.16f * u4) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + (u1 - 0.5f) * boxW * 0.7f
                        + wind * 0.8f + flutter,
                    originY + relY,
                    spin,
                    sizeI * (0.85f + 0.15f * std::sin(pi * p)),
                    std::min(p * 5.0f, 1.0f) * std::sin(pi * p) * speedFade,
                });
            }
        } else if (burst.kind == "feather") {
            // 羽毛（镜像 particles.burst_particles_at feather 分支）：
            // 2026-10-21 走字风——持续向右后方漂 + 小扑动 + 固定后倾，
            // 不再绕中心点大幅钟摆；stretchX 仍做 0.55~1.0 羽片侧翻。
            // sweep>0 飘入落到字底、sweep<0 飘散向右下、sweep==0 唱字缓沉。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            constexpr float pi = 3.14159265358979323846f;
            const float spawn = u3;
            // AE Box emitter + 连续出生：每颗在 0~45% 寿命内错峰出生，
            // 不做首帧预滚（避免入场粒子直接出现在文字内部）；最大尺寸
            // 档按 burst seed 轮换到不同粒子（序列扰动）。
            const int particleCount = std::max(burst.count, 1);
            const int depthSlot = (
                i + static_cast<int>(
                    burst.seed % static_cast<std::uint32_t>(particleCount)
                )
            ) % particleCount;
            const float delay = spawn * life
                * (burst.sweep == 0 ? kFallSpawnSpreadSing : kFallSpawnSpread);
            if (tau < delay) {
                continue;
            }
            const float base = std::clamp(
                (tau - delay)
                    / std::max(static_cast<float>(life) - delay, 1.0f),
                0.0f,
                1.0f
            );
            const float p = base;
            if (p <= 0.0f) {
                continue;
            }
            const float wobble = std::sin(
                (u2 + p * (0.32f + 0.28f * u5)) * 2.0f * pi
            ) * size * (0.20f + 0.10f * u4);
            const float wind = kFeatherWindFraction * boxW * p;
            const float depth = particleCount <= 1
                ? 0.5f
                : std::clamp(
                    (static_cast<float>(depthSlot) + 0.30f * u4)
                        / static_cast<float>(particleCount - 1),
                    0.0f,
                    1.0f
                );
            const float sizeI = size
                * (kFeatherSizeMin + kFeatherSizeSpan * depth);
            const float spin = u3 * 360.0f
                + p * (120.0f + 240.0f * u5)
                    * (u4 >= 0.5f ? 1.0f : -1.0f)
                + kFeatherLeanDeg;
            const float flutter = 0.55f + 0.45f * std::abs(
                std::cos((p * 1.5f + u5) * pi)
            );
            const float holdFade = p <= kFeatherFadeStart
                ? 1.0f
                : std::pow(
                    std::max(
                        (1.0f - p) / (1.0f - kFeatherFadeStart), 0.0f
                    ),
                    1.2f
                );
            if (burst.sweep > 0) {
                // 羽毛飘入：非零初速 + 重力加速，穿过文字落到不可观测区。
                const float fall = fallProgress(p);
                const float landX = (u1 - 0.5f) * boxW * 0.90f;
                const float landY = (0.42f + 0.18f * u4) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 0.80f;
                const float startY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * fall
                        + wind + wobble,
                    originY + relY,
                    spin,
                    sizeI * (0.90f + 0.10f * std::sin(pi * p)),
                    std::min(p * 4.0f, 1.0f) * speedFade,
                    flutter,
                    1.0f,
                });
            } else if (burst.sweep < 0) {
                // 羽毛飘散：起始高度铺满字框上半部，落到不可观测区。
                const float fall = fallProgress(p);
                const float startX = (u1 - 0.5f) * boxW * 0.90f;
                const float endX = startX + (0.25f + 0.70f * u5) * boxW;
                const float startY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
                const float landY = (0.50f + 0.20f * u4) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + startX + (endX - startX) * fall
                        + wind + wobble,
                    originY + relY,
                    spin,
                    sizeI * (0.90f + 0.10f * (1.0f - p)),
                    std::min(p * 4.0f, 1.0f) * speedFade,
                    flutter,
                    1.0f,
                });
            } else {
                // 羽毛飘动：字框上半出生，落到不可观测区再淡出。
                const float fall = fallProgress(p);
                const float startY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
                const float landY = (0.42f + 0.18f * u4) * boxH;
                const float relY = startY + (landY - startY) * fall;
                const float speedFade = fallSpeedFade(p);
                out.push_back(FxParticle{
                    originX + (u1 - 0.5f) * boxW * 0.75f
                        + wind * 0.8f + wobble,
                    originY + relY,
                    spin,
                    sizeI * (0.88f + 0.12f * std::sin(pi * p)),
                    std::min(p * 5.0f, 1.0f) * std::sin(pi * p) * speedFade,
                    flutter,
                    1.0f,
                });
            }
        } else if (burst.kind == "snow" || burst.kind == "snow_solid") {
            // 雪花飘落（镜像 particles.burst_particles_at snow 分支）：
            // 2026-10-21 走字风——持续向右后方漂 + 小扑动 + 固定后倾，
            // 不再绕中心点对称摆；出生锚 = ruby 盒中心-底部二分之一处，
            // 匀速下沉、尺寸随播放由大变小。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            const float spawn = u3;
            // AE Box emitter + 连续出生：每颗在 0~45% 寿命内错峰出生，
            // 不做首帧预滚（避免入场粒子直接出现在文字内部）；最大尺寸
            // 档按 burst seed 轮换到不同粒子（序列扰动）。
            const int particleCount = std::max(burst.count, 1);
            const int depthSlot = (
                i + static_cast<int>(
                    burst.seed % static_cast<std::uint32_t>(particleCount)
                )
            ) % particleCount;
            const float delay = spawn * life
                * (burst.sweep == 0 ? kFallSpawnSpreadSing : kFallSpawnSpread);
            if (tau < delay) {
                continue;
            }
            const float base = std::clamp(
                (tau - delay)
                    / std::max(static_cast<float>(life) - delay, 1.0f),
                0.0f,
                1.0f
            );
            const float p = base;
            if (p <= 0.0f) {
                continue;
            }
            constexpr float pi = 3.14159265358979323846f;
            const float wobble = std::sin(
                (u2 + p * (0.28f + 0.22f * u5)) * 2.0f * pi
            ) * size * (0.18f + 0.10f * u4);
            const float wind = kSnowWindFraction * boxW * p;
            const float depth = particleCount <= 1
                ? 0.5f
                : std::clamp(
                    (static_cast<float>(depthSlot) + 0.30f * u4)
                        / static_cast<float>(particleCount - 1),
                    0.0f,
                    1.0f
                );
            const float sizeI =
                size * (kSnowSizeMin + kSnowSizeSpan * depth)
                * (1.05f - 0.40f * p);
            const float spin = u3 * 360.0f
                + p * (80.0f + 140.0f * u5) * (u4 >= 0.5f ? 1.0f : -1.0f)
                + kSnowLeanDeg;
            const float birthY = -(kFallStartMin + kFallStartSpan * u2) * boxH;
            const float fall = fallProgress(p);
            const float speedFade = fallSpeedFade(p);
            if (burst.sweep > 0) {
                // 雪花飘入：自字顶上方落入并向字底汇聚；末段随速度渐隐。
                const float landX = (u1 - 0.5f) * boxW * 0.85f;
                const float landY = (0.48f + 0.14f * u4) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 0.6f;
                const float relY = birthY + (landY - birthY) * fall;
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * fall
                        + wind + wobble,
                    originY + relY,
                    spin,
                    sizeI,
                    std::min(p * 3.0f, 1.0f) * speedFade,
                });
            } else if (burst.sweep < 0) {
                // 雪花飘散：从主文字上方出发，向字底汇聚；终点向右，
                // 末段随速度渐隐。
                const float landX = (u1 - 0.5f) * boxW * 0.85f;
                const float endX = landX + (0.35f + 0.6f * u5) * boxW;
                const float landY = (0.50f + 0.24f * u2) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 0.5f;
                const float relY = birthY + (landY - birthY) * fall;
                out.push_back(FxParticle{
                    originX + startX + (endX - startX) * fall
                        + wind + wobble,
                    originY + relY,
                    spin,
                    sizeI,
                    std::min(p * 3.0f, 1.0f) * speedFade,
                });
            } else {
                // 雪花飘动（唱字）：与出入场同一起始高度，向字底汇聚；
                // 末段随速度渐隐。
                const float landY = (0.48f + 0.14f * u4) * boxH;
                const float relY = birthY + (landY - birthY) * fall;
                out.push_back(FxParticle{
                    originX + (u1 - 0.5f) * boxW * 0.7f
                        + wind * 0.8f + wobble,
                    originY + relY,
                    spin,
                    sizeI,
                    std::min(p * 4.0f, 1.0f) * std::sin(pi * p) * speedFade,
                });
            }
        }
    }
    return out;
}

}  // namespace krok::subtitle::native::direct2d
