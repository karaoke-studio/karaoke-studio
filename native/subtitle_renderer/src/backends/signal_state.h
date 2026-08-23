#pragma once

#include "render_backend.h"

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

VolumeSignalGeometry volumeSignalGeometry(const TextStyle &style);

VolumeSignalState volumeSignalState(
    int lineStartMs,
    const TextStyle &style,
    int tMs,
    int displayEndMs,
    bool signalHead
);

ShapeSignalGeometry shapeSignalGeometry(const TextStyle &style);

ShapeSignalState shapeSignalState(
    int lineStartMs,
    const TextStyle &style,
    int tMs,
    int displayEndMs,
    bool signalHead
);

}  // namespace krok::subtitle::native
