#include "signal_state.h"

#include <algorithm>
#include <cmath>

namespace krok::subtitle::native {
namespace {

float volumeFlashAlpha(int elapsed, int duration, const TextStyle &style) {
    if (duration <= 0 || elapsed < 0) {
        return 0.0f;
    }
    const int times = std::max(style.volumeFlashTimes, 0);
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
        static_cast<float>(style.volumeTransitionRatioPct) / 100.0f,
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
    VolumeSignalGeometry geometry;
    geometry.count = std::clamp(style.volumeColumnCount, 1, 16);
    geometry.size = std::max(style.volumeSize, 1.0f);
    geometry.columnWidth = std::max(style.volumeColumnWidth, 1.0f);
    geometry.columnSpacing = std::max(style.volumeColumnSpacing, 0.0f);
    geometry.strokeExtent = std::max(style.litStrokeWidth, 0.0f);
    geometry.pitch = geometry.columnWidth + geometry.columnSpacing;
    geometry.groupWidth = geometry.count * geometry.pitch
        - geometry.columnSpacing + geometry.strokeExtent * 2.0f;
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

ShapeSignalState shapeSignalState(
    int lineStartMs,
    const TextStyle &style,
    int tMs,
    int displayEndMs,
    bool signalHead
) {
    ShapeSignalState state;
    if (!signalHead) {
        return state;
    }
    if (style.vertical || !style.litEnabled || style.litStyle == "volume") {
        return state;
    }
    const int duration = std::max(style.signalsDurationMs, 0);
    const int activeDuration = std::max(
        duration - std::max(style.litWaitingTimeMs, 0), 0
    );
    if (activeDuration <= 0) {
        return state;
    }
    const int signalEnd = lineStartMs + style.litTimeOffsetMs;
    const int activeStart = signalEnd - activeDuration;
    if (tMs < activeStart || tMs >= displayEndMs) {
        return state;
    }
    state.visible = true;
    const int elapsed = std::max(tMs - activeStart, 0);
    const int count = std::clamp(style.litNumber, 1, 8);
    if (activeDuration <= 0 || count <= 1) {
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
            static_cast<float>(style.litTransitionRatioPct) / 100.0f,
            0.0f,
            1.0f
        );
        const float transitionPhase = 1.0f - phase;
        const float progress = ratio <= 0.0f
            ? 1.0f
            : std::clamp(
                (transitionPhase - (1.0f - ratio)) / ratio, 0.0f, 1.0f
            );
        if (style.litTransitionMode == "fade") {
            state.activeOpacity = 1.0f - progress;
        } else if (style.litTransitionMode == "slide") {
            state.activeOpacity = progress;
            const float distance = std::max(style.litTransitionDistance, 0.0f)
                * (1.0f - progress);
            constexpr float pi = 3.14159265358979323846f;
            const float radians = style.litTransitionAngleDeg * pi / 180.0f;
            state.dx = -std::cos(radians) * distance;
            state.dy = -std::sin(radians) * distance;
        }
    }
    return state;
}

VolumeSignalState volumeSignalState(
    int lineStartMs,
    const TextStyle &style,
    int tMs,
    int displayEndMs,
    bool signalHead
) {
    VolumeSignalState state;
    if (!signalHead) {
        return state;
    }
    if (style.vertical || !style.litEnabled || style.litStyle != "volume") {
        return state;
    }
    const int duration = std::max(style.signalsDurationMs, 0);
    const int activeDuration = std::max(
        duration - std::max(style.litWaitingTimeMs, 0), 0
    );
    if (activeDuration <= 0) {
        return state;
    }
    const int signalEnd = lineStartMs + style.litTimeOffsetMs;
    const int activeStart = signalEnd - activeDuration;
    if (tMs < activeStart || tMs >= displayEndMs) {
        return state;
    }
    const int elapsed = std::min(
        std::max(tMs - activeStart, 0),
        std::max(activeDuration - 1, 0)
    );
    const int count = std::clamp(style.volumeColumnCount, 1, 16);
    const int times = std::max(style.volumeFlashTimes, 0);
    const float flashRatio = std::max(style.volumeFlashDurationRatio, 0.0f);
    state.visible = true;
    state.opacity = 1.0f;
    int fillElapsed = elapsed;
    int fillDuration = activeDuration;
    if (times > 0 && flashRatio > 0.0f) {
        const float fillDurationFloat = static_cast<float>(activeDuration)
            / (static_cast<float>(times) * flashRatio + 1.0f);
        const float flashDuration = std::max(
            static_cast<float>(activeDuration) - fillDurationFloat, 0.0f
        );
        if (static_cast<float>(elapsed) < flashDuration) {
            state.activeIndex = -1;
            state.opacity = volumeFlashAlpha(
                elapsed,
                std::max(static_cast<int>(flashDuration), 1),
                style
            );
            return state;
        }
        fillElapsed = std::max(static_cast<int>(elapsed - flashDuration), 0);
        fillDuration = std::max(static_cast<int>(fillDurationFloat), 1);
    }
    const float raw = static_cast<float>(count * fillElapsed)
        / static_cast<float>(std::max(fillDuration, 1));
    state.activeIndex = std::clamp(static_cast<int>(raw), 0, count - 1);
    return state;
}

}  // namespace krok::subtitle::native
