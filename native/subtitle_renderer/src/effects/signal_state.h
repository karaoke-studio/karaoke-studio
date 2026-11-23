#pragma once

#include "../model/text_style.h"
#include <cstdint>

namespace krok::subtitle::native {

struct VolumeSignalGeometry {
    int count = 1;
    float size = 1.0f;
    float columnWidth = 1.0f;
    float columnSpacing = 0.0f;
    float strokeExtent = 0.0f;
    float pitch = 1.0f;
    float groupWidth = 1.0f;
    float frontHeight = 1.0f;
    float heightDelta = 0.0f;
    float alignBaseShift = 0.0f;
    float alignDeltaShift = 0.0f;
};

struct VolumeSignalState {
    bool visible = false;
    int activeIndex = -1;
    float opacity = 0.0f;
    int fillElapsedMs = 0;
    float fillDurationMs = 0.0f;
};

struct VolumeSignalClock {
    int count;
    int activeDurationMs;
    std::int64_t endMs;
    int flashes;
    float flashRatio;
    int transitionRatioPct;
};

struct ShapeSignalGeometry {
    int count = 1;
    float size = 1.0f;
    float tracking = 0.0f;
    float strokeExtent = 0.0f;
    float groupWidth = 1.0f;
};

struct ShapeSignalState {
    bool visible = false;
    int activeIndex = -1;
    float activeOpacity = 1.0f;
    float dx = 0.0f;
    float dy = 0.0f;
};

struct ShapeSignalClock {
    enum class Transition { None, Fade, Slide };
    int count;
    int activeDurationMs;
    std::int64_t endMs;
    int transitionRatioPct;
    float transitionAngleDeg;
    float transitionDistance;
    Transition transition;
};

VolumeSignalGeometry volumeSignalGeometry(const TextStyle &style);

VolumeSignalClock volumeSignalClock(int lineStartMs, const TextStyle &style);
VolumeSignalState volumeSignalState(const VolumeSignalClock &clock, int tMs);

VolumeSignalState volumeSignalState(
    int lineStartMs,
    const TextStyle &style,
    int tMs,
    int displayStartMs,
    int displayEndMs,
    bool signalHead
);

ShapeSignalGeometry shapeSignalGeometry(const TextStyle &style);
ShapeSignalClock shapeSignalClock(int lineStartMs, const TextStyle &style);
ShapeSignalState shapeSignalState(const ShapeSignalClock &clock, int tMs);

ShapeSignalState shapeSignalState(
    int lineStartMs,
    const TextStyle &style,
    int tMs,
    int displayStartMs,
    int displayEndMs,
    bool signalHead
);

}  // namespace krok::subtitle::native
