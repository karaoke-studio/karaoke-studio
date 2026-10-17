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
    const auto staggerProgress = [&]() {
        const int step = count <= 1 ? 0 : staggerMs / (count - 1);
        return clampedRatio(tMs - startMs - step * index, travelMs);
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
        // 火焰档本体（2026-10-18 用户口径）：入场=烧的过程中的快速出现
        //（8 倍速透明度）；退场=渐隐中缩小——纵向比横向收得更狠
        //（55% / 22%），字像被火从下往上烧塌；灰烬与余烬由 planner 伴生。
        const float p = staggerProgress;
        if (exitPhase) {
            const float q = smoothstep(p);
            state.opacity = 1.0f - q;
            state.scaleX = 1.0f - 0.22f * q;
            state.scaleY = 1.0f - 0.55f * q;
        } else {
            state.opacity = std::min(p * 8.0f, 1.0f);
        }
        return state;
    }
    if (effect == "sparkle" || effect == "ripple" || effect == "note"
        || effect == "heart" || effect == "petal"
        || effect == "snow" || effect == "snow_solid") {
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
            const float birth = u3 * life * 0.30f;
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
                std::sin((u2 + p * 1.7f) * 2.0f * pi) * 0.30f
                + std::sin((u4 * 0.7f + p * 3.3f) * 2.0f * pi) * 0.12f
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
            const float lean = std::cos((u2 + p * 1.7f) * 2.0f * pi) * 10.0f
                + (u4 - 0.5f) * 8.0f;
            out.push_back(FxParticle{
                originX + (u1 - 0.5f) * boxW * 0.55f + sway,
                originY + (0.12f + 0.30f * u2) * boxH - rise,
                lean,
                size * (0.45f + 0.65f * grow) * cool
                    * (0.85f + 0.30f * u1),
                alpha,
                1.0f - 0.25f * p,
                1.0f + 0.95f * p,
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
            const float delay = u3 * 110.0f;
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
            const float sway = std::sin(
                (u2 + p * (0.9f + 0.9f * u5)) * 2.0f * pi
            ) * size * (0.8f + 0.7f * u4);
            const float sizeI = size * (0.72f + 0.55f * u4);
            const float spin = u3 * 360.0f
                + p * (160.0f + 240.0f * u5) * (u4 >= 0.5f ? 1.0f : -1.0f);
            if (burst.sweep > 0) {
                // 花瓣飘入：自字形顶上方（溢出 0.10~0.40 box_h，星光同
                // 量级）ease-out 减速飘落进字框，抵达后熄灭。
                const float eased = 1.0f - (1.0f - p) * (1.0f - p);
                const float landX = (u1 - 0.5f) * boxW * 0.85f;
                const float landY = (u4 - 0.5f) * boxH * 0.55f;
                const float startX = landX + (u5 - 0.5f) * boxW * 0.7f;
                const float startY = -(0.60f + 0.30f * u2) * boxH;
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * eased + sway,
                    originY + startY + (landY - startY) * eased,
                    spin,
                    sizeI * (0.85f + 0.15f * std::sin(pi * p)),
                    std::min(p * 5.0f, 1.0f) * (1.0f - p) * (1.0f - p * 0.4f),
                });
            } else if (burst.sweep < 0) {
                // 花瓣飘散：横向飘逸保持随机（sin 摇摆），只有**结束
                // 位置**落在起点右侧（幅度随机；2026-10 用户口径：飘逸
                // 随机、终点向右），落到字底下方（0.05~0.65 box_h）淡出。
                const float eased = p * p * (3.0f - 2.0f * p);
                const float startX = (u1 - 0.5f) * boxW * 0.85f;
                const float endX = startX + (0.5f + 0.9f * u5) * boxW;
                const float startY = (u4 - 0.5f) * boxH * 0.55f;
                out.push_back(FxParticle{
                    originX + startX + (endX - startX) * eased + sway,
                    originY + startY
                        + (0.55f + 0.30f * u2) * boxH * eased,
                    spin,
                    sizeI * (0.90f + 0.10f * (1.0f - p)),
                    (1.0f - p) * (1.0f - p * 0.5f),
                });
            } else {
                // 花瓣飘动：字框上半出生（星光的偏上口径），小行程缓沉，
                // sin 包络淡入淡出。
                const float eased = p * p * (3.0f - 2.0f * p);
                out.push_back(FxParticle{
                    originX + (u1 - 0.5f) * boxW * 0.7f + sway,
                    originY - (0.25f + 0.30f * u4) * boxH
                        + (0.40f + 0.30f * u2) * boxH * eased,
                    spin,
                    sizeI * (0.85f + 0.15f * std::sin(pi * p)),
                    std::min(p * 5.0f, 1.0f) * std::sin(pi * p),
                });
            }
        } else if (burst.kind == "snow" || burst.kind == "snow_solid") {
            // 雪花飘落（镜像 particles.burst_particles_at snow 分支，
            // 2026-10 用户口径）：出生锚 = ruby 盒中心-底部二分之一处
            //（birthY 窄随机带）；匀速下沉 + 宽幅低频摇摆 + 慢自转；
            // 尺寸随播放由大变小（1.05→0.65 线性递减）。sweep>0 飘入、
            // sweep<0 飘散（终点向右，同花瓣口径）、sweep==0 唱字飘动。
            const float u1 = fxUnitHash(index, 1u);
            const float u2 = fxUnitHash(index, 2u);
            const float u3 = fxUnitHash(index, 3u);
            const float u4 = fxUnitHash(index, 4u);
            const float u5 = fxUnitHash(index, 5u);
            const float delay = u3 * 160.0f;
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
            const float sway = std::sin(
                (u2 + p * (0.35f + 0.45f * u5)) * 2.0f * pi
            ) * size * (0.75f + 0.65f * u4);
            const float sizeI =
                size * (0.70f + 0.50f * u4) * (1.05f - 0.40f * p);
            const float spin = u3 * 360.0f
                + p * (80.0f + 140.0f * u5) * (u4 >= 0.5f ? 1.0f : -1.0f);
            const float birthY = -(0.68f + 0.22f * u2) * boxH;
            const float eased = p;  // 匀速下沉（雪的终端速度感）
            if (burst.sweep > 0) {
                // 雪花飘入：自 ruby 锚一路落到字底部（2026-10 用户口径：
                // 雪从天上落到地上，不落在字中间）。
                const float landX = (u1 - 0.5f) * boxW * 0.85f;
                const float landY = (0.35f + 0.20f * u4) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 0.6f;
                out.push_back(FxParticle{
                    originX + startX + (landX - startX) * eased + sway,
                    originY + birthY + (landY - birthY) * eased,
                    spin,
                    sizeI,
                    std::min(p * 4.0f, 1.0f) * (1.0f - p) * (1.0f - p * 0.4f),
                });
            } else if (burst.sweep < 0) {
                // 雪花飘散：同样自 ruby 锚出生落到字底部（不考虑与字面
                // 的连续性——雪从天上落到地上），横向随机摇摆、终点向右。
                const float landX = (u1 - 0.5f) * boxW * 0.85f;
                const float endX = landX + (0.35f + 0.6f * u5) * boxW;
                const float landY = (0.45f + 0.30f * u2) * boxH;
                const float startX = landX + (u5 - 0.5f) * boxW * 0.5f;
                out.push_back(FxParticle{
                    originX + startX + (endX - startX) * eased + sway,
                    originY + birthY + (landY - birthY) * eased,
                    spin,
                    sizeI,
                    (1.0f - p) * (1.0f - p * 0.5f),
                });
            } else {
                // 雪花飘动：自 ruby 锚出生，匀速缓沉穿过字框，
                // sin 包络淡入淡出。
                const float landY = (u4 - 0.5f) * boxH * 0.6f;
                out.push_back(FxParticle{
                    originX + (u1 - 0.5f) * boxW * 0.7f + sway,
                    originY + birthY + (landY - birthY) * eased,
                    spin,
                    sizeI,
                    std::min(p * 4.0f, 1.0f) * std::sin(pi * p),
                });
            }
        }
    }
    return out;
}

}  // namespace krok::subtitle::native::direct2d
