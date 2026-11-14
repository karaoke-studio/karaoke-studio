#include "geometry.h"
#include "unit_hash.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native::effects {
namespace {
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


}  // namespace krok::subtitle::native::effects
