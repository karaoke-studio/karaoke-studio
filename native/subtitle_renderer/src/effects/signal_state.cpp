#include "signal_state.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native {
namespace {

float volumeFlashAlpha(int elapsed, int duration, const VolumeSignalClock &clock) {
    if (duration <= 0 || elapsed < 0) {
        return 0.0f;
    }
    const int times = clock.flashes;
    if (times == 0) {
        return 1.0f;
    }
    const float perFlash = static_cast<float>(duration) / static_cast<float>(times);
    if (perFlash <= 0.0f) {
        return 1.0f;
    }
    float phase = std::fmod(static_cast<float>(elapsed) / perFlash, 1.0f) * 2.0f;
    if (phase > 1.0f) {
        phase = 2.0f - phase;
    }
    const float transition = std::clamp(
        static_cast<float>(clock.transitionRatioPct) / 100.0f,
        0.0f,
        1.0f
    );
    if (transition <= 0.0f) {
        return 1.0f - ((phase * 2.0f - 1.0f) > 0.0f ? 1.0f : 0.0f);
    }
    const float fade = std::clamp(
        ((phase * 3.0f - 1.0f) * 0.67f) / transition,
        0.0f,
        1.0f
    );
    return 1.0f - fade;
}

}  // namespace

VolumeSignalGeometry volumeSignalGeometry(const TextStyle &style) {
    // 列距口径镜像 Painter 的 volume_signal_geometry：每列单元在自己的
    // 柱体两侧各预留一份描边厚度（pitch 含 2*strokeExtent），相邻柱的描
    // 边外缘间隔恰为 columnSpacing；groupWidth 只在两端收一次
    // columnSpacing。漏掉每列的 2*stroke 会让柱间墨迹间隔缩到
    // spacing - stroke，两端画面分歧。
    VolumeSignalGeometry geometry;
    geometry.count = std::clamp(style.volumeColumnCount, 1, 16);
    geometry.size = std::max(style.volumeSize, 1.0f);
    geometry.columnWidth = std::max(style.volumeColumnWidth, 1.0f);
    geometry.columnSpacing = std::max(style.volumeColumnSpacing, 0.0f);
    const bool independentVolume = style.volumeEnabled;
    geometry.strokeExtent = std::max(
        independentVolume ? style.volumeStrokeWidth : style.litStrokeWidth,
        0.0f
    );
    geometry.pitch = geometry.columnWidth + geometry.columnSpacing
        + geometry.strokeExtent * 2.0f;
    geometry.groupWidth = geometry.count * geometry.pitch
        - geometry.columnSpacing;
    const float ratio = std::max(style.volumeRatio, 0.01f);
    float baseFactor = ratio;
    float depthFactor = 1.0f;
    if (ratio > 1.0f) {
        depthFactor = 1.0f / ratio;
        baseFactor = 1.0f;
    }
    geometry.frontHeight = baseFactor * geometry.size;
    geometry.heightDelta = geometry.count < 2
        ? 0.0f
        : ((depthFactor - baseFactor) * geometry.size)
            / static_cast<float>(geometry.count - 1);
    if (style.volumeAlign == 1) {
        geometry.alignBaseShift = (1.0f - baseFactor) * geometry.size * 0.5f;
        geometry.alignDeltaShift = -geometry.heightDelta * 0.5f;
    } else if (style.volumeAlign == 2) {
        geometry.alignBaseShift = (1.0f - baseFactor) * geometry.size;
        geometry.alignDeltaShift = -geometry.heightDelta;
    }
    return geometry;
}

ShapeSignalGeometry shapeSignalGeometry(const TextStyle &style) {
    ShapeSignalGeometry geometry;
    geometry.count = std::clamp(style.litNumber, 1, 8);
    geometry.size = std::max(style.litSize, 1.0f);
    geometry.tracking = std::max(style.litTracking, 0.0f);
    geometry.strokeExtent = std::max(style.litStrokeWidth, 0.0f)
        + std::max(style.litStrokeSoften, 0.0f);
    geometry.groupWidth = geometry.count * geometry.size
        + std::max(geometry.count - 1, 0)
            * (geometry.size * 0.5f + geometry.tracking);
    return geometry;
}

ShapeSignalClock shapeSignalClock(int lineStartMs, const TextStyle &style) {
    using Transition = ShapeSignalClock::Transition;
    const auto transition = style.litTransitionMode == "fade" ? Transition::Fade
        : style.litTransitionMode == "slide" ? Transition::Slide : Transition::None;
    const int duration = std::max(style.signalsDurationMs, 0);
    return {std::clamp(style.litNumber, 1, 8), std::max(duration - std::max(style.litWaitingTimeMs, 0), 0),
            std::int64_t(lineStartMs) + style.litTimeOffsetMs, style.litTransitionRatioPct,
            style.litTransitionAngleDeg, style.litTransitionDistance, transition};
}

ShapeSignalState shapeSignalState(const ShapeSignalClock &clock, int tMs) {
    ShapeSignalState state;
    const int activeDuration = clock.activeDurationMs;
    if (activeDuration <= 0) return state;
    const auto activeStart = clock.endMs - activeDuration;
    state.visible = true;
    const auto elapsed = std::max<std::int64_t>(tMs - activeStart, 0);
    const int count = clock.count;
    if (count <= 1) {
        state.activeIndex = 0;
    } else if (elapsed >= activeDuration) {
        state.activeIndex = -1;
        return state;
    } else {
        const float raw = static_cast<float>((activeDuration - elapsed) * count)
            / static_cast<float>(activeDuration);
        state.activeIndex = std::clamp(static_cast<int>(raw), 0, count - 1);
        const float phase = std::clamp(
            raw - static_cast<float>(state.activeIndex), 0.0f, 1.0f
        );
        const float ratio = std::clamp(
            static_cast<float>(clock.transitionRatioPct) / 100.0f,
            0.0f,
            1.0f
        );
        const float transitionPhase = 1.0f - phase;
        const float progress = ratio <= 0.0f
            ? 1.0f
            : std::clamp(
                (transitionPhase - (1.0f - ratio)) / ratio, 0.0f, 1.0f
            );
        if (clock.transition == ShapeSignalClock::Transition::Fade) {
            state.activeOpacity = 1.0f - progress;
        } else if (clock.transition == ShapeSignalClock::Transition::Slide) {
            // Exit = time-reversed entrance (mirrors Painter's
            // lit_extinguish_transition_state): opacity 1→0 while the
            // displacement grows from the slot toward the entrance origin
            // (−angle direction). Replaying the entrance curve here left the
            // active lamp invisible for most of its reign.
            state.activeOpacity = 1.0f - progress;
            const float distance = std::max(clock.transitionDistance, 0.0f)
                * progress;
            constexpr float pi = 3.14159265358979323846f;
            const float radians = clock.transitionAngleDeg * pi / 180.0f;
            state.dx = -std::cos(radians) * distance;
            state.dy = -std::sin(radians) * distance;
        }
    }
    return state;
}

ShapeSignalState shapeSignalState(
    int lineStartMs, const TextStyle &style, int tMs, int displayStartMs,
    int displayEndMs, bool signalHead
) {
    // The row's display window gates a countdown whose clock is independent.
    if (!signalHead || style.vertical || !style.litEnabled || style.litStyle == "volume"
        || tMs < displayStartMs || tMs >= displayEndMs) {
        return {};
    }
    return shapeSignalState(shapeSignalClock(lineStartMs, style), tMs);
}

VolumeSignalClock volumeSignalClock(int lineStartMs, const TextStyle &style) {
    const int duration = std::max(style.volumeEnabled ? style.volumeDurationMs : style.signalsDurationMs, 0);
    const int wait = style.volumeEnabled ? style.volumeWaitingTimeMs : style.litWaitingTimeMs;
    const int offset = style.volumeEnabled ? style.volumeTimeOffsetMs : style.litTimeOffsetMs;
    return {std::clamp(style.volumeColumnCount, 1, 16), std::max(duration - std::max(wait, 0), 0),
            std::int64_t(lineStartMs) + offset, std::max(style.volumeFlashTimes, 0),
            std::max(style.volumeFlashDurationRatio, 0.f), style.volumeTransitionRatioPct};
}

VolumeSignalState volumeSignalState(const VolumeSignalClock &clock, int tMs) {
    VolumeSignalState state;
    const int activeDuration = clock.activeDurationMs;
    if (activeDuration <= 0) return state;
    const auto activeStart = clock.endMs - activeDuration;
    const int elapsed = int(std::clamp<std::int64_t>(tMs - activeStart, 0, activeDuration - 1));
    const int count = clock.count;
    const int times = clock.flashes;
    const float flashRatio = clock.flashRatio;
    state.visible = true;
    state.opacity = 1.0f;
    state.fillElapsedMs = elapsed;
    state.fillDurationMs = static_cast<float>(activeDuration);
    int fillDuration = activeDuration;
    if (times > 0 && flashRatio > 0.0f) {
        state.fillDurationMs = static_cast<float>(activeDuration)
            / (static_cast<float>(times) * flashRatio + 1.0f);
        const float flashDuration = std::max(
            static_cast<float>(activeDuration) - state.fillDurationMs, 0.0f
        );
        if (static_cast<float>(elapsed) < flashDuration) {
            state.fillElapsedMs = 0;
            state.activeIndex = -1;
            state.opacity = volumeFlashAlpha(
                elapsed,
                std::max(static_cast<int>(flashDuration), 1),
                clock
            );
            return state;
        }
        state.fillElapsedMs = std::max(static_cast<int>(elapsed - flashDuration), 0);
        fillDuration = std::max(static_cast<int>(state.fillDurationMs), 1);
    }
    const float raw = static_cast<float>(std::int64_t(count) * state.fillElapsedMs)
        / static_cast<float>(std::max(fillDuration, 1));
    state.activeIndex = std::clamp(static_cast<int>(raw), 0, count - 1);
    return state;
}

VolumeSignalState volumeSignalState(
    int lineStartMs, const TextStyle &style, int tMs, int displayStartMs,
    int displayEndMs, bool signalHead
) {
    const bool legacyVolume = style.litEnabled && style.litStyle == "volume";
    if (!signalHead || style.vertical || (!style.volumeEnabled && !legacyVolume)
        || tMs < displayStartMs || tMs >= displayEndMs) {
        return {};
    }
    return volumeSignalState(volumeSignalClock(lineStartMs, style), tMs);
}

}  // namespace krok::subtitle::native
