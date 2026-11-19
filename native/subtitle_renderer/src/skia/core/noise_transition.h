#pragma once

class SkCanvas;
class SkImage;

namespace subtitle::skia {
struct NoiseTransition {
    int entryMs = 0, exitMs = 0, singingEnd = 0;
    float fontPx = 0;

    int levelAt(int time, int start, int end) const;
    void composite(SkCanvas&, const SkImage&, int level) const;
};
}
